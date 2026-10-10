from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

TESTS_DIRECTORY = Path(__file__).resolve().parent
BACKEND_DIRECTORY = TESTS_DIRECTORY.parent
if str(BACKEND_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIRECTORY))
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))

import run_k116_diagnostic_2x100 as diagnostic  # noqa: E402
import run_k116_loopback_smoke as smoke  # noqa: E402


class _FakeProcessSampler:
    def __init__(self) -> None:
        self.samples = 0

    def sample(self) -> dict[str, int]:
        self.samples += 1
        return {"working_set_bytes": self.samples}


def test_metric_sampler_keeps_memory_cadence_without_walking_temp_every_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_sampler = _FakeProcessSampler()
    monkeypatch.setattr(smoke, "_WindowsProcessSampler", lambda: process_sampler)
    sampler = smoke._MetricSampler(tmp_path)  # pyright: ignore[reportPrivateUsage]
    clock = iter((0.0, 0.01, 1.0, 1.01, 5.0, 5.01))
    monkeypatch.setattr(smoke.time, "perf_counter", lambda: next(clock))
    inventory_samples: list[tuple[int, int]] = []

    def inventory(_path: Path) -> tuple[int, int]:
        inventory_samples.append((len(inventory_samples) + 1, len(inventory_samples) + 1))
        return inventory_samples[-1]

    monkeypatch.setattr(smoke, "_temporary_inventory", inventory)

    sampler._record()  # pyright: ignore[reportPrivateUsage]
    sampler._record()  # pyright: ignore[reportPrivateUsage]
    sampler._record()  # pyright: ignore[reportPrivateUsage]

    assert process_sampler.samples == 3
    assert sampler.samples == 3
    assert inventory_samples == [(1, 1), (2, 2)]
    assert sampler.peak_temporary_bytes == 2
    assert sampler.peak_temporary_file_count == 2


def test_watchdog_capture_uses_shared_exact_inventory_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary_root = tmp_path / "data" / "temp"
    temporary_root.mkdir(parents=True)
    database_path = tmp_path / "data" / "database" / "yomira.sqlite3"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path):
        pass

    calls: list[Path] = []

    def inventory(path: Path) -> tuple[int, int]:
        calls.append(path)
        return 73, 9

    monkeypatch.setattr(smoke, "_temporary_inventory", inventory)

    result = diagnostic._capture_final(tmp_path)  # pyright: ignore[reportPrivateUsage]
    capture = json.loads((tmp_path / "watchdog_final_capture.json").read_text(encoding="utf-8"))

    assert result == 0
    assert calls == [temporary_root]
    assert capture["temporary_inventory"]["bytes"] == 73
    assert capture["temporary_inventory"]["file_count"] == 9
