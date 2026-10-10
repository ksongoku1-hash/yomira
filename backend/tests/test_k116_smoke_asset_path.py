from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))

from run_k116_loopback_smoke import (  # noqa: E402
    _resolve_asset_path,  # pyright: ignore[reportPrivateUsage]
)


def test_indexed_library_relative_path_is_resolved_once_under_data_root(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    relative_path = "library/chapters/fixture.cbz"

    actual = _resolve_asset_path(data_root, relative_path)

    assert actual == data_root / Path(*relative_path.split("/"))
