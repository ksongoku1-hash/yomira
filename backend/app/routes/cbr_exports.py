"""Build a verified aggregate CBR from complete local chapter CBZ assets."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import UUID4, BaseModel, ConfigDict, Field

from app.runtime import RuntimeState
from app.services.cbr_builder import CbrExportResult, CbrExportService
from app.services.rar5_store_writer import MAX_CBR_PAGE_COUNT

router = APIRouter(prefix="/api/v1/cbr-exports", tags=["cbr-exports"])


class CbrExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: UUID4
    chapter_ids: list[UUID4] = Field(min_length=1, max_length=MAX_CBR_PAGE_COUNT)


class CbrExportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: UUID
    filename: str
    series_title: str
    chapter_labels: list[str]
    page_count: int
    size_bytes: int
    sha256: str


class CbrExportEnvelope(BaseModel):
    data: CbrExportResponse


def _service(request: Request) -> CbrExportService:
    runtime = cast(RuntimeState, request.app.state.runtime)
    service = runtime.cbr_export_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CBR export service is not initialized.",
        )
    return service


@router.post(
    "",
    response_model=CbrExportEnvelope,
    responses={404: {}, 409: {}, 422: {}, 500: {}, 503: {}, 507: {}},
    summary="Build a verified CBR from selected ready chapters",
)
def create_cbr_export(body: CbrExportRequest, request: Request) -> CbrExportEnvelope:
    result: CbrExportResult = _service(request).create(
        series_id=UUID(str(body.series_id)),
        chapter_ids=tuple(UUID(str(chapter_id)) for chapter_id in body.chapter_ids),
    )
    return CbrExportEnvelope(
        data=CbrExportResponse(
            asset_id=result.asset.asset_id,
            filename=result.filename,
            series_title=result.series_title,
            chapter_labels=list(result.chapter_labels),
            page_count=result.page_count,
            size_bytes=result.asset.size_bytes,
            sha256=result.asset.sha256,
        )
    )
