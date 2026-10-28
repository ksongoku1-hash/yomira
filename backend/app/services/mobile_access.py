"""Opt-in LAN listener; credentials live only for the current application session."""

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import socket
from collections.abc import Generator
from time import monotonic
from typing import cast
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import Message, Receive, Scope, Send

from app.domain.setting import Setting
from app.persistence.mappers.setting import setting_to_row
from app.persistence.models.setting import SettingRow
from app.runtime import RuntimeState

# shortcut: HTTP is limited to trusted home networks; add trusted TLS before wider access.
PORT = 5183
SESSION_SECONDS = 8 * 60 * 60
COOKIE = "yomira_mobile"
AUTH_SCOPE = "yomira.mobile.authenticated"
STARTUP_SETTING = "mobile.startup"
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    )
)
logger = logging.getLogger("yomira.mobile")


def private_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return any(address in network for network in PRIVATE_NETWORKS)
    except ValueError:
        return False


def local_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        addresses.update(
            str(item[4][0])
            for item in socket.getaddrinfo(
                socket.gethostname(),
                None,
                socket.AF_INET,
                socket.SOCK_STREAM,
            )
        )
    except OSError:
        pass
    return sorted(address for address in addresses if private_address(address))


class MobileStartup(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    enabled: bool = False
    address: str = Field(default="", max_length=64)

    @field_validator("address")
    @classmethod
    def private_network_only(cls, value: str) -> str:
        if value and not private_address(value):
            raise ValueError("Only private network addresses are accepted")
        return value


class MobileServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        # The main loopback server owns process signals.
        yield


class MobileAccess:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.host = ""
        self.pin = ""
        self.sessions: dict[str, float] = {}
        self.attempts: dict[str, list[float]] = {}
        self.server: MobileServer | None = None
        self.task: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self.startup = MobileStartup()
        self.startup_error: str | None = None

    async def start(self) -> None:
        try:
            database = cast(RuntimeState, self.app.state.runtime).database
            if database is None:
                return
            with database.read_session() as session:
                row = session.get(SettingRow, STARTUP_SETTING)
                if row is not None:
                    if row.schema_version != 1 or len(row.value_json) > 512:
                        raise ValueError("Invalid mobile startup setting")
                    self.startup = MobileStartup.model_validate_json(row.value_json)
            if self.startup.enabled:
                addresses = local_addresses()
                address = (
                    self.startup.address
                    if self.startup.address in addresses
                    else addresses[0]
                    if len(addresses) == 1
                    else ""
                )
                if not address:
                    self.startup_error = "Elige la red del PC y activa el acceso desde móvil."
                    return
                await self.enable(address)
        except Exception:
            self.startup_error = (
                "No se pudo iniciar el acceso móvil. Revisa la red y vuelve a activarlo."
            )
            logger.exception("mobile.automatic_start_failed")

    def save_startup(self, startup: MobileStartup) -> None:
        database = cast(RuntimeState, self.app.state.runtime).database
        if database is None:
            raise RuntimeError("The library is not ready")
        with database.write_session() as session:
            session.merge(
                setting_to_row(
                    Setting.new_local(
                        STARTUP_SETTING,
                        startup.model_dump_json(),
                    )
                )
            )
        self.startup = startup
        if not startup.enabled:
            self.startup_error = None

    @property
    def enabled(self) -> bool:
        return self.server is not None and self.server.started and not self.server.should_exit

    def status(self, *, local: bool) -> dict[str, object]:
        result: dict[str, object] = {"enabled": self.enabled, "local": local}
        if local:
            result.update(
                addresses=local_addresses(),
                url=(f"http://{self.host}:{PORT}" if self.enabled else None),
                pin=self.pin if self.enabled else None,
                auto_start=self.startup.enabled,
                startup_error=self.startup_error,
                windows_firewall_supported=os.name == "nt",
            )
        return result

    async def enable(self, host: str) -> None:
        async with self.lock:
            if host not in local_addresses():
                raise ValueError("Elige una dirección privada de este PC.")
            if self.enabled and self.host == host:
                return
            await self.close()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                # Exclusive binding prevents another process sharing the selected port on Windows.
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                listener.bind((host, PORT))
                listener.listen(128)
                listener.setblocking(False)
                self.host = host
                self.pin = f"{secrets.randbelow(1_000_000):06d}"
                self.server = MobileServer(
                    uvicorn.Config(
                        self,
                        host=host,
                        port=PORT,
                        lifespan="off",
                        proxy_headers=False,
                        access_log=False,
                        log_config=None,
                        timeout_graceful_shutdown=5,
                    )
                )
                self.task = asyncio.create_task(self.server.serve(sockets=[listener]))
                for _ in range(100):
                    if self.server.started:
                        self.startup_error = None
                        return
                    if self.task.done():
                        await self.task
                        raise OSError("No se pudo iniciar la conexión móvil.")
                    await asyncio.sleep(0.02)
                raise OSError("La conexión móvil tardó demasiado en iniciar.")
            except BaseException:
                listener.close()
                await self.close()
                raise

    async def close(self) -> None:
        self.pin = ""
        self.sessions.clear()
        self.attempts.clear()
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            try:
                await self.task
            except Exception:
                logger.exception("mobile.listener_stopped_unexpectedly")
        self.server = None
        self.task = None

    def login_page(self, error: str = "", status: int = 200) -> HTMLResponse:
        return HTMLResponse(
            """<!doctype html><html lang="es"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Yomira · Conectar</title>
<style>body{background:#101519;color:#edf2f4;font:16px system-ui;margin:0;min-height:100dvh;
display:grid;place-items:center}main{box-sizing:border-box;width:min(100%,420px);padding:32px}
input,button{box-sizing:border-box;width:100%;font:inherit;padding:14px;border-radius:12px;
margin:10px 0;border:1px solid #53636c}input{background:#19242b;color:inherit;letter-spacing:.4em}
button{background:#7dd9f5;color:#0a151b;font-weight:700;cursor:pointer}p{line-height:1.6;color:#bdcbd2}
</style><main><h1>Yomira</h1><p>Introduce el PIN que aparece en Ajustes → Acceso desde móvil
en tu PC.</p><form method="post" action="/mobile/login"><label for="pin">PIN de 6 cifras</label>
<input id="pin" name="pin" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" minlength="6"
autocomplete="off" required autofocus><button>Conectar</button></form><p role="alert">"""
            + error
            + """</p><p>El PC debe permanecer encendido. Usa únicamente
tu red Wi-Fi de confianza. La sesión dura 8 horas.</p></main></html>""",
            status_code=status,
            headers={
                "Cache-Control": "no-store",
                # Form submissions need their same-origin Origin header to pass the gateway.
                "Referrer-Policy": "same-origin",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "form-action 'self'; frame-ancestors 'none'"
                ),
            },
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        origin = f"http://{self.host}:{PORT}"
        peer = request.client.host if request.client else ""
        path = request.url.path
        headers = request.headers
        if (
            not self.enabled
            or not private_address(peer)
            or headers.get("host") != f"{self.host}:{PORT}"
            or headers.get("sec-fetch-site") == "cross-site"
            or headers.get("origin", origin) != origin
            or (request.method not in {"GET", "HEAD"} and headers.get("origin") != origin)
        ):
            response: Response = (
                JSONResponse({"message": "Conexión móvil rechazada."}, 403)
                if path.startswith("/api/")
                else self.login_page(
                    "No se pudo validar la conexión. Vuelve a introducir el PIN desde esta página.",
                    403,
                )
            )
        elif path == "/mobile/login" and request.method == "POST":
            now = monotonic()
            self.attempts = {
                key: [t for t in times if now - t < 300]
                for key, times in self.attempts.items()
                if times and now - times[-1] < 300
            }
            recent = self.attempts.get(peer, [])
            if len(recent) >= 5 or sum(map(len, self.attempts.values())) >= 50:
                response = self.login_page("Demasiados intentos. Espera 5 minutos.", 429)
            else:
                recent.append(now)
                self.attempts[peer] = recent
                body = bytearray()
                try:
                    async with asyncio.timeout(10):
                        async for chunk in request.stream():
                            body.extend(chunk)
                            if len(body) > 128:
                                break
                except TimeoutError:
                    body = bytearray(b"invalid")
                try:
                    pin = parse_qs(body.decode("ascii"), max_num_fields=2).get("pin", [""])[0]
                except UnicodeError, ValueError:
                    pin = ""
                if (
                    len(body) <= 128
                    and len(pin) == 6
                    and pin.isascii()
                    and pin.isdigit()
                    and hmac.compare_digest(pin, self.pin)
                ):
                    self.sessions = {
                        key: expiry for key, expiry in self.sessions.items() if expiry > now
                    }
                    if len(self.sessions) >= 16:
                        del self.sessions[next(iter(self.sessions))]
                    token = secrets.token_urlsafe(32)
                    self.sessions[hashlib.sha256(token.encode()).hexdigest()] = (
                        now + SESSION_SECONDS
                    )
                    response = RedirectResponse("/", 303)
                    response.set_cookie(
                        COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict"
                    )
                else:
                    response = self.login_page("PIN incorrecto. Revísalo en tu PC.", 401)
        elif path == "/mobile/login":
            response = self.login_page()
        else:
            token = request.cookies.get(COOKIE, "")
            digest = hashlib.sha256(token.encode()).hexdigest()
            authenticated = self.sessions.get(digest, 0) > monotonic()
            if path == "/mobile/logout" and request.method == "POST":
                self.sessions.pop(digest, None)
                response = RedirectResponse("/mobile/login", 303)
                response.delete_cookie(COOKIE)
            elif not authenticated:
                response = (
                    JSONResponse({"message": "Vuelve a conectar con el PIN de tu PC."}, 401)
                    if path.startswith("/api/")
                    else RedirectResponse("/mobile/login", 303)
                )
            elif (
                path.startswith(
                    (
                        "/api/v1/system/updates",
                        "/api/v1/usb-exports",
                        "/api/v1/cbr-exports",
                        "/api/v1/library/folders",
                    )
                )
                or (path == "/api/v1/system/mobile" and request.method != "GET")
                or path.startswith("/api/v1/system/mobile/")
            ):
                response = JSONResponse({"message": "Esta acción se realiza desde el PC."}, 403)
            else:
                scope[AUTH_SCOPE] = True

                async def private_response(message: Message) -> None:
                    if message["type"] == "http.response.start" and path.startswith("/api/"):
                        MutableHeaders(scope=message)["Cache-Control"] = "private, no-store"
                    await send(message)

                await self.app(scope, receive, private_response)
                return
        response.headers["Cache-Control"] = "no-store"
        await response(scope, receive, send)
