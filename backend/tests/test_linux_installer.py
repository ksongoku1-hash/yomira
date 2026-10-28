import importlib.util
import subprocess
from pathlib import Path

import pytest


def test_desktop_installation_uses_only_user_paths_and_quotes_special_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = Path(__file__).resolve().parents[2] / "scripts/install-linux-desktop.py"
    spec = importlib.util.spec_from_file_location("linux_installer", path)
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    root = tmp_path / 'Mangas $HOME %i'
    monkeypatch.setattr(installer, "ROOT", root)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    calls: list[list[str]] = []

    def run(command: list[str], *, check: bool) -> None:
        assert check
        calls.append(command)

    monkeypatch.setattr(subprocess, "run", run)
    installer.install()
    unit = (tmp_path / "config/systemd/user/yomira.service").read_text(encoding="utf-8")
    desktop = (tmp_path / "data/applications/yomira.desktop").read_text(encoding="utf-8")
    assert 'ExecStart="' in unit and '$$HOME %%i' in unit
    assert 'Exec="' in desktop and r'\$HOME %%i' in desktop
    assert "Terminal=false" in desktop
    assert calls == [["systemctl", "--user", "daemon-reload"]]
    with pytest.raises(ValueError):
        installer.quote("bad\npath")
