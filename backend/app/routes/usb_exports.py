"""Durable USB export operations and bounded export history."""

import base64
import binascii
import json
import re
from datetime import datetime
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from pydantic import UUID4, BaseModel, ConfigDict, Field

from app.domain.export_job import ExportJob, ExportJobStatus
from app.errors import ApiErrorEnvelope
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.repositories.export_job import (
    ExportHistoryCapacityError,
    ExportIdempotencyConflict,
)
from app.runtime import RuntimeState
from app.services.export_jobs import (
    DEFAULT_EXPORT_HISTORY_PAGE_SIZE,
    MAX_EXPORT_HISTORY_PAGE_SIZE,
    ExportAssetUnavailable,
    UsbExportJobService,
)

router = APIRouter(prefix="/api/v1/usb-exports", tags=["usb-exports"])
_CURSOR_VERSION = 1
_MAX_CURSOR_LENGTH = 256
_CURSOR_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")


class UsbExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: UUID4
    target_id: str = Field(min_length=1, max_length=128)


class UsbChapterExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str = Field(min_length=1, max_length=128)


class UsbExportTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str
    display_name: str
    file_system: str
    total_bytes: int
    free_bytes: int


class UsbExportTargetsEnvelope(BaseModel):
    data: list[UsbExportTarget]


class UsbExportProgress(BaseModel):
    stage: str = Field(min_length=1, max_length=64)
    processed_bytes: int = Field(ge=0, le=8 * 1024**3)
    total_bytes: int = Field(ge=1, le=8 * 1024**3)


class UsbExportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    export_id: UUID
    asset_id: UUID
    target_id: str
    target_label: str
    file_system: str
    destination_name: str
    expected_size_bytes: int
    expected_sha256: str
    status: ExportJobStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    terminal_at: datetime | None
    trace_id: str
    error_code: str | None
    failure_stage: str | None
    retryable: bool | None
    cause_type: str | None
    progress: UsbExportProgress | None = None


class UsbExportListMeta(BaseModel):
    limit: int
    offset: int
    total: int


class UsbExportListEnvelope(BaseModel):
    data: list[UsbExportItem]
    meta: UsbExportListMeta


class UsbExportCursorMeta(BaseModel):
    limit: int
    total: int
    next_cursor: str | None


class UsbExportCursorListEnvelope(BaseModel):
    data: list[UsbExportItem]
    meta: UsbExportCursorMeta


class UsbExportEnvelope(BaseModel):
    data: UsbExportItem


def _export_service(request: Request) -> UsbExportJobService:
    runtime = cast(RuntimeState, request.app.state.runtime)
    service = runtime.usb_export_job_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="USB export service is not initialized.",
        )
    return service


@router.get(
    "/targets",
    response_model=UsbExportTargetsEnvelope,
    responses={503: {"model": ApiErrorEnvelope}},
    summary="List currently mounted removable export targets",
)
def list_usb_export_targets(request: Request) -> UsbExportTargetsEnvelope:
    service = _export_service(request)
    try:
        targets = service.list_targets()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Removable export targets could not be inspected.",
        ) from None
    return UsbExportTargetsEnvelope(
        data=[
            UsbExportTarget(
                target_id=target.target_id,
                display_name=_safe_target_label(
                    target.display_name,
                    target.mount_path.drive if target.mount_path is not None else "",
                ),
                file_system=target.file_system,
                total_bytes=target.total_bytes,
                free_bytes=target.free_bytes,
            )
            for target in targets
        ]
    )


@router.get(
    "",
    response_model=UsbExportListEnvelope,
    responses={
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="List durable USB export jobs",
)
def list_usb_exports(
    request: Request,
    limit: int = Query(
        default=DEFAULT_EXPORT_HISTORY_PAGE_SIZE,
        ge=1,
        le=MAX_EXPORT_HISTORY_PAGE_SIZE,
    ),
    offset: int = Query(default=0, ge=0, le=10_000_000),
) -> UsbExportListEnvelope:
    service = _export_service(request)
    page = service.list_jobs(limit=limit, offset=offset)
    return UsbExportListEnvelope(
        data=[_export_item(item, service) for item in page.items],
        meta=UsbExportListMeta(limit=page.limit, offset=page.offset, total=page.total),
    )


@router.get(
    "/pages",
    response_model=UsbExportCursorListEnvelope,
    responses={422: {"model": ApiErrorEnvelope}, 503: {"model": ApiErrorEnvelope}},
    summary="List durable USB export jobs with keyset pagination",
)
def list_usb_exports_cursor(
    request: Request,
    limit: int = Query(
        default=DEFAULT_EXPORT_HISTORY_PAGE_SIZE,
        ge=1,
        le=MAX_EXPORT_HISTORY_PAGE_SIZE,
    ),
    cursor: str | None = Query(default=None, max_length=_MAX_CURSOR_LENGTH),
) -> UsbExportCursorListEnvelope:
    try:
        before = _decode_cursor(cursor) if cursor is not None else None
        service = _export_service(request)
        page = service.list_jobs_after(limit=limit, before=before)
    except ValueError, binascii.Error, json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The USB export history cursor is invalid.",
        ) from None
    next_cursor = (
        _encode_cursor(datetime_to_epoch_us(page.items[-1].created_at), page.items[-1].export_id)
        if page.has_more and page.items
        else None
    )
    return UsbExportCursorListEnvelope(
        data=[_export_item(item, service) for item in page.items],
        meta=UsbExportCursorMeta(
            limit=page.limit,
            total=page.total,
            next_cursor=next_cursor,
        ),
    )


@router.post(
    "",
    response_model=UsbExportEnvelope,
    responses={
        404: {"model": ApiErrorEnvelope},
        409: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Create or retrieve an idempotent USB export job",
)
def create_usb_export(
    request: UsbExportRequest,
    api_request: Request,
    idempotency_key: str = Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
    ),
) -> UsbExportEnvelope:
    service = _export_service(api_request)
    try:
        job = service.export_asset(
            UUID(str(request.asset_id)),
            target_id=request.target_id,
            idempotency_key=idempotency_key,
        )
    except ExportAssetUnavailable:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested verified CBZ or CBR asset was not found.",
        ) from None
    except ExportIdempotencyConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key was already used for a different request.",
        ) from None
    except ExportHistoryCapacityError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Export history cannot reserve a safe slot for another job.",
        ) from None
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The USB export request is invalid.",
        ) from None
    return UsbExportEnvelope(data=_export_item(job))


@router.post(
    "/chapters/{chapter_id}",
    response_model=UsbExportEnvelope,
    responses={
        404: {"model": ApiErrorEnvelope},
        409: {"model": ApiErrorEnvelope},
        422: {"model": ApiErrorEnvelope},
        503: {"model": ApiErrorEnvelope},
    },
    summary="Export a ready chapter CBZ to the selected removable target",
)
def create_chapter_usb_export(
    chapter_id: UUID4,
    body: UsbChapterExportRequest,
    api_request: Request,
    idempotency_key: str = Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
    ),
) -> UsbExportEnvelope:
    service = _export_service(api_request)
    try:
        job = service.export_chapter(
            UUID(str(chapter_id)),
            target_id=body.target_id,
            idempotency_key=idempotency_key,
        )
    except ExportAssetUnavailable:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested ready chapter CBZ was not found.",
        ) from None
    except ExportIdempotencyConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key was already used for a different request.",
        ) from None
    except ExportHistoryCapacityError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Export history cannot reserve a safe slot for another job.",
        ) from None
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The USB chapter export request is invalid.",
        ) from None
    return UsbExportEnvelope(data=_export_item(job))


def _safe_target_label(label: str, drive: str) -> str:
    cleaned = "".join(
        character for character in label if ord(character) >= 32 and ord(character) != 127
    ).strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned:
        return drive[:2] if drive else "Unidad removible"
    return cleaned[:128]


def _encode_cursor(created_at_us: int, export_id: UUID) -> str:
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "created_at_us": created_at_us,
            "export_id": str(export_id),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[int, UUID]:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_CURSOR_LENGTH
        or _CURSOR_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("cursor must be bounded base64url text")
    padded = value + "=" * (-len(value) % 4)
    decoded: object = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    if type(decoded) is not dict:
        raise ValueError("cursor payload must be an object")
    payload = cast(dict[object, object], decoded)
    version = payload.get("v")
    created_at_us = payload.get("created_at_us")
    export_id_value = payload.get("export_id")
    if (
        set(payload) != {"v", "created_at_us", "export_id"}
        or type(version) is not int
        or version != _CURSOR_VERSION
        or type(created_at_us) is not int
        or not 0 < created_at_us <= 9_223_372_036_854_775_807
        or type(export_id_value) is not str
    ):
        raise ValueError("cursor payload is invalid")
    export_id = UUID(export_id_value)
    if export_id.version != 4 or str(export_id) != export_id_value:
        raise ValueError("cursor export_id must be a canonical UUIDv4")
    if _encode_cursor(created_at_us, export_id) != value:
        raise ValueError("cursor encoding is not canonical")
    return created_at_us, export_id


def _export_item(job: ExportJob, service: UsbExportJobService | None = None) -> UsbExportItem:
    progress = (
        service.progress_for_trace(job.trace_id)
        if service is not None and job.status == ExportJobStatus.EXPORTING
        else None
    )
    return UsbExportItem(
        export_id=job.export_id,
        asset_id=job.asset_id,
        target_id=job.target_id,
        target_label=job.target_label,
        file_system=job.file_system,
        destination_name=job.destination_name,
        expected_size_bytes=job.expected_size_bytes,
        expected_sha256=job.expected_sha256,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        terminal_at=job.terminal_at,
        trace_id=job.trace_id,
        error_code=job.error_code,
        failure_stage=job.failure_stage,
        retryable=job.retryable,
        cause_type=job.cause_type,
        progress=UsbExportProgress.model_validate(progress) if progress is not None else None,
    )
