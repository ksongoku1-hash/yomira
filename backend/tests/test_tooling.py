import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]


def test_python_runtime_uses_pinned_minor_version() -> None:
    assert sys.version_info[:2] == (3, 14)


def test_sqlite_runtime_matches_pinned_build() -> None:
    runtime_pins = json.loads(
        (PROJECT_ROOT / "runtime-versions.json").read_text(encoding="utf-8")
    )
    assert sqlite3.sqlite_version == runtime_pins["sqlite"]["version"]
