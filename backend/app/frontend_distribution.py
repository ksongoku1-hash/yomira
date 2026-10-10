"""Serve the built single-page UI from the local production backend."""

from __future__ import annotations

import stat
import sys
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


class FrontendDistributionError(RuntimeError):
    """The production UI build is missing or cannot be safely served."""

    def __init__(self) -> None:
        super().__init__("The built frontend is not available.")


def _is_plain_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not reparse


def _is_plain_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not reparse


def _is_frontend_directory(path: Path) -> bool:
    return (
        _is_plain_directory(path)
        and _is_plain_file(path / "index.html")
        and _is_plain_directory(path / "assets")
    )


def resolve_frontend_directory() -> Path | None:
    """Resolve the bundled UI or the Vite build in a source checkout.

    PyInstaller places its data files at ``<_MEIPASS>/app/web``. Development
    and test checkouts may instead use ``<repo>/frontend/dist``.
    """
    if bool(getattr(sys, "frozen", False)):
        bundle_root_value = getattr(sys, "_MEIPASS", None)
        if not isinstance(bundle_root_value, str):
            return None
        candidate = Path(bundle_root_value) / "app" / "web"
    else:
        candidate = Path(__file__).resolve().parents[2] / "frontend" / "dist"

    return candidate if _is_frontend_directory(candidate) else None


def register_frontend_routes(app: FastAPI, directory: Path) -> None:
    """Register built assets and safe SPA fallback routes on a production app."""
    if not _is_frontend_directory(directory):
        raise FrontendDistributionError()

    index_path = directory / "index.html"
    assets_path = directory / "assets"
    app.mount(
        "/assets",
        StaticFiles(directory=str(assets_path), check_dir=True),
        name="frontend-assets",
    )

    @app.get("/", include_in_schema=False)
    async def get_frontend_index() -> FileResponse:
        return FileResponse(index_path, media_type="text/html; charset=utf-8")

    @app.get("/{client_route:path}", include_in_schema=False)
    async def get_frontend_route(client_route: str) -> FileResponse:
        normalized = client_route.casefold()
        if normalized == "api" or normalized.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found.")
        if PurePosixPath(client_route).suffix:
            raise HTTPException(status_code=404, detail="Frontend resource not found.")
        return FileResponse(index_path, media_type="text/html; charset=utf-8")
