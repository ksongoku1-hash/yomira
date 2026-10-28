from typing import cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.runtime import RuntimeState
from app.services.reader_cache_settings import (
    ReaderCacheSettings,
    ReaderCacheSettingsError,
    ReaderCacheSettingsService,
)

router = APIRouter(prefix="/api/v1/settings/reader-cache", tags=["settings"])


class ReaderCacheRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    limit_mb: int = Field(ge=128, le=8192)


class ReaderCacheData(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")
    limit_mb: int
    used_bytes: int
    entries: int
    file_bytes: int


class ReaderCacheEnvelope(BaseModel):
    data: ReaderCacheData
    meta: dict[str, str]


def _service(request: Request) -> ReaderCacheSettingsService:
    runtime = cast(RuntimeState, request.app.state.runtime)
    service = getattr(runtime, "reader_cache_settings_service", None)
    if not isinstance(service, ReaderCacheSettingsService):
        raise HTTPException(status_code=503, detail="Reader cache settings are unavailable.")
    return service


def _envelope(value: ReaderCacheSettings) -> ReaderCacheEnvelope:
    return ReaderCacheEnvelope(data=ReaderCacheData.model_validate(value), meta={})


@router.get("", response_model=ReaderCacheEnvelope)
def get_reader_cache(request: Request) -> ReaderCacheEnvelope:
    try:
        return _envelope(_service(request).get())
    except ReaderCacheSettingsError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None


@router.put("", response_model=ReaderCacheEnvelope)
def put_reader_cache(body: ReaderCacheRequest, request: Request) -> ReaderCacheEnvelope:
    try:
        return _envelope(_service(request).put(body.limit_mb))
    except ReaderCacheSettingsError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
