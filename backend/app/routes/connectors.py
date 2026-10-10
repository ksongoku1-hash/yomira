"""Loopback API endpoints for explicitly registered source connectors."""

import asyncio
import logging
from datetime import datetime
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.connectors.chapter import ConnectorChapter
from app.connectors.errors import SourceConnectorError
from app.connectors.metadata import ConnectorCapability
from app.connectors.registry import (
    ConnectorRegistryError,
    ConnectorRegistryErrorReason,
    RegisteredSourceConnector,
)
from app.connectors.search import (
    DEFAULT_SEARCH_PAGE_BUDGET,
    MAX_SEARCH_PAGE_BUDGET,
    ConnectorSearchQuery,
    ConnectorSearchResult,
)
from app.domain.download_job import DownloadJobStatus, DownloadPriority
from app.errors import ApiErrorEnvelope, ErrorCode
from app.runtime import RuntimeState
from app.services.jobs import (
    MAX_SELECTED_CHAPTERS,
    DownloadJobSummary,
    InvalidChapterSelection,
)
from app.tracing import create_trace_id, current_trace_id

logger = logging.getLogger("yomira.connectors")

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])
MAX_SEARCH_QUERY_LENGTH = 200
MAX_REMOTE_REFERENCE_LENGTH = 512


class ConnectorSearchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote_id: str | None
    remote_url: str | None
    remote_title: str | None
    cover_url: str | None = None
    source_label: str | None = None


class ConnectorSearchMeta(BaseModel):
    connector_id: str
    query: str


class ConnectorSearchEnvelope(BaseModel):
    data: list[ConnectorSearchItem]
    meta: ConnectorSearchMeta


class ConnectorDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str
    connector_version: str
    capabilities: list[ConnectorCapability]


class ConnectorListEnvelope(BaseModel):
    data: list[ConnectorDescriptor]


class ConnectorChapterItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote_id: str | None
    display_label: str
    remote_label: str | None
    chapter_raw_label: str
    chapter_sort_key: list[int]
    language: str | None


class ConnectorChapterMeta(BaseModel):
    connector_id: str
    remote_series_id: str


class CreateDownloadJobsRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    chapter_remote_ids: list[str] = Field(min_length=1, max_length=MAX_SELECTED_CHAPTERS)
    priority: DownloadPriority = Field(default=DownloadPriority.NORMAL, strict=False)

    @field_validator("chapter_remote_ids")
    @classmethod
    def require_unique_nonempty_ids(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > MAX_REMOTE_REFERENCE_LENGTH for item in value):
            raise ValueError("chapter remote IDs must be nonempty and at most 512 chars")
        if len(set(value)) != len(value):
            raise ValueError("chapter remote IDs must be unique")
        return value


class DownloadJobItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    chapter_source_id: UUID
    series_id: UUID
    chapter_id: UUID
    status: DownloadJobStatus
    priority: DownloadPriority
    series_title: str
    chapter_label: str
    created: bool
    created_at: datetime
    updated_at: datetime


class DownloadJobsMeta(BaseModel):
    connector_id: str
    remote_series_id: str


class DownloadJobsEnvelope(BaseModel):
    data: list[DownloadJobItem]
    meta: DownloadJobsMeta


class LibrarySeriesImportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: UUID
    series_title: str
    created: bool


class LibrarySeriesImportEnvelope(BaseModel):
    data: LibrarySeriesImportItem
    meta: DownloadJobsMeta


class ConnectorChapterEnvelope(BaseModel):
    data: list[ConnectorChapterItem]
    meta: ConnectorChapterMeta


class ConnectorCoverItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cover_url: str | None


class ConnectorCoverEnvelope(BaseModel):
    data: ConnectorCoverItem
    meta: ConnectorChapterMeta


def _connector_registry(request: Request):
    runtime = cast(RuntimeState, request.app.state.runtime)
    registry = runtime.connector_registry
    if registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Connector registry is not initialized.",
        )
    return registry


def _resolve_connector(
    request: Request,
    connector_id: str,
    capability: ConnectorCapability,
) -> RegisteredSourceConnector:
    registry = _connector_registry(request)
    try:
        return registry.resolve(connector_id, required_capability=capability)
    except ConnectorRegistryError as error:
        if error.reason is ConnectorRegistryErrorReason.UNKNOWN_CONNECTOR_ID:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Connector was not found.",
            ) from None
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Connector could not be initialized.",
        ) from None


@router.get(
    "",
    response_model=ConnectorListEnvelope,
    responses={503: {"model": ApiErrorEnvelope}},
    summary="List explicitly registered source connectors",
)
async def list_connectors(request: Request) -> ConnectorListEnvelope:
    registry = _connector_registry(request)
    return ConnectorListEnvelope(
        data=[
            ConnectorDescriptor(
                connector_id=metadata.connector_id,
                connector_version=metadata.connector_version,
                capabilities=sorted(metadata.capabilities, key=lambda item: item.value),
            )
            for metadata in registry.registered_metadata
        ]
    )


@router.get(
    "/{connector_id}/search",
    response_model=ConnectorSearchEnvelope,
    responses={
        404: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        502: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Search a registered source connector",
)
async def search_connector(
    connector_id: str,
    request: Request,
    query: str = Query(min_length=1, max_length=MAX_SEARCH_QUERY_LENGTH),
    page_budget: int = Query(
        default=DEFAULT_SEARCH_PAGE_BUDGET,
        ge=1,
        le=MAX_SEARCH_PAGE_BUDGET,
    ),
) -> ConnectorSearchEnvelope:
    connector = _resolve_connector(request, connector_id, ConnectorCapability.SEARCH)
    results = await connector.search(ConnectorSearchQuery(query=query, page_budget=page_budget))
    return ConnectorSearchEnvelope(
        data=[_search_item(result) for result in results],
        meta=ConnectorSearchMeta(connector_id=connector_id, query=query),
    )


@router.get(
    "/{connector_id}/series/{remote_series_id:path}/cover",
    response_model=ConnectorCoverEnvelope,
    responses={
        404: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        502: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Get a registered source connector's public cover URL",
)
async def get_connector_cover(
    connector_id: str,
    request: Request,
    remote_series_id: str = Path(min_length=1, max_length=MAX_REMOTE_REFERENCE_LENGTH),
    title: str | None = Query(default=None, min_length=1, max_length=MAX_SEARCH_QUERY_LENGTH),
) -> ConnectorCoverEnvelope:
    connector = _resolve_connector(request, connector_id, ConnectorCapability.METADATA)
    runtime = cast(RuntimeState, request.app.state.runtime)
    cover_url = None
    try:
        metadata = connector.normalize_metadata(await connector.get_series(remote_series_id))
        cover_url = metadata.cover_url
        title = metadata.title or title
    except SourceConnectorError:
        if title is None or runtime.cover_asset_service is None:
            raise
    origins = _connector_registry(request).get_network_origins(connector_id)
    if cover_url is not None:
        try:
            parsed = urlsplit(cover_url)
            origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        except ValueError:
            origin = ""
            parsed = None
        if (
            parsed is None
            or parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path.startswith("/")
            or origin not in origins
        ):
            cover_url = None
    if cover_url is None and title and runtime.cover_asset_service is not None:
        cover_url = await runtime.cover_asset_service.catalog_cover(title)
    return ConnectorCoverEnvelope(
        data=ConnectorCoverItem(cover_url=cover_url),
        meta=ConnectorChapterMeta(connector_id=connector_id, remote_series_id=remote_series_id),
    )


@router.get(
    "/{connector_id}/series/{remote_series_id:path}/chapters",
    response_model=ConnectorChapterEnvelope,
    responses={
        404: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        502: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="List chapters from a registered source connector",
)
async def list_connector_chapters(
    connector_id: str,
    request: Request,
    remote_series_id: str = Path(min_length=1, max_length=MAX_REMOTE_REFERENCE_LENGTH),
) -> ConnectorChapterEnvelope:
    connector = _resolve_connector(
        request,
        connector_id,
        ConnectorCapability.CHAPTER_LIST,
    )
    chapters = await connector.get_chapters(remote_series_id)
    return ConnectorChapterEnvelope(
        data=[_chapter_item(chapter) for chapter in chapters],
        meta=ConnectorChapterMeta(
            connector_id=connector_id,
            remote_series_id=remote_series_id,
        ),
    )


@router.get("/{connector_id}/series/{remote_series_id:path}/availability")
def get_chapter_availability(
    connector_id: str,
    request: Request,
    remote_series_id: str = Path(min_length=1, max_length=MAX_REMOTE_REFERENCE_LENGTH),
) -> dict[str, object]:
    _resolve_connector(request, connector_id, ConnectorCapability.CHAPTER_LIST)
    runtime = cast(RuntimeState, request.app.state.runtime)
    service = runtime.download_job_service
    if service is None:
        raise HTTPException(status_code=503, detail="Download service is not initialized.")
    return {"data": service.chapter_availability(connector_id, remote_series_id)}


@router.post(
    "/{connector_id}/series/{remote_series_id:path}/library-series",
    response_model=LibrarySeriesImportEnvelope,
    responses={
        404: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        501: {"model": ApiErrorEnvelope},
        502: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Import source metadata into the local library without creating downloads",
)
async def import_connector_library_series(
    connector_id: str,
    request: Request,
    remote_series_id: str = Path(min_length=1, max_length=MAX_REMOTE_REFERENCE_LENGTH),
) -> LibrarySeriesImportEnvelope:
    connector = _resolve_connector(request, connector_id, ConnectorCapability.METADATA)
    runtime = cast(RuntimeState, request.app.state.runtime)
    service = runtime.download_job_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Library import service is not initialized.",
        )
    raw_metadata = await connector.get_series(remote_series_id)
    normalized_metadata = connector.normalize_metadata(raw_metadata)
    summary = await asyncio.to_thread(
        service.import_library_series,
        connector=connector.metadata,
        remote_series_id=remote_series_id,
        series_metadata=normalized_metadata,
    )
    await _ensure_imported_cover(runtime, summary.series_id)
    return LibrarySeriesImportEnvelope(
        data=LibrarySeriesImportItem(
            series_id=summary.series_id,
            series_title=summary.series_title,
            created=summary.created,
        ),
        meta=DownloadJobsMeta(connector_id=connector_id, remote_series_id=remote_series_id),
    )


@router.post(
    "/{connector_id}/series/{remote_series_id:path}/download-jobs",
    response_model=DownloadJobsEnvelope,
    responses={
        404: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        501: {"model": ApiErrorEnvelope},
        502: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Create durable jobs from selected connector chapters",
)
async def create_selected_download_jobs(
    connector_id: str,
    request: Request,
    selection: CreateDownloadJobsRequest,
    remote_series_id: str = Path(
        min_length=1,
        max_length=MAX_REMOTE_REFERENCE_LENGTH,
    ),
) -> DownloadJobsEnvelope:
    _resolve_connector(request, connector_id, ConnectorCapability.PAGE_LIST)
    metadata_connector = _resolve_connector(
        request,
        connector_id,
        ConnectorCapability.METADATA,
    )
    raw_metadata = await metadata_connector.get_series(remote_series_id)
    normalized_metadata = metadata_connector.normalize_metadata(raw_metadata)

    chapter_connector = _resolve_connector(
        request,
        connector_id,
        ConnectorCapability.CHAPTER_LIST,
    )
    chapters = await chapter_connector.get_chapters(remote_series_id)
    runtime = cast(RuntimeState, request.app.state.runtime)
    service = runtime.download_job_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Download job service is not initialized.",
        )
    try:
        jobs = await asyncio.to_thread(
            service.create_selected_jobs,
            connector=metadata_connector.metadata,
            remote_series_id=remote_series_id,
            series_metadata=normalized_metadata,
            chapters=chapters,
            selected_remote_ids=selection.chapter_remote_ids,
            priority=selection.priority,
        )
    except InvalidChapterSelection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or more selected chapters are no longer available.",
        ) from None
    if runtime.download_dispatcher is not None:
        runtime.download_dispatcher.notify_new_jobs()
    for series_id in {summary.series_id for summary in jobs}:
        await _ensure_imported_cover(runtime, series_id)
    return DownloadJobsEnvelope(
        data=[_download_job_item(item) for item in jobs],
        meta=DownloadJobsMeta(
            connector_id=connector_id,
            remote_series_id=remote_series_id,
        ),
    )


async def _ensure_imported_cover(runtime: RuntimeState, series_id: UUID) -> None:
    if runtime.cover_asset_service is None:
        return
    try:
        await runtime.cover_asset_service.ensure_cover(series_id)
    except Exception as error:
        # The library import is durable. A failed cover must not misreport its creation.
        logger.warning(
            "series.cover_import_failed",
            extra={
                "error_code": ErrorCode.IMG_THUMBNAIL_FAILED.value,
                "trace_id": current_trace_id() or create_trace_id(),
                "stage": "automatic_cover_import",
                "cause_type": type(error).__name__,
                "series_id": str(series_id),
                "retryable": True,
            },
        )


def _search_item(result: ConnectorSearchResult) -> ConnectorSearchItem:
    return ConnectorSearchItem(
        remote_id=result.remote_id,
        remote_url=result.remote_url,
        remote_title=result.remote_title,
        cover_url=result.cover_url,
        source_label=result.source_label,
    )


def _chapter_item(chapter: ConnectorChapter) -> ConnectorChapterItem:
    return ConnectorChapterItem(
        remote_id=chapter.remote_id,
        display_label=chapter.display_label,
        remote_label=chapter.remote_label,
        chapter_raw_label=chapter.chapter_raw_label,
        chapter_sort_key=list(chapter.chapter_sort_key),
        language=chapter.language,
    )


def _download_job_item(summary: DownloadJobSummary) -> DownloadJobItem:
    return DownloadJobItem(
        job_id=summary.job.job_id,
        chapter_source_id=summary.job.chapter_source_id,
        series_id=summary.series_id,
        chapter_id=summary.chapter_id,
        status=summary.job.status,
        priority=summary.job.priority,
        series_title=summary.series_title,
        chapter_label=summary.chapter_label,
        created=summary.created,
        created_at=summary.job.created_at,
        updated_at=summary.job.updated_at,
    )
