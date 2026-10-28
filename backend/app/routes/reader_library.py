from collections.abc import Callable
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import UUID4, BaseModel, ConfigDict

from app.runtime import RuntimeState
from app.services.reader_library import (
    ReaderBookmark,
    ReaderLibraryConflict,
    ReaderLibraryCorrupt,
    ReaderLibraryData,
    ReaderLibraryImport,
    ReaderLibraryService,
    ReaderWorkPreferences,
)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(
    prefix="/api/v1/reader/library", tags=["reader"], dependencies=[Depends(_no_store)]
)


class ReaderLibraryEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: ReaderLibraryData
    meta: dict[str, str] = {}


def _service(request: Request) -> ReaderLibraryService:
    runtime = cast(RuntimeState, request.app.state.runtime)
    if runtime.database is None:
        raise HTTPException(
            status_code=503,
            detail="La biblioteca no está disponible.",
            headers={"Cache-Control": "private, no-store"},
        )
    return ReaderLibraryService(runtime.database)


def _response(operation: Callable[[], ReaderLibraryData]) -> ReaderLibraryEnvelope:
    try:
        return ReaderLibraryEnvelope(data=operation())
    except ReaderLibraryConflict as error:
        raise HTTPException(
            status_code=409, detail=str(error), headers={"Cache-Control": "private, no-store"}
        ) from None
    except ReaderLibraryCorrupt as error:
        raise HTTPException(
            status_code=500, detail=str(error), headers={"Cache-Control": "private, no-store"}
        ) from None


@router.get("", response_model=ReaderLibraryEnvelope)
def get_reader_library(request: Request) -> ReaderLibraryEnvelope:
    return _response(_service(request).get)


@router.put("/preferences/{series_id}", response_model=ReaderLibraryEnvelope)
def put_reader_work_preferences(
    series_id: UUID4, body: ReaderWorkPreferences, request: Request
) -> ReaderLibraryEnvelope:
    return _response(lambda: _service(request).put_preferences(series_id, body))


@router.put("/bookmarks", response_model=ReaderLibraryEnvelope)
def put_reader_bookmark(body: ReaderBookmark, request: Request) -> ReaderLibraryEnvelope:
    return _response(lambda: _service(request).put_bookmark(body))


@router.delete("/bookmarks/{chapter_id}/{page_index}", response_model=ReaderLibraryEnvelope)
def remove_reader_bookmark(
    chapter_id: UUID4, request: Request, page_index: int = Path(ge=0, le=9999)
) -> ReaderLibraryEnvelope:
    return _response(lambda: _service(request).remove_bookmark(chapter_id, page_index))


@router.post("/import", response_model=ReaderLibraryEnvelope)
def import_reader_library(body: ReaderLibraryImport, request: Request) -> ReaderLibraryEnvelope:
    return _response(lambda: _service(request).import_legacy(body))
