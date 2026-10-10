"""Bounded API for the durable DownloadJob queue, history, and explicit actions."""

import asyncio
import logging
from datetime import datetime
from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import UUID4, BaseModel, ConfigDict

from app.domain.download_job import DownloadJobStatus, DownloadPriority
from app.domain.job_cancellation import JobCancellationNotAllowed
from app.domain.job_pause import UnknownActiveJob
from app.errors import ApiErrorEnvelope
from app.runtime import RuntimeState
from app.services.jobs import (
    DEFAULT_DOWNLOAD_JOB_PAGE_SIZE,
    MAX_DOWNLOAD_JOB_PAGE_SIZE,
    DownloadJobService,
    DownloadJobSummary,
    JobArchiveNotAllowed,
    JobRetryNotAllowed,
)
from app.tracing import current_trace_id

logger = logging.getLogger("yomira.jobs")
router = APIRouter(prefix="/api/v1/download-jobs", tags=["download-jobs"])
cancel_router = APIRouter(prefix="/api/v1/downloads", tags=["downloads"])


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
    created_at: datetime
    updated_at: datetime
    cancel_requested_at: datetime | None


class DownloadJobListMeta(BaseModel):
    limit: int
    offset: int
    total: int
    states: dict[DownloadJobStatus, int]
    page_progress: list[dict[str, object]]
    waiting_sources: int = 0
    retry_after_seconds: int = 0
    recovering_job_id: UUID | None = None


class DownloadJobListEnvelope(BaseModel):
    data: list[DownloadJobItem]
    meta: DownloadJobListMeta


class DownloadJobEnvelope(BaseModel):
    data: DownloadJobItem


def _job_service(request: Request) -> DownloadJobService:
    runtime = cast(RuntimeState, request.app.state.runtime)
    service = runtime.download_job_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Download job service is not initialized.",
        )
    return service


@router.get(
    "",
    response_model=DownloadJobListEnvelope,
    responses={
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="List durable download jobs",
)
def list_download_jobs(
    request: Request,
    limit: int = Query(
        default=DEFAULT_DOWNLOAD_JOB_PAGE_SIZE,
        ge=1,
        le=MAX_DOWNLOAD_JOB_PAGE_SIZE,
    ),
    offset: int = Query(default=0, ge=0, le=10_000_000),
    archived: bool = Query(default=False),
) -> DownloadJobListEnvelope:
    page = _job_service(request).list_jobs(limit=limit, offset=offset, archived=archived)
    runtime = cast(RuntimeState, request.app.state.runtime)
    dispatcher = runtime.download_dispatcher
    waiting, retry_after = dispatcher.source_wait if dispatcher is not None else (0, 0)
    return DownloadJobListEnvelope(
        data=[_job_item(item) for item in page.items],
        meta=DownloadJobListMeta(
            limit=page.limit,
            offset=page.offset,
            total=page.total,
            states=page.states,
            page_progress=list(page.page_progress),
            waiting_sources=waiting,
            retry_after_seconds=retry_after,
            recovering_job_id=dispatcher.recovering_job_id if dispatcher is not None else None,
        ),
    )


@router.post(
    "/{job_id}/archive",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"model": ApiErrorEnvelope},
        409: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Remove a terminal job from the visible queue while preserving its history",
)
async def archive_download_job(job_id: UUID4, request: Request) -> Response:
    service = _job_service(request)
    try:
        archived = await asyncio.to_thread(service.archive_job, UUID(str(job_id)))
    except JobArchiveNotAllowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Only a finalized job without pending cancellation can be removed from the queue."
            ),
        ) from None
    if archived is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The download job was not found.",
        )
    logger.info(
        "download_job.archived",
        extra={"job_id": str(job_id), "trace_id": current_trace_id(), "stage": "queue_archive"},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{job_id}/restore",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Restore an archived terminal job to the visible queue",
)
async def restore_download_job(job_id: UUID4, request: Request) -> Response:
    service = _job_service(request)
    restored = await asyncio.to_thread(service.restore_job, UUID(str(job_id)))
    if restored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The download job was not found.",
        )
    logger.info(
        "download_job.restored",
        extra={"job_id": str(job_id), "trace_id": current_trace_id(), "stage": "queue_restore"},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@cancel_router.post(
    "/{job_id}/retry",
    response_model=DownloadJobEnvelope,
    responses={
        404: {"model": ApiErrorEnvelope},
        409: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Retry a failed download with a new durable job identity",
)
async def retry_download_job(job_id: UUID4, request: Request) -> DownloadJobEnvelope:
    service = _job_service(request)
    try:
        summary = await asyncio.to_thread(service.retry_job, UUID(str(job_id)))
    except JobRetryNotAllowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The download job cannot be retried in its current state.",
        ) from None
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The download job was not found.",
        )
    runtime = cast(RuntimeState, request.app.state.runtime)
    if runtime.download_dispatcher is not None and summary.job.status is DownloadJobStatus.QUEUED:
        runtime.download_dispatcher.notify_new_jobs()
    logger.info(
        "download_job.retry_requested",
        extra={
            "job_id": str(job_id),
            "trace_id": current_trace_id(),
            "stage": "manual_retry",
        },
    )
    logger.info(
        "download_job.retry_created" if summary.created else "download_job.retry_reused",
        extra={
            "job_id": str(summary.job.job_id),
            "trace_id": current_trace_id(),
            "stage": "manual_retry",
        },
    )
    return DownloadJobEnvelope(data=_job_item(summary))


@cancel_router.post(
    "/{job_id}/cancel",
    response_model=DownloadJobEnvelope,
    responses={
        404: {"model": ApiErrorEnvelope},
        409: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Request durable cancellation of a download job",
)
async def cancel_download_job(job_id: UUID4, request: Request) -> DownloadJobEnvelope:
    runtime = cast(RuntimeState, request.app.state.runtime)
    service = runtime.download_job_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Download job service is not initialized.",
        )
    try:
        summary = await asyncio.to_thread(service.cancel_job, UUID(str(job_id)))
    except JobCancellationNotAllowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The download job is no longer cancellable.",
        ) from None
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The download job was not found.",
        )

    # SQLite is authoritative. Notify only after the request is durable; the
    # worker also checks the durable field at safe checkpoints.
    controller = runtime.job_pause_controller
    if summary.job.cancel_requested_at is not None and controller is not None:
        try:
            controller.notify_cancellation_requested(summary.job.job_id)
        except UnknownActiveJob:
            pass
    return DownloadJobEnvelope(data=_job_item(summary))


def _job_item(summary: DownloadJobSummary) -> DownloadJobItem:
    return DownloadJobItem(
        job_id=summary.job.job_id,
        chapter_source_id=summary.job.chapter_source_id,
        series_id=summary.series_id,
        chapter_id=summary.chapter_id,
        status=summary.job.status,
        priority=summary.job.priority,
        series_title=summary.series_title,
        chapter_label=summary.chapter_label,
        created_at=summary.job.created_at,
        updated_at=summary.job.updated_at,
        cancel_requested_at=summary.job.cancel_requested_at,
    )
