"""Run one K116 fixture smoke with only recursive temp sampling disabled."""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import run_k116_loopback_smoke as smoke  # noqa: E402


def _skip_recursive_temporary_inventory(_path: Path) -> tuple[int, int]:
    """Avoid the harness's recursive os.walk; zero is not an observed count."""
    return 0, 0


smoke._temporary_inventory = _skip_recursive_temporary_inventory  # pyright: ignore[reportPrivateUsage]


if __name__ == "__main__":
    raise SystemExit(smoke.main())
