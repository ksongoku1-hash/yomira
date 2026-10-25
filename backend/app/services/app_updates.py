"""GitHub releases updater for the per-user Windows installation."""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import httpx

from app.version import APP_VERSION

REPOSITORY = "ksongoku1-hash/yomira"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_INSTALLER_BYTES = 1024 * 1024 * 1024
logger = logging.getLogger("yomira.updates")

# Run outside the installation: the executable and its DLLs must be unlocked first.
INSTALL_SCRIPT = r"""
param([int]$ParentId, [string]$Installer, [string]$ExpectedHash,
      [string]$Executable, [string]$Result)
$ErrorActionPreference = 'Stop'
$restart = $false
try {
    $parent = Get-Process -Id $ParentId -ErrorAction SilentlyContinue
    if ($null -ne $parent -and -not $parent.WaitForExit(120000)) {
        throw 'Yomira no termino de cerrar. No se reemplazo ningun archivo.'
    }
    $restart = $true
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Installer)
    try {
        $bytes = $sha.ComputeHash($stream)
        $hash = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
    } finally { $stream.Dispose(); $sha.Dispose() }
    if ($hash -ne $ExpectedHash) {
        throw 'El instalador cambio despues de su verificacion.'
    }
    $directory = Split-Path -LiteralPath $Executable
    $arguments = @('/SP-', '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        ('/DIR="' + $directory + '"'), ('/LOG="' + $Installer + '.log"'))
    $launch = @{FilePath=$Installer; ArgumentList=$arguments; WindowStyle='Hidden';
        PassThru=$true; Wait=$true}
    $setup = Start-Process @launch
    if ($setup.ExitCode -ne 0) { throw ('El instalador termino con codigo ' + $setup.ExitCode) }
    @{ok=$true; message='Yomira se actualizo correctamente.'} | ConvertTo-Json |
        Set-Content -LiteralPath $Result -Encoding UTF8
} catch {
    @{ok=$false; message=$_.Exception.Message} | ConvertTo-Json |
        Set-Content -LiteralPath $Result -Encoding UTF8
} finally {
    if ($restart -and (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        $launch = @{FilePath=$Executable; WorkingDirectory=(Split-Path -LiteralPath $Executable);
            WindowStyle='Hidden'}
        Start-Process @launch
    }
}
"""


def installed_executable() -> Path | None:
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return None
    import winreg

    executable = Path(sys.executable).resolve()
    key_name = (
        "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\"
        "{10ECACF2-F35D-4E4E-A962-6F4E7B11E01E}_is1"
    )
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            key_name,
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            location, _ = winreg.QueryValueEx(key, "InstallLocation")
        if (
            executable.name.lower() == "yomira.exe"
            and Path(location).resolve() == executable.parent
        ):
            return executable
    except OSError, TypeError, ValueError:
        pass
    return None


def version_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"v?(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})", value):
        raise ValueError("La version publicada no es una version estable valida.")
    return tuple(int(part) for part in value.removeprefix("v").split("."))


def release_installer(release: object) -> dict[str, str | int] | None:
    if not isinstance(release, dict):
        raise ValueError("GitHub no devolvio una version publicada.")
    fields = cast(dict[str, object], release)
    if fields.get("draft") is not False or fields.get("prerelease") is not False:
        raise ValueError("GitHub no devolvio una version estable publicada.")
    tag = fields.get("tag_name")
    if not isinstance(tag, str):
        raise ValueError("Falta la version publicada.")
    if version_tuple(tag) <= version_tuple(APP_VERSION):
        return None
    version = tag.removeprefix("v")
    name = f"Yomira-Setup-{version}.exe"
    expected_url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
    assets = fields.get("assets")
    if not isinstance(assets, list):
        raise ValueError("Falta el instalador de la version publicada.")
    matches = [
        cast(dict[str, object], asset)
        for asset in cast(list[object], assets)
        if isinstance(asset, dict) and cast(dict[str, object], asset).get("name") == name
    ]
    if len(matches) != 1:
        raise ValueError("La version publicada no contiene un unico instalador compatible.")
    asset = matches[0]
    digest, size = asset.get("digest"), asset.get("size")
    if (
        asset.get("browser_download_url") != expected_url
        or asset.get("state") != "uploaded"
        or not isinstance(digest, str)
        or not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest)
        or type(size) is not int
        or not 0 < size <= MAX_INSTALLER_BYTES
    ):
        raise ValueError("El instalador no tiene un origen, tamano o SHA-256 valido.")
    return {
        "version": version,
        "name": name,
        "url": expected_url,
        "sha256": digest[7:].lower(),
        "size": size,
    }


def validate_download_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname
        not in {
            "github.com",
            "release-assets.githubusercontent.com",
            "objects.githubusercontent.com",
        }
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise ValueError("GitHub redirigio la descarga a un origen no permitido.")


class AppUpdater:
    def __init__(self, data_root: Path) -> None:
        self.executable = installed_executable()
        self.directory = data_root / "updates"
        self.state = "idle" if self.executable else "unsupported"
        self.message = (
            ""
            if self.executable
            else ("Las actualizaciones automaticas requieren la version instalada de Windows.")
        )
        self.latest_version: str | None = None
        self.downloaded = 0
        self.total = 0
        self.asset: dict[str, str | int] | None = None
        self.task: asyncio.Task[None] | None = None
        self.loop_task: asyncio.Task[None] | None = None
        self.last_check = 0.0
        try:
            result = json.loads((self.directory / "result.json").read_text(encoding="utf-8-sig"))
            if result.get("ok") is False:
                self.state = "error" if self.executable else "unsupported"
                self.message = (
                    "La instalacion anterior no se completo. Puedes reintentar desde aqui."
                )
        except OSError, ValueError, AttributeError:
            pass

    def status(self) -> dict[str, object]:
        return {
            "state": self.state,
            "current_version": APP_VERSION,
            "latest_version": self.latest_version,
            "downloaded_bytes": self.downloaded,
            "total_bytes": self.total,
            "message": self.message,
            "releases_url": RELEASES_URL,
        }

    def start(self) -> None:
        if self.executable is not None:
            self.loop_task = asyncio.create_task(self._periodic_check())

    async def _periodic_check(self) -> None:
        await asyncio.sleep(10)
        while True:
            self.check()
            await asyncio.sleep(6 * 60 * 60)

    async def close(self) -> None:
        tasks = [task for task in (self.loop_task, self.task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def check(self) -> None:
        if self.executable is None or self.state in {"ready", "installing"}:
            return
        now = asyncio.get_running_loop().time()
        if (self.task is not None and not self.task.done()) or now - self.last_check < 60:
            return
        self.last_check = now
        self.downloaded = self.total = 0
        self.state, self.message = "checking", "Buscando una nueva version…"
        self.task = asyncio.create_task(self._check_and_download())

    async def _check_and_download(self) -> None:
        partial: Path | None = None
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30, connect=10),
                trust_env=False,
                headers={
                    "User-Agent": f"Yomira/{APP_VERSION}",
                    "Accept": "application/vnd.github+json",
                },
            ) as client:
                async with client.stream("GET", LATEST_URL) as response:
                    if response.status_code == 404:
                        self.state, self.message = (
                            "up_to_date",
                            "No hay una version nueva publicada.",
                        )
                        return
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1024 * 1024:
                            raise ValueError("La respuesta de GitHub es demasiado grande.")
                self.asset = release_installer(json.loads(body))
                if self.asset is None:
                    self.state, self.message = "up_to_date", "Yomira esta actualizado."
                    return
                asset = self.asset
                self.latest_version = str(asset["version"])
                self.total, self.downloaded = int(asset["size"]), 0
                self.directory.mkdir(parents=True, exist_ok=True)
                destination = self.directory / str(asset["name"])
                partial = destination.with_suffix(".part")
                if destination.is_file() and await asyncio.to_thread(self._verified, destination):
                    self.downloaded = self.total
                else:
                    if shutil.disk_usage(self.directory).free < self.total * 2 + 50 * 1024 * 1024:
                        raise ValueError(
                            "No hay suficiente espacio para preparar la actualizacion."
                        )
                    self.state, self.message = (
                        "downloading",
                        "Descargando actualizacion en segundo plano…",
                    )
                    await self._download(client, str(asset["url"]), partial)
                    if not await asyncio.to_thread(self._verified, partial):
                        raise ValueError(
                            "La descarga no coincide con el SHA-256 publicado por GitHub."
                        )
                    partial.replace(destination)
                # Keep only the current installer; never touch library or reading data.
                for previous in self.directory.glob("Yomira-Setup-*.exe"):
                    if previous != destination and re.fullmatch(
                        r"Yomira-Setup-\d+\.\d+\.\d+\.exe", previous.name
                    ):
                        previous.unlink(missing_ok=True)
                self.state, self.message = "ready", "Actualizacion lista para instalar."
        except OSError, ValueError, httpx.HTTPError:
            logger.exception("updates.check_failed")
            self.state, self.message = (
                "error",
                (
                    "No se pudo preparar la actualizacion. "
                    "Comprueba tu conexion y vuelve a intentarlo."
                ),
            )
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)

    async def _download(self, client: httpx.AsyncClient, url: str, partial: Path) -> None:
        for _ in range(6):
            validate_download_url(url)
            async with client.stream(
                "GET", url, headers={"Accept": "application/octet-stream"}
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("GitHub devolvio una redireccion sin destino.")
                    url = str(response.url.join(location))
                    continue
                response.raise_for_status()
                with partial.open("wb") as output:
                    async for chunk in response.aiter_bytes(256 * 1024):
                        if self.downloaded + len(chunk) > self.total:
                            raise ValueError("El instalador supera el tamano publicado.")
                        self.downloaded += len(chunk)
                        output.write(chunk)
                return
        raise ValueError("Demasiadas redirecciones al descargar la actualizacion.")

    def _verified(self, path: Path) -> bool:
        if self.asset is None or not path.is_file() or path.stat().st_size != self.total:
            return False
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest() == self.asset["sha256"]

    async def install(self) -> None:
        if self.state != "ready" or self.asset is None or self.executable is None:
            raise ValueError("La actualizacion todavia no esta lista.")
        self.state = "installing"
        installer = self.directory / str(self.asset["name"])
        try:
            if installed_executable() != self.executable or not await asyncio.to_thread(
                self._verified, installer
            ):
                raise ValueError(
                    "La instalacion o el archivo cambiaron. Busca la actualizacion otra vez."
                )
            script = self.directory / "install-update.ps1"
            script.write_text(INSTALL_SCRIPT, encoding="utf-8-sig")
            result = self.directory / "result.json"
            result.unlink(missing_ok=True)
            powershell = (
                Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            )
            subprocess.Popen(
                [
                    str(powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-ParentId",
                    str(os.getpid()),
                    "-Installer",
                    str(installer),
                    "-ExpectedHash",
                    str(self.asset["sha256"]),
                    "-Executable",
                    str(self.executable),
                    "-Result",
                    str(result),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            self.message = "Cerrando Yomira para instalar la actualizacion…"
        except OSError, ValueError, KeyError:
            self.state, self.message = (
                "error",
                "No se pudo iniciar el instalador. Vuelve a buscar la actualizacion.",
            )
            raise
