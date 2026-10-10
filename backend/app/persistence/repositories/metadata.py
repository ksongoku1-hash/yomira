from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.metadata import MetadataField, MetadataOverride
from app.domain.series_source import SeriesSource
from app.persistence.mappers.metadata_override import (
    metadata_override_from_row,
    metadata_override_to_row,
)
from app.persistence.mappers.series_source import series_source_from_row, series_source_to_row
from app.persistence.models.metadata_override import MetadataOverrideRow
from app.persistence.models.series_source import SeriesSourceRow
from app.persistence.models.source import SourceRow


class MetadataRepository:
    def list_overrides(
        self,
        session: Session,
        series_id: UUID,
    ) -> tuple[MetadataOverride, ...]:
        return self.list_overrides_many(session, (series_id,))

    def list_overrides_many(
        self,
        session: Session,
        series_ids: tuple[UUID, ...],
    ) -> tuple[MetadataOverride, ...]:
        if not series_ids:
            return ()
        rows = session.scalars(
            select(MetadataOverrideRow)
            .where(MetadataOverrideRow.series_id.in_(tuple(str(item) for item in series_ids)))
            .order_by(MetadataOverrideRow.series_id.asc(), MetadataOverrideRow.field_name.asc())
        ).all()
        return tuple(metadata_override_from_row(row) for row in rows)

    def list_series_sources(
        self,
        session: Session,
        series_id: UUID,
    ) -> tuple[SeriesSource, ...]:
        return self.list_series_sources_many(session, (series_id,))

    def list_series_sources_many(
        self,
        session: Session,
        series_ids: tuple[UUID, ...],
    ) -> tuple[SeriesSource, ...]:
        if not series_ids:
            return ()
        rows = session.execute(
            select(SeriesSourceRow, SourceRow.display_name, SourceRow.connector_id)
            .join(SourceRow, SourceRow.source_id == SeriesSourceRow.source_id)
            .where(SeriesSourceRow.series_id.in_(tuple(str(item) for item in series_ids)))
            .order_by(
                SeriesSourceRow.series_id.asc(),
                SeriesSourceRow.source_id.asc(),
                SeriesSourceRow.series_source_id.asc(),
            )
        ).all()
        return tuple(
            series_source_from_row(
                row, source_display_name=display_name, source_connector_id=connector_id
            )
            for row, display_name, connector_id in rows
        )

    def get_series_source(
        self,
        session: Session,
        series_source_id: UUID,
    ) -> SeriesSource | None:
        result = session.execute(
            select(SeriesSourceRow, SourceRow.display_name, SourceRow.connector_id)
            .join(SourceRow, SourceRow.source_id == SeriesSourceRow.source_id)
            .where(SeriesSourceRow.series_source_id == str(series_source_id))
        ).one_or_none()
        if result is None:
            return None
        row, display_name, connector_id = result
        return series_source_from_row(
            row, source_display_name=display_name, source_connector_id=connector_id
        )

    def save_override(self, session: Session, override: MetadataOverride) -> None:
        row = metadata_override_to_row(override)
        key = (row.series_id, row.field_name)
        existing = session.get(MetadataOverrideRow, key)
        if existing is None:
            session.add(row)
            return
        existing.value_json = row.value_json
        existing.cover_asset_id = row.cover_asset_id
        existing.updated_at_us = row.updated_at_us

    def delete_override(
        self,
        session: Session,
        series_id: UUID,
        field: MetadataField,
    ) -> bool:
        row = session.get(MetadataOverrideRow, (str(series_id), field.value))
        if row is None:
            return False
        session.delete(row)
        return True

    def update_series_source(self, session: Session, source: SeriesSource) -> bool:
        existing = session.get(SeriesSourceRow, str(source.series_source_id))
        if existing is None:
            return False

        row = series_source_to_row(source)
        existing.remote_title = row.remote_title
        existing.remote_cover_url = row.remote_cover_url
        existing.remote_author = row.remote_author
        existing.remote_description = row.remote_description
        existing.remote_genres_json = row.remote_genres_json
        existing.remote_status = row.remote_status
        existing.remote_language = row.remote_language
        existing.last_checked_at_us = row.last_checked_at_us
        return True
