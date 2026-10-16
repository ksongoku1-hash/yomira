"""Windows acceptance with the compiled motor and a synthetic loopback-only Lua fixture."""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
from PIL import Image
from sqlalchemy import func, select

from app.connectors.chapter import ConnectorChapter
from app.connectors.fmd2 import FMD2_SOURCE_ID, Fmd2Connector
from app.connectors.pages import ConnectorPageReference
from app.connectors.registry import ConnectorRegistration, ConnectorRegistry
from app.connectors.sandbox import ConnectorHostContext
from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.factory import create_app
from app.persistence.atomic_file_publisher import ADR_0087_RETRY_POLICY, AtomicFilePublisher
from app.persistence.database import SQLiteDatabase
from app.persistence.disk_budget import DiskBudgetManager
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.reader_session import ReaderSessionRow
from app.persistence.windows_paths import DataRootIdentity
from app.runtime import RuntimeState
from app.secrets.vault import WindowsDpapiSecretVault
from app.services.download_transport import (
    DownloadTransport,
    DownloadTransportPolicy,
    NetworkOrigin,
)
from app.services.image_validation import ImageValidationPolicy, validate_image_file
from app.services.jobs import DownloadJobService
from app.services.online_reader import OnlineReaderSessionService
from app.services.reader_asset import ReaderAssetLimits, ReaderAssetService
from app.services.reader_progress import ReaderProgressService
from app.services.reader_session import HybridReaderChapterLookup, ReaderSessionService
from app.services.source_pages import SourcePageService
from app.settings import Settings


async def accept_reader_cache(
    connector: Fmd2Connector,
    data: Path,
    chapter: str,
    image: bytes,
    origin: str,
    callback_marker: Path,
    callback_release: Path,
    release_second: threading.Event,
) -> dict[str, object]:
    callback_marker.unlink()
    callback_release.unlink()
    release_second.clear()
    (data / "database").mkdir()
    identity = DataRootIdentity.capture(data)
    database = SQLiteDatabase(data / "database/yomira.sqlite3", data_root_identity=identity)
    MigrationRunner(database, data / "backups").upgrade_to_head()
    registry = ConnectorRegistry(
        context=ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(data)),
        registrations=(
            ConnectorRegistration(
                metadata=connector.metadata,
                factory=lambda _c: connector,
                network_origins=frozenset({origin}),
            ),
        ),
    )
    registered = DownloadJobService(database).register_reader_chapter(
        connector=connector.metadata,
        remote_series_id="synthetic-reader-series",
        series_metadata=ConnectorSeriesMetadata(title="Synthetic Reader"),
        chapters=(
            ConnectorChapter(
                chapter, None, "1", "1", 1, None, None, None, (1,), None, "en", "Chapter 1"
            ),
        ),
        selected_remote_id=chapter,
    )
    budget = DiskBudgetManager(database)
    http = httpx.AsyncHTTPTransport(trust_env=False)
    async with httpx.AsyncClient(transport=http, trust_env=False) as client:
        transport = DownloadTransport(
            client,
            DownloadTransportPolicy(
                allowed_origins=frozenset({NetworkOrigin.from_url(origin)}),
                max_bytes=1024,
                chunk_size=64,
                connect_timeout_seconds=2,
                read_timeout_seconds=2,
                write_timeout_seconds=2,
                pool_timeout_seconds=2,
                max_redirects=0,
                max_retry_after_seconds=1,
                global_concurrency=1,
                per_host_concurrency=1,
            ),
            data_root=data,
            data_root_identity=identity,
            storage_gate=database.storage_gate,
            disk_budget_manager=budget,
            test_transport=http,
            secret_header_provider=connector.page_secret_headers,
        )
        service = ReaderSessionService(
            database,
            source_pages=SourcePageService(registry),
            transport=transport,
            disk_budget=budget,
            publisher=AtomicFilePublisher(
                data_root_identity=identity,
                retry_policy=ADR_0087_RETRY_POLICY,
                sleep=time.sleep,
                clock=time.monotonic,
                storage_gate=database.storage_gate,
            ),
            image_policy=ImageValidationPolicy(1024, 100, 100, 10_000),
        )
        local = ReaderAssetService(
            database,
            data_root_identity=identity,
            limits=ReaderAssetLimits(2_147_483_648, 67_108_864, 65_536),
        )
        lookup = HybridReaderChapterLookup(local, service)
        app = create_app(Settings(app_env="test", data_root=data))
        runtime = cast(RuntimeState, app.state.runtime)
        runtime.database = database
        runtime.reader_asset_service = local
        runtime.reader_session_service = service
        runtime.reader_progress_service = ReaderProgressService(
            database, lookup, page_available=lookup.page_available
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as reader:
                route = f"/api/v1/reader/{registered.chapter_id}"
                opened = await reader.post(route + "/session", json={})
                assert opened.status_code == 200, opened.text
                sid = opened.json()["data"]["session_id"]
                deadline = time.monotonic() + 15
                while not callback_marker.exists():
                    assert time.monotonic() < deadline, "Reader callback not reached"
                    assert service.status(registered.chapter_id).state != "failed"
                    await asyncio.sleep(0.05)
                assert (await reader.get(route + "/pages/1")).status_code == 404
                callback_release.write_text("ok", encoding="utf-8")
                while not service.status(registered.chapter_id).ready_pages:
                    assert time.monotonic() < deadline, "Reader first page not cached"
                    assert service.status(registered.chapter_id).state != "failed"
                    await asyncio.sleep(0.05)
                state = (await reader.get(route + "/session")).json()["data"]
                assert state["state"] == "acquiring" and state["ready_pages"] == [1]
                assert (await reader.get(route)).json()["data"]["page_count"] == 2
                assert (await reader.get(route + "/pages/1")).content == image
                assert (await reader.get(route + "/pages/2")).status_code == 404
                saved = await reader.put(route + "/progress", json={"page_index": 0})
                assert saved.status_code == 200
                release_second.set()
                while True:
                    with database.read_session() as db:
                        row = db.get(ReaderSessionRow, sid)
                        assert row is not None
                        released = row.native_released
                    if service.status(registered.chapter_id).state == "complete" and released:
                        break
                    assert time.monotonic() < deadline + 15, "Reader cleanup not settled"
                    await asyncio.sleep(0.05)
                assert not (data.parent / "stage" / sid).exists()
                await service.close()
                service.recover()
                assert (await reader.get(route + "/pages/2")).content == image
                assert (await reader.get(route + "/progress")).json()["data"]["page_index"] == 0
            return {
                "reader_api_first_page_before_completion": True,
                "reader_callback_pending_hidden": True,
                "reader_core_cache_verified": True,
                "reader_native_staging_released": True,
                "reader_cached_pages_after_quiescent_recovery": True,
                "reader_canonical_progress_retained": True,
            }
        finally:
            callback_release.write_text("ok", encoding="utf-8")
            release_second.set()
            await service.close()
            database.close()


async def accept_memory_reader(
    connector: Fmd2Connector,
    runtime: Path,
    chapter: str,
    image: bytes,
    origin: str,
    requests: list[str],
    interrupt_provider: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, object]:
    data = runtime / "memory-core"
    (data / "database").mkdir(parents=True)
    identity = DataRootIdentity.capture(data)
    database = SQLiteDatabase(data / "database/yomira.sqlite3", data_root_identity=identity)
    MigrationRunner(database, data / "backups").upgrade_to_head()
    registry = ConnectorRegistry(
        context=ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(data)),
        registrations=(
            ConnectorRegistration(
                metadata=connector.metadata,
                factory=lambda _c: connector,
                network_origins=frozenset({origin}),
            ),
        ),
    )
    registration = DownloadJobService(database).register_reader_chapter(
        connector=connector.metadata,
        remote_series_id="memory-reader-fixture",
        series_metadata=ConnectorSeriesMetadata(title="Memory Reader"),
        chapters=(
            ConnectorChapter(
                chapter, None, "1", "1", 1, None, None, None, (1,), None, "en", "Chapter 1"
            ),
        ),
        selected_remote_id=chapter,
    )
    budget = DiskBudgetManager(database)

    def forbid_reservation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Online reading must not reserve disk space")

    budget.reserve_bounded_operation = forbid_reservation  # type: ignore[method-assign]
    http = httpx.AsyncHTTPTransport(trust_env=False)
    async with httpx.AsyncClient(transport=http, trust_env=False) as client:
        transport = DownloadTransport(
            client,
            DownloadTransportPolicy(
                allowed_origins=frozenset({NetworkOrigin.from_url(origin)}),
                max_bytes=67_108_864,
                chunk_size=65_536,
                connect_timeout_seconds=2,
                read_timeout_seconds=5,
                write_timeout_seconds=2,
                pool_timeout_seconds=2,
                max_redirects=0,
                max_retry_after_seconds=1,
                global_concurrency=2,
                per_host_concurrency=2,
            ),
            data_root=data,
            data_root_identity=identity,
            storage_gate=database.storage_gate,
            disk_budget_manager=budget,
            test_transport=http,
            secret_header_provider=connector.page_secret_headers,
        )
        service = OnlineReaderSessionService(
            database,
            source_pages=SourcePageService(registry),
            transport=transport,
            image_policy=ImageValidationPolicy(67_108_864, 40_000, 40_000, 100_000_000),
        )
        local = ReaderAssetService(
            database,
            data_root_identity=identity,
            limits=ReaderAssetLimits(2_147_483_648, 67_108_864, 65_536),
        )
        lookup = HybridReaderChapterLookup(local, service)
        app = create_app(Settings(app_env="test", data_root=data))
        state = cast(RuntimeState, app.state.runtime)
        state.database, state.reader_asset_service = database, local
        state.reader_session_service = service
        state.reader_progress_service = ReaderProgressService(
            database, lookup, page_available=lookup.page_available
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as reader:
                route = f"/api/v1/reader/{registration.chapter_id}"
                opened = await reader.post(route + "/session", json={})
                assert opened.status_code == 200, opened.text
                assert opened.json()["data"]["page_count"] == 10_000
                assert opened.json()["data"]["ready_pages"] == []
                assert requests == [], "Opening a plan must not fetch chapter images"
                assert (await reader.get(route)).json()["data"]["page_count"] == 10_000
                assert (
                    await reader.put(route + "/progress", json={"page_index": 9999})
                ).status_code == 422
                sid = opened.json()["data"]["session_id"]
                async with httpx.AsyncClient(base_url=origin, trust_env=False) as anonymous:
                    assert (await anonymous.get(f"/v1/reader/{sid}/pages/1")).status_code == 401
                for ordinal in (1, 10_000):
                    if ordinal == 10_000 and interrupt_provider is not None:
                        assert (
                            await reader.put(route + "/progress", json={"page_index": 0})
                        ).status_code == 200
                        await interrupt_provider()
                        assert (await reader.get(route + "/pages/10000")).status_code == 503
                        assert (
                            await reader.get(route + "/progress")
                        ).json()["data"]["page_index"] == 0
                        reopened = await reader.post(route + "/session", json={"restart": True})
                        assert reopened.status_code == 200, reopened.text
                        assert reopened.json()["data"]["session_id"] != sid
                        assert reopened.json()["data"]["page_count"] == 10_000
                        assert reopened.json()["data"]["ready_pages"] == []
                        assert (
                            await reader.put(route + "/progress", json={"page_index": 9999})
                        ).status_code == 422
                        assert (
                            await reader.get(route + "/progress")
                        ).json()["data"]["page_index"] == 0
                        old = connector.resolve_page(
                            ConnectorPageReference(f"fmd2-online:v1:{sid}:1")
                        )
                        assert old.secret_reference is not None
                        assert (
                            await client.get(
                                old.url, headers=connector.page_secret_headers(old.secret_reference)
                            )
                        ).status_code == 404
                    page = await reader.get(route + f"/pages/{ordinal}")
                    assert page.status_code == 200, page.text
                    assert page.content == image and "no-store" in page.headers["cache-control"]
                assert requests == ["/page/1", "/page/10000"], requests
                assert (await reader.get(route + "/pages/0")).status_code == 404
                assert (await reader.get(route + "/pages/10001")).status_code == 404
                saved = await reader.put(route + "/progress", json={"page_index": 9999})
                assert saved.status_code == 200, saved.text
                assert saved.json()["data"]["percentage"] == "100.00"
                assert not (runtime / "stage").exists()
                assert not (data / "cache/reader").exists()
                assert not list(data.rglob("*.part")) and not list(data.rglob("*.page"))
                assert not list(data.rglob("*.cbz")) and not list(data.rglob("*.png"))
                with database.read_session() as session:
                    assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
                    assert session.scalar(select(func.count()).select_from(ReaderSessionRow)) == 0
                paused = await reader.delete(route + "/session")
                assert paused.status_code == 200
                reopened = await reader.post(route + "/session", json={"restart": True})
                assert reopened.status_code == 200, reopened.text
                assert (await reader.get(route + "/progress")).json()["data"]["page_index"] == 9999
            return {
                "result": "PASS",
                "synthetic_only": True,
                "online_plan_pages": 10_000,
                "image_requests": requests.copy(),
                "online_image_files": 0,
                "online_download_jobs": 0,
                "no_disk_reservation": True,
                "authorization_required": True,
                "no_store": True,
                "progress_survives_reopen": True,
                "provider_restart_recovered": interrupt_provider is not None,
                "last_page_without_full_chapter": True,
                "full_chapter_estimated_bytes": len(image) * 10_000,
            }
        finally:
            await service.close()
            database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--dll-root", required=True, type=Path)
    parser.add_argument("--lua-root", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--memory-reader", action="store_true")
    args = parser.parse_args()
    runtime = args.runtime_root.resolve()
    if os.name != "nt" or runtime.exists() or not runtime.parent.is_dir():
        raise RuntimeError("Requires Windows and a new isolated runtime directory")
    runtime.mkdir()
    shutil.copyfile(args.executable, runtime / "fmd.exe")
    for dependency in args.dll_root.glob("*.dll"):
        shutil.copyfile(dependency, runtime / dependency.name)
    shutil.copyfile(args.dll_root / "7za.exe", runtime / "7za.exe")
    for folder in ("utils", "extras"):
        shutil.copytree(args.lua_root / folder, runtime / "lua" / folder)
    (runtime / "lua/modules").mkdir()
    (runtime / "userdata").mkdir()
    (runtime / "data").mkdir()
    # The fixture's empty catalog prevents the DB updater from contacting a source.
    with sqlite3.connect(runtime / "data" / f"{FMD2_SOURCE_ID}.db") as db:
        db.execute(
            "CREATE TABLE masterlist (link TEXT NOT NULL PRIMARY KEY, title TEXT, "
            "alttitles TEXT, authors TEXT, artists TEXT, genres TEXT, status TEXT, "
            "summary TEXT, numchapter INTEGER, jdn INTEGER)"
        )
    (runtime / "userdata/settings.json").write_text(
        json.dumps(
            {
                "general": {
                    "OneInstanceOnly": False,
                    "MinimizeOnStart": True,
                    "MinimizeToTray": True,
                },
                "update": {
                    "AutoCheckLatestVersion": False,
                    "AutoCheckFavStartup": False,
                    "AutoCheckFavInterval": False,
                },
                "logger": {"Enabled": True, "LogFileName": "fixture.log"},
            }
        ),
        encoding="utf-8",
    )
    buffer = io.BytesIO()
    if args.memory_reader:
        Image.effect_noise((512, 512), 100).save(buffer, format="PNG")
    else:
        Image.new("RGB", (8, 12), "white").save(buffer, format="PNG")
    image = buffer.getvalue()
    callback_marker = runtime / "callback_pending"
    callback_release = runtime / "release_callback"
    release_second = threading.Event()
    image_requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if args.memory_reader and self.path.startswith("/page/"):
                if self.headers.get("Referer") != "https://www.webtoons.com/":
                    self.send_error(403)
                    return
                image_requests.append(self.path)
                body, content_type = image, "image/png"
            elif self.path == "/page/2":
                if not release_second.wait(30):
                    self.send_error(503)
                    return
                body, content_type = image, "image/png"
            elif self.path == "/page/1":
                body, content_type = image, "image/png"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *arguments: object) -> None:
            pass

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fixture.daemon_threads = True
    fixture_origin = f"http://127.0.0.1:{fixture.server_port}"
    (runtime / "lua/modules/WebToons.lua").write_text(
        f"""
function GetPageNumber()
  TASK.PageLinks.Add('{fixture_origin}/page/1')
  TASK.PageLinks.Add('{fixture_origin}/page/2')
  return true
end
function AfterImageSaved()
  if FILENAME:match('001.png$') then
    local marker = assert(io.open('{callback_marker.as_posix()}', 'w'))
    marker:write('pending')
    marker:close()
    for i = 1, 1200 do
      local release = io.open('{callback_release.as_posix()}', 'r')
      if release then release:close() return true end
      sleep(25)
    end
    return false
  end
  return true
end
function Init()
  local m = NewWebsiteModule()
  m.ID = '{FMD2_SOURCE_ID}'
  m.Name = 'WebToons'
  m.Category = 'Synthetic test only'
  m.RootURL = 'https://www.webtoons.com/'
  m.OnGetPageNumber = 'GetPageNumber'
  m.OnAfterImageSaved = 'AfterImageSaved'
end
""",
        encoding="utf-8",
    )
    if args.memory_reader:
        (runtime / "lua/modules/WebToons.lua").write_text(
            f"""
function GetPageNumber()
  TASK.PageNumber = URL:match('small') and 3 or 10000
  return true
end
function GetImageURL()
  TASK.PageLinks[WORKID] = '{fixture_origin}/page/' .. (WORKID + 1)
  return true
end
function BeforeDownloadImage()
  HTTP.Headers.Values['Referer'] = MODULE.RootURL
  return true
end
function Init()
  local m = NewWebsiteModule()
  m.ID = '{FMD2_SOURCE_ID}'
  m.Name = 'WebToons'
  m.RootURL = 'https://www.webtoons.com/'
  m.OnGetPageNumber = 'GetPageNumber'
  m.OnGetImageURL = 'GetImageURL'
  m.OnBeforeDownloadImage = 'BeforeDownloadImage'
end
""",
            encoding="utf-8",
        )
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        api_port = reservation.getsockname()[1]
    token = secrets.token_urlsafe(48)
    origin = f"http://127.0.0.1:{api_port}"
    environment = os.environ.copy()
    environment.update(
        YOMIRA_FMD2_API_TOKEN=token,
        YOMIRA_FMD2_API_PORT=str(api_port),
        YOMIRA_FMD2_STAGING_ROOT=str(runtime / "stage"),
    )
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    server_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    server_thread.start()
    process = subprocess.Popen(
        [str(runtime / "fmd.exe"), "--lua-dofile"],
        cwd=runtime,
        env=environment,
        startupinfo=startup,
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    async def exercise() -> dict[str, object]:
        job = uuid4()
        async with httpx.AsyncClient(
            base_url=origin,
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            follow_redirects=False,
            timeout=5,
        ) as control:
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Fixture motor exited during startup")
                try:
                    response = await control.get("/v1/sources")
                    if response.status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("Fixture API did not start")
                await asyncio.sleep(0.1)
            connector = Fmd2Connector(runtime / "fmd.exe", runtime / "stage", api_client=control)
            chapter = "yomira-fmd2:v1:" + base64.urlsafe_b64encode(
                json.dumps(["/en/fixture/list", "/en/fixture/chapter", 1]).encode()
            ).decode().rstrip("=")
            if args.memory_reader:
                try:
                    result = await accept_memory_reader(
                        connector, runtime, chapter, image, origin, image_requests
                    )
                    small = "yomira-fmd2:v1:" + base64.urlsafe_b64encode(
                        json.dumps(["/en/fixture/list", "/en/fixture/small", 2]).encode()
                    ).decode().rstrip("=")
                    pages = await asyncio.wait_for(connector.acquire_pages(job, small, "Small"), 15)
                    assert len(pages) == 3
                    assert len(list((runtime / "stage" / str(job)).glob("*.png"))) == 3
                    await connector.release_reader_staging(job, restored=True)
                    result["explicit_download_still_writes_verified_source_pages"] = True
                    return result
                finally:
                    await control.post("/v1/shutdown")
            acquisition = asyncio.create_task(connector.acquire_pages(job, chapter, "Synthetic"))
            try:
                deadline = time.monotonic() + 15
                while not callback_marker.exists():
                    if acquisition.done():
                        await acquisition
                        raise RuntimeError("Acquisition ended before the post-save callback")
                    assert time.monotonic() < deadline, "Post-save callback not reached"
                    await asyncio.sleep(0.1)
                assert (runtime / "stage" / str(job) / "001.png").is_file()
                before = await connector.get_ready_pages(job)
                assert before.total_pages == 2 and not before.pages
                route = f"/v1/downloads/{job}/ready/{before.generation}/files/001.png"
                assert (await control.get(route)).status_code == 404, (
                    "Callback is still writing/processing"
                )
                assert (await control.get(f"/v1/downloads/{job}/files/001.png")).status_code == 404
                callback_release.write_text("ok", encoding="utf-8")
                deadline = time.monotonic() + 10
                ready = before
                while not ready.pages:
                    ready = await connector.get_ready_pages(job, ready)
                    assert time.monotonic() < deadline, "First page was not published"
                    await asyncio.sleep(0.05)
                assert [ordinal for ordinal, _ in ready.pages] == [1] and not acquisition.done()
                async with httpx.AsyncClient(base_url=origin, trust_env=False) as unauthorized:
                    assert (await unauthorized.get(route)).status_code == 401
                stale = route.replace(str(ready.generation), str(uuid4()))
                assert (await control.get(stale)).status_code == 404
                assert (await control.get(route.replace("001.png", "002.png"))).status_code == 404
                data = runtime / "core"
                data.mkdir()
                http = httpx.AsyncHTTPTransport(trust_env=False)
                async with httpx.AsyncClient(transport=http, trust_env=False) as client:
                    transport = DownloadTransport(
                        client,
                        DownloadTransportPolicy(
                            allowed_origins=frozenset({NetworkOrigin.from_url(origin)}),
                            max_bytes=1024,
                            chunk_size=64,
                            connect_timeout_seconds=2,
                            read_timeout_seconds=2,
                            write_timeout_seconds=2,
                            pool_timeout_seconds=2,
                            max_redirects=0,
                            max_retry_after_seconds=1,
                            global_concurrency=1,
                            per_host_concurrency=1,
                        ),
                        data_root=data,
                        data_root_identity=DataRootIdentity.capture(data),
                        test_transport=http,
                        secret_header_provider=connector.page_secret_headers,
                    )
                    downloaded = await transport.download(
                        connector.resolve_page(ready.pages[0][1]), job_id=job, ordinal=1, attempt=1
                    )
                    valid = validate_image_file(
                        downloaded.path,
                        downloaded.content_type or "",
                        policy=ImageValidationPolicy(1024, 100, 100, 10_000),
                    )
                    assert valid.sha256 == downloaded.sha256 and not acquisition.done()
                assert await connector.connection_available()
                waiter_id = uuid4()
                waiter = asyncio.create_task(
                    connector.acquire_pages(waiter_id, chapter, "Waiting chapter")
                )
                try:
                    await asyncio.sleep(0.05)
                    waiting_status = await control.get(f"/v1/downloads/{waiter_id}")
                    assert waiting_status.status_code == 404 and not waiter.done()
                    assert waiting_status.json() == {"error": {"code": "not_found"}}
                    assert not (runtime / "stage" / str(waiter_id)).exists()
                    observed = await connector.get_ready_pages(job, ready)
                    assert observed == ready and not acquisition.done()
                finally:
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
                assert await connector.get_ready_pages(job, ready) == ready
                release_second.set()
                pages = await asyncio.wait_for(acquisition, 15)
                completed = await connector.get_ready_pages(job, ready)
                assert len(pages) == 2 and completed.is_complete
                cache_report = await accept_reader_cache(
                    connector,
                    data,
                    chapter,
                    image,
                    origin,
                    callback_marker,
                    callback_release,
                    release_second,
                )
                return {
                    **cache_report,
                    "result": "PASS",
                    "synthetic_only": True,
                    "first_page_before_completion": True,
                    "connection_probe_preserves_active_reader": True,
                    "second_acquisition_waits_without_native_task": True,
                    "cancelled_waiter_preserves_active_reader": True,
                    "callback_pending_hidden": True,
                    "authorization_required": True,
                    "stale_generation_rejected": True,
                    "pending_page_hidden": True,
                    "page_count": 2,
                    "first_page_sha256": valid.sha256,
                }
            finally:
                callback_release.write_text("ok", encoding="utf-8")
                release_second.set()
                if not acquisition.done():
                    acquisition.cancel()
                await asyncio.gather(acquisition, return_exceptions=True)
                with suppress(httpx.RequestError):
                    await control.post("/v1/shutdown")

    report: dict[str, object] | None = None
    forced_shutdown = False
    try:
        report = asyncio.run(exercise())
    finally:
        callback_release.write_text("ok", encoding="utf-8")
        release_second.set()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            forced_shutdown = True
            process.terminate()
            process.wait(timeout=5)
        fixture.shutdown()
        fixture.server_close()
        server_thread.join(timeout=5)
    assert report is not None
    report.update(native_exit_code=process.returncode, forced_shutdown=forced_shutdown)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    assert process.returncode == 0 and not forced_shutdown, "Native shutdown did not finish cleanly"
    print(json.dumps(report))


if __name__ == "__main__":
    main()
