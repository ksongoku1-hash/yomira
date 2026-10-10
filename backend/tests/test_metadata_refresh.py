import asyncio
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from support.sqlite import open_test_database

from app.connectors.mock import MockConnector
from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.domain.metadata import MetadataAuthority, MetadataField
from app.domain.series import Series
from app.domain.series_source import SeriesSource
from app.domain.source import Source, SourceHealthStatus
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.series import series_to_row
from app.persistence.mappers.series_source import series_source_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.series_source import SeriesSourceRow
from app.persistence.repositories.metadata import MetadataRepository
from app.persistence.repositories.series import SeriesRepository
from app.services.jobs import DownloadJobService
from app.services.metadata import MetadataService

NOW = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[SQLiteDatabase]:
    database = open_test_database(tmp_path)
    try:
        MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
        yield database
    finally:
        database.close()


def _seed_series(database: SQLiteDatabase) -> tuple[Series, SeriesSource]:
    series = Series.new_local(
        "canonical",
        "Local title",
        author="Local author",
        description="Local description",
        genres=("Local",),
        status="local-status",
        language="es",
        now=NOW,
    )
    source = Source.new_local(
        "yomira.mock", "1.0.0", "Mock Manga", True, SourceHealthStatus.HEALTHY, now=NOW
    )
    association = SeriesSource.new_local(
        series.series_id,
        source.source_id,
        remote_id="mock-series-1",
        remote_url="https://source.invalid/series/1",
        remote_title="Remote title",
        remote_cover_url="https://source.invalid/cover.jpg",
        remote_author="Remote author",
        remote_description="Remote description",
        remote_genres=("Action",),
        remote_status="ongoing",
        remote_language="ja",
        last_checked_at=NOW,
    )
    with database.write_session() as session:
        session.add(series_to_row(series))
        session.add(source_to_row(source))
        session.flush()
        session.add(series_source_to_row(association))
    return series, association


def test_resolve_many_uses_one_session_and_two_queries_with_full_authority(
    database: SQLiteDatabase,
) -> None:
    series, association = _seed_series(database)
    other = Series.new_local("second", "Second title", now=NOW)
    unrelated = Series.new_local("unrelated", "Unrelated title", now=NOW)
    conflicting_source = Source.new_local(
        "other.mock", "1.0.0", "Other Manga", True, SourceHealthStatus.HEALTHY, now=NOW
    )
    with database.write_session() as session:
        session.add(series_to_row(other))
        session.add(series_to_row(unrelated))
        session.add(source_to_row(conflicting_source))
        session.flush()
        for item in (
            SeriesSource.new_local(
                series.series_id,
                conflicting_source.source_id,
                remote_id="alternate-1",
                remote_author="Conflicting author",
                remote_cover_url="https://other.invalid/cover.jpg",
            ),
            SeriesSource.new_local(
                other.series_id,
                association.source_id,
                remote_id="second-1",
                remote_genres=(),
                remote_language="ja",
            ),
            SeriesSource.new_local(
                unrelated.series_id,
                association.source_id,
                remote_id="unrelated-1",
                remote_author="Unrelated author",
            ),
        ):
            session.add(series_source_to_row(item))
    service = MetadataService(database)
    assert service.set_user_override(series.series_id, MetadataField.COVER, None, now=NOW)
    assert service.set_user_override(other.series_id, MetadataField.DESCRIPTION, "", now=NOW)
    statements: list[str] = []

    def capture_sql(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", capture_sql)
    try:
        with patch.object(database, "read_session", wraps=database.read_session) as read_session:
            resolved = service.resolve_many((series, other))
            read_session.assert_called_once()
    finally:
        event.remove(database.engine, "before_cursor_execute", capture_sql)

    assert len(statements) == 2
    assert set(resolved) == {series.series_id, other.series_id}
    assert resolved[series.series_id] == service.resolve_series(series.series_id)
    assert resolved[other.series_id] == service.resolve_series(other.series_id)
    first_fields = {item.field: item for item in resolved[series.series_id]}
    assert first_fields[MetadataField.AUTHOR].value == "Local author"
    assert first_fields[MetadataField.AUTHOR].authority is MetadataAuthority.LOCAL_CANONICAL
    assert first_fields[MetadataField.AUTHOR].source_conflict is True
    source_names = {
        item.source_display_name for item in first_fields[MetadataField.AUTHOR].source_values
    }
    assert source_names == {"Mock Manga", "Other Manga"}
    assert first_fields[MetadataField.COVER].authority is MetadataAuthority.USER_OVERRIDE
    assert first_fields[MetadataField.COVER].value is None
    assert first_fields[MetadataField.COVER].source_conflict is True
    other_fields = {item.field: item for item in resolved[other.series_id]}
    assert other_fields[MetadataField.GENRES].value == ()
    assert other_fields[MetadataField.GENRES].authority is MetadataAuthority.SOURCE_METADATA
    assert other_fields[MetadataField.DESCRIPTION].value == ""
    assert other_fields[MetadataField.DESCRIPTION].authority is MetadataAuthority.USER_OVERRIDE
    assert other_fields[MetadataField.AUTHOR].value is None
    assert other_fields[MetadataField.AUTHOR].authority is None


def test_resolve_many_empty_does_not_open_a_session(database: SQLiteDatabase) -> None:
    with patch.object(database, "read_session", wraps=database.read_session) as read_session:
        assert MetadataService(database).resolve_many(()) == {}
        read_session.assert_not_called()


@pytest.mark.parametrize(
    "metadata",
    (
        ConnectorSeriesMetadata(
            title="Refreshed title",
            cover_url="https://source.invalid/refreshed.jpg",
            author="Refreshed author",
            description="Refreshed description",
            genres=("Misterio", "Acción"),
            status="completed",
            language="ko",
        ),
        ConnectorSeriesMetadata(genres=()),
    ),
)
def test_refresh_source_replaces_all_remote_fields_and_preserves_local_authority(
    database: SQLiteDatabase,
    metadata: ConnectorSeriesMetadata,
) -> None:
    series, association = _seed_series(database)
    service = MetadataService(database)
    assert service.set_user_override(series.series_id, MetadataField.COVER, None, now=NOW)
    assert service.set_user_override(series.series_id, MetadataField.DESCRIPTION, None, now=NOW)
    assert service.refresh_source(association.series_source_id, metadata, checked_at=LATER)
    assert service.list_sources(series.series_id) == (
        replace(
            association,
            remote_title=metadata.title,
            remote_cover_url=metadata.cover_url,
            remote_author=metadata.author,
            remote_description=metadata.description,
            remote_genres=metadata.genres,
            remote_status=metadata.status,
            remote_language=metadata.language,
            last_checked_at=LATER,
            source_display_name="Mock Manga",
            source_connector_id="yomira.mock",
        ),
    )
    with database.read_session() as session:
        assert SeriesRepository().get(session, series.series_id) == series
    resolved = {item.field: item for item in service.resolve_series(series.series_id) or ()}
    assert resolved[MetadataField.TITLE].value == "Local title"
    assert resolved[MetadataField.AUTHOR].value == "Local author"
    assert resolved[MetadataField.AUTHOR].authority is MetadataAuthority.LOCAL_CANONICAL
    assert tuple(item.value for item in resolved[MetadataField.AUTHOR].source_values) == (
        () if metadata.author is None else (metadata.author,)
    )
    assert resolved[MetadataField.DESCRIPTION].value is None
    assert resolved[MetadataField.DESCRIPTION].authority is MetadataAuthority.USER_OVERRIDE
    assert resolved[MetadataField.COVER].value is None
    assert resolved[MetadataField.COVER].authority is MetadataAuthority.USER_OVERRIDE
    assert tuple(item.value for item in resolved[MetadataField.COVER].source_values) == (
        () if metadata.cover_url is None else (metadata.cover_url,)
    )
    assert resolved[MetadataField.GENRES].value == ("Local",)


def test_refresh_source_rolls_back_all_fields_on_repository_failure(
    database: SQLiteDatabase,
) -> None:
    series, association = _seed_series(database)

    class FailingRepository(MetadataRepository):
        def update_series_source(self, session: Session, source: SeriesSource) -> bool:
            assert super().update_series_source(session, source)
            raise RuntimeError("fixture write failure")

    service = MetadataService(database, metadata_repository=FailingRepository())
    with pytest.raises(RuntimeError, match="fixture write failure"):
        service.refresh_source(
            association.series_source_id,
            ConnectorSeriesMetadata(title="Rolled back title"),
            checked_at=LATER,
        )
    assert service.list_sources(series.series_id) == (
        replace(
            association, source_display_name="Mock Manga", source_connector_id="yomira.mock"
        ),
    )


def test_refresh_source_missing_and_default_checked_at(database: SQLiteDatabase) -> None:
    series, association = _seed_series(database)
    service = MetadataService(database)
    assert service.refresh_source(uuid4(), ConnectorSeriesMetadata()) is False
    assert service.list_sources(uuid4()) == ()
    started = datetime.now(UTC)
    assert service.refresh_source(association.series_source_id, ConnectorSeriesMetadata())
    source = service.list_sources(series.series_id)[0]
    assert source.last_checked_at is not None
    assert started <= source.last_checked_at <= datetime.now(UTC)


def test_selected_jobs_refreshes_existing_series_source_and_keeps_local_values(
    database: SQLiteDatabase,
) -> None:
    series, association = _seed_series(database)
    connector = MockConnector()
    chapters = asyncio.run(connector.get_chapters("mock-series-1"))
    jobs = DownloadJobService(database)
    metadata_service = MetadataService(database)
    assert metadata_service.set_user_override(series.series_id, MetadataField.COVER, None, now=NOW)
    first = jobs.create_selected_jobs(
        connector=connector.metadata,
        remote_series_id="mock-series-1",
        series_metadata=ConnectorSeriesMetadata(
            title="Initial import title",
            cover_url="https://source.invalid/import-cover.jpg",
            author="Initial import author",
            description="Initial import description",
            genres=("Initial genre",),
            status="ongoing",
            language="ja",
        ),
        chapters=chapters,
        selected_remote_ids=("mock-chapter-1",),
        now=NOW,
    )
    second = jobs.create_selected_jobs(
        connector=connector.metadata,
        remote_series_id="mock-series-1",
        series_metadata=ConnectorSeriesMetadata(title="Updated import title", genres=()),
        chapters=chapters,
        selected_remote_ids=("mock-chapter-1",),
        now=LATER,
    )
    assert first[0].created is True
    assert second[0].created is False
    assert second[0].job.job_id == first[0].job.job_id
    assert second[0].series_id == series.series_id
    assert metadata_service.list_sources(series.series_id) == (
        replace(
            association,
            remote_title="Updated import title",
            remote_cover_url=None,
            remote_author=None,
            remote_description=None,
            remote_genres=(),
            remote_status=None,
            remote_language=None,
            last_checked_at=LATER,
            source_display_name="Mock Manga",
            source_connector_id="yomira.mock",
        ),
    )
    with database.read_session() as session:
        assert SeriesRepository().get(session, series.series_id) == series
        assert session.scalar(select(func.count()).select_from(SeriesSourceRow)) == 1
    cover = next(
        item
        for item in metadata_service.resolve_series(series.series_id) or ()
        if item.field is MetadataField.COVER
    )
    assert cover.has_user_override is True
    assert cover.authority is MetadataAuthority.USER_OVERRIDE
    assert cover.value is None
