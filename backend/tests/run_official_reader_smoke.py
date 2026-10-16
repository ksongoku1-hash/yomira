"""Bounded official-source probe with the unchanged native adapter and RAM transport."""

# Standalone acceptance adjusts sys.path before importing application modules.
# ruff: noqa: E402
import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "backend"))
import httpx

from app.connectors.errors import SourceConnectorError
from app.connectors.fmd2 import FMD2_SOURCE_ID, Fmd2Connector
from app.fmd2_bridge import fmd2_stable_chapter_reference
from app.persistence.windows_paths import DataRootIdentity
from app.services.download_transport import (
    DownloadTransport,
    DownloadTransportPolicy,
    NetworkOrigin,
)
from app.services.image_validation import ImageValidationPolicy, validate_image_bytes

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--motor", type=Path, required=True)
parser.add_argument("--runtime-root", type=Path, required=True)
parser.add_argument("--report", type=Path, required=True)
args = parser.parse_args()
motor = args.motor.resolve(strict=True)
runtime = args.runtime_root.resolve()
assert os.name == "nt" and runtime.parent.is_dir()
assert not runtime.exists()
runtime.mkdir()
pin = json.loads((root / "third_party/fmd2/upstream.json").read_text())
assert (
    hashlib.sha256((motor / "fmd.exe").read_bytes()).hexdigest()
    == pin["reference_executable_sha256"]
)
for item in [motor / "fmd.exe", motor / "7za.exe", *motor.glob("*.dll")]:
    shutil.copyfile(item, runtime / item.name)
for folder in ["utils", "extras"]:
    shutil.copytree(motor / "lua" / folder, runtime / "lua" / folder)
(runtime / "lua/modules").mkdir()
shutil.copyfile(motor / "lua/modules/WebToons.lua", runtime / "lua/modules/WebToons.lua")
(runtime / "userdata").mkdir()
(runtime / "data").mkdir()
with sqlite3.connect(runtime / "data" / f"{FMD2_SOURCE_ID}.db") as db:
    db.execute(
        "CREATE TABLE masterlist (link TEXT NOT NULL PRIMARY KEY, title TEXT, "
        "alttitles TEXT, authors TEXT, artists TEXT, genres TEXT, status TEXT, "
        "summary TEXT, numchapter INTEGER, jdn INTEGER)"
    )
(runtime / "userdata/settings.json").write_text(
    json.dumps(
        {
            "general": {"OneInstanceOnly": False, "MinimizeOnStart": True, "MinimizeToTray": True},
            "update": {
                "AutoCheckLatestVersion": False,
                "AutoCheckFavStartup": False,
                "AutoCheckFavInterval": False,
            },
        }
    ),
    encoding="utf-8",
)
with socket.socket() as reservation:
    reservation.bind(("127.0.0.1", 0))
    port = reservation.getsockname()[1]
baseline_images = {
    p.relative_to(runtime).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in runtime.rglob("*")
    if p.suffix.lower() in {".png", ".jpg", ".webp"}
}
origin = f"http://127.0.0.1:{port}"
token = secrets.token_urlsafe(48)
environment = os.environ.copy()
environment.update(
    YOMIRA_FMD2_API_PORT=str(port),
    YOMIRA_FMD2_API_TOKEN=token,
    YOMIRA_FMD2_STAGING_ROOT=str(runtime / "stage"),
)
startup = subprocess.STARTUPINFO()
startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
startup.wShowWindow = subprocess.SW_HIDE
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
url = "https://www.webtoons.com/en/romance/lore-olympus/episode-1/viewer?title_no=1320&episode_no=1"
proof: dict[str, object] = {
    "result": "BLOCKED",
    "source": "WebToons",
    "chapter_url": url,
    "adapter_modified": False,
    "synthetic_pages": False,
    "images_written": 0,
    "download_jobs_created": 0,
    "native_sha256": pin["reference_executable_sha256"],
}


async def exercise() -> None:
    async def observe(response: httpx.Response) -> None:
        if response.request.url.path == "/v1/reader":
            await response.aread()
            assert len(response.content) < 65536
            proof["native_plan_http_status"] = response.status_code
            if response.status_code != 200:
                proof["native_plan_error"] = response.json().get("error", {}).get("code")

    async with httpx.AsyncClient(
        base_url=origin,
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        timeout=30,
        follow_redirects=False,
        event_hooks={"response": [observe]},
    ) as control:
        connector = Fmd2Connector(runtime / "fmd.exe", runtime / "stage", api_client=control)
        sid = uuid4()
        try:
            for _ in range(100):
                assert process.poll() is None
                try:
                    if (await control.get("/v1/sources", timeout=1)).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("Isolated provider did not become ready")
            chapter = "yomira-fmd2:v1:" + base64.urlsafe_b64encode(
                json.dumps(
                    [
                        "/en/romance/lore-olympus/list?title_no=1320",
                        "/en/romance/lore-olympus/episode-1/viewer?title_no=1320&episode_no=1",
                        1,
                    ]
                ).encode()
            ).decode().rstrip("=")
            chapter = fmd2_stable_chapter_reference(chapter)
            proof["reference_version"] = "v3"
            proof["phase"] = "plan"
            pages = await connector.open_online_pages(sid, chapter)
            proof["page_count"] = len(pages)
            assert 1 <= len(pages) <= 10000
            assert not (runtime / "stage").exists()
            async with httpx.AsyncClient(trust_env=False) as anonymous:
                assert (
                    await anonymous.get(origin + f"/v1/reader/{sid}/pages/1")
                ).status_code == 401
            data = runtime / "ram-reader"
            data.mkdir()
            http = httpx.AsyncHTTPTransport(trust_env=False)
            async with httpx.AsyncClient(transport=http, trust_env=False) as client:
                transport = DownloadTransport(
                    client,
                    DownloadTransportPolicy(
                        allowed_origins=frozenset({NetworkOrigin.from_url(origin)}),
                        max_bytes=67_108_864,
                        chunk_size=65_536,
                        connect_timeout_seconds=2,
                        read_timeout_seconds=30,
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
                proof["phase"] = "first_page"
                page = await transport.fetch_memory(
                    connector.resolve_page(pages[0]),
                    session_id=sid,
                    ordinal=1,
                    allowed_origins=frozenset({NetworkOrigin.from_url(origin)}),
                )
                image = validate_image_bytes(
                    page.body,
                    page.content_type,
                    policy=ImageValidationPolicy(67_108_864, 40_000, 40_000, 100_000_000),
                )
                proof.update(
                    result="PASS",
                    first_image_bytes=len(page.body),
                    first_image_mime=image.mime,
                    first_image_sha256=image.sha256,
                    authorization_required=True,
                )
        except SourceConnectorError as error:
            proof.update(error_code=error.code.value, retryable=error.retryable)
        except Exception as error:
            proof.update(error_type=type(error).__name__)
        finally:
            try:
                await connector.close_online_pages(sid)
            finally:
                await control.post("/v1/shutdown", timeout=10)


try:
    asyncio.run(exercise())
finally:
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        assert process.args == [str(runtime / "fmd.exe"), "--lua-dofile"]
        process.terminate()
        process.wait(timeout=5)
    assert {
        p.relative_to(runtime).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in runtime.rglob("*")
        if p.suffix.lower() in {".png", ".jpg", ".webp"}
    } == baseline_images
    assert not list(runtime.rglob("*.page")) and not list(runtime.rglob("*.part"))
    assert not list(runtime.rglob("*.cbz"))
    proof["native_exit_code"] = process.returncode
    args.report.write_text(json.dumps(proof, indent=2), encoding="utf-8")
    if process.returncode != 0:
        proof["result"] = "FAIL"
        args.report.write_text(json.dumps(proof, indent=2), encoding="utf-8")
    print(json.dumps(proof))
    raise SystemExit(0 if proof["result"] == "PASS" else 1)
