"""Repeat the bounded K116 loopback pipeline in one process.

This test-only wrapper does not change production code or claim the K116 gate.
Each iteration delegates to ``run_k116_loopback_smoke`` and preserves its
metrics, resource observations, synthetic data, and integrity evidence.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = BACKEND_ROOT / "tests"
for import_root in (str(BACKEND_ROOT), str(TESTS_ROOT)):
    if import_root not in sys.path:
        sys.path.insert(0, import_root)

import run_k116_loopback_smoke as smoke  # noqa: E402


def _idle_samples(
    sampler: smoke._WindowsProcessSampler,  # pyright: ignore[reportPrivateUsage]
    count: int,
) -> list[dict[str, int]]:
    samples: list[dict[str, int]] = []
    for _ in range(count):
        time.sleep(1)
        samples.append(sampler.sample())
    return samples


def _resource_summary(samples: list[dict[str, int]]) -> dict[str, int]:
    return {
        "working_set_median_bytes": int(
            median(sample["working_set_bytes"] for sample in samples)
        ),
        "private_bytes_median": int(
            median(sample["private_bytes"] for sample in samples)
        ),
        "handle_count_median": int(
            median(sample["handle_count"] for sample in samples)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--runs", default=3, type=int)
    parser.add_argument("--idle-samples", default=5, type=int)
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("The bounded K116 soak requires Windows resource counters.")
    if not 2 <= args.runs <= 5:
        raise SystemExit("--runs must be between 2 and 5 for a bounded same-process sample.")
    if not 3 <= args.idle_samples <= 10:
        raise SystemExit("--idle-samples must be between 3 and 10.")

    started_at_utc = datetime.now(UTC).isoformat()
    artifact_root = args.artifact_root.resolve()
    artifact_root.mkdir(parents=True, exist_ok=False)
    os.environ["YOMIRA_K116_BASETEMP"] = str(artifact_root)
    process_sampler = smoke._WindowsProcessSampler()  # pyright: ignore[reportPrivateUsage]
    initial_samples = _idle_samples(process_sampler, args.idle_samples)
    iterations: list[dict[str, Any]] = []

    for iteration_number in range(1, args.runs + 1):
        gc.collect()
        before = _resource_summary(_idle_samples(process_sampler, args.idle_samples))
        preexisting = set(artifact_root.glob("k116-loopback-*"))
        started = time.perf_counter()
        exit_code = smoke.main()
        elapsed_seconds = time.perf_counter() - started
        gc.collect()
        after_samples = _idle_samples(process_sampler, args.idle_samples)
        after = _resource_summary(after_samples)
        new_runs = sorted(
            set(artifact_root.glob("k116-loopback-*")) - preexisting,
            key=lambda path: path.name,
        )
        if len(new_runs) != 1:
            raise RuntimeError(
                f"expected exactly one smoke artifact directory, found {len(new_runs)}"
            )
        metrics_path = new_runs[0] / "metrics.json"
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        observed = metrics.get("observed", {})
        workload = metrics.get("workload", {})
        expected_bytes = int(workload.get("expected_download_payload_bytes", 0))
        page_occurrences = int(workload.get("total_page_occurrences", 0))
        iterations.append(
            {
                "iteration": iteration_number,
                "exit_code": exit_code,
                "artifact_directory": str(new_runs[0]),
                "metrics_path": str(metrics_path),
                "pipeline_elapsed_seconds": elapsed_seconds,
                "synthetic_bytes_per_second": (
                    expected_bytes / elapsed_seconds if elapsed_seconds else None
                ),
                "page_occurrences_per_second": (
                    page_occurrences / elapsed_seconds if elapsed_seconds else None
                ),
                "fixture_pipeline_result": metrics.get("fixture_pipeline_result"),
                "k116_gate_result": metrics.get("k116_gate_result"),
                "integrity": metrics.get("integrity"),
                "workload": workload,
                "observed": observed,
                "process_resource_medians_before": before,
                "process_resource_medians_after_idle": after,
                "process_resource_delta_after_idle": {
                    key: after[key] - before[key] for key in after
                },
            }
        )
        if exit_code != 0:
            break

    summary = {
        "started_at_utc": started_at_utc,
        "finished_at_utc": datetime.now(UTC).isoformat(),
        "host_platform": sys.platform,
        "python_version": sys.version,
        "artifact_root": str(artifact_root),
        "iterations_requested": args.runs,
        "idle_samples_per_boundary": args.idle_samples,
        "idle_sample_interval_seconds": 1,
        "same_process_repetitions": True,
        "profile": "each iteration: 2 jobs x 100 pages x 65536-byte synthetic PNGs",
        "network_scope": "loopback fixture only; Internet unused",
        "usb_used": False,
        "initial_idle_resource_medians": _resource_summary(initial_samples),
        "iterations": iterations,
        "stress_gate_result": "NO PASS",
        "no_pass_reason": (
            "Repeated 2x100 fixture runs do not cover SPEC section 94's "
            "100/500/1000/2000-page leak sequence, approved resource thresholds, "
            "failure recovery under sustained load, or crash/reopen boundaries."
        ),
    }
    summary_path = artifact_root / "bounded_soak_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "summary_path": str(summary_path),
                "stress_gate_result": summary["stress_gate_result"],
                "iterations_completed": len(iterations),
                "resource_deltas_after_idle": [
                    iteration["process_resource_delta_after_idle"]
                    for iteration in iterations
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if len(iterations) == args.runs and all(
        iteration["exit_code"] == 0 for iteration in iterations
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
