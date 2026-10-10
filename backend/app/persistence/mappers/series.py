from uuid import UUID

from app.domain.series import Series
from app.persistence.mappers.json_fields import (
    decode_optional_string_tuple,
    encode_optional_string_tuple,
)
from app.persistence.mappers.timestamps import datetime_to_epoch_us, epoch_us_to_datetime
from app.persistence.models.series import SeriesRow


def series_to_row(series: Series) -> SeriesRow:
    return SeriesRow(
        series_id=str(series.series_id),
        canonical_title=series.canonical_title,
        display_title=series.display_title,
        description=series.description,
        status=series.status,
        language=series.language,
        author=series.author,
        genres_json=encode_optional_string_tuple(series.genres),
        cover_asset_id=(
            str(series.cover_asset_id) if series.cover_asset_id is not None else None
        ),
        created_at_us=datetime_to_epoch_us(series.created_at),
        updated_at_us=datetime_to_epoch_us(series.updated_at),
    )


def series_from_row(row: SeriesRow) -> Series:
    return Series(
        series_id=UUID(row.series_id),
        canonical_title=row.canonical_title,
        display_title=row.display_title,
        description=row.description,
        status=row.status,
        language=row.language,
        author=row.author,
        genres=decode_optional_string_tuple(row.genres_json, "genres_json"),
        cover_asset_id=(UUID(row.cover_asset_id) if row.cover_asset_id is not None else None),
        created_at=epoch_us_to_datetime(row.created_at_us),
        updated_at=epoch_us_to_datetime(row.updated_at_us),
    )
