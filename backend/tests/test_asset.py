import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from support.sqlite import open_test_database, sqlite_test_context

from app.domain import (
    Asset,
    AssetType,
    Chapter,
    ChapterSource,
    Series,
    SeriesSource,
    Source,
    SourceHealthStatus,
)
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.asset import asset_from_row, asset_to_row
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.migrations.runner import (
    DatabaseIntegrityError,
    DatabaseMigrationError,
    MigrationRunner,
)
from app.persistence.models import AssetRow, SeriesRow

NOW = datetime(2026, 9, 26, 13, 15, 22, 123_456, tzinfo=UTC)
NOW_US = datetime_to_epoch_us(NOW)
SHA256 = "a" * 64


def test_asset_preserves_safe_portable_path_and_exact_metadata() -> None:
    local_time = datetime(
        2026,
        9,
        26,
        9,
        15,
        22,
        123_456,
        tzinfo=timezone(timedelta(hours=-4)),
    )
    asset = Asset.new_local(
        AssetType.CBZ,
        "Naruto/日本語 📚.cbz",
        SHA256,
        0,
        created_at=local_time,
        verified_at=NOW,
    )

    assert asset.asset_id.version == 4
    assert asset.asset_type is AssetType.CBZ
    assert asset.relative_path == "Naruto/日本語 📚.cbz"
    assert asset.size_bytes == 0
    assert asset.created_at == local_time.astimezone(UTC)
    assert asset.verified_at == NOW
    assert {item.value for item in AssetType} == {
        "COVER",
        "THUMBNAIL",
        "CBZ",
        "CBR",
        "PAGE_CACHE",
        "TEMP",
        "EXPORT_COPY",
    }


def test_asset_rejects_unsafe_paths_bad_hash_size_type_and_naive_time() -> None:
    unsafe_paths = (
        "",
        "/absolute/file.cbz",
        "C:/user/library/file.cbz",
        "folder\\file.cbz",
        "../file.cbz",
        "folder/../file.cbz",
        "folder/./file.cbz",
        "folder//file.cbz",
        "folder/file.cbz/",
        "folder/bad:name.cbz",
        "folder/bad?.cbz",
        "folder/CON.txt",
        "folder/LPT1.log",
    )
    for relative_path in unsafe_paths:
        with pytest.raises(ValueError):
            Asset.new_local(AssetType.CBZ, relative_path, SHA256, 1, created_at=NOW)

    with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
        Asset.new_local(AssetType.CBZ, "file.cbz", "A" * 64, 1, created_at=NOW)

    with pytest.raises(ValueError, match="size_bytes must be a non-negative integer"):
        Asset.new_local(AssetType.CBZ, "file.cbz", SHA256, -1, created_at=NOW)

    with pytest.raises(ValueError, match="size_bytes must be a non-negative integer"):
        Asset.new_local(
            AssetType.CBZ,
            "file.cbz",
            SHA256,
            cast(int, 1.5),
            created_at=NOW,
        )

    with pytest.raises(ValueError, match="supported AssetType"):
        Asset.new_local(cast(AssetType, "UNKNOWN"), "file.cbz", SHA256, 1, created_at=NOW)

    with pytest.raises(ValueError, match="timezone information"):
        Asset.new_local(
            AssetType.CBZ,
            "file.cbz",
            SHA256,
            1,
            created_at=datetime(2026, 9, 26),
        )


def test_asset_mapper_round_trips_creation_and_optional_verification() -> None:
    asset = Asset.new_local(
        AssetType.EXPORT_COPY,
        "exports/device-1/chapter.cbz",
        SHA256,
        1_234_567,
        created_at=datetime(1969, 12, 31, 23, 59, 59, 456_789, tzinfo=UTC),
        verified_at=None,
    )
    row = asset_to_row(asset)

    assert asset_from_row(row) == asset
    assert row.verified_at_us is None
    assert row.created_at_us == datetime_to_epoch_us(asset.created_at)


def _insert_asset(
    session: Session,
    asset_id: str,
    asset_type: str,
    relative_path: str,
    sha256: str,
    size_bytes: int | float,
    created_at_us: int | float,
    verified_at_us: int | float | None,
) -> None:
    session.execute(
        text(
            "INSERT INTO assets (asset_id, asset_type, relative_path, sha256, size_bytes, "
            "created_at_us, verified_at_us) VALUES (:asset_id, :asset_type, :relative_path, "
            ":sha256, :size_bytes, :created_at_us, :verified_at_us)"
        ),
        {
            "asset_id": asset_id,
            "asset_type": asset_type,
            "relative_path": relative_path,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "created_at_us": created_at_us,
            "verified_at_us": verified_at_us,
        },
    )


def test_asset_schema_validates_values_and_allows_shared_hashes(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    try:
        report = runner.upgrade_to_head()
        assert report.current_revision == "0024_reader_sessions"

        first = Asset.new_local(
            AssetType.COVER,
            "covers/series-a.webp",
            SHA256,
            2048,
            created_at=NOW,
        )
        second = Asset.new_local(
            AssetType.THUMBNAIL,
            "thumbnails/series-a.webp",
            SHA256,
            1024,
            created_at=NOW,
        )
        with database.write_session() as session:
            session.add_all([asset_to_row(first), asset_to_row(second)])
        with database.read_session() as session:
            row = session.get(AssetRow, str(first.asset_id))
            hash_matches = session.execute(
                text("SELECT count(*) FROM assets WHERE sha256 = :sha256"),
                {"sha256": SHA256},
            ).scalar_one()
            index_names = {
                index_row[1]
                for index_row in session.connection()
                .exec_driver_sql("PRAGMA index_list('assets')")
                .all()
            }
        assert row is not None
        assert asset_from_row(row) == first
        assert hash_matches == 2
        assert "ix_assets_sha256" in index_names

        invalid_rows: tuple[tuple[str, str, str, int | float, int | float], ...] = (
            ("not-a-uuid", "CBZ", "file.cbz", 1, NOW_US),
            ("123e4567-e89b-12d3-a456-426614174000", "CBZ", "file.cbz", 1, NOW_US),
            (str(uuid4()).upper(), "CBZ", "file.cbz", 1, NOW_US),
            (str(uuid4()), "UNKNOWN", "file.cbz", 1, NOW_US),
            (str(uuid4()), "CBZ", "/absolute/file.cbz", 1, NOW_US),
            (str(uuid4()), "CBZ", "../file.cbz", 1, NOW_US),
            (str(uuid4()), "CBZ", "folder\\file.cbz", 1, NOW_US),
            (str(uuid4()), "CBZ", "folder/bad:name.cbz", 1, NOW_US),
            (str(uuid4()), "CBZ", "folder/bad?.cbz", 1, NOW_US),
            (str(uuid4()), "CBZ", "file.cbz", -1, NOW_US),
            (str(uuid4()), "CBZ", "file.cbz", 1.5, NOW_US),
            (str(uuid4()), "CBZ", "file.cbz", 1, 1.5),
        )
        for asset_id, asset_type, relative_path, size_bytes, created_at_us in invalid_rows:
            with pytest.raises(IntegrityError):
                with database.write_session() as session:
                    _insert_asset(
                        session,
                        asset_id,
                        asset_type,
                        relative_path,
                        SHA256,
                        size_bytes,
                        created_at_us,
                        None,
                    )

        invalid_hashes = ("A" * 64, "g" * 64, "short")
        for invalid_hash in invalid_hashes:
            with pytest.raises(IntegrityError):
                with database.write_session() as session:
                    _insert_asset(
                        session,
                        str(uuid4()),
                        "CBZ",
                        f"new/{uuid4()}.cbz",
                        invalid_hash,
                        1,
                        NOW_US,
                        None,
                    )
    finally:
        database.close()


def _upgrade_to_previous_revision(
    database: SQLiteDatabase,
    runner: MigrationRunner,
) -> None:
    with database.exclusive_connection() as connection:
        config = Config(str(runner.config_path))
        config.attributes["connection"] = connection
        command.upgrade(config, "0006_chapter_sources")
        connection.commit()


def test_asset_migration_preserves_series_children_and_adds_cover_reference(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    backup_directory = tmp_path / "backups"
    runner = MigrationRunner(database, backup_directory)
    try:
        _upgrade_to_previous_revision(database, runner)
        series = Series(
            series_id=uuid4(),
            canonical_title="Preserve me",
            display_title="Display",
            description="Keep this text",
            status="Future status",
            language="x-test",
            cover_asset_id=None,
            created_at=NOW,
            updated_at=NOW,
        )
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
            now=NOW,
        )
        series_source = SeriesSource.new_local(
            series.series_id,
            source.source_id,
            remote_id="series-remote",
        )
        chapter_source = ChapterSource.new_local(
            chapter.chapter_id,
            source.source_id,
            remote_id="chapter-remote",
            available=True,
        )
        with database.write_session() as session:
            session.execute(
                text(
                    "INSERT INTO series (series_id, canonical_title, display_title, "
                    "description, status, language, created_at_us, updated_at_us) "
                    "VALUES (:series_id, :canonical_title, :display_title, :description, "
                    ":status, :language, :created_at_us, :updated_at_us)"
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
            session.add(source_to_row(source))
            session.flush()
            session.add(chapter_to_row(chapter))
            session.flush()
            session.execute(
                text(
                    "INSERT INTO series_sources (series_source_id, series_id, source_id, "
                    "remote_id, remote_url, remote_title, last_checked_at_us) "
                    "VALUES (:series_source_id, :series_id, :source_id, :remote_id, "
                    ":remote_url, :remote_title, :last_checked_at_us)"
                ),
                {
                    "series_source_id": str(series_source.series_source_id),
                    "series_id": str(series_source.series_id),
                    "source_id": str(series_source.source_id),
                    "remote_id": series_source.remote_id,
                    "remote_url": series_source.remote_url,
                    "remote_title": series_source.remote_title,
                    "last_checked_at_us": (
                        datetime_to_epoch_us(series_source.last_checked_at)
                        if series_source.last_checked_at is not None
                        else None
                    ),
                },
            )
            session.add(chapter_source_to_row(chapter_source))

        report = runner.upgrade_to_head()

        with database.read_session() as session:
            series_row = session.get(SeriesRow, str(series.series_id))
            child_counts = {
                "series_sources": session.execute(
                    text("SELECT count(*) FROM series_sources WHERE series_id = :series_id"),
                    {"series_id": str(series.series_id)},
                ).scalar_one(),
                "chapters": session.execute(
                    text("SELECT count(*) FROM chapters WHERE series_id = :series_id"),
                    {"series_id": str(series.series_id)},
                ).scalar_one(),
                "chapter_sources": session.execute(
                    text("SELECT count(*) FROM chapter_sources WHERE chapter_id = :chapter_id"),
                    {"chapter_id": str(chapter.chapter_id)},
                ).scalar_one(),
            }
            foreign_key_errors = (
                session.connection().exec_driver_sql("PRAGMA foreign_key_check").all()
            )
        assert report.current_revision == "0024_reader_sessions"
        assert report.backup_path is not None and report.backup_path.is_file()
        assert series_row is not None
        assert series_row.canonical_title == "Preserve me"
        assert series_row.display_title == "Display"
        assert series_row.description == "Keep this text"
        assert series_row.status == "Future status"
        assert series_row.language == "x-test"
        assert series_row.cover_asset_id is None
        assert child_counts == {"series_sources": 1, "chapters": 1, "chapter_sources": 1}
        assert foreign_key_errors == []

        asset = Asset.new_local(AssetType.COVER, "covers/preserve.webp", SHA256, 50, created_at=NOW)
        with database.write_session() as session:
            session.add(asset_to_row(asset))
            session.flush()
            session.execute(
                text("UPDATE series SET cover_asset_id = :asset_id WHERE series_id = :series_id"),
                {"asset_id": str(asset.asset_id), "series_id": str(series.series_id)},
            )
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text("DELETE FROM assets WHERE asset_id = :asset_id"),
                    {"asset_id": str(asset.asset_id)},
                )
        with database.read_session() as session:
            cover_asset_id = session.execute(
                text("SELECT cover_asset_id FROM series WHERE series_id = :series_id"),
                {"series_id": str(series.series_id)},
            ).scalar_one()
        assert cover_asset_id == str(asset.asset_id)
    finally:
        database.close()


def test_asset_migration_refuses_orphan_cover_without_losing_series(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    backup_directory = tmp_path / "backups"
    runner = MigrationRunner(database, backup_directory)
    orphan_cover_id = str(uuid4())
    series_id = str(uuid4())
    try:
        _upgrade_to_previous_revision(database, runner)
        with database.write_session() as session:
            session.execute(
                text(
                    "INSERT INTO series (series_id, canonical_title, display_title, "
                    "cover_asset_id, created_at_us, updated_at_us) "
                    "VALUES (:series_id, 'Keep orphan reference', 'Display', :cover_asset_id, "
                    ":created_at_us, :updated_at_us)"
                ),
                {
                    "series_id": series_id,
                    "cover_asset_id": orphan_cover_id,
                    "created_at_us": NOW_US,
                    "updated_at_us": NOW_US,
                },
            )

        with pytest.raises(DatabaseMigrationError) as raised:
            runner.upgrade_to_head()

        assert raised.value.restored is True
        assert raised.value.backup_path is not None
        assert raised.value.backup_path.is_file()
        with database.read_session() as session:
            row = session.execute(
                text(
                    "SELECT canonical_title, cover_asset_id FROM series "
                    "WHERE series_id = :series_id"
                ),
                {"series_id": series_id},
            ).one()
            current_revision = session.execute(text("SELECT version_num FROM alembic_version"))
            revision = current_revision.scalar_one()
            tables = set(
                session.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'table'")
                ).scalars()
            )
        assert tuple(row) == ("Keep orphan reference", orphan_cover_id)
        assert revision == "0006_chapter_sources"
        assert "assets" not in tables
    finally:
        database.close()


def test_database_integrity_check_rejects_existing_orphan_asset_reference(
    tmp_path: Path,
) -> None:
    database_context = sqlite_test_context(tmp_path)
    database_path = database_context.database_path
    database = database_context.open()
    runner = MigrationRunner(database, tmp_path / "backups")
    try:
        runner.upgrade_to_head()
    finally:
        database.close()

    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER tr_series_cover_asset_type_insert")
        connection.execute(
            "INSERT INTO series (series_id, canonical_title, display_title, cover_asset_id, "
            "created_at_us, updated_at_us) VALUES (?, 'Orphan', 'Orphan', ?, ?, ?)",
            (str(uuid4()), str(uuid4()), NOW_US, NOW_US),
        )

    reopened_database = database_context.open()
    reopened_runner = MigrationRunner(reopened_database, tmp_path / "backups")
    try:
        with pytest.raises(DatabaseIntegrityError):
            reopened_runner.upgrade_to_head()
    finally:
        reopened_database.close()
