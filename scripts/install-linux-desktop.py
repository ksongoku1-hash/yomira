"""Install only this user's systemd unit and application menu entries."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def quote(value: Path | str, *, desktop: bool = False) -> str:
    text = str(value)
    if any(character in text for character in "\r\n\0"):
        raise ValueError("Invalid installation path")
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if desktop:
        text = text.replace("$", "\\$").replace("`", "\\`")
    return '"' + text + '"'


def xdg(name: str, fallback: str) -> Path:
    value = os.environ.get(name, "")
    return Path(value) if value and Path(value).is_absolute() else Path.home() / fallback


def install() -> None:
    config = xdg("XDG_CONFIG_HOME", ".config") / "systemd/user"
    apps = xdg("XDG_DATA_HOME", ".local/share") / "applications"
    config.mkdir(parents=True, exist_ok=True)
    apps.mkdir(parents=True, exist_ok=True)
    unit = (
        "[Unit]\nDescription=Yomira local manga reader\nAfter=network.target\n\n[Service]\n"
        "Type=exec\nUMask=0077\nKillMode=control-group\nTimeoutStopSec=180\n"
        f"WorkingDirectory={quote(ROOT / 'backend')}\n"
        f"Environment={quote('XDG_DATA_HOME=' + str(xdg('XDG_DATA_HOME', '.local/share')))}\n"
        f"ExecStart={quote(str(ROOT / '.venv/bin/python').replace('$', '$$'))} {quote(str(ROOT / 'scripts/run-yomira-linux.py').replace('$', '$$'))}\n"
    )
    (config / "yomira.service").write_text(unit, encoding="utf-8")
    icon = ROOT / "frontend/dist/assets/brand/yomira-glacial.svg"
    for name, label, script in (
        ("yomira", "Yomira", "Abrir-Yomira.sh"),
        ("yomira-stop", "Cerrar Yomira", "Cerrar-Yomira.sh"),
    ):
        (apps / f"{name}.desktop").write_text(
            "[Desktop Entry]\nType=Application\n"
            f"Name={label}\nExec={quote(ROOT / script, desktop=True)}\n"
            f"Icon={icon}\nTerminal=false\nCategories=Office;Viewer;\n",
            encoding="utf-8",
        )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)


if __name__ == "__main__":
    install()
