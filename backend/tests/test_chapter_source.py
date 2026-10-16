from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from support.sqlite import open_test_database

from app.domain import Chapter, ChapterSource, Series, Source, SourceHealthStatus
from app.persistence.base import PersistenceBase
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.chapter_source import (
    chapter_source_from_row,
    chapter_source_to_row,
)
from app.persistence.mappers.series import series_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models import ChapterSourceRow

NOW = datetime(2026, 9, 26, 9, 20, 30, 456_789, tzinfo=UTC)
NOW_US = datetime_to_epoch_us(NOW)


def test_chapter_source_preserves_remote_text_and_normalizes_last_seen() -> None:
    local_time = datetime(
        2026,
        9,
        26,
        5,
        20,
        30,
        456_789,
        tzinfo=timezone(timedelta(hours=-4)),
    )
    chapter_source = ChapterSource.new_local(
        uuid4(),
        uuid4(),
        remote_id="  chapter/001  ",
        remote_url="https://example.invalid/work/42/chapter/001?edition=raw",
        remote_label="  12.10\nSpecial  ",
        available=True,
        last_seen_at=local_time,
    )

    assert chapter_source.chapter_source_id.version == 4
    assert chapter_source.remote_id == "  chapter/001  "
    assert chapter_source.remote_url == "https://example.invalid/work/42/chapter/001?edition=raw"
    assert chapter_source.remote_label == "  12.10\nSpecial  "
    assert chapter_source.available is True
    assert chapter_source.last_seen_at == local_time.astimezone(UTC)


def test_chapter_source_validates_identity_reference_boolean_and_timestamp() -> None:
    legacy_id = UUID("123e4567-e89b-12d3-a456-426614174000")
    with pytest.raises(ValueError, match="chapter_source_id must be a UUIDv4"):
        ChapterSource(
            chapter_source_id=legacy_id,
            chapter_id=uuid4(),
            source_id=uuid4(),
            remote_id="chapter-1",
            remote_url=None,
            remote_label=None,
            available=True,
            last_seen_at=None,
        )

    with pytest.raises(ValueError, match="remote_id or remote_url must be provided"):
        ChapterSource.new_local(uuid4(), uuid4(), available=False)

    with pytest.raises(ValueError, match="remote_id must not be empty"):
        ChapterSource.new_local(
            uuid4(), uuid4(), remote_id="", remote_url="/chapter/1", available=True
        )

    with pytest.raises(ValueError, match="available must be a boolean"):
        ChapterSource.new_local(uuid4(), uuid4(), remote_id="chapter-1", available=cast(bool, 1))

    with pytest.raises(ValueError, match="timezone information"):
        ChapterSource.new_local(
            uuid4(),
            uuid4(),
            remote_url="/chapter/1",
            available=False,
            last_seen_at=datetime(2026, 9, 26),
        )


def test_chapter_source_mapper_round_trips_observed_and_unchecked_rows() -> None:
    observed = ChapterSource.new_local(
        uuid4(),
        uuid4(),
        remote_id="future-id",
        remote_label="Future label value",
        available=True,
        last_seen_at=NOW,
    )
    unchecked = ChapterSource.new_local(
        uuid4(),
        uuid4(),
        remote_url="/chapter/without-id",
        remote_label="",
        available=False,
    )

    assert chapter_source_from_row(chapter_source_to_row(observed)) == observed
    assert chapter_source_from_row(chapter_source_to_row(unchecked)) == unchecked


def test_chapter_source_metadata_has_restrict_foreign_keys_and_only_needed_indexes() -> None:
    assert ChapterSourceRow.__tablename__ == "chapter_sources"
    sorted_table_names = {table.name for table in PersistenceBase.metadata.sorted_tables}
    assert {"chapters", "sources", "chapter_sources"} <= sorted_table_names

    foreign_keys = {
        foreign_key.target_fullname: foreign_key
        for foreign_key in ChapterSourceRow.__table__.foreign_keys
    }
    assert foreign_keys["chapters.chapter_id"].ondelete == "RESTRICT"
    assert foreign_keys["sources.source_id"].ondelete == "RESTRICT"


def _insert_chapter_source(
    session: Session,
    chapter_source_id: str,
    chapter_id: str,
    source_id: str,
    remote_id: str | None,
    remote_url: str | None,
    remote_label: str | None,
    available: bool | int,
    last_seen_at_us: int | float | None,
) -> None:
    session.execute(
        text(
            "INSERT INTO chapter_sources (chapter_source_id, chapter_id, source_id, remote_id, "
            "remote_url, remote_label, available, last_seen_at_us) VALUES (:chapter_source_id, "
            ":chapter_id, :source_id, :remote_id, :remote_url, :remote_label, :available, "
            ":last_seen_at_us)"
        ),
        {
            "chapter_source_id": chapter_source_id,
            "chapter_id": chapter_id,
            "source_id": source_id,
            "remote_id": remote_id,
            "remote_url": remote_url,
            "remote_label": remote_label,
            "available": available,
            "last_seen_at_us": last_seen_at_us,
        },
    )


def test_chapter_source_migration_keeps_versions_and_enforces_relationships(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    try:
        report = runner.upgrade_to_head()
        assert report.current_revision == "0024_reader_sessions"

        series = Series.new_local("Series", "Series")
        chapter = Chapter.new_local(
            series.series_id,
            "1",
            "Chapter 1",
            chapter_sort_key=(1, 0, 0),
        )
        source = Source.new_local(
            "connector",
            "v1",
            "Connector",
            True,
            SourceHealthStatus.HEALTHY,
        )
        first_version = ChapterSource.new_local(
            chapter.chapter_id,
            source.source_id,
            remote_id="1",
            remote_url="https://example.invalid/work/a/chapter/1",
            remote_label="1",
            available=True,
            last_seen_at=NOW,
        )
        second_version = ChapterSource.new_local(
            chapter.chapter_id,
            source.source_id,
            remote_id="1",
            remote_url="https://example.invalid/work/a/chapter/1",
            remote_label="1 (revised)",
            available=False,
        )
        with database.write_session() as session:
            session.add_all([series_to_row(series), source_to_row(source)])
            session.flush()
            session.add(chapter_to_row(chapter))
            session.flush()
            session.add_all(
                [chapter_source_to_row(first_version), chapter_source_to_row(second_version)]
            )

        with database.read_session() as session:
            first_row = session.get(ChapterSourceRow, str(first_version.chapter_source_id))
            second_row = session.get(ChapterSourceRow, str(second_version.chapter_source_id))
            chapter_count = session.execute(
                text(
                    "SELECT count(*) FROM chapter_sources "
                    "WHERE chapter_id = :chapter_id AND source_id = :source_id"
                ),
                {
                    "chapter_id": str(chapter.chapter_id),
                    "source_id": str(source.source_id),
                },
            ).scalar_one()
            index_names = {
                row[1]
                for row in session.connection()
                .exec_driver_sql("PRAGMA index_list('chapter_sources')")
                .all()
            }
        assert first_row is not None
        assert second_row is not None
        assert chapter_source_from_row(first_row) == first_version
        assert chapter_source_from_row(second_row) == second_version
        assert chapter_count == 2
        assert {"ix_chapter_sources_chapter_id", "ix_chapter_sources_source_id"} <= index_names
        assert not any("available" in index_name for index_name in index_names)

        invalid_rows: tuple[
            tuple[str, str | None, str | None, bool | int, int | float | None], ...
        ] = (
            ("not-a-uuid", "id", None, True, NOW_US),
            ("123e4567-e89b-12d3-a456-426614174000", "id", None, True, NOW_US),
            (str(uuid4()).upper(), "id", None, True, NOW_US),
            (str(uuid4()), None, None, True, NOW_US),
            (str(uuid4()), "", None, True, NOW_US),
            (str(uuid4()), None, "", True, NOW_US),
            (str(uuid4()), "id", None, 2, NOW_US),
            (str(uuid4()), "id", None, False, 1.5),
        )
        for chapter_source_id, remote_id, remote_url, available, last_seen_at_us in invalid_rows:
            with pytest.raises(IntegrityError):
                with database.write_session() as session:
                    _insert_chapter_source(
                        session,
                        chapter_source_id,
                        str(chapter.chapter_id),
                        str(source.source_id),
                        remote_id,
                        remote_url,
                        None,
                        available,
                        last_seen_at_us,
                    )

        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                _insert_chapter_source(
                    session,
                    str(uuid4()),
                    str(uuid4()),
                    str(source.source_id),
                    "missing-chapter",
                    None,
                    None,
                    True,
                    None,
                )
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                _insert_chapter_source(
                    session,
                    str(uuid4()),
                    str(chapter.chapter_id),
                    str(uuid4()),
                    "missing-source",
                    None,
                    None,
                    True,
                    None,
                )
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text("DELETE FROM chapters WHERE chapter_id = :chapter_id"),
                    {"chapter_id": str(chapter.chapter_id)},
                )
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text("DELETE FROM sources WHERE source_id = :source_id"),
                    {"source_id": str(source.source_id)},
                )
    finally:
        database.close()
