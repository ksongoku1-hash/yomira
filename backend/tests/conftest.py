from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def deterministic_windows_root_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make test roots appear as stable local volumes without Win32 calls."""
    try:
        from app.persistence import windows_paths
    except ModuleNotFoundError as error:
        if error.name == "app.persistence.windows_paths":
            return
        raise

    from support.windows_paths import deterministic_windows_root_adapters

    for name, adapter in deterministic_windows_root_adapters().items():
        monkeypatch.setattr(windows_paths, name, adapter)
