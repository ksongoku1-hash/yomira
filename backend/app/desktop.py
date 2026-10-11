"""Open the local web interface when the Windows executable is launched."""

import json
import os
import webbrowser
from pathlib import Path
from time import monotonic, sleep
from typing import cast
from urllib.error import URLError
from urllib.request import urlopen

from app.version import API_VERSION


def configure_bundled_motor(executable: Path) -> None:
    motor = executable.parent / "Motor" / "fmd.exe"
    if "YOMIRA_FMD2_EXECUTABLE" not in os.environ and motor.is_file():
        os.environ["YOMIRA_FMD2_EXECUTABLE"] = str(motor)


def interface_is_ready(origin: str) -> bool:
    try:
        with urlopen(f"{origin}/api/v1/system/version", timeout=1) as response:
            version = json.load(response)
        with urlopen(f"{origin}/api/v1/system/readiness", timeout=1) as response:
            readiness = json.load(response)
        if not isinstance(version, dict) or not isinstance(readiness, dict):
            return False
        version_fields = cast(dict[str, object], version)
        readiness_fields = cast(dict[str, object], readiness)
        return (
            version_fields.get("api_version") == API_VERSION
            and isinstance(version_fields.get("app_version"), str)
            and readiness_fields.get("status") == "ready"
        )
    except (OSError, URLError, ValueError):
        return False


def open_when_ready(origin: str, timeout: float = 90) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if interface_is_ready(origin):
            webbrowser.open(f"{origin}/library")
            return
        sleep(0.3)
    print(f"No se pudo abrir el navegador. Revisa los mensajes y entra en {origin}/library.")
