import json
from typing import cast
from uuid import UUID

from app.domain.metadata import MetadataField, MetadataOverride, MetadataValue
from app.persistence.mappers.timestamps import datetime_to_epoch_us, epoch_us_to_datetime
from app.persistence.models.metadata_override import MetadataOverrideRow


def metadata_override_to_row(override: MetadataOverride) -> MetadataOverrideRow:
    cover_asset_id: str | None = None
    if override.field is MetadataField.COVER:
        cover_asset_id = str(override.value) if override.value is not None else None
        value_json = None
    else:
        value: object = override.value
        if override.field is MetadataField.GENRES and override.value is not None:
            value = list(cast(tuple[str, ...], override.value))
        value_json = json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    return MetadataOverrideRow(
        series_id=str(override.series_id),
        field_name=override.field.value,
        value_json=value_json,
        cover_asset_id=cover_asset_id,
        created_at_us=datetime_to_epoch_us(override.created_at),
        updated_at_us=datetime_to_epoch_us(override.updated_at),
    )


def metadata_override_from_row(row: MetadataOverrideRow) -> MetadataOverride:
    field = MetadataField(row.field_name)
    if field is MetadataField.COVER:
        if row.value_json is not None:
            raise ValueError("cover metadata override cannot contain a JSON value")
        value: MetadataValue = UUID(row.cover_asset_id) if row.cover_asset_id else None
    else:
        if row.cover_asset_id is not None or row.value_json is None:
            raise ValueError("metadata override value columns do not match the field")
        try:
            decoded: object = json.loads(row.value_json)
        except json.JSONDecodeError as error:
            raise ValueError("metadata override value must be valid JSON") from error
        if field is MetadataField.GENRES:
            if decoded is None:
                value = None
            elif not isinstance(decoded, list):
                raise ValueError("genres override must be a JSON array of strings or null")
            else:
                genres: list[str] = []
                for item in cast(list[object], decoded):
                    if type(item) is not str:
                        raise ValueError("genres override must be a JSON array of strings or null")
                    genres.append(item)
                value = tuple(genres)
        else:
            if decoded is not None and type(decoded) is not str:
                raise ValueError(f"{field.value} override must be a string or null")
            value = decoded

    return MetadataOverride(
        series_id=UUID(row.series_id),
        field=field,
        value=value,
        created_at=epoch_us_to_datetime(row.created_at_us),
        updated_at=epoch_us_to_datetime(row.updated_at_us),
    )
