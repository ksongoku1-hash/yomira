from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

from app.domain.series import Series
from app.domain.series_source import SeriesSource
from app.domain.timestamps import normalise_utc

MetadataCandidateValue = str | UUID | tuple[str, ...]
MetadataValue = MetadataCandidateValue | None
_CATALOG_CONNECTOR_ID = "yomira.metadata.mangadex"


class MetadataField(StrEnum):
    TITLE = "title"
    COVER = "cover"
    AUTHOR = "author"
    DESCRIPTION = "description"
    GENRES = "genres"
    STATUS = "status"
    LANGUAGE = "language"


class MetadataAuthority(StrEnum):
    USER_OVERRIDE = "USER_OVERRIDE"
    LOCAL_CANONICAL = "LOCAL_CANONICAL"
    SOURCE_METADATA = "SOURCE_METADATA"


def validate_metadata_value(field: MetadataField, value: MetadataValue) -> None:
    if field is MetadataField.COVER:
        if value is not None and (not isinstance(value, UUID) or value.version != 4):
            raise ValueError("cover metadata must be a UUIDv4 or None")
        return

    if field is MetadataField.GENRES:
        if value is not None and (
            type(value) is not tuple or any(type(genre) is not str for genre in value)
        ):
            raise ValueError("genres metadata must be a tuple of strings or None")
        return

    if value is not None and type(value) is not str:
        raise ValueError(f"{field.value} metadata must be a string or None")


def _validate_source_value(field: MetadataField, value: MetadataValue) -> None:
    if field is MetadataField.COVER:
        if value is not None and type(value) is not str:
            raise ValueError("source cover metadata must be a URL string or None")
        return
    validate_metadata_value(field, value)


@dataclass(frozen=True, slots=True)
class MetadataOverride:
    series_id: UUID
    field: MetadataField
    value: MetadataValue
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.series_id.version != 4:
            raise ValueError("series_id must be a UUIDv4")
        try:
            field = MetadataField(self.field)
        except (TypeError, ValueError) as error:
            raise ValueError("field must be a supported MetadataField") from error
        object.__setattr__(self, "field", field)
        validate_metadata_value(field, self.value)
        created_at = normalise_utc(self.created_at, "created_at")
        updated_at = normalise_utc(self.updated_at, "updated_at")
        if updated_at < created_at:
            raise ValueError("updated_at must not precede created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

    @classmethod
    def new_local(
        cls,
        series_id: UUID,
        field: MetadataField,
        value: MetadataValue,
        *,
        now: datetime | None = None,
    ) -> MetadataOverride:
        timestamp = now if now is not None else datetime.now(UTC)
        return cls(
            series_id=series_id,
            field=field,
            value=value,
            created_at=timestamp,
            updated_at=timestamp,
        )


@dataclass(frozen=True, slots=True)
class MetadataSourceValue:
    series_source_id: UUID
    source_id: UUID
    value: MetadataCandidateValue
    source_display_name: str | None = None


@dataclass(frozen=True, slots=True)
class MetadataResolution:
    field: MetadataField
    value: MetadataValue
    authority: MetadataAuthority | None
    local_value: MetadataValue
    source_values: tuple[MetadataSourceValue, ...]
    has_user_override: bool
    user_override_value: MetadataValue
    source_conflict: bool
    effective_source_ids: tuple[UUID, ...]


def resolve_series_metadata(
    series: Series,
    sources: tuple[SeriesSource, ...],
    overrides: tuple[MetadataOverride, ...],
) -> tuple[MetadataResolution, ...]:
    override_by_field: dict[MetadataField, MetadataOverride] = {}
    for override in overrides:
        if override.series_id != series.series_id:
            raise ValueError("metadata override belongs to a different series")
        if override.field in override_by_field:
            raise ValueError("metadata field has more than one user override")
        override_by_field[override.field] = override

    ordered_sources = tuple(
        sorted(sources, key=lambda source: (str(source.source_id), str(source.series_source_id)))
    )
    if any(source.series_id != series.series_id for source in ordered_sources):
        raise ValueError("series source belongs to a different series")

    return tuple(
        _resolve_field(series, ordered_sources, override_by_field.get(field), field)
        for field in MetadataField
    )


def _resolve_field(
    series: Series,
    sources: tuple[SeriesSource, ...],
    override: MetadataOverride | None,
    field: MetadataField,
) -> MetadataResolution:
    local_value = _local_value(series, field)
    source_values_list: list[MetadataSourceValue] = []
    primary_source_values: list[MetadataSourceValue] = []
    for source in sources:
        source_value = _source_value(source, field)
        if source_value is not None:
            candidate = MetadataSourceValue(
                source.series_source_id,
                source.source_id,
                source_value,
                source.source_display_name,
            )
            source_values_list.append(candidate)
            if source.source_connector_id != _CATALOG_CONNECTOR_ID:
                primary_source_values.append(candidate)
    source_values = tuple(source_values_list)
    distinct_source_values: list[MetadataCandidateValue] = []
    for candidate in source_values:
        if candidate.value not in distinct_source_values:
            distinct_source_values.append(candidate.value)
    source_conflict = len(distinct_source_values) > 1
    preferred_source_values = tuple(primary_source_values) or source_values
    distinct_preferred_values: list[MetadataCandidateValue] = []
    for candidate in preferred_source_values:
        if candidate.value not in distinct_preferred_values:
            distinct_preferred_values.append(candidate.value)

    if override is not None:
        value = override.value
        authority = MetadataAuthority.USER_OVERRIDE
        effective_source_ids: tuple[UUID, ...] = ()
    elif local_value is not None:
        value = local_value
        authority = MetadataAuthority.LOCAL_CANONICAL
        effective_source_ids = ()
    elif len(distinct_preferred_values) == 1:
        value = distinct_preferred_values[0]
        authority = MetadataAuthority.SOURCE_METADATA
        effective_source_ids = tuple(
            candidate.source_id
            for candidate in preferred_source_values
            if candidate.value == value
        )
    else:
        value = None
        authority = None
        effective_source_ids = ()

    return MetadataResolution(
        field=field,
        value=value,
        authority=authority,
        local_value=local_value,
        source_values=source_values,
        has_user_override=override is not None,
        user_override_value=override.value if override is not None else None,
        source_conflict=source_conflict,
        effective_source_ids=effective_source_ids,
    )


def _local_value(series: Series, field: MetadataField) -> MetadataValue:
    return {
        MetadataField.TITLE: series.display_title,
        MetadataField.COVER: series.cover_asset_id,
        MetadataField.AUTHOR: series.author,
        MetadataField.DESCRIPTION: series.description,
        MetadataField.GENRES: series.genres,
        MetadataField.STATUS: series.status,
        MetadataField.LANGUAGE: series.language,
    }[field]


def _source_value(source: SeriesSource, field: MetadataField) -> MetadataValue:
    return {
        MetadataField.TITLE: source.remote_title,
        MetadataField.COVER: source.remote_cover_url,
        MetadataField.AUTHOR: source.remote_author,
        MetadataField.DESCRIPTION: source.remote_description,
        MetadataField.GENRES: source.remote_genres,
        MetadataField.STATUS: source.remote_status,
        MetadataField.LANGUAGE: source.remote_language,
    }[field]


def with_local_metadata_value(
    series: Series,
    field: MetadataField,
    value: MetadataValue,
    *,
    now: datetime | None = None,
) -> Series:
    validate_metadata_value(field, value)
    updated_at = now if now is not None else datetime.now(UTC)
    if field is MetadataField.TITLE:
        if value is None:
            raise ValueError("local title metadata must not be None")
        return replace(series, display_title=cast(str, value), updated_at=updated_at)
    if field is MetadataField.COVER:
        return replace(series, cover_asset_id=cast(UUID | None, value), updated_at=updated_at)
    if field is MetadataField.AUTHOR:
        return replace(series, author=cast(str | None, value), updated_at=updated_at)
    if field is MetadataField.DESCRIPTION:
        return replace(series, description=cast(str | None, value), updated_at=updated_at)
    if field is MetadataField.GENRES:
        return replace(series, genres=cast(tuple[str, ...] | None, value), updated_at=updated_at)
    if field is MetadataField.STATUS:
        return replace(series, status=cast(str | None, value), updated_at=updated_at)
    return replace(series, language=cast(str | None, value), updated_at=updated_at)


def with_source_metadata_value(
    source: SeriesSource,
    field: MetadataField,
    value: MetadataValue,
    *,
    checked_at: datetime | None = None,
) -> SeriesSource:
    _validate_source_value(field, value)
    last_checked_at = checked_at if checked_at is not None else datetime.now(UTC)
    if field is MetadataField.TITLE:
        return replace(
            source,
            remote_title=cast(str | None, value),
            last_checked_at=last_checked_at,
        )
    if field is MetadataField.COVER:
        return replace(
            source,
            remote_cover_url=cast(str | None, value),
            last_checked_at=last_checked_at,
        )
    if field is MetadataField.AUTHOR:
        return replace(
            source,
            remote_author=cast(str | None, value),
            last_checked_at=last_checked_at,
        )
    if field is MetadataField.DESCRIPTION:
        return replace(
            source,
            remote_description=cast(str | None, value),
            last_checked_at=last_checked_at,
        )
    if field is MetadataField.GENRES:
        return replace(
            source,
            remote_genres=cast(tuple[str, ...] | None, value),
            last_checked_at=last_checked_at,
        )
    if field is MetadataField.STATUS:
        return replace(
            source,
            remote_status=cast(str | None, value),
            last_checked_at=last_checked_at,
        )
    return replace(
        source,
        remote_language=cast(str | None, value),
        last_checked_at=last_checked_at,
    )
