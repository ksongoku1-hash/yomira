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
from app.domain.metadata import MetadataAuthority, MetadataField, MetadataOverride
from app.domain.series import Series
from app.domain.series_source import SeriesSource
from app.domain.source import Source, SourceHealthStatus
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.asset import asset_to_row
from app.persistence.mappers.metadata_override import metadata_override_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.mappers.series_source import series_source_from_row, series_source_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.migrations.runner import DatabaseIntegrityError, MigrationRunner
from app.persistence.models.series_source import SeriesSourceRow
from app.persistence.repositories.metadata import MetadataRepository
from app.persistence.repositories.series import SeriesRepository
from app.services.metadata import MetadataService

NOW = datetime(2026, 9, 26, 14, 0, tzinfo=UTC)


def _metadata_fixture(
    tmp_path: Path,
) -> tuple[SQLiteDatabase, MetadataService, Series, SeriesSource]:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    series = Series.new_local("canonical", "Local title", description="Local description", now=NOW)
    source = Source.new_local(
        "mock-manga",
        "1.0.0",
        "Mock Manga",
        True,
        SourceHealthStatus.HEALTHY,
        now=NOW,
    )
    association = SeriesSource.new_local(
        series.series_id,
        source.source_id,
        remote_id="work-1",
        remote_title="Remote title",
        remote_author="Remote author",
        remote_description="Remote description",
        remote_genres=("Action", "Drama"),
        remote_status="ongoing",
        remote_language="ja",
        last_checked_at=NOW,
    )
    with database.write_session() as session:
        session.add(series_to_row(series))
        session.add(source_to_row(source))
        session.flush()
        session.add(series_source_to_row(association))
    return database, MetadataService(database), series, association


def test_catalog_metadata_uses_connector_identity_and_preserves_original_data(
    tmp_path: Path,
) -> None:
    database, service, series, original = _metadata_fixture(tmp_path)
    catalog_source = Source.new_local(
        "yomira.metadata.mangadex",
        "1.0.0",
        "External catalog",
        True,
        SourceHealthStatus.HEALTHY,
        now=NOW,
    )
    catalog = SeriesSource.new_local(
        series.series_id,
        catalog_source.source_id,
        remote_id=str(uuid4()),
        remote_title="Catalog title",
        remote_author="Catalog author",
        remote_description="Catalog description",
        remote_cover_url="https://catalog.invalid/cover.jpg",
        remote_genres=("Comedy",),
        remote_status="completed",
        remote_language="en",
        last_checked_at=NOW,
    )
    try:
        with database.write_session() as session:
            session.add(source_to_row(catalog_source))
            session.flush()
            session.add(series_source_to_row(catalog))

        resolved = {item.field: item for item in service.resolve_series(series.series_id) or ()}
        assert resolved[MetadataField.AUTHOR].value == "Remote author"
        assert resolved[MetadataField.AUTHOR].source_conflict is True
        assert resolved[MetadataField.AUTHOR].effective_source_ids == (original.source_id,)
        assert {candidate.value for candidate in resolved[MetadataField.AUTHOR].source_values} == {
            "Remote author", "Catalog author"
        }
        assert resolved[MetadataField.COVER].value == catalog.remote_cover_url
        assert resolved[MetadataField.COVER].effective_source_ids == (catalog.source_id,)
        assert resolved[MetadataField.GENRES].value == ("Action", "Drama")
        assert resolved[MetadataField.STATUS].value == "ongoing"
        assert resolved[MetadataField.LANGUAGE].value == "ja"
        assert resolved[MetadataField.TITLE].value == "Local title"
        assert resolved[MetadataField.DESCRIPTION].value == "Local description"
        assert resolved[MetadataField.DESCRIPTION].authority is MetadataAuthority.LOCAL_CANONICAL

        sources = service.list_sources(series.series_id)
        by_source_id = {item.source_id: item for item in sources}
        assert by_source_id[original.source_id].source_connector_id == "mock-manga"
        assert by_source_id[catalog.source_id].source_connector_id == "yomira.metadata.mangadex"
        with database.read_session() as session:
            assert SeriesRepository().get(session, series.series_id) == series
            original_row = session.get(SeriesSourceRow, str(original.series_source_id))
            catalog_row = session.get(SeriesSourceRow, str(catalog.series_source_id))
            assert original_row is not None
            assert catalog_row is not None
            assert series_source_from_row(original_row) == original
            assert series_source_from_row(catalog_row) == catalog

        assert service.set_user_override(series.series_id, MetadataField.COVER, None, now=NOW)
        assert service.set_user_override(series.series_id, MetadataField.AUTHOR, "", now=NOW)
        overridden = {
            item.field: item for item in service.resolve_series(series.series_id) or ()
        }
        assert overridden[MetadataField.COVER].value is None
        assert overridden[MetadataField.COVER].authority is MetadataAuthority.USER_OVERRIDE
        assert overridden[MetadataField.AUTHOR].value == ""
        assert overridden[MetadataField.AUTHOR].authority is MetadataAuthority.USER_OVERRIDE
        assert len(overridden[MetadataField.AUTHOR].source_values) == 2
    finally:
        database.close()


def test_metadata_repository_enriches_list_and_get_from_source_without_schema_changes(
    tmp_path: Path,
) -> None:
    database, _service, series, association = _metadata_fixture(tmp_path)
    repository = MetadataRepository()
    try:
        with database.read_session() as session:
            sources = repository.list_series_sources_many(session, (series.series_id,))
            assert len(sources) == 1
            assert sources[0].source_connector_id == "mock-manga"
            assert sources[0].source_display_name == "Mock Manga"
            assert repository.get_series_source(session, association.series_source_id) == sources[0]
            assert repository.get_series_source(session, uuid4()) is None
            columns = {
                row[1]
                for row in session.connection().exec_driver_sql("PRAGMA table_info(series_sources)")
            }
            assert "source_connector_id" not in columns
            assert "source_display_name" not in columns
    finally:
        database.close()


def test_series_source_mapper_enrichment_is_ephemeral() -> None:
    source = SeriesSource.new_local(uuid4(), uuid4(), remote_id="remote-1", remote_author="Exact")
    row = series_source_to_row(source)
    enriched = series_source_from_row(
        row,
        source_display_name="Catalog display name",
        source_connector_id="yomira.metadata.mangadex",
    )

    assert enriched == replace(
        source,
        source_display_name="Catalog display name",
        source_connector_id="yomira.metadata.mangadex",
    )
    assert series_source_from_row(series_source_to_row(enriched)) == source


def test_metadata_service_persists_overrides_and_does_not_drop_source_values(
    tmp_path: Path,
) -> None:
    database, service, series, association = _metadata_fixture(tmp_path)
    try:
        service.set_user_override(series.series_id, MetadataField.DESCRIPTION, "", now=NOW)
        service.set_user_override(series.series_id, MetadataField.COVER, None, now=NOW)
        resolved = {item.field: item for item in service.resolve_series(series.series_id) or ()}

        assert resolved[MetadataField.DESCRIPTION].value == ""
        assert resolved[MetadataField.DESCRIPTION].authority is MetadataAuthority.USER_OVERRIDE
        assert resolved[MetadataField.COVER].has_user_override is True
        assert resolved[MetadataField.COVER].value is None
        assert resolved[MetadataField.AUTHOR].value == "Remote author"
        assert resolved[MetadataField.AUTHOR].authority is MetadataAuthority.SOURCE_METADATA
        assert resolved[MetadataField.AUTHOR].source_values[0].source_display_name == ("Mock Manga")
        assert resolved[MetadataField.GENRES].value == ("Action", "Drama")

        assert (
            service.set_local_value(
                series.series_id,
                MetadataField.AUTHOR,
                "Local author",
                now=NOW,
            )
            is True
        )
        assert (
            service.set_source_metadata(
                association.series_source_id,
                MetadataField.TITLE,
                "Refreshed remote title",
                checked_at=NOW,
            )
            is True
        )
        assert (
            service.set_source_metadata(
                association.series_source_id,
                MetadataField.COVER,
                "https://source.invalid/new-cover.jpg",
                checked_at=NOW,
            )
            is True
        )
        after_updates = {
            item.field: item for item in service.resolve_series(series.series_id) or ()
        }
        assert after_updates[MetadataField.AUTHOR].value == "Local author"
        assert after_updates[MetadataField.AUTHOR].authority is MetadataAuthority.LOCAL_CANONICAL
        assert after_updates[MetadataField.TITLE].source_values[0].value == (
            "Refreshed remote title"
        )
        assert after_updates[MetadataField.COVER].value is None
        assert after_updates[MetadataField.COVER].source_values[0].value == (
            "https://source.invalid/new-cover.jpg"
        )
        assert service.clear_user_override(series.series_id, MetadataField.COVER) is True
        after_cover_override_clear = {
            item.field: item for item in service.resolve_series(series.series_id) or ()
        }
        assert after_cover_override_clear[MetadataField.COVER].value == (
            "https://source.invalid/new-cover.jpg"
        )
        assert (
            after_cover_override_clear[MetadataField.COVER].authority
            is MetadataAuthority.SOURCE_METADATA
        )

        service.set_source_metadata(
            association.series_source_id,
            MetadataField.DESCRIPTION,
            "New source description",
            checked_at=NOW,
        )
        after_source_refresh = {
            item.field: item for item in service.resolve_series(series.series_id) or ()
        }
        assert after_source_refresh[MetadataField.DESCRIPTION].value == ""
        assert after_source_refresh[MetadataField.DESCRIPTION].source_values[0].value == (
            "New source description"
        )

        assert service.clear_user_override(series.series_id, MetadataField.DESCRIPTION) is True
        after_clear = {item.field: item for item in service.resolve_series(series.series_id) or ()}
        assert after_clear[MetadataField.DESCRIPTION].value == "Local description"
        assert after_clear[MetadataField.DESCRIPTION].authority is MetadataAuthority.LOCAL_CANONICAL
    finally:
        database.close()


def test_metadata_cover_override_rejects_a_non_cover_asset(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    series = Series.new_local("canonical", "Local title", now=NOW)
    thumbnail = Asset.new_local(
        AssetType.THUMBNAIL,
        "thumbnails/item.webp",
        "a" * 64,
        10,
        created_at=NOW,
    )
    override = MetadataOverride.new_local(
        series.series_id,
        MetadataField.COVER,
        thumbnail.asset_id,
        now=NOW,
    )
    try:
        with database.write_session() as session:
            session.add(series_to_row(series))
            session.add(asset_to_row(thumbnail))

        with pytest.raises(IntegrityError):
            with database.write_session() as session:
                session.add(metadata_override_to_row(override))
    finally:
        database.close()


def test_metadata_schema_gate_rejects_a_missing_cover_type_trigger(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    runner.upgrade_to_head()
    try:
        with database.exclusive_connection() as connection:
            connection.exec_driver_sql("DROP TRIGGER tr_metadata_override_cover_asset_type_update")
            connection.commit()

        with pytest.raises(DatabaseIntegrityError):
            runner.upgrade_to_head()
    finally:
        database.close()


def test_metadata_schema_upgrade_preserves_existing_series_and_remote_title(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    series_id = str(uuid4())
    source_id = str(uuid4())
    series_source_id = str(uuid4())
    now_us = 1_790_420_400_000_000
    try:
        with database.exclusive_connection() as connection:
            config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "0012_cover_asset_type")
            connection.commit()

        with database.write_session() as session:
            session.execute(
                text(
                    "INSERT INTO series (series_id, canonical_title, display_title, description, "
                    "status, language, cover_asset_id, created_at_us, updated_at_us) "
                    "VALUES (:series_id, 'canonical', 'Display', 'Exact description', "
                    "'Publishing', 'pt-BR', NULL, :timestamp, :timestamp)"
                ),
                {"series_id": series_id, "timestamp": now_us},
            )
            session.execute(
                text(
                    "INSERT INTO sources (source_id, connector_id, connector_version, "
                    "display_name, enabled, health_status, created_at_us, updated_at_us) "
                    "VALUES (:source_id, 'test', '1.0', 'Test source', 1, 'HEALTHY', "
                    ":timestamp, :timestamp)"
                ),
                {"source_id": source_id, "timestamp": now_us},
            )
            session.execute(
                text(
                    "INSERT INTO series_sources (series_source_id, series_id, source_id, "
                    "remote_id, remote_url, remote_title, last_checked_at_us) "
                    "VALUES (:series_source_id, :series_id, :source_id, 'remote-1', NULL, "
                    "'Exact remote title', :timestamp)"
                ),
                {
                    "series_source_id": series_source_id,
                    "series_id": series_id,
                    "source_id": source_id,
                    "timestamp": now_us,
                },
            )

        report = runner.upgrade_to_head()

        assert report.previous_revision == "0012_cover_asset_type"
        assert report.current_revision == "0024_reader_sessions"
        assert report.backup_path is not None and report.backup_path.is_file()
        with database.read_session() as session:
            local_row = session.execute(
                text(
                    "SELECT canonical_title, display_title, description, status, language, "
                    "author, genres_json FROM series WHERE series_id = :series_id"
                ),
                {"series_id": series_id},
            ).one()
            source_row = session.execute(
                text(
                    "SELECT remote_title, remote_author, remote_description, remote_status, "
                    "remote_language, remote_genres_json FROM series_sources "
                    "WHERE series_source_id = :series_source_id"
                ),
                {"series_source_id": series_source_id},
            ).one()
            override_count = session.execute(
                text("SELECT count(*) FROM metadata_overrides")
            ).scalar_one()

        assert tuple(local_row) == (
            "canonical",
            "Display",
            "Exact description",
            "Publishing",
            "pt-BR",
            None,
            None,
        )
        assert tuple(source_row) == (
            "Exact remote title",
            None,
            None,
            None,
            None,
            None,
        )
        assert override_count == 0
    finally:
        database.close()
