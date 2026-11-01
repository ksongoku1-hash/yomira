import subprocess
import sys
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.desktop import interface_is_ready

origin = "http://127.0.0.1:8000"
if not interface_is_ready(origin):
    subprocess.run(["systemctl", "--user", "start", "yomira.service"], check=True)
    for _ in range(180):
        if interface_is_ready(origin):
            break
        time.sleep(0.5)
    else:
        raise SystemExit("Yomira no inició. Consulta: journalctl --user -u yomira.service")
webbrowser.open(origin)
