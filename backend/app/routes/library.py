import asyncio
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import UUID4, BaseModel, ConfigDict, field_validator, model_validator

from app.connectors.metadata import ConnectorCapability
from app.connectors.registry import ConnectorRegistryError
from app.domain.metadata import (
    MetadataAuthority,
    MetadataField,
    MetadataResolution,
    MetadataValue,
    resolve_series_metadata,
)
from app.domain.series import Series
from app.errors import ApiErrorBody, ApiErrorEnvelope, ErrorCode
from app.fmd2_bridge import FMD2_METADATA, fmd2_source_display_name
from app.persistence.storage_gate import StorageGateClosedError
from app.runtime import RuntimeState
from app.services.cover_assets import CoverAssetError
from app.services.external_metadata import MetadataAssociationConflict
from app.services.library import (
    DEFAULT_LIBRARY_PAGE_SIZE,
    MAX_LIBRARY_PAGE_SIZE,
    LibraryReadingStatus,
    LibraryService,
)
from app.services.metadata import MetadataService
from app.services.reader_asset import ReaderAssetError
from app.services.recent_views import DEFAULT_RECENT_LIMIT, MAX_RECENT_LIMIT
from app.services.thumbnails import ThumbnailProcessingError, ThumbnailVariant
from app.tracing import resolve_request_trace_id

router = APIRouter(tags=["library"])


class SeriesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    series_id: UUID
    canonical_title: str
    display_title: str
    description: str | None
    status: str | None
    language: str | None
    cover_asset_id: UUID | None
    author: str | None = None
    genres: list[str] | None = None
    created_at: datetime
    updated_at: datetime


class LibraryMeta(BaseModel):
    limit: int
    offset: int
    total: int


class LibraryResponseEnvelope(BaseModel):
    data: list[SeriesResponse]
    meta: LibraryMeta


class RecentLibraryMeta(BaseModel):
    limit: int


class RecentLibraryResponseEnvelope(BaseModel):
    data: list[SeriesResponse]
    meta: RecentLibraryMeta


class SeriesMeta(BaseModel):
    pass


MetadataApiValue = str | list[str] | None


class MetadataSourceValueResponse(BaseModel):
    series_source_id: UUID
    source_id: UUID
    source_display_name: str | None
    value: MetadataApiValue


class MetadataResolutionResponse(BaseModel):
    field: MetadataField
    value: MetadataApiValue
    authority: MetadataAuthority | None
    local_value: MetadataApiValue
    source_values: list[MetadataSourceValueResponse]
    has_user_override: bool
    user_override_value: MetadataApiValue
    source_conflict: bool
    effective_source_ids: list[UUID]


class ReadingSourceResponse(BaseModel):
    series_source_id: UUID
    connector_id: str
    remote_series_id: str
    display_name: str
    author: str | None
    description: str | None
    genres: list[str] | None
    status: str | None
    language: str | None


class SeriesDetailResponse(SeriesResponse):
    metadata: list[MetadataResolutionResponse]
    reading_sources: list[ReadingSourceResponse] = []


class SeriesResponseEnvelope(BaseModel):
    data: SeriesDetailResponse
    meta: SeriesMeta


class ExternalMetadataCandidateResponse(BaseModel):
    remote_id: UUID4
    title: str
    year: int | None
    author: str | None


class ExternalMetadataMeta(BaseModel):
    provider: str = "MangaDex"
    total: int
    limit: int = 10


class ExternalMetadataEnvelope(BaseModel):
    data: list[ExternalMetadataCandidateResponse]
    meta: ExternalMetadataMeta


class ExternalMetadataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote_id: UUID4


class MetadataOverrideRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    field: MetadataField
    value: str | list[str] | None

    @field_validator("field", mode="before")
    @classmethod
    def parse_field(cls, value: object) -> MetadataField:
        if not isinstance(value, str):
            raise ValueError("field must be a supported metadata field")
        try:
            return MetadataField(value)
        except ValueError as error:
            raise ValueError("field must be a supported metadata field") from error

    @model_validator(mode="after")
    def validate_value_for_field(self) -> MetadataOverrideRequest:
        if self.field is MetadataField.GENRES:
            if self.value is not None and not isinstance(self.value, list):
                raise ValueError("genres overrides must be an array of strings or null")
        elif self.field is MetadataField.COVER:
            if self.value is not None:
                if not isinstance(self.value, str):
                    raise ValueError("cover overrides must be an asset UUID or null")
                try:
                    cover_asset_id = UUID(self.value)
                except ValueError as error:
                    raise ValueError("cover overrides must be an asset UUID or null") from error
                if cover_asset_id.version != 4 or str(cover_asset_id) != self.value:
                    raise ValueError("cover overrides must be a canonical UUIDv4 or null")
        elif self.value is not None and not isinstance(self.value, str):
            raise ValueError(f"{self.field.value} overrides must be a string or null")
        return self


def _library_service(request: Request) -> LibraryService:
    runtime = cast(RuntimeState, request.app.state.runtime)
    if runtime.library_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Library service is not configured.",
        )
    return runtime.library_service


def _metadata_service(request: Request) -> MetadataService:
    runtime = cast(RuntimeState, request.app.state.runtime)
    if runtime.metadata_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Metadata service is not configured.",
        )
    return runtime.metadata_service


def _api_metadata_value(value: MetadataValue) -> MetadataApiValue:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    return value


def _metadata_response(
    resolution: MetadataResolution,
) -> MetadataResolutionResponse:
    return MetadataResolutionResponse(
        field=resolution.field,
        value=_api_metadata_value(resolution.value),
        authority=resolution.authority,
        local_value=_api_metadata_value(resolution.local_value),
        source_values=[
            MetadataSourceValueResponse(
                series_source_id=candidate.series_source_id,
                source_id=candidate.source_id,
                source_display_name=candidate.source_display_name,
                value=_api_metadata_value(candidate.value),
            )
            for candidate in resolution.source_values
        ],
        has_user_override=resolution.has_user_override,
        user_override_value=_api_metadata_value(resolution.user_override_value),
        source_conflict=resolution.source_conflict,
        effective_source_ids=list(resolution.effective_source_ids),
    )


def _series_response(series: Series, resolutions: tuple[MetadataResolution, ...]) -> SeriesResponse:
    values = {item.field: item.value for item in resolutions}
    cover_value = values[MetadataField.COVER]
    return SeriesResponse(
        series_id=series.series_id,
        canonical_title=series.canonical_title,
        display_title=cast(str | None, values[MetadataField.TITLE]) or series.canonical_title,
        description=cast(str | None, values[MetadataField.DESCRIPTION]),
        status=cast(str | None, values[MetadataField.STATUS]),
        language=cast(str | None, values[MetadataField.LANGUAGE]),
        author=cast(str | None, values[MetadataField.AUTHOR]),
        genres=list(cast(tuple[str, ...], values[MetadataField.GENRES]))
        if values[MetadataField.GENRES] is not None
        else None,
        cover_asset_id=cover_value if isinstance(cover_value, UUID) else None,
        created_at=series.created_at,
        updated_at=series.updated_at,
    )


def _summaries(request: Request, items: tuple[Series, ...]) -> list[SeriesResponse]:
    runtime = cast(RuntimeState, request.app.state.runtime)
    resolved = (
        runtime.metadata_service.resolve_many(items) if runtime.metadata_service is not None else {}
    )
    return [
        _series_response(
            series, resolved.get(series.series_id, resolve_series_metadata(series, (), ()))
        )
        for series in items
    ]


@router.get(
    "/api/v1/library",
    response_model=LibraryResponseEnvelope,
    responses={
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="List local series",
)
async def list_library(
    request: Request,
    limit: int = Query(default=DEFAULT_LIBRARY_PAGE_SIZE, ge=1, le=MAX_LIBRARY_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    reading_status: LibraryReadingStatus | None = None,
) -> LibraryResponseEnvelope:
    page = _library_service(request).list_series(
        limit=limit,
        offset=offset,
        reading_status=reading_status,
    )
    return LibraryResponseEnvelope(
        data=await asyncio.to_thread(_summaries, request, page.items),
        meta=LibraryMeta(limit=page.limit, offset=page.offset, total=page.total),
    )


@router.get("/api/v1/library/local-imports")
async def local_import_status(request: Request) -> dict[str, object]:
    service = cast(RuntimeState, request.app.state.runtime).local_import_service
    if service is None:
        raise HTTPException(status_code=503, detail="Local import is not configured.")
    return await asyncio.to_thread(service.status)


@router.post("/api/v1/library/folders/{kind}/open")
async def open_library_folder(
    kind: Literal["library", "inbox", "downloads"],
    request: Request,
) -> Response:
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Open folders from Yomira on this computer.")
    service = cast(RuntimeState, request.app.state.runtime).local_import_service
    if service is None:
        raise HTTPException(status_code=503, detail="Local import is not configured.")
    try:
        await asyncio.to_thread(service.open_folder, kind)
    except OSError, NotImplementedError, StorageGateClosedError:
        payload = ApiErrorEnvelope(
            error=ApiErrorBody(
                code=ErrorCode.LIB_PATH_UNAVAILABLE,
                message=(
                    "No se pudo abrir la carpeta. Copia su ruta y ábrela desde el Explorador; "
                    "comprueba que siga disponible en este equipo."
                ),
                trace_id=resolve_request_trace_id(request),
                retryable=True,
            )
        )
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))
    return Response(status_code=204)


@router.post("/api/v1/library/local-imports/retry")
async def retry_local_imports(request: Request) -> dict[str, object]:
    service = cast(RuntimeState, request.app.state.runtime).local_import_service
    if service is None:
        raise HTTPException(status_code=503, detail="Local import is not configured.")
    service.retry_failed()
    return await asyncio.to_thread(service.status)


@router.get(
    "/api/v1/library/recent",
    response_model=RecentLibraryResponseEnvelope,
    responses={
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="List recently added local series",
)
async def list_recent_library(
    request: Request,
    limit: int = Query(default=DEFAULT_RECENT_LIMIT, ge=1, le=MAX_RECENT_LIMIT),
) -> RecentLibraryResponseEnvelope:
    items = _library_service(request).list_recent(limit=limit)
    return RecentLibraryResponseEnvelope(
        data=await asyncio.to_thread(_summaries, request, items),
        meta=RecentLibraryMeta(limit=limit),
    )


@router.get(
    "/api/v1/series/{series_id}",
    response_model=SeriesResponseEnvelope,
    responses={
        404: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Get one local series",
)
async def get_series(
    series_id: UUID4,
    request: Request,
) -> SeriesResponseEnvelope:
    series_id_value = UUID(str(series_id))
    series = _library_service(request).get_series(series_id_value)
    if series is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Series was not found.",
        )
    runtime = cast(RuntimeState, request.app.state.runtime)
    resolutions = (
        runtime.metadata_service.resolve_series(series_id_value)
        if runtime.metadata_service is not None
        else resolve_series_metadata(series, (), ())
    )
    if resolutions is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Series was not found.",
        )
    summary = _series_response(series, resolutions)
    reading_sources: list[ReadingSourceResponse] = []
    registry = runtime.connector_registry
    if registry is not None and runtime.metadata_service is not None:
        linked_metadata = {
            item.series_source_id: item
            for item in runtime.metadata_service.list_sources(series_id_value)
        }
        for link in _library_service(request).list_reading_sources(series_id_value):
            try:
                descriptor = registry.get_metadata(link.connector_id)
            except ConnectorRegistryError:
                continue
            if (
                descriptor.connector_version != link.connector_version
                or not {
                    ConnectorCapability.METADATA,
                    ConnectorCapability.CHAPTER_LIST,
                    ConnectorCapability.PAGE_LIST,
                }
                <= descriptor.capabilities
            ):
                continue
            source_metadata = linked_metadata.get(link.series_source_id)
            if source_metadata is None:
                continue
            display_name = link.display_name
            if link.connector_id == FMD2_METADATA.connector_id:
                display_name = fmd2_source_display_name(link.remote_series_id) or display_name
            reading_sources.append(
                ReadingSourceResponse(
                    series_source_id=link.series_source_id,
                    connector_id=link.connector_id,
                    remote_series_id=link.remote_series_id,
                    display_name=display_name,
                    author=source_metadata.remote_author,
                    description=source_metadata.remote_description,
                    genres=list(source_metadata.remote_genres)
                    if source_metadata.remote_genres
                    else None,
                    status=source_metadata.remote_status,
                    language=source_metadata.remote_language,
                )
            )
    return SeriesResponseEnvelope(
        data=SeriesDetailResponse(
            **summary.model_dump(),
            metadata=[_metadata_response(item) for item in resolutions],
            reading_sources=reading_sources,
        ),
        meta=SeriesMeta(),
    )


@router.get(
    "/api/v1/series/{series_id}/cover",
    responses={404: {"model": ApiErrorEnvelope}, 503: {"model": ApiErrorEnvelope}},
)
async def get_cover(
    series_id: UUID4, request: Request, variant: ThumbnailVariant = ThumbnailVariant.MEDIUM
) -> Response:
    runtime = cast(RuntimeState, request.app.state.runtime)
    if runtime.cover_asset_service is None:
        raise HTTPException(status_code=503, detail="Cover service is not configured.")
    try:
        cover = await runtime.cover_asset_service.thumbnail(UUID(str(series_id)), variant)
    except ValueError, ReaderAssetError, ThumbnailProcessingError:
        raise CoverAssetError(ErrorCode.IMG_THUMBNAIL_FAILED) from None
    if cover is None:
        raise HTTPException(status_code=404, detail="Series cover was not found.")
    headers = {
        "ETag": f'"{cover.sha256}"',
        "Cache-Control": "private, no-cache",
        "X-Content-Type-Options": "nosniff",
    }
    if request.headers.get("if-none-match") == headers["ETag"]:
        return Response(status_code=304, headers=headers)
    return Response(content=cover.content, media_type=cover.mime_type, headers=headers)


@router.post("/api/v1/series/{series_id}/metadata/refresh", response_model=SeriesResponseEnvelope)
async def refresh_metadata(series_id: UUID4, request: Request) -> SeriesResponseEnvelope:
    runtime = cast(RuntimeState, request.app.state.runtime)
    if _library_service(request).get_series(UUID(str(series_id))) is None:
        raise HTTPException(status_code=404, detail="Series was not found.")
    if runtime.cover_asset_service is None:
        raise HTTPException(status_code=503, detail="Metadata refresh is not configured.")
    try:
        await runtime.cover_asset_service.refresh(UUID(str(series_id)))
    except ValueError, ReaderAssetError, ThumbnailProcessingError:
        raise CoverAssetError(ErrorCode.IMG_THUMBNAIL_FAILED) from None
    return await get_series(series_id, request)


@router.get(
    "/api/v1/series/{series_id}/metadata/external-candidates",
    response_model=ExternalMetadataEnvelope,
)
async def external_metadata_candidates(
    series_id: UUID4, request: Request
) -> ExternalMetadataEnvelope:
    runtime = cast(RuntimeState, request.app.state.runtime)
    if _library_service(request).get_series(UUID(str(series_id))) is None:
        raise HTTPException(status_code=404, detail="Series was not found.")
    if runtime.cover_asset_service is None:
        raise HTTPException(status_code=503, detail="Metadata lookup is not configured.")
    lookup = await runtime.cover_asset_service.external_candidates(UUID(str(series_id)))
    return ExternalMetadataEnvelope(
        data=[
            ExternalMetadataCandidateResponse(
                remote_id=UUID(c.remote_id), title=c.title, year=c.year, author=c.metadata.author
            )
            for c in lookup.candidates[:10]
        ],
        meta=ExternalMetadataMeta(total=lookup.total),
    )


@router.post("/api/v1/series/{series_id}/metadata/external", response_model=SeriesResponseEnvelope)
async def apply_external_metadata(
    series_id: UUID4, selection: ExternalMetadataRequest, request: Request
) -> SeriesResponseEnvelope:
    runtime = cast(RuntimeState, request.app.state.runtime)
    if _library_service(request).get_series(UUID(str(series_id))) is None:
        raise HTTPException(status_code=404, detail="Series was not found.")
    if runtime.cover_asset_service is None:
        raise HTTPException(status_code=503, detail="Metadata import is not configured.")
    try:
        await runtime.cover_asset_service.apply_external(
            UUID(str(series_id)), str(selection.remote_id)
        )
    except MetadataAssociationConflict:
        raise HTTPException(
            status_code=409, detail="External metadata identity is already associated."
        ) from None
    except ValueError, ReaderAssetError, ThumbnailProcessingError:
        raise CoverAssetError(ErrorCode.IMG_THUMBNAIL_FAILED) from None
    return await get_series(series_id, request)


@router.put(
    "/api/v1/series/{series_id}/metadata-overrides",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Set one local metadata override",
)
async def set_metadata_override(
    series_id: UUID4,
    override: MetadataOverrideRequest,
    request: Request,
) -> Response:
    series_id_value = UUID(str(series_id))
    if override.field is MetadataField.COVER:
        value: MetadataValue = UUID(override.value) if isinstance(override.value, str) else None
    elif override.field is MetadataField.GENRES:
        value = tuple(override.value) if isinstance(override.value, list) else None
    else:
        value = cast(str | None, override.value)

    saved = _metadata_service(request).set_user_override(
        series_id_value,
        override.field,
        value,
    )
    if not saved:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Series was not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/api/v1/series/{series_id}/metadata-overrides/{field}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Remove one local metadata override",
)
async def clear_metadata_override(
    series_id: UUID4,
    field: MetadataField,
    request: Request,
) -> Response:
    series_id_value = UUID(str(series_id))
    library_service = _library_service(request)
    if library_service.get_series(series_id_value) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Series was not found.",
        )
    _metadata_service(request).clear_user_override(series_id_value, field)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
