from typing import Literal, cast

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from app.services.app_updates import AppUpdater

router = APIRouter(prefix="/api/v1/system/updates", tags=["system"])


class UpdateStatus(BaseModel):
    state: Literal[
        "unsupported",
        "idle",
        "checking",
        "up_to_date",
        "downloading",
        "ready",
        "installing",
        "error",
    ]
    current_version: str
    latest_version: str | None
    downloaded_bytes: int
    total_bytes: int
    message: str
    releases_url: str


def _updater(request: Request, *, mutation: bool = False) -> AppUpdater:
    if mutation and request.headers.get("x-yomira-update") != "1":
        raise HTTPException(403, "Update requests require the local application header.")
    return cast(AppUpdater, request.app.state.updater)


@router.get("", response_model=UpdateStatus)
async def get_updates(request: Request) -> dict[str, object]:
    return _updater(request).status()


@router.post("/check", response_model=UpdateStatus)
async def check_updates(request: Request) -> dict[str, object]:
    updater = _updater(request, mutation=True)
    updater.check()
    return updater.status()


@router.post("/install", response_model=UpdateStatus)
async def install_update(request: Request, background_tasks: BackgroundTasks) -> dict[str, object]:
    updater = _updater(request, mutation=True)
    shutdown = getattr(request.app.state, "request_shutdown", None)
    if not callable(shutdown):
        raise HTTPException(409, "This server cannot restart to install updates.")
    try:
        await updater.install()
    except (OSError, ValueError, KeyError) as error:
        raise HTTPException(409, "The update could not be started safely.") from error
    background_tasks.add_task(shutdown)
    return updater.status()
