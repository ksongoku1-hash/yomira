from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from support.sqlite import open_test_database

from app.domain.asset import Asset, AssetType
from app.domain.series import Series
from app.errors import ErrorCode
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.asset import asset_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.migrations.runner import (
    DatabaseIntegrityError,
    DatabaseMigrationError,
    MigrationRunner,
)
from app.services.library import LibraryService

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
SHA256 = "a" * 64


def _asset(asset_type: AssetType) -> Asset:
    return Asset.new_local(
        asset_type,
        f"covers/{uuid4()}.webp",
        SHA256,
        10,
        created_at=NOW,
        verified_at=NOW,
    )


def _series_with_cover(asset: Asset) -> Series:
    return replace(
        Series.new_local("Cover test", "Cover test", now=NOW),
        cover_asset_id=asset.asset_id,
    )


def _migrate_to_settings(database: SQLiteDatabase, runner: MigrationRunner) -> None:
    config = Config(str(runner.config_path))
    with database.exclusive_connection() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0011_settings")
        connection.commit()


def test_series_cover_must_reference_an_asset_of_type_cover(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    runner.upgrade_to_head()
    thumbnail = _asset(AssetType.THUMBNAIL)
    series = _series_with_cover(thumbnail)
    try:
        with database.write_session() as session:
            session.add(asset_to_row(thumbnail))

        with pytest.raises(IntegrityError):
            with database.write_session() as session:
                session.add(series_to_row(series))
    finally:
        database.close()


def test_referenced_cover_asset_cannot_change_to_another_asset_type(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    runner.upgrade_to_head()
    cover = _asset(AssetType.COVER)
    series = _series_with_cover(cover)
    try:
        with database.write_session() as session:
            session.add(asset_to_row(cover))
            session.add(series_to_row(series))

        with pytest.raises(IntegrityError):
            with database.write_session() as session:
                session.execute(
                    text("UPDATE assets SET asset_type = 'THUMBNAIL' WHERE asset_id = :asset_id"),
                    {"asset_id": str(cover.asset_id)},
                )

        with database.read_session() as session:
            asset_type = session.execute(
                text("SELECT asset_type FROM assets WHERE asset_id = :asset_id"),
                {"asset_id": str(cover.asset_id)},
            ).scalar_one()
        assert asset_type == AssetType.COVER.value
    finally:
        database.close()


def test_library_series_persists_a_cover_asset_reference(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    cover = _asset(AssetType.COVER)
    series = _series_with_cover(cover)
    library = LibraryService(database)
    try:
        with database.write_session() as session:
            session.add(asset_to_row(cover))

        library.add_series(series)

        restored_series = library.get_series(series.series_id)
        assert restored_series is not None
        assert restored_series.cover_asset_id == cover.asset_id
    finally:
        database.close()


def test_upgrade_refuses_to_relabel_legacy_cover_and_restores_backup(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    backup_directory = tmp_path / "backups"
    runner = MigrationRunner(database, backup_directory)
    _migrate_to_settings(database, runner)
    thumbnail = _asset(AssetType.THUMBNAIL)
    series = _series_with_cover(thumbnail)
    try:
        with database.write_session() as session:
            session.add(asset_to_row(thumbnail))
            session.flush()
            session.execute(
                text(
                    "INSERT INTO series (series_id, canonical_title, display_title, "
                    "description, status, language, cover_asset_id, created_at_us, "
                    "updated_at_us) VALUES (:series_id, :canonical_title, :display_title, "
                    ":description, :status, :language, :cover_asset_id, :created_at_us, "
                    ":updated_at_us)"
                ),
                {
                    "series_id": str(series.series_id),
                    "canonical_title": series.canonical_title,
                    "display_title": series.display_title,
                    "description": series.description,
                    "status": series.status,
                    "language": series.language,
                    "cover_asset_id": str(thumbnail.asset_id),
                    "created_at_us": 1_790_420_400_000_000,
                    "updated_at_us": 1_790_420_400_000_000,
                },
            )

        with pytest.raises(DatabaseMigrationError) as raised:
            runner.upgrade_to_head()

        assert raised.value.code == ErrorCode.DB_MIGRATION_FAILED
        assert raised.value.restored is True
        assert raised.value.backup_path is not None
        with database.read_session() as session:
            current_revision = session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            asset_type = session.execute(
                text("SELECT asset_type FROM assets WHERE asset_id = :asset_id"),
                {"asset_id": str(thumbnail.asset_id)},
            ).scalar_one()
            cover_id = session.execute(
                text("SELECT cover_asset_id FROM series WHERE series_id = :series_id"),
                {"series_id": str(series.series_id)},
            ).scalar_one()
        assert current_revision == "0011_settings"
        assert asset_type == AssetType.THUMBNAIL.value
        assert cover_id == str(thumbnail.asset_id)
    finally:
        database.close()


def test_database_gate_rejects_missing_cover_integrity_trigger(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    runner.upgrade_to_head()
    try:
        with database.exclusive_connection() as connection:
            connection.exec_driver_sql("DROP TRIGGER tr_series_cover_asset_type_update")
            connection.commit()

        with pytest.raises(DatabaseIntegrityError) as raised:
            runner.upgrade_to_head()

        assert raised.value.code == ErrorCode.DB_INTEGRITY_FAILED
    finally:
        database.close()
