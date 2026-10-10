from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any


def deterministic_windows_root_adapters() -> dict[str, Callable[..., Any]]:
    """Return the same fake Win32 volume adapters used by the test fixture."""

    def get_volume_path_name(_path: Path) -> str:
        return "C:\\"

    def get_drive_type(_volume_root: str) -> int:
        return 3  # DRIVE_FIXED

    def get_file_id_info(path: Path) -> tuple[int, bytes]:
        canonical_path = Path(path).resolve()
        digest = hashlib.sha256(
            str(canonical_path).casefold().encode("utf-8")
        ).digest()
        return 0x594F4D495241, digest[:16]

    return {
        "_get_volume_path_name": get_volume_path_name,
        "_get_drive_type": get_drive_type,
        "_get_file_id_info": get_file_id_info,
    }


def install_deterministic_windows_root_adapters() -> None:
    """Install the deterministic adapters in a spawned test process."""
    from app.persistence import windows_paths

    for name, adapter in deterministic_windows_root_adapters().items():
        setattr(windows_paths, name, adapter)
