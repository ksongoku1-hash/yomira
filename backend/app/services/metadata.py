from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.domain.metadata import (
    MetadataField,
    MetadataOverride,
    MetadataResolution,
    MetadataValue,
    resolve_series_metadata,
    with_local_metadata_value,
    with_source_metadata_value,
)
from app.domain.series import Series
from app.domain.series_source import SeriesSource
from app.persistence.database import SQLiteDatabase
from app.persistence.repositories.metadata import MetadataRepository
from app.persistence.repositories.series import SeriesRepository


class MetadataService:
    def __init__(
        self,
        database: SQLiteDatabase,
        series_repository: SeriesRepository | None = None,
        metadata_repository: MetadataRepository | None = None,
    ) -> None:
        self._database = database
        self._series_repository = series_repository or SeriesRepository()
        self._metadata_repository = metadata_repository or MetadataRepository()

    def resolve_series(self, series_id: UUID) -> tuple[MetadataResolution, ...] | None:
        with self._database.read_session() as session:
            series = self._series_repository.get(session, series_id)
            if series is None:
                return None
            sources = self._metadata_repository.list_series_sources(session, series_id)
            overrides = self._metadata_repository.list_overrides(session, series_id)
        return resolve_series_metadata(series, sources, overrides)

    def resolve_many(
        self, series: tuple[Series, ...]
    ) -> dict[UUID, tuple[MetadataResolution, ...]]:
        if not series:
            return {}
        series_ids = tuple(item.series_id for item in series)
        with self._database.read_session() as session:
            sources = self._metadata_repository.list_series_sources_many(session, series_ids)
            overrides = self._metadata_repository.list_overrides_many(session, series_ids)
        sources_by_series: dict[UUID, list[SeriesSource]] = {}
        overrides_by_series: dict[UUID, list[MetadataOverride]] = {}
        for source in sources:
            sources_by_series.setdefault(source.series_id, []).append(source)
        for override in overrides:
            overrides_by_series.setdefault(override.series_id, []).append(override)
        return {
            item.series_id: resolve_series_metadata(
                item,
                tuple(sources_by_series.get(item.series_id, ())),
                tuple(overrides_by_series.get(item.series_id, ())),
            )
            for item in series
        }

    def list_sources(self, series_id: UUID) -> tuple[SeriesSource, ...]:
        with self._database.read_session() as session:
            return self._metadata_repository.list_series_sources(session, series_id)

    def refresh_source(
        self,
        series_source_id: UUID,
        metadata: ConnectorSeriesMetadata,
        *,
        checked_at: datetime | None = None,
    ) -> bool:
        if type(metadata) is not ConnectorSeriesMetadata:
            raise TypeError("metadata must be normalized ConnectorSeriesMetadata")
        observed_at = checked_at if checked_at is not None else datetime.now(UTC)
        with self._database.write_session() as session:
            source = self._metadata_repository.get_series_source(session, series_source_id)
            if source is None:
                return False
            updated = replace(
                source,
                remote_title=metadata.title,
                remote_cover_url=metadata.cover_url,
                remote_author=metadata.author,
                remote_description=metadata.description,
                remote_genres=metadata.genres,
                remote_status=metadata.status,
                remote_language=metadata.language,
                last_checked_at=observed_at,
            )
            return self._metadata_repository.update_series_source(session, updated)

    def set_local_value(
        self,
        series_id: UUID,
        field: MetadataField,
        value: MetadataValue,
        *,
        now: datetime | None = None,
    ) -> bool:
        with self._database.write_session() as session:
            current = self._series_repository.get(session, series_id)
            if current is None:
                return False
            updated = with_local_metadata_value(current, field, value, now=now)
            return self._series_repository.update(session, updated)

    def set_user_override(
        self,
        series_id: UUID,
        field: MetadataField,
        value: MetadataValue,
        *,
        now: datetime | None = None,
    ) -> bool:
        with self._database.write_session() as session:
            if self._series_repository.get(session, series_id) is None:
                return False
            override = MetadataOverride.new_local(series_id, field, value, now=now)
            self._metadata_repository.save_override(session, override)
            return True

    def clear_user_override(self, series_id: UUID, field: MetadataField) -> bool:
        with self._database.write_session() as session:
            return self._metadata_repository.delete_override(session, series_id, field)

    def set_source_metadata(
        self,
        series_source_id: UUID,
        field: MetadataField,
        value: MetadataValue,
        *,
        checked_at: datetime | None = None,
    ) -> bool:
        with self._database.write_session() as session:
            source = self._metadata_repository.get_series_source(session, series_source_id)
            if source is None:
                return False
            updated = with_source_metadata_value(source, field, value, checked_at=checked_at)
            return self._metadata_repository.update_series_source(session, updated)
