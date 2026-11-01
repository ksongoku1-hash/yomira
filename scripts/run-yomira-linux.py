"""Start the native Linux backend; the desktop launcher opens the browser."""

import os
import sys
from pathlib import Path

if sys.platform != "linux":
    raise SystemExit("Este lanzador requiere Linux. Usa el acceso habitual en Windows.")

os.umask(0o077)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.chdir(ROOT / "backend")
os.environ.setdefault("YOMIRA_APP_ENV", "prod")
os.environ.setdefault("YOMIRA_API_HOST", "127.0.0.1")
os.environ.setdefault("YOMIRA_API_PORT", "8000")

from app.main import main

main()
