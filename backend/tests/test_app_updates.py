import asyncio
import hashlib
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, TypedDict
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from app.factory import create_app
from app.services import app_updates as updates
from app.settings import Settings

PAYLOAD = b"verified installer fixture"
NEXT = "99.0.0"


class ReleasePayload(TypedDict):
    draft: bool
    prerelease: bool
    tag_name: str
    assets: list[dict[str, object]]


def release() -> ReleasePayload:
    name = f"Yomira-Setup-{NEXT}.exe"
    return {
        "draft": False,
        "prerelease": False,
        "tag_name": f"v{NEXT}",
        "assets": [
            {
                "name": name,
                "state": "uploaded",
                "size": len(PAYLOAD),
                "digest": "sha256:" + hashlib.sha256(PAYLOAD).hexdigest(),
                "browser_download_url": f"https://github.com/{updates.REPOSITORY}/releases/download/v{NEXT}/{name}",
            }
        ],
    }


@pytest.fixture
def updater(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> updates.AppUpdater:
    executable = tmp_path / "installed" / "Yomira.exe"
    monkeypatch.setattr(updates, "installed_executable", lambda: executable)
    return updates.AppUpdater(tmp_path / "profile")


@pytest.mark.parametrize(
    "value", ["v1.2.3-beta", "1.2", "01.2.3", "1.2.3/evil", "1.2.3.4", "-1.2.3"]
)
def test_rejects_non_stable_versions(value: str) -> None:
    with pytest.raises(ValueError):
        updates.version_tuple(value)


def test_semantic_comparison_and_no_downgrade() -> None:
    assert updates.version_tuple("v1.10.0") > updates.version_tuple("1.9.99")
    data = release()
    data["tag_name"] = "v" + updates.APP_VERSION
    assert updates.release_installer(data) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("size", 0),
        ("size", True),
        ("size", updates.MAX_INSTALLER_BYTES + 1),
        ("digest", None),
        ("digest", "sha256:123"),
        ("state", "new"),
        ("browser_download_url", "https://evil.example/setup.exe"),
    ],
)
def test_rejects_untrusted_installers(field: str, value: object) -> None:
    data = release()
    data["assets"][0][field] = value
    with pytest.raises(ValueError):
        updates.release_installer(data)


@pytest.mark.parametrize("field", ["draft", "prerelease"])
def test_rejects_unpublished_releases(field: Literal["draft", "prerelease"]) -> None:
    data = release()
    data[field] = True
    with pytest.raises(ValueError):
        updates.release_installer(data)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/asset",
        "https://github.com.evil.example/asset",
        "https://user@github.com/asset",
        "https://github.com:444/asset",
        "file:///C:/setup.exe",
        "https://127.0.0.1/setup.exe",
    ],
)
def test_rejects_unsafe_redirects(url: str) -> None:
    with pytest.raises(ValueError):
        updates.validate_download_url(url)


def install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        updates.httpx,
        "AsyncClient",
        partial(real_client, transport=httpx.MockTransport(handler)),
    )


def test_download_verify_cache_and_duplicate_check(
    updater: updates.AppUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if str(request.url) == updates.LATEST_URL:
            return httpx.Response(200, json=release())
        if request.url.host == "github.com":
            return httpx.Response(
                302,
                headers={"location": "https://release-assets.githubusercontent.com/fixture"},
            )
        return httpx.Response(200, content=PAYLOAD)

    install_transport(monkeypatch, handler)

    async def run() -> None:
        updater.check()
        first_task = updater.task
        updater.check()
        assert updater.task is first_task
        assert updater.task is not None
        await updater.task
        assert updater.state == "ready"
        assert updater.downloaded == len(PAYLOAD)
        assert (updater.directory / f"Yomira-Setup-{NEXT}.exe").read_bytes() == PAYLOAD
        assert not list(updater.directory.glob("*.part"))
        updater.state, updater.last_check = "error", 0
        updater.check()
        assert updater.task is not None
        await updater.task
        assert updater.state == "ready"
        assert len(requests) == 4  # The second check reuses the verified installer.
        await updater.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode",
    [
        "digest",
        "truncated",
        "oversized",
        "redirect",
        "network",
        "missing_location",
        "disk",
    ],
)
def test_failed_download_never_becomes_installable(
    updater: updates.AppUpdater, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == updates.LATEST_URL:
            return httpx.Response(200, json=release())
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.example/setup.exe"})
        if mode == "missing_location":
            return httpx.Response(302)
        if mode == "network":
            raise httpx.ConnectError("offline")
        body = (
            b"x" * len(PAYLOAD)
            if mode == "digest"
            else PAYLOAD[:-1]
            if mode == "truncated"
            else PAYLOAD + b"extra"
        )
        return httpx.Response(200, content=body)

    install_transport(monkeypatch, handler)
    if mode == "disk":
        def no_disk_space(_path: Path) -> SimpleNamespace:
            return SimpleNamespace(free=0)

        monkeypatch.setattr(updates.shutil, "disk_usage", no_disk_space)

    async def run() -> None:
        updater.check()
        assert updater.task is not None
        await updater.task
        assert updater.state == "error"
        assert not list(updater.directory.glob("*.exe"))
        assert not list(updater.directory.glob("*.part"))
        with pytest.raises(ValueError):
            await updater.install()

    asyncio.run(run())


def test_offline_check_does_not_block_startup_or_repeat_immediately(
    updater: updates.AppUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_transport(monkeypatch, lambda _: httpx.Response(403))

    async def run() -> None:
        updater.start()
        assert updater.loop_task is not None
        updater.check()
        assert updater.task is not None
        await updater.task
        assert updater.state == "error"
        first = updater.task
        updater.check()
        assert updater.task is first
        await updater.close()
        assert updater.loop_task.cancelled()

    asyncio.run(run())


def test_portable_and_source_do_not_check_or_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates, "installed_executable", lambda: None)
    service = updates.AppUpdater(tmp_path)
    service.start()
    service.check()
    assert service.state == "unsupported"
    assert service.task is service.loop_task is None
    assert not (tmp_path / "updates").exists()
    with pytest.raises(ValueError):
        asyncio.run(service.install())


def prepare_ready(updater: updates.AppUpdater) -> None:
    updater.asset = updates.release_installer(release())
    updater.total = len(PAYLOAD)
    updater.directory.mkdir(parents=True)
    (updater.directory / f"Yomira-Setup-{NEXT}.exe").write_bytes(PAYLOAD)
    updater.state = "ready"


def test_install_spawns_hidden_helper_without_running_installer_in_server(
    updater: updates.AppUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_ready(updater)
    spawn = Mock()
    monkeypatch.setattr(updates.subprocess, "Popen", spawn)
    asyncio.run(updater.install())
    args, kwargs = spawn.call_args
    command = args[0]
    assert command[0].endswith("powershell.exe")
    assert "-File" in command and "-ParentId" in command
    assert command[command.index("-Executable") + 1] == str(updater.executable)
    assert kwargs["creationflags"] & updates.subprocess.CREATE_NO_WINDOW
    assert updater.state == "installing"


def test_install_refuses_changed_download(
    updater: updates.AppUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_ready(updater)
    (updater.directory / f"Yomira-Setup-{NEXT}.exe").write_bytes(b"tampered")
    spawn = Mock()
    monkeypatch.setattr(updates.subprocess, "Popen", spawn)
    with pytest.raises(ValueError):
        asyncio.run(updater.install())
    spawn.assert_not_called()
    assert updater.state == "error"


def test_routes_require_header_and_trusted_origin_and_only_shutdown_after_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates, "installed_executable", lambda: None)
    app: FastAPI = create_app(Settings(app_env="test", data_root=tmp_path))
    shutdown = Mock()
    app.state.request_shutdown = shutdown

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1"
        ) as client:
            assert (await client.get("/api/v1/system/updates")).json()["state"] == "unsupported"
            assert (await client.post("/api/v1/system/updates/check")).status_code == 403
            headers = {"X-Yomira-Update": "1"}
            assert (
                await client.post(
                    "/api/v1/system/updates/check",
                    headers={**headers, "Origin": "https://evil.example"},
                )
            ).status_code == 403
            assert (
                await client.post("/api/v1/system/updates/install", headers=headers)
            ).status_code == 409
            shutdown.assert_not_called()
            assert (await client.post("/api/v1/system/updates/check", headers=headers)).json()[
                "state"
            ] == "unsupported"

    asyncio.run(run())


def test_install_route_prepares_helper_before_requesting_graceful_shutdown(
    updater: updates.AppUpdater, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_ready(updater)
    events: list[str] = []

    def prepare_helper(*_args: object, **_kwargs: object) -> None:
        events.append("helper")

    monkeypatch.setattr(updates.subprocess, "Popen", prepare_helper)
    app = create_app(Settings(app_env="test", data_root=tmp_path / "api"))
    app.state.updater = updater

    async def shutdown() -> None:
        events.append("shutdown")

    app.state.request_shutdown = shutdown

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post(
                "/api/v1/system/updates/install", headers={"X-Yomira-Update": "1"}
            )
            assert response.status_code == 200
            assert response.json()["state"] == "installing"
            assert events == ["helper", "shutdown"]
            assert (
                await client.post(
                    "/api/v1/system/updates/install", headers={"X-Yomira-Update": "1"}
                )
            ).status_code == 409
            assert events == ["helper", "shutdown"]

    asyncio.run(run())


def test_install_detection_requires_matching_inno_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import winreg
    from contextlib import nullcontext

    exe = tmp_path / "installed" / "Yomira.exe"
    monkeypatch.setattr(updates.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updates.sys, "executable", str(exe))
    monkeypatch.setattr(winreg, "OpenKey", Mock(return_value=nullcontext(object())))
    monkeypatch.setattr(
        winreg, "QueryValueEx", Mock(return_value=(str(exe.parent) + "\\", winreg.REG_SZ))
    )
    assert updates.installed_executable() == exe
    monkeypatch.setattr(
        winreg, "QueryValueEx", Mock(return_value=(str(tmp_path / "other"), winreg.REG_SZ))
    )
    assert updates.installed_executable() is None
