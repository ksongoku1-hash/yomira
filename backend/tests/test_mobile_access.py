import asyncio
import base64
import hashlib
import signal
import socket
from collections.abc import Generator
from pathlib import Path
from time import monotonic
from typing import cast

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from pytest import MonkeyPatch
from support.sqlite import open_test_database

from app.factory import create_app
from app.persistence.database import SQLiteDatabase
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.setting import SettingRow
from app.runtime import RuntimeState
from app.services import mobile_access
from app.services.mobile_access import (
    AUTH_SCOPE,
    COOKIE,
    STARTUP_SETTING,
    MobileAccess,
    MobileServer,
    MobileStartup,
)
from app.settings import Settings

ORIGIN = "http://192.168.1.29:5183"


def gateway() -> MobileAccess:
    app = create_app(Settings(app_env="test"))
    manager = cast(MobileAccess, app.state.mobile_access)
    manager.host = "192.168.1.29"
    manager.pin = "123456"
    manager.server = MobileServer(uvicorn.Config(app, lifespan="off"))
    manager.server.started = True
    return manager


def client(manager: MobileAccess, peer: str = "192.168.1.30") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=manager, client=(peer, 1234)),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN},
    )


def test_login_protects_api_images_and_frontend_and_hides_pin() -> None:
    async def check() -> None:
        manager = gateway()
        async with client(manager) as browser:
            assert (await browser.get("/api/v1/system/mobile")).status_code == 401
            assert (await browser.get("/api/v1/reader/x/pages/1")).status_code == 401
            assert (await browser.get("/")).headers["location"] == "/mobile/login"
            page = await browser.get("/mobile/login")
            assert manager.pin not in page.text
            assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
            assert page.headers["referrer-policy"] == "same-origin"
            result = await browser.post("/mobile/login", data={"pin": manager.pin})
            assert result.status_code == 303
            assert "HttpOnly" in result.headers["set-cookie"]
            assert "SameSite=strict" in result.headers["set-cookie"]
            status = await browser.get("/api/v1/system/mobile")
            assert status.json() == {"enabled": True, "local": False}
            assert "no-store" in status.headers["cache-control"]
            assert (await browser.get("/api/v1/system/version")).status_code == 200
            await browser.post("/mobile/logout")
            assert (await browser.get("/api/v1/system/version")).status_code == 401

    asyncio.run(check())


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Host": "evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Host": "127.0.0.1:5183"},
    ],
)
def test_gateway_rejects_foreign_origins_and_hosts(headers: dict[str, str]) -> None:
    async def check() -> None:
        async with client(gateway()) as browser:
            assert (
                await browser.post("/mobile/login", data={"pin": "123456"}, headers=headers)
            ).status_code == 403

    asyncio.run(check())


def test_rejected_form_is_a_readable_page_without_relaxing_origin_validation() -> None:
    async def check() -> None:
        manager = gateway()
        async with client(manager) as browser:
            rejected = await browser.post(
                "/mobile/login", data={"pin": manager.pin}, headers={"Origin": "null"}
            )
            assert rejected.status_code == 403
            assert rejected.headers["content-type"] == "text/html; charset=utf-8"
            assert rejected.headers["referrer-policy"] == "same-origin"
            assert "Vuelve a introducir el PIN" in rejected.text
            assert manager.sessions == {}
            api = await browser.get("/api/v1/system/mobile", headers={"Origin": "null"})
            assert api.status_code == 403
            assert api.json() == {"message": "Conexión móvil rechazada."}
            accepted = await browser.post("/mobile/login", data={"pin": manager.pin})
            assert accepted.status_code == 303
            assert (await browser.get("/api/v1/system/version")).status_code == 200

    asyncio.run(check())


def test_login_requires_origin_and_bounds_attempts_and_body() -> None:
    async def check() -> None:
        manager = gateway()
        async with client(manager) as browser:
            browser.headers.pop("Origin")
            assert (
                await browser.post("/mobile/login", data={"pin": manager.pin})
            ).status_code == 403
            browser.headers["Origin"] = ORIGIN
            assert (
                await browser.post("/mobile/login", content="pin=" + "1" * 200)
            ).status_code == 401
            for _ in range(4):
                assert (
                    await browser.post("/mobile/login", data={"pin": "000000"})
                ).status_code == 401
            assert (
                await browser.post("/mobile/login", data={"pin": manager.pin})
            ).status_code == 429
            manager.attempts = {"192.168.1.30": [monotonic() - 301]}
            assert (
                await browser.post("/mobile/login", data={"pin": manager.pin})
            ).status_code == 303

    asyncio.run(check())


def test_expired_and_disabled_sessions_are_rejected() -> None:
    async def check() -> None:
        manager = gateway()
        async with client(manager) as browser:
            await browser.post("/mobile/login", data={"pin": manager.pin})
            token = browser.cookies[COOKIE]
            digest = hashlib.sha256(token.encode()).hexdigest()
            manager.sessions[digest] = monotonic() - 1
            assert (await browser.get("/api/v1/system/version")).status_code == 401
            await manager.close()
            assert not manager.sessions and not manager.pin
            assert (await browser.get("/api/v1/system/version")).status_code == 403

    asyncio.run(check())


def test_pc_actions_stay_on_pc_and_forwarded_headers_cannot_escalate() -> None:
    async def check() -> None:
        manager = gateway()
        async with client(manager) as browser:
            await browser.post("/mobile/login", data={"pin": manager.pin})
            for path in (
                "/api/v1/system/mobile/disable",
                "/api/v1/system/mobile/firewall",
                "/api/v1/system/mobile/startup",
                "/api/v1/system/updates/install",
                "/api/v1/library/folders/library/open",
                "/api/v1/usb-exports",
                "/api/v1/cbr-exports",
            ):
                response = await browser.post(
                    path, headers={"X-Forwarded-For": "127.0.0.1", "X-Yomira-Mobile": "1"}
                )
                assert response.status_code == 403, path
        async with client(manager, "8.8.8.8") as external:
            assert (await external.get("/mobile/login")).status_code == 403

    asyncio.run(check())


@pytest.fixture
def persisted_mobile(tmp_path: Path) -> Generator[tuple[MobileAccess, SQLiteDatabase]]:
    app = create_app(Settings(app_env="test", data_root=tmp_path))
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    cast(RuntimeState, app.state.runtime).database = database
    try:
        yield cast(MobileAccess, app.state.mobile_access), database
    finally:
        database.close()


def test_startup_choice_survives_recreation_and_never_saves_credentials(
    persisted_mobile: tuple[MobileAccess, SQLiteDatabase],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, database = persisted_mobile
    calls: list[str] = []

    async def record_enable(_manager: MobileAccess, host: str) -> None:
        calls.append(host)

    monkeypatch.setattr(MobileAccess, "enable", record_enable)
    monkeypatch.setattr(mobile_access, "local_addresses", lambda: ["192.168.1.29"])
    asyncio.run(manager.start())
    assert not manager.startup.enabled and calls == []
    manager.pin = "654321"
    manager.sessions["a-secret-session"] = monotonic() + 300
    manager.save_startup(MobileStartup(enabled=True, address="192.168.1.29"))
    with database.read_session() as session:
        row = session.get(SettingRow, STARTUP_SETTING)
        assert row is not None
        assert row.value_json == '{"enabled":true,"address":"192.168.1.29"}'
    recreated = MobileAccess(manager.app)
    asyncio.run(recreated.start())
    assert recreated.startup.enabled and calls == ["192.168.1.29"]
    recreated.save_startup(MobileStartup(enabled=False))
    asyncio.run(MobileAccess(manager.app).start())
    assert calls == ["192.168.1.29"]


@pytest.mark.parametrize(
    "addresses,chosen",
    [
        (["192.168.1.42"], "192.168.1.42"),
        (["10.0.0.2", "192.168.1.29"], "192.168.1.29"),
        ([], None),
        (["10.0.0.2", "192.168.1.42"], None),
    ],
)
def test_automatic_start_uses_assigned_network_and_reports_ambiguity(
    persisted_mobile: tuple[MobileAccess, SQLiteDatabase],
    monkeypatch: MonkeyPatch,
    addresses: list[str],
    chosen: str | None,
) -> None:
    manager, _ = persisted_mobile
    manager.save_startup(MobileStartup(enabled=True, address="192.168.1.29"))
    calls: list[str] = []

    async def record_enable(_manager: MobileAccess, host: str) -> None:
        calls.append(host)

    monkeypatch.setattr(MobileAccess, "enable", record_enable)
    monkeypatch.setattr(mobile_access, "local_addresses", lambda: addresses)
    recreated = MobileAccess(manager.app)
    asyncio.run(recreated.start())
    assert calls == ([chosen] if chosen else [])
    assert bool(recreated.startup_error) == (chosen is None)


@pytest.mark.parametrize(
    "value,schema",
    [
        ('{"enabled":"true","address":"192.168.1.29"}', 1),
        ('{"enabled":true,"address":"8.8.8.8"}', 1),
        ('{"enabled":true,"address":"192.168.1.29","unknown":true}', 1),
        ('{"enabled":true,"address":"192.168.1.29"}', 2),
    ],
)
def test_bad_startup_settings_do_not_open_listener_or_prevent_app_start(
    persisted_mobile: tuple[MobileAccess, SQLiteDatabase],
    monkeypatch: MonkeyPatch,
    value: str,
    schema: int,
) -> None:
    manager, database = persisted_mobile
    with database.write_session() as session:
        session.add(
            SettingRow(
                setting_key=STARTUP_SETTING,
                value_json=value,
                schema_version=schema,
                updated_at_us=0,
            )
        )

    calls: list[str] = []

    async def unexpected_enable(_manager: MobileAccess, _host: str) -> None:
        calls.append(_host)
        raise AssertionError("Invalid settings must never open a listener")

    monkeypatch.setattr(MobileAccess, "enable", unexpected_enable)
    asyncio.run(manager.start())
    assert manager.startup_error and not manager.enabled and calls == []


def test_busy_mobile_port_does_not_prevent_main_application_start(
    persisted_mobile: tuple[MobileAccess, SQLiteDatabase],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, _ = persisted_mobile
    manager.save_startup(MobileStartup(enabled=True, address="192.168.1.29"))
    monkeypatch.setattr(mobile_access, "local_addresses", lambda: ["192.168.1.29"])

    async def busy_port(_manager: MobileAccess, _host: str) -> None:
        raise OSError("Port occupied")

    monkeypatch.setattr(MobileAccess, "enable", busy_port)
    asyncio.run(manager.start())
    assert manager.startup_error and not manager.enabled and manager.startup.enabled


def test_startup_api_is_local_strict_and_persistent(
    persisted_mobile: tuple[MobileAccess, SQLiteDatabase],
    monkeypatch: MonkeyPatch,
) -> None:
    manager, _ = persisted_mobile
    monkeypatch.setattr(mobile_access, "local_addresses", lambda: ["192.168.1.29"])
    from app.routes import mobile

    monkeypatch.setattr(mobile, "local_addresses", lambda: ["192.168.1.29"])

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:8000",
        ) as browser:
            body = {"enabled": True, "address": "192.168.1.29"}
            path = "/api/v1/system/mobile/startup"
            assert (await browser.post(path, json=body)).status_code == 403
            response = await browser.post(path, json=body, headers={"X-Yomira-Mobile": "1"})
            assert response.status_code == 200 and response.json()["auto_start"] is True
            assert response.headers["cache-control"] == "no-store"
            for invalid in ({"enabled": "true"}, {"enabled": True, "address": "8.8.8.8"}):
                assert (
                    await browser.post(path, json=invalid, headers={"X-Yomira-Mobile": "1"})
                ).status_code == 422
        recreated = MobileAccess(manager.app)

        async def record_enable(_host: str) -> None:
            return

        monkeypatch.setattr(recreated, "enable", record_enable)
        await recreated.start()
        assert recreated.startup.enabled

    asyncio.run(check())


def test_local_control_needs_loopback_and_header_not_just_host() -> None:
    async def check() -> None:
        manager = gateway()
        for peer, expected in (("127.0.0.1", 200), ("192.168.1.30", 403)):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=manager.app, client=(peer, 1234)),
                base_url="http://127.0.0.1:8000",
            ) as browser:
                status = await browser.get("/api/v1/system/mobile")
                assert ("pin" in status.json()) == (peer == "127.0.0.1")
                assert (await browser.post("/api/v1/system/mobile/disable")).status_code == 403
                assert (
                    await browser.post(
                        "/api/v1/system/mobile/disable", headers={"X-Yomira-Mobile": "1"}
                    )
                ).status_code == expected
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app), base_url=ORIGIN
        ) as browser:
            assert (
                await browser.get("/api/v1/system/version", headers={AUTH_SCOPE: "true"})
            ).status_code == 403

    asyncio.run(check())


def test_enable_rejects_unassigned_and_public_addresses(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(mobile_access, "local_addresses", lambda: ["192.168.1.29"])

    async def check() -> None:
        for address in ("0.0.0.0", "8.8.8.8", "127.0.0.1", "192.168.1.99"):
            with pytest.raises(ValueError):
                await MobileAccess(FastAPI()).enable(address)

    asyncio.run(check())


def test_real_listener_authenticates_and_releases_port_without_taking_process_signals(
    monkeypatch: MonkeyPatch,
) -> None:
    addresses = mobile_access.local_addresses()
    if not addresses:
        pytest.skip("No private network interface on this machine")
    address = addresses[0]
    with socket.socket() as probe:
        probe.bind((address, 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(mobile_access, "PORT", port)

    async def check() -> None:
        signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        app = create_app(Settings(app_env="test"))
        manager = cast(MobileAccess, app.state.mobile_access)
        try:
            await manager.enable(address)
            origin = f"http://{address}:{port}"
            async with httpx.AsyncClient(
                base_url=origin, trust_env=False, headers={"Origin": origin}
            ) as browser:
                assert (await browser.get("/api/v1/system/version")).status_code == 401
                assert (
                    await browser.post("/mobile/login", data={"pin": manager.pin})
                ).status_code == 303
                assert (await browser.get("/api/v1/system/version")).status_code == 200
                status = await browser.get("/api/v1/system/mobile")
                assert status.json() == {"enabled": True, "local": False}
                assert not app.state.runtime.ready
            assert all(signal.getsignal(sig) == handler for sig, handler in signals.items())
        finally:
            await manager.close()
        assert not manager.enabled and not manager.sessions and not manager.pin
        with socket.socket() as probe:
            probe.bind((address, port))

    asyncio.run(check())


def test_firewall_permission_is_scoped_and_not_a_writable_elevated_script(
    monkeypatch: MonkeyPatch,
) -> None:
    from app.routes import mobile

    commands: list[list[str]] = []

    def capture(args: list[str], **kwargs: object) -> None:
        commands.append(args)

    monkeypatch.setattr(mobile.subprocess, "Popen", capture)

    async def check() -> None:
        manager = gateway()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:8000",
        ) as browser:
            response = await browser.post(
                "/api/v1/system/mobile/firewall", headers={"X-Yomira-Mobile": "1"}
            )
            assert response.status_code == 200
        command = commands[0][-1]
        encoded = command.split("'-EncodedCommand', '")[1].split("'")[0]
        script = base64.b64decode(encoded).decode("utf-16le")
        assert "-Profile Private" in script and "-RemoteAddress LocalSubnet" in script
        assert "-LocalAddress '192.168.1.29'" in script and "-LocalPort 5183" in script
        assert "-Program '" in script and "-Verb RunAs" in command
        assert "'-File'" not in command

    asyncio.run(check())
