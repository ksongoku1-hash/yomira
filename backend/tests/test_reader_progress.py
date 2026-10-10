from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from support.sqlite import open_test_database

from app.domain import Chapter, ReaderProgress, Series
from app.persistence.base import PersistenceBase
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.reader_progress import (
    reader_progress_from_row,
    reader_progress_to_row,
)
from app.persistence.mappers.series import series_to_row
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models import ReaderProgressRow

NOW = datetime(2026, 9, 26, 17, 42, 11, 234_567, tzinfo=UTC)
NOW_US = datetime_to_epoch_us(NOW)


def _create_records(now: datetime = NOW) -> tuple[Series, Chapter]:
    series = Series.new_local("Series", "Series", now=now)
    chapter = Chapter.new_local(
        series.series_id,
        "12.10",
        "Chapter 12.10",
        chapter_sort_key=(12, 10, 0),
    )
    return series, chapter


def _create_database_with_chapter(
    tmp_path: Path,
    *,
    before_revision: str | None = None,
) -> tuple[SQLiteDatabase, Series, Chapter]:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    if before_revision is None:
        runner.upgrade_to_head()
    else:
        with database.exclusive_connection() as connection:
            config = Config(str(runner.config_path))
            config.attributes["connection"] = connection
            command.upgrade(config, before_revision)
            connection.commit()

    series, chapter = _create_records()
    with database.write_session() as session:
        if before_revision is None:
            session.add(series_to_row(series))
        else:
            session.execute(
                text(
                    "INSERT INTO series (series_id, canonical_title, display_title, "
                    "description, status, language, cover_asset_id, created_at_us, "
                    "updated_at_us) VALUES (:series_id, :canonical_title, :display_title, "
                    ":description, :status, :language, NULL, :created_at_us, :updated_at_us)"
                ),
                {
                    "series_id": str(series.series_id),
                    "canonical_title": series.canonical_title,
                    "display_title": series.display_title,
                    "description": series.description,
                    "status": series.status,
                    "language": series.language,
                    "created_at_us": datetime_to_epoch_us(series.created_at),
                    "updated_at_us": datetime_to_epoch_us(series.updated_at),
                },
            )
        session.flush()
        session.add(chapter_to_row(chapter))
    return database, series, chapter


def _insert_progress(
    session: Session,
    *,
    series_id: str,
    chapter_id: str,
    page_index: int | float,
    percentage_basis_points: int | float,
    updated_at_us: int | float = NOW_US,
) -> None:
    session.execute(
        text(
            "INSERT INTO reader_progress (series_id, chapter_id, page_index, "
            "percentage_basis_points, updated_at_us) VALUES (:series_id, :chapter_id, "
            ":page_index, :percentage_basis_points, :updated_at_us)"
        ),
        {
            "series_id": series_id,
            "chapter_id": chapter_id,
            "page_index": page_index,
            "percentage_basis_points": percentage_basis_points,
            "updated_at_us": updated_at_us,
        },
    )


def test_reader_progress_preserves_exact_position_and_normalizes_utc() -> None:
    series, chapter = _create_records()
    local_time = datetime(
        2026,
        9,
        26,
        13,
        42,
        11,
        234_567,
        tzinfo=timezone(timedelta(hours=-4)),
    )
    progress = ReaderProgress.new_local(
        series.series_id,
        chapter.chapter_id,
        0,
        Decimal("12.34"),
        updated_at=local_time,
    )

    assert progress.page_index == 0
    assert progress.percentage == Decimal("12.34")
    assert progress.updated_at == local_time.astimezone(UTC)
    assert {
        progress.percentage
        for progress in (
            ReaderProgress.new_local(series.series_id, chapter.chapter_id, 0, Decimal("0")),
            ReaderProgress.new_local(series.series_id, chapter.chapter_id, 9, Decimal("100.00")),
        )
    } == {Decimal("0"), Decimal("100.00")}


def test_reader_progress_rejects_lossy_or_invalid_values() -> None:
    series, chapter = _create_records()
    legacy_id = UUID("123e4567-e89b-12d3-a456-426614174000")

    with pytest.raises(ValueError, match="series_id must be a UUIDv4"):
        ReaderProgress.new_local(legacy_id, chapter.chapter_id, 0, Decimal("0"))

    for invalid_page_index in (-1, True, 1.5):
        with pytest.raises(ValueError, match="page_index must be a non-negative integer"):
            ReaderProgress.new_local(
                series.series_id,
                chapter.chapter_id,
                cast(int, invalid_page_index),
                Decimal("0"),
            )

    invalid_percentages = (
        Decimal("-0.01"),
        Decimal("100.01"),
        Decimal("1.234"),
        Decimal("NaN"),
        Decimal("Infinity"),
        cast(Decimal, 50.0),
        cast(Decimal, "50"),
    )
    for percentage in invalid_percentages:
        with pytest.raises(ValueError):
            ReaderProgress.new_local(
                series.series_id,
                chapter.chapter_id,
                0,
                percentage,
            )

    with pytest.raises(ValueError, match="timezone information"):
        ReaderProgress.new_local(
            series.series_id,
            chapter.chapter_id,
            0,
            Decimal("1.25"),
            updated_at=datetime(2026, 9, 26),
        )


def test_reader_progress_mapper_round_trips_basis_points_without_float() -> None:
    series, chapter = _create_records()
    progress = ReaderProgress.new_local(
        series.series_id,
        chapter.chapter_id,
        7,
        Decimal("99.99"),
        updated_at=NOW,
    )

    row = reader_progress_to_row(progress)

    assert row.percentage_basis_points == 9_999
    assert row.updated_at_us == NOW_US
    assert reader_progress_from_row(row) == progress


def test_reader_progress_schema_enforces_series_chapter_pair_and_single_position(
    tmp_path: Path,
) -> None:
    database, series, chapter = _create_database_with_chapter(tmp_path)
    other_series = Series.new_local("Other series", "Other series", now=NOW)
    progress = ReaderProgress.new_local(
        series.series_id,
        chapter.chapter_id,
        4,
        Decimal("45.67"),
        updated_at=NOW,
    )
    try:
        with database.write_session() as session:
            session.add(series_to_row(other_series))
            session.add(reader_progress_to_row(progress))

        with database.read_session() as session:
            row = session.get(
                ReaderProgressRow,
                (str(series.series_id), str(chapter.chapter_id)),
            )
            primary_key_columns = inspect(session.connection()).get_pk_constraint(
                "reader_progress"
            )["constrained_columns"]
            chapter_index_names = {
                index_row[1]
                for index_row in session.connection()
                .exec_driver_sql("PRAGMA index_list('chapters')")
                .all()
            }
            foreign_key_errors = (
                session.connection().exec_driver_sql("PRAGMA foreign_key_check").all()
            )

        assert row is not None
        assert reader_progress_from_row(row) == progress
        assert row.percentage_basis_points == 4_567
        assert primary_key_columns == ["series_id", "chapter_id"]
        assert "ux_chapters_series_chapter_id" in chapter_index_names
        assert {table.name for table in PersistenceBase.metadata.sorted_tables} >= {
            "chapters",
            "reader_progress",
        }
        assert foreign_key_errors == []

        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                _insert_progress(
                    session,
                    series_id=str(other_series.series_id),
                    chapter_id=str(chapter.chapter_id),
                    page_index=0,
                    percentage_basis_points=0,
                )
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text("DELETE FROM chapters WHERE chapter_id = :chapter_id"),
                    {"chapter_id": str(chapter.chapter_id)},
                )
    finally:
        database.close()


def test_reader_progress_schema_rejects_invalid_numbers_and_missing_parents(
    tmp_path: Path,
) -> None:
    database, series, chapter = _create_database_with_chapter(tmp_path)
    try:
        invalid_rows: tuple[tuple[str, str, int | float, int | float, int | float], ...] = (
            (str(series.series_id), str(chapter.chapter_id), -1, 0, NOW_US),
            (str(series.series_id), str(chapter.chapter_id), 1.5, 0, NOW_US),
            (str(series.series_id), str(chapter.chapter_id), 0, -1, NOW_US),
            (str(series.series_id), str(chapter.chapter_id), 0, 10_001, NOW_US),
            (str(series.series_id), str(chapter.chapter_id), 0, 1.5, NOW_US),
            (str(series.series_id), str(uuid4()), 0, 0, NOW_US),
            (str(uuid4()), str(chapter.chapter_id), 0, 0, NOW_US),
            (str(series.series_id), str(chapter.chapter_id), 0, 0, 1.5),
        )
        for series_id, chapter_id, page_index, percentage, updated_at_us in invalid_rows:
            with pytest.raises(IntegrityError):
                with database.write_session() as session:
                    _insert_progress(
                        session,
                        series_id=series_id,
                        chapter_id=chapter_id,
                        page_index=page_index,
                        percentage_basis_points=percentage,
                        updated_at_us=updated_at_us,
                    )

        with database.write_session() as session:
            _insert_progress(
                session,
                series_id=str(series.series_id),
                chapter_id=str(chapter.chapter_id),
                page_index=2,
                percentage_basis_points=1,
            )
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                _insert_progress(
                    session,
                    series_id=str(series.series_id),
                    chapter_id=str(chapter.chapter_id),
                    page_index=2,
                    percentage_basis_points=1,
                )
    finally:
        database.close()


def test_reader_progress_migration_preserves_chapters_and_adds_fk_index(
    tmp_path: Path,
) -> None:
    database, series, chapter = _create_database_with_chapter(
        tmp_path,
        before_revision="0009_download_pages",
    )
    try:
        report = MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
        progress = ReaderProgress.new_local(
            series.series_id,
            chapter.chapter_id,
            12,
            Decimal("68.25"),
            updated_at=NOW,
        )
        with database.write_session() as session:
            session.add(reader_progress_to_row(progress))
        with database.read_session() as session:
            saved_chapter = session.execute(
                text(
                    "SELECT chapter_raw_label, display_label FROM chapters "
                    "WHERE chapter_id = :chapter_id"
                ),
                {"chapter_id": str(chapter.chapter_id)},
            ).one()
            foreign_key_errors = (
                session.connection().exec_driver_sql("PRAGMA foreign_key_check").all()
            )

        assert report.previous_revision == "0009_download_pages"
        assert report.current_revision == "0023_whole_manga_export_size"
        assert report.changed is True
        assert report.backup_path is not None and report.backup_path.is_file()
        assert tuple(saved_chapter) == ("12.10", "Chapter 12.10")
        assert foreign_key_errors == []
    finally:
        database.close()
