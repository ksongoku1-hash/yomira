from uuid import UUID

from app.domain.series_source import SeriesSource
from app.persistence.mappers.json_fields import (
    decode_optional_string_tuple,
    encode_optional_string_tuple,
)
from app.persistence.mappers.timestamps import datetime_to_epoch_us, epoch_us_to_datetime
from app.persistence.models.series_source import SeriesSourceRow


def series_source_to_row(series_source: SeriesSource) -> SeriesSourceRow:
    return SeriesSourceRow(
        series_source_id=str(series_source.series_source_id),
        series_id=str(series_source.series_id),
        source_id=str(series_source.source_id),
        remote_id=series_source.remote_id,
        remote_url=series_source.remote_url,
        remote_title=series_source.remote_title,
        remote_cover_url=series_source.remote_cover_url,
        remote_author=series_source.remote_author,
        remote_description=series_source.remote_description,
        remote_genres_json=encode_optional_string_tuple(series_source.remote_genres),
        remote_status=series_source.remote_status,
        remote_language=series_source.remote_language,
        last_checked_at_us=(
            datetime_to_epoch_us(series_source.last_checked_at)
            if series_source.last_checked_at is not None
            else None
        ),
    )


def series_source_from_row(
    row: SeriesSourceRow,
    *,
    source_display_name: str | None = None,
    source_connector_id: str | None = None,
) -> SeriesSource:
    return SeriesSource(
        series_source_id=UUID(row.series_source_id),
        series_id=UUID(row.series_id),
        source_id=UUID(row.source_id),
        remote_id=row.remote_id,
        remote_url=row.remote_url,
        remote_title=row.remote_title,
        remote_cover_url=row.remote_cover_url,
        remote_author=row.remote_author,
        remote_description=row.remote_description,
        remote_genres=decode_optional_string_tuple(
            row.remote_genres_json,
            "remote_genres_json",
        ),
        remote_status=row.remote_status,
        remote_language=row.remote_language,
        last_checked_at=(
            epoch_us_to_datetime(row.last_checked_at_us)
            if row.last_checked_at_us is not None
            else None
        ),
        source_display_name=source_display_name,
        source_connector_id=source_connector_id,
    )
