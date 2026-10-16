"""Restart an owned Windows provider against a synthetic, isolated loopback source."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from unittest.mock import patch

from PIL import Image
from run_h2a_native_progressive_smoke import accept_memory_reader

from app.connectors.fmd2 import FMD2_SOURCE_ID, Fmd2Connector


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--motor", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    runtime = args.runtime_root.resolve()
    if os.name != "nt" or runtime.exists() or not runtime.parent.is_dir():
        raise RuntimeError("Requires Windows and a new isolated runtime directory")
    motor = args.motor.resolve(strict=True)
    pin = json.loads((Path(__file__).parents[2] / "third_party/fmd2/upstream.json").read_text())
    digest = hashlib.sha256((motor / "fmd.exe").read_bytes()).hexdigest()
    assert digest == pin["reference_executable_sha256"]
    runtime.mkdir()
    shutil.copyfile(motor / "fmd.exe", runtime / "fmd.exe")
    for dependency in motor.glob("*.dll"):
        shutil.copyfile(dependency, runtime / dependency.name)
    shutil.copyfile(motor / "7za.exe", runtime / "7za.exe")
    for folder in ("utils", "extras"):
        shutil.copytree(motor / "lua" / folder, runtime / "lua" / folder)
    (runtime / "lua/modules").mkdir()
    (runtime / "userdata").mkdir()
    (runtime / "data").mkdir()
    with sqlite3.connect(runtime / "data" / f"{FMD2_SOURCE_ID}.db") as db:
        db.execute(
            "CREATE TABLE masterlist (link TEXT NOT NULL PRIMARY KEY, title TEXT, "
            "alttitles TEXT, authors TEXT, artists TEXT, genres TEXT, status TEXT, "
            "summary TEXT, numchapter INTEGER, jdn INTEGER)"
        )
    (runtime / "userdata/settings.json").write_text(json.dumps({
        "general": {"OneInstanceOnly": False, "MinimizeOnStart": True, "MinimizeToTray": True},
        "update": {"AutoCheckLatestVersion": False, "AutoCheckFavStartup": False,
                   "AutoCheckFavInterval": False},
    }), encoding="utf-8")
    buffer = io.BytesIO()
    Image.new("RGB", (8, 12), "white").save(buffer, format="PNG")
    image = buffer.getvalue()
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in {"/page/1", "/page/10000"}:
                self.send_error(404)
                return
            if self.headers.get("Referer") != "https://www.webtoons.com/":
                self.send_error(403)
                return
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(image)))
            self.end_headers()
            self.wfile.write(image)

        def log_message(self, format: str, *arguments: object) -> None:
            pass

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fixture.daemon_threads = True
    fixture_origin = f"http://127.0.0.1:{fixture.server_port}"
    (runtime / "lua/modules/WebToons.lua").write_text(f"""
function GetPageNumber()
  TASK.PageNumber = 10000
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
""", encoding="utf-8")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    processes: list[subprocess.Popen[bytes]] = []
    original_popen = subprocess.Popen

    def launch(command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        assert command == [str(runtime / "fmd.exe")]
        assert kwargs["cwd"] == runtime
        assert isinstance(kwargs["env"], dict)
        environment = dict(cast(Mapping[str, str], kwargs["env"]))
        assert environment["YOMIRA_FMD2_STAGING_ROOT"] == str(runtime / "stage")
        environment["YOMIRA_FMD2_API_PORT"] = str(port)
        kwargs["env"] = environment
        process = original_popen([*command, "--lua-dofile"], **kwargs)  # type: ignore[arg-type]
        processes.append(process)
        return process

    async def exercise() -> dict[str, object]:
        connector = Fmd2Connector(runtime / "fmd.exe", runtime / "stage")
        try:
            await connector.start()

            async def interrupt() -> None:
                assert len(processes) == 1
                process = processes[0]
                assert process.args == [str(runtime / "fmd.exe"), "--lua-dofile"]
                assert process.poll() is None
                process.kill()
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
                assert process.returncode != 0

            chapter = "yomira-fmd2:v1:" + base64.urlsafe_b64encode(
                json.dumps(["/en/fixture/list", "/en/fixture/chapter", 1]).encode()
            ).decode().rstrip("=")
            result = await accept_memory_reader(
                connector, runtime, chapter, image, origin, requests, interrupt
            )
            assert len(processes) == 2 and processes[0].pid != processes[1].pid
            result.update(native_sha256=digest, owned_provider_restart=True,
                          old_pid=processes[0].pid, recovered_pid=processes[1].pid)
            return result
        finally:
            await connector.close()

    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    try:
        with patch("app.fmd2_bridge.FMD2_API_ORIGIN", origin), \
                patch("app.fmd2_bridge.subprocess.Popen", launch):
            result = asyncio.run(exercise())
        assert len(processes) == 2 and all(p.poll() is not None for p in processes)
        assert processes[1].returncode == 0
        result.update(recovered_native_exit_code=0, synthetic_only=True,
                      production_provider_modified=False)
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result))
    finally:
        fixture.shutdown()
        fixture.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
