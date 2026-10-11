import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from sqlalchemy import func, select
from support.sqlite import open_test_database

from app.connectors.metadata import ConnectorCapability, ConnectorMetadata
from app.connectors.mock import MockConnector
from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.domain.metadata import MetadataAuthority, MetadataField
from app.persistence.database import SQLiteDatabase
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.series import SeriesRow
from app.persistence.models.series_source import SeriesSourceRow
from app.persistence.models.source import SourceRow
from app.services.jobs import DownloadJobService, LibrarySeriesImportSummary
from app.services.metadata import MetadataService

_CATALOG = ConnectorMetadata(
    connector_id="fixture.catalog",
    connector_version="1.0.0",
    capabilities=frozenset({ConnectorCapability.SEARCH, ConnectorCapability.METADATA}),
)
_METADATA = ConnectorSeriesMetadata(
    title="Source title",
    author="Source author",
    description="Source description",
    cover_url="https://fixture.invalid/cover.jpg",
    genres=("Action",),
    language="es",
)


def _database(tmp_path: Path) -> SQLiteDatabase:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    return database


def test_library_import_is_idempotent_and_preserves_local_authority(tmp_path: Path) -> None:
    database = _database(tmp_path)
    service = DownloadJobService(database)
    metadata_service = MetadataService(database)
    try:
        first = service.import_library_series(
            connector=_CATALOG, remote_series_id="series/1", series_metadata=_METADATA
        )
        assert first.created is True
        assert metadata_service.set_user_override(first.series_id, MetadataField.TITLE, "My title")
        assert metadata_service.set_local_value(
            first.series_id, MetadataField.DESCRIPTION, "My local description"
        )
        refreshed = replace(_METADATA, title="New source title", author="New source author")
        second = service.import_library_series(
            connector=_CATALOG, remote_series_id="series/1", series_metadata=refreshed
        )
        assert second.series_id == first.series_id
        assert second.created is False
        assert second.series_title == "My title"
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(SourceRow)) == 1
            assert session.scalar(select(func.count()).select_from(SeriesRow)) == 1
            assert session.scalar(select(func.count()).select_from(SeriesSourceRow)) == 1
            for model in (ChapterRow, ChapterSourceRow, DownloadJobRow):
                assert session.scalar(select(func.count()).select_from(model)) == 0
            source = session.scalar(select(SourceRow))
            association = session.scalar(select(SeriesSourceRow))
            series = session.get(SeriesRow, str(first.series_id))
            assert source is not None and association is not None and series is not None
            assert source.connector_id == _CATALOG.connector_id
            assert association.source_id == source.source_id
            assert association.remote_id == "series/1"
            assert association.remote_title == "New source title"
            assert association.remote_author == "New source author"
            assert association.remote_cover_url == _METADATA.cover_url
            assert series.canonical_title == "Source title"
            assert series.description == "My local description"
        resolved = metadata_service.resolve_series(first.series_id)
        assert resolved is not None
        title = next(value for value in resolved if value.field is MetadataField.TITLE)
        assert title.value == "My title"
        assert title.authority is MetadataAuthority.USER_OVERRIDE
        author = next(value for value in resolved if value.field is MetadataField.AUTHOR)
        assert author.value == "New source author"
        assert author.authority is MetadataAuthority.SOURCE_METADATA
    finally:
        database.shutdown()


def test_library_import_keeps_same_remote_id_isolated_by_source(tmp_path: Path) -> None:
    database = _database(tmp_path)
    service = DownloadJobService(database)
    try:
        first = service.import_library_series(
            connector=_CATALOG, remote_series_id="series/1", series_metadata=_METADATA
        )
        second = service.import_library_series(
            connector=replace(_CATALOG, connector_id="fixture.other"),
            remote_series_id="series/1",
            series_metadata=replace(_METADATA, title="Unrelated work"),
        )
        assert first.series_id != second.series_id
        assert first.created and second.created
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(SourceRow)) == 2
            assert session.scalar(select(func.count()).select_from(SeriesRow)) == 2
            assert session.scalar(select(func.count()).select_from(SeriesSourceRow)) == 2
    finally:
        database.shutdown()


def test_library_import_explicit_null_title_override_uses_canonical_fallback(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    service = DownloadJobService(database)
    metadata_service = MetadataService(database)
    try:
        imported = service.import_library_series(
            connector=_CATALOG, remote_series_id="series/1", series_metadata=_METADATA
        )
        assert metadata_service.set_local_value(
            imported.series_id, MetadataField.TITLE, "Local title"
        )
        assert metadata_service.set_user_override(imported.series_id, MetadataField.TITLE, None)
        repeated = service.import_library_series(
            connector=_CATALOG, remote_series_id="series/1", series_metadata=_METADATA
        )
        assert repeated.series_title == "Source title"
        resolved = metadata_service.resolve_series(imported.series_id)
        assert resolved is not None
        title = next(value for value in resolved if value.field is MetadataField.TITLE)
        assert title.has_user_override and title.value is None
        assert title.local_value == "Local title"
    finally:
        database.shutdown()


def test_concurrent_library_imports_create_one_identity(tmp_path: Path) -> None:
    database = _database(tmp_path)
    service = DownloadJobService(database)

    def import_series() -> LibrarySeriesImportSummary:
        return service.import_library_series(
            connector=_CATALOG, remote_series_id="series/1", series_metadata=_METADATA
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(import_series) for _ in range(2)]
            results = tuple(future.result() for future in futures)
        assert len({result.series_id for result in results}) == 1
        assert sum(result.created for result in results) == 1
    finally:
        database.shutdown()


def test_later_download_selection_reuses_imported_series(tmp_path: Path) -> None:
    database = _database(tmp_path)
    service = DownloadJobService(database)
    connector = MockConnector()
    try:
        metadata = connector.normalize_metadata(asyncio.run(connector.get_series("mock-series-1")))
        imported = service.import_library_series(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=metadata,
        )
        jobs = service.create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=metadata,
            chapters=asyncio.run(connector.get_chapters("mock-series-1")),
            selected_remote_ids=("mock-chapter-1",),
        )
        assert jobs[0].series_id == imported.series_id
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(SeriesRow)) == 1
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 1
    finally:
        database.shutdown()
