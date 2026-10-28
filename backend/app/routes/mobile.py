import base64
import ipaddress
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app.services.mobile_access import (
    AUTH_SCOPE,
    PORT,
    MobileAccess,
    MobileStartup,
    local_addresses,
)

router = APIRouter(prefix="/api/v1/system/mobile", tags=["system"])


def local_control(request: Request) -> bool:
    try:
        return (
            request.scope.get(AUTH_SCOPE) is not True
            and request.client is not None
            and ipaddress.ip_address(request.client.host).is_loopback
        )
    except ValueError:
        return False


def require_local_control(request: Request) -> MobileAccess:
    if not local_control(request) or request.headers.get("x-yomira-mobile") != "1":
        raise HTTPException(403, "Activa o desactiva el acceso desde el PC.")
    return cast(MobileAccess, request.app.state.mobile_access)


class MobileEnableRequest(BaseModel):
    address: str


@router.get("")
async def mobile_status(request: Request, response: Response) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    manager = cast(MobileAccess, request.app.state.mobile_access)
    return manager.status(local=local_control(request))


@router.post("/enable")
async def mobile_enable(
    body: MobileEnableRequest, request: Request, response: Response
) -> dict[str, object]:
    manager = require_local_control(request)
    try:
        await manager.enable(body.address)
    except (OSError, ValueError) as error:
        raise HTTPException(
            409,
            "No se pudo abrir la conexión. Revisa la red o el puerto 5183.",
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return manager.status(local=True)


@router.post("/disable")
async def mobile_disable(request: Request) -> dict[str, object]:
    manager = require_local_control(request)
    async with manager.lock:
        await manager.close()
    return manager.status(local=True)


@router.post("/startup")
async def mobile_startup(
    body: MobileStartup,
    request: Request,
    response: Response,
) -> dict[str, object]:
    manager = require_local_control(request)
    address = manager.host if manager.enabled else body.address
    if address and address not in local_addresses():
        raise HTTPException(409, "Elige una dirección de red actual de este PC.")
    try:
        manager.save_startup(MobileStartup(enabled=body.enabled, address=address))
    except RuntimeError as error:
        raise HTTPException(
            503, "La biblioteca todavía no está lista. Vuelve a intentarlo."
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return manager.status(local=True)


@router.post("/firewall")
async def mobile_firewall(request: Request) -> dict[str, str]:
    manager = require_local_control(request)
    if not manager.enabled or os.name != "nt":
        raise HTTPException(409, "Activa primero el acceso desde móvil en Windows.")

    # Only validated local addresses, integer ports and literal paths reach this fixed script.
    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    program = str(Path(sys.executable).resolve())
    script = (
        "$ErrorActionPreference='Stop'\n"
        "$rule=Get-NetFirewallRule -Name 'Yomira-Mobile-Private' -ErrorAction SilentlyContinue\n"
        "if ($rule) { Remove-NetFirewallRule -Name 'Yomira-Mobile-Private' }\n"
        "New-NetFirewallRule -Name 'Yomira-Mobile-Private' "
        "-DisplayName 'Yomira móvil (red privada)' "
        f"-Direction Inbound -Action Allow -Protocol TCP -LocalPort {PORT} "
        f"-LocalAddress {literal(manager.host)} -RemoteAddress LocalSubnet "
        f"-Profile Private -Program {literal(program)} | Out-Null\n"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
        r"System32\WindowsPowerShell\v1.0\powershell.exe"
    )
    command = (
        f"Start-Process -FilePath {literal(str(powershell))} -Verb RunAs -WindowStyle Hidden "
        f"-ArgumentList @('-NoProfile','-EncodedCommand', '{encoded}')"
    )
    subprocess.Popen(
        [str(powershell), "-NoProfile", "-Command", command],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return {"message": "Acepta el aviso de Windows y prueba la dirección desde el móvil."}
