"""Prepare and retrieve one EPUB containing all downloaded chapters of a manga."""

import asyncio
import zipfile
from collections.abc import Iterator
from typing import cast
from urllib.parse import quote
from uuid import UUID
from xml.etree import ElementTree as ET

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.runtime import RuntimeState
from app.services.reader_asset import ReaderAssetError
from app.services.series_epub import SeriesEpubService

router = APIRouter(prefix="/api/v1/series", tags=["epub"])


def _service(request: Request) -> SeriesEpubService:
    service = cast(RuntimeState, request.app.state.runtime).series_epub_service
    if service is None:
        raise HTTPException(503, "La preparación de EPUB todavía no está disponible.")
    return service


@router.get("/{series_id}/epub")
async def epub_status(series_id: UUID, request: Request) -> dict[str, object]:
    try:
        return {"data": await asyncio.to_thread(_service(request).status, series_id)}
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except ReaderAssetError:
        raise HTTPException(
            409, "Los capítulos cambiaron. Actualiza la ficha y reintenta."
        ) from None


@router.post("/{series_id}/epub", status_code=202)
async def prepare_epub(series_id: UUID, request: Request) -> dict[str, object]:
    try:
        return {"data": await _service(request).create(series_id)}
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except ReaderAssetError:
        raise HTTPException(
            409, "Los capítulos cambiaron. Actualiza la ficha y reintenta."
        ) from None
    except RuntimeError:
        raise HTTPException(503, "La aplicación se está cerrando. Reintenta al abrirla.") from None


@router.get("/{series_id}/epub/file")
async def download_epub(series_id: UUID, request: Request) -> StreamingResponse:
    try:
        stream, filename, size = await asyncio.to_thread(_service(request).open_download, series_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError:
        raise HTTPException(409, "El EPUB no está disponible. Vuelve a prepararlo.") from None

    def chunks() -> Iterator[bytes]:
        try:
            while payload := stream.read(1024 * 1024):
                yield payload
        finally:
            stream.close()

    return StreamingResponse(
        chunks(),
        media_type="application/epub+zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
            "Content-Length": str(size),
            "Cache-Control": "no-store",
        },
        background=BackgroundTask(stream.close),
    )
