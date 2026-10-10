from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC
from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import UUID4, BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from app.domain.reader_progress import ReaderProgress
from app.errors import ApiErrorEnvelope
from app.runtime import RuntimeState
from app.services.reader_asset import (
    MAX_READER_PAGE_SIZE,
    ReaderAssetConflict,
    ReaderAssetError,
    ReaderAssetNotFound,
    ReaderAssetService,
    ReaderPageStream,
)
from app.services.reader_progress import (
    ReaderProgressChapterUnavailable,
    ReaderProgressPageOutOfRange,
    ReaderProgressService,
)
from app.services.recent_views import DEFAULT_RECENT_LIMIT, MAX_RECENT_LIMIT

router = APIRouter(tags=["reader"])


class ReaderChapterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    chapter_id: UUID
    series_id: UUID
    display_label: str
    page_count: int


class ReaderChapterListMeta(BaseModel):
    limit: int
    offset: int
    total: int


class ReaderChapterListEnvelope(BaseModel):
    data: list[ReaderChapterResponse]
    meta: ReaderChapterListMeta


class ReaderChapterEnvelope(BaseModel):
    data: ReaderChapterResponse
    meta: dict[str, str]


class ReaderProgressRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    page_index: int = Field(ge=0)


class ReaderProgressResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: UUID
    chapter_id: UUID
    page_index: int
    percentage: str
    updated_at: str


class ReaderProgressEnvelope(BaseModel):
    data: ReaderProgressResponse | None
    meta: dict[str, str]


class ReaderProgressRecentItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: UUID
    display_title: str
    chapter_id: UUID
    display_label: str
    page_index: int
    percentage: str
    updated_at: str


class ReaderProgressRecentMeta(BaseModel):
    limit: int


class ReaderProgressRecentEnvelope(BaseModel):
    data: list[ReaderProgressRecentItem]
    meta: ReaderProgressRecentMeta


def _reader_service(request: Request) -> ReaderAssetService:
    runtime = cast(RuntimeState, request.app.state.runtime)
    service = getattr(runtime, "reader_asset_service", None)
    if not isinstance(service, ReaderAssetService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reader service is not configured.",
        )
    return service


def _reader_progress_service(request: Request) -> ReaderProgressService:
    runtime = cast(RuntimeState, request.app.state.runtime)
    service = getattr(runtime, "reader_progress_service", None)
    if not isinstance(service, ReaderProgressService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reader progress service is not configured.",
        )
    return service


def _reader_progress_response(progress: ReaderProgress) -> ReaderProgressResponse:
    updated_at = progress.updated_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    basis_points = progress.percentage_basis_points
    return ReaderProgressResponse(
        series_id=progress.series_id,
        chapter_id=progress.chapter_id,
        page_index=progress.page_index,
        percentage=f"{basis_points // 100}.{basis_points % 100:02d}",
        updated_at=updated_at,
    )


def _translate_reader_error(error: ReaderAssetError) -> HTTPException:
    if isinstance(error, ReaderAssetNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested reader content is unavailable.",
        )
    if isinstance(error, ReaderAssetConflict):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The indexed reader content changed during the request.",
        )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="The indexed reader content failed integrity verification.",
    )


@router.get(
    "/api/v1/reader/series/{series_id}/chapters",
    response_model=ReaderChapterListEnvelope,
    responses={
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="List chapters with a verified CBZ ready for reading",
)
def list_reader_chapters(
    series_id: UUID4,
    request: Request,
    limit: int = Query(default=100, ge=1, le=MAX_READER_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> ReaderChapterListEnvelope:
    service = _reader_service(request)
    try:
        page = service.list_chapters_for_series(
            UUID(str(series_id)),
            limit=limit,
            offset=offset,
        )
    except ReaderAssetError as error:
        raise _translate_reader_error(error) from None
    return ReaderChapterListEnvelope(
        data=[ReaderChapterResponse.model_validate(item) for item in page.items],
        meta=ReaderChapterListMeta(
            limit=page.limit,
            offset=page.offset,
            total=page.total,
        ),
    )


@router.get(
    "/api/v1/reader/progress/recent",
    response_model=ReaderProgressRecentEnvelope,
    responses={
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="List recently saved reader progress",
)
async def list_recent_reader_progress(
    request: Request,
    limit: int = Query(default=DEFAULT_RECENT_LIMIT, ge=1, le=MAX_RECENT_LIMIT),
) -> ReaderProgressRecentEnvelope:
    entries = _reader_progress_service(request).list_recent(limit=limit)
    return ReaderProgressRecentEnvelope(
        data=[
            ReaderProgressRecentItem(
                series_id=entry.progress.series_id,
                display_title=entry.display_title,
                chapter_id=entry.progress.chapter_id,
                display_label=entry.display_label,
                page_index=entry.progress.page_index,
                percentage=(
                    f"{entry.progress.percentage_basis_points // 100}."
                    f"{entry.progress.percentage_basis_points % 100:02d}"
                ),
                updated_at=(
                    entry.progress.updated_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
                ),
            )
            for entry in entries
        ],
        meta=ReaderProgressRecentMeta(limit=limit),
    )


@router.get(
    "/api/v1/reader/{chapter_id}",
    response_model=ReaderChapterEnvelope,
    responses={
        404: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Get reader metadata for a verified chapter",
)
async def get_reader_chapter(
    chapter_id: UUID4,
    request: Request,
) -> ReaderChapterEnvelope:
    service = _reader_service(request)
    chapter = service.get_chapter(UUID(str(chapter_id)))
    if chapter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested reader chapter is unavailable.",
        )
    return ReaderChapterEnvelope(
        data=ReaderChapterResponse.model_validate(chapter),
        meta={},
    )


@router.get(
    "/api/v1/reader/{chapter_id}/progress",
    response_model=ReaderProgressEnvelope,
    responses={
        404: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Get the saved reader position for a verified chapter",
)
async def get_reader_progress(
    chapter_id: UUID4,
    request: Request,
) -> ReaderProgressEnvelope:
    service = _reader_progress_service(request)
    try:
        progress = service.get(UUID(str(chapter_id)))
    except ReaderProgressChapterUnavailable:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested reader chapter is unavailable.",
        ) from None
    return ReaderProgressEnvelope(
        data=_reader_progress_response(progress) if progress is not None else None,
        meta={},
    )


@router.put(
    "/api/v1/reader/{chapter_id}/progress",
    response_model=ReaderProgressEnvelope,
    responses={
        404: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Save a zero-based reader position for a verified chapter",
)
async def put_reader_progress(
    chapter_id: UUID4,
    body: ReaderProgressRequest,
    request: Request,
) -> ReaderProgressEnvelope:
    service = _reader_progress_service(request)
    try:
        progress = service.save(UUID(str(chapter_id)), body.page_index)
    except ReaderProgressChapterUnavailable:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested reader chapter is unavailable.",
        ) from None
    except ReaderProgressPageOutOfRange:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="page_index is outside the available chapter pages.",
        ) from None
    return ReaderProgressEnvelope(
        data=_reader_progress_response(progress),
        meta={},
    )


@router.get(
    "/api/v1/reader/{chapter_id}/pages/{page}",
    responses={
        404: {"model": ApiErrorEnvelope},
        409: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Stream one verified page from a published CBZ",
)
async def get_reader_page(
    chapter_id: UUID4,
    page: int,
    request: Request,
) -> StreamingResponse:
    service = _reader_service(request)
    task = asyncio.create_task(asyncio.to_thread(service.open_page, UUID(str(chapter_id)), page))
    try:
        stream = await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_close_unclaimed_page)
        raise
    except ReaderAssetError as error:
        raise _translate_reader_error(error) from None
    return StreamingResponse(
        stream.iter_chunks(),
        media_type=stream.media_type,
        headers={
            "Cache-Control": "private, no-transform",
            "Content-Length": str(stream.size_bytes),
            "ETag": stream.etag,
        },
        background=BackgroundTask(stream.close),
    )


def _close_unclaimed_page(task: asyncio.Task[ReaderPageStream]) -> None:
    if not task.cancelled():
        with suppress(Exception):
            task.result().close()
