from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.errors import ApiErrorBody, ApiErrorDetail, ApiErrorEnvelope, ErrorCode
from app.runtime import RuntimeState
from app.services.reader_settings import (
    ReaderMode,
    ReaderSettings,
    ReaderSettingsService,
    ReaderSettingsValueError,
)
from app.tracing import TRACE_ID_HEADER, resolve_request_trace_id

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


class ReaderSettingsRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    cache_limit_mb: int = Field(ge=16, le=500)
    preload_ahead: int = Field(ge=0, le=2)
    mode: Literal["single", "vertical", "double"]
    rtl: bool
    e_reader: bool


class ReaderSettingsData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_limit_mb: int
    preload_ahead: int
    mode: ReaderMode
    rtl: bool
    e_reader: bool


class ReaderSettingsEnvelope(BaseModel):
    data: ReaderSettingsData
    meta: dict[str, str]


def _reader_settings_service(request: Request) -> ReaderSettingsService:
    runtime = cast(RuntimeState, request.app.state.runtime)
    service = runtime.reader_settings_service
    if not isinstance(service, ReaderSettingsService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reader settings service is not configured.",
        )
    return service


def _settings_envelope(settings: ReaderSettings) -> ReaderSettingsEnvelope:
    return ReaderSettingsEnvelope(
        data=ReaderSettingsData(
            cache_limit_mb=settings.cache_limit_mb,
            preload_ahead=settings.preload_ahead,
            mode=settings.mode,
            rtl=settings.rtl,
            e_reader=settings.e_reader,
        ),
        meta={},
    )


def _invalid_persisted_settings_response(
    request: Request,
    error: ReaderSettingsValueError,
) -> JSONResponse:
    trace_id = resolve_request_trace_id(request)
    payload = ApiErrorEnvelope(
        error=ApiErrorBody(
            code=ErrorCode.SYS_INVALID_CONFIG,
            message="La configuración persistida del lector es inválida.",
            trace_id=trace_id,
            retryable=False,
            details=[ApiErrorDetail(field=error.setting_key, issue=error.issue)],
        )
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=payload.model_dump(mode="json", exclude_none=True),
        headers={TRACE_ID_HEADER: trace_id},
    )


@router.get("/reader", response_model=ReaderSettingsEnvelope)
async def get_reader_settings(
    request: Request,
) -> ReaderSettingsEnvelope | JSONResponse:
    service = _reader_settings_service(request)
    try:
        return _settings_envelope(service.get())
    except ReaderSettingsValueError as error:
        return _invalid_persisted_settings_response(request, error)


@router.put("/reader", response_model=ReaderSettingsEnvelope)
async def put_reader_settings(
    body: ReaderSettingsRequest,
    request: Request,
) -> ReaderSettingsEnvelope:
    service = _reader_settings_service(request)
    settings = ReaderSettings(
        cache_limit_mb=body.cache_limit_mb,
        preload_ahead=body.preload_ahead,
        mode=body.mode,
        rtl=body.rtl,
        e_reader=body.e_reader,
    )
    return _settings_envelope(service.put(settings))
