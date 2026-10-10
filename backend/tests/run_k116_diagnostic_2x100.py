"""One K116 2x100 loopback run with bounded test-only timing evidence.

The normal smoke remains the workload authority. This wrapper records selected
in-process intervals and writes durable progress artifacts; it does not change
product modules, fixture responses, or configured limits.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = BACKEND_ROOT / "tests"
for import_root in (str(BACKEND_ROOT), str(TESTS_ROOT)):
    if import_root not in sys.path:
        sys.path.insert(0, import_root)

import run_k116_loopback_smoke as smoke  # noqa: E402

import app.persistence.manifest_store as manifest_store_module  # noqa: E402
import app.services.download_worker as download_worker_module  # noqa: E402
from app.persistence.atomic_file_publisher import AtomicFilePublisher  # noqa: E402
from app.services.download_transport import DownloadTransport  # noqa: E402
from app.services.page_manifest_writer import PageManifestWriter  # noqa: E402


def _atomic_json_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class _DiagnosticRecorder:
    def __init__(self) -> None:
        self.started_ns = time.perf_counter_ns()
        self._lock = threading.Lock()
        self._events_pending: list[dict[str, Any]] = []
        self._totals: dict[str, dict[str, int]] = defaultdict(
            lambda: {"count": 0, "total_ns": 0, "max_ns": 0}
        )
        self._run_directory: Path | None = None
        self._latest_statuses: dict[str, str] = {}
        self._sidecar_stop = threading.Event()
        self._sidecar_thread: threading.Thread | None = None
        self._sidecar_error: str | None = None

    def attach(self, run_directory: Path) -> None:
        with self._lock:
            self._run_directory = run_directory
        self._sidecar_thread = threading.Thread(
            target=self._sidecar_loop,
            name="yomira-k116-diagnostic-sidecar",
            daemon=False,
        )
        self._sidecar_thread.start()
        self.flush_progress()

    def record(
        self,
        phase: str,
        started_ns: int,
        *,
        job_id: object | None = None,
        ordinal: object | None = None,
        attempt: object | None = None,
        detail: object | None = None,
    ) -> None:
        ended_ns = time.perf_counter_ns()
        duration_ns = max(0, ended_ns - started_ns)
        event = {
            "phase": phase,
            "start_perf_counter_ns": started_ns,
            "end_perf_counter_ns": ended_ns,
            "duration_ns": duration_ns,
            "elapsed_since_harness_start_ns": max(0, ended_ns - self.started_ns),
            "job_id": str(job_id) if job_id is not None else None,
            "ordinal": ordinal,
            "attempt": attempt,
            "detail": detail,
        }
        with self._lock:
            totals = self._totals[phase]
            totals["count"] += 1
            totals["total_ns"] += duration_ns
            totals["max_ns"] = max(totals["max_ns"], duration_ns)
            self._events_pending.append(event)

    def record_aggregate(self, phase: str, duration_ns: int) -> None:
        with self._lock:
            totals = self._totals[phase]
            totals["count"] += 1
            totals["total_ns"] += duration_ns
            totals["max_ns"] = max(totals["max_ns"], duration_ns)

    def update_statuses(self, statuses: dict[str, str]) -> None:
        with self._lock:
            self._latest_statuses = dict(statuses)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            totals = {name: dict(values) for name, values in self._totals.items()}
            statuses = dict(self._latest_statuses)
            run_directory = self._run_directory
            sidecar_error = self._sidecar_error
        now_ns = time.perf_counter_ns()
        return {
            "captured_at_utc": datetime.now(UTC).isoformat(),
            "harness_pid": os.getpid(),
            "elapsed_seconds": (now_ns - self.started_ns) / 1_000_000_000,
            "artifact_directory": str(run_directory) if run_directory else None,
            "data_root": str(run_directory / "data") if run_directory else None,
            "latest_job_statuses": statuses,
            "phase_intervals": totals,
            "interval_events_path": (
                str(run_directory / "diagnostic_intervals.jsonl")
                if run_directory
                else None
            ),
            "sampler_inventory_interval_seconds": 5,
            "sidecar_error": sidecar_error,
        }

    def flush_progress(self) -> None:
        run_directory = self._run_directory
        if run_directory is None:
            return
        try:
            with self._lock:
                pending = self._events_pending
                self._events_pending = []
            if pending:
                events_path = run_directory / "diagnostic_intervals.jsonl"
                with events_path.open("a", encoding="utf-8", newline="\n") as stream:
                    for event in pending:
                        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            _atomic_json_write(
                run_directory / "diagnostic_progress.json",
                self.snapshot(),
            )
        except Exception as error:  # diagnostic failure must not mask workload
            with self._lock:
                self._sidecar_error = type(error).__name__

    def _sidecar_loop(self) -> None:
        while not self._sidecar_stop.wait(5.0):
            self.flush_progress()

    def finish(self) -> None:
        self._sidecar_stop.set()
        thread = self._sidecar_thread
        if thread is not None:
            thread.join(timeout=6)
        self.flush_progress()
        run_directory = self._run_directory
        if run_directory is not None:
            _atomic_json_write(
                run_directory / "diagnostic_summary.json",
                self.snapshot(),
            )


_RECORDER = _DiagnosticRecorder()
async def _measure_preflight(
    original: Callable[..., Any],
    transport: DownloadTransport,
    **kwargs: Any,
) -> Any:
    started_ns = time.perf_counter_ns()
    try:
        return await original(transport, **kwargs)
    finally:
        _RECORDER.record(
            "temporary_budget_preflight",
            started_ns,
            job_id=kwargs.get("job_id"),
            ordinal=kwargs.get("ordinal"),
            attempt=kwargs.get("attempt"),
        )


def _instrument_product_symbols() -> None:
    original_no_reparse = manifest_store_module._assert_no_reparse  # pyright: ignore[reportPrivateUsage]

    def measured_no_reparse(path: Path, **kwargs: Any) -> Any:
        started_ns = time.perf_counter_ns()
        try:
            return original_no_reparse(path, **kwargs)
        finally:
            identity = kwargs.get("identity")
            canonical_path = getattr(identity, "canonical_path", None)
            try:
                if not isinstance(canonical_path, Path):
                    raise ValueError("data-root identity is unavailable")
                relative = path.relative_to(canonical_path)
                depth = len(relative.parts)
            except (AttributeError, ValueError):
                depth = -1
            parent_check = (
                "cached_parent"
                if kwargs.get("expected_parent_identity") is not None
                else "walk"
            )
            phase = (
                f"path_validation:{kwargs.get('stage', 'unknown')}:"
                f"{parent_check}:depth_{depth}"
            )
            _RECORDER.record_aggregate(
                phase,
                time.perf_counter_ns() - started_ns,
            )

    manifest_store_module._assert_no_reparse = measured_no_reparse  # type: ignore[assignment]

    original_preflight = DownloadTransport._preflight_job_temporary_budget  # pyright: ignore[reportPrivateUsage]

    async def measured_preflight(self: DownloadTransport, **kwargs: Any) -> Any:
        return await _measure_preflight(original_preflight, self, **kwargs)

    DownloadTransport._preflight_job_temporary_budget = measured_preflight  # type: ignore[method-assign]

    original_snapshot = PageManifestWriter.publish_snapshot

    def measured_snapshot(
        writer: PageManifestWriter,
        job_id: object,
        pages: tuple[object, ...],
    ) -> Any:
        started_ns = time.perf_counter_ns()
        try:
            return original_snapshot(writer, job_id, pages)  # type: ignore[arg-type]
        finally:
            _RECORDER.record(
                "manifest_snapshot_publish",
                started_ns,
                job_id=job_id,
                detail={"page_count": len(pages)},
            )

    PageManifestWriter.publish_snapshot = measured_snapshot  # type: ignore[method-assign]

    original_validate = download_worker_module.validate_image_file

    def measured_validate(*args: Any, **kwargs: Any) -> Any:
        started_ns = time.perf_counter_ns()
        try:
            return original_validate(*args, **kwargs)
        finally:
            path = args[0] if args else kwargs.get("path")
            _RECORDER.record(
                "image_validation",
                started_ns,
                job_id=kwargs.get("job_id"),
                detail={"file_name": Path(path).name if path is not None else None},
            )

    download_worker_module.validate_image_file = measured_validate

    original_publish = AtomicFilePublisher.publish_no_replace

    def measured_publish(
        publisher: AtomicFilePublisher,
        source: Path,
        destination: Path,
        **kwargs: Any,
    ) -> Any:
        started_ns = time.perf_counter_ns()
        try:
            return original_publish(
                publisher,
                source,
                destination,
                **kwargs,
            )
        finally:
            suffix = Path(destination).suffix.lower()
            _RECORDER.record(
                "atomic_promotion_cbz" if suffix == ".cbz" else "atomic_promotion_page",
                started_ns,
                job_id=kwargs.get("job_id"),
                detail={"destination_suffix": suffix},
            )

    AtomicFilePublisher.publish_no_replace = measured_publish  # type: ignore[method-assign]


def _install_harness_instrumentation() -> None:
    original_read_statuses = smoke._read_job_statuses  # pyright: ignore[reportPrivateUsage]

    async def measured_read_statuses(database: Any, job_ids: tuple[Any, ...]) -> dict[str, str]:
        statuses = await original_read_statuses(database, job_ids)
        _RECORDER.update_statuses(statuses)
        return statuses

    smoke._read_job_statuses = measured_read_statuses  # pyright: ignore[reportPrivateUsage]

    original_new_run_directory = smoke._new_run_directory  # pyright: ignore[reportPrivateUsage]

    def measured_new_run_directory() -> Path:
        run_directory = original_new_run_directory()
        _RECORDER.attach(run_directory)
        return run_directory

    smoke._new_run_directory = measured_new_run_directory  # pyright: ignore[reportPrivateUsage]

    original_run_smoke = smoke._run_smoke  # pyright: ignore[reportPrivateUsage]

    async def measured_run_smoke(run_directory: Path, host_observer: Any) -> dict[str, Any]:
        try:
            return await original_run_smoke(run_directory, host_observer)
        finally:
            temporary_bytes, file_count = smoke._temporary_inventory(  # pyright: ignore[reportPrivateUsage]
                run_directory / "data" / "temp"
            )
            _atomic_json_write(
                run_directory / "exact_final_inventory.json",
                {
                    "captured_at_utc": datetime.now(UTC).isoformat(),
                    "inventory_is_exact": True,
                    "temporary_bytes": temporary_bytes,
                    "temporary_file_count": file_count,
                    "capture_point": "after_smoke_pipeline_and_dispatcher_stop",
                },
            )

    smoke._run_smoke = measured_run_smoke  # pyright: ignore[reportPrivateUsage]
    _instrument_product_symbols()


def _capture_final(run_directory: Path) -> int:
    """Capture exact preserved state after a run, including a watchdog stop."""
    data_root = run_directory / "data"
    database_path = data_root / "database" / "yomira.sqlite3"
    temporary_root = data_root / "temp"
    temporary_bytes, temporary_count = smoke._temporary_inventory(  # pyright: ignore[reportPrivateUsage]
        temporary_root
    )
    files: list[dict[str, Any]] = []
    for path in sorted(data_root.rglob("*")) if data_root.exists() else []:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            files.append(
                {
                    "relative_path": str(path.relative_to(data_root)),
                    "size_bytes": path.stat().st_size,
                }
            )
        except OSError as error:
            files.append(
                {
                    "relative_path": str(path.relative_to(data_root)),
                    "stat_error": type(error).__name__,
                }
            )

    quick_check: dict[str, Any] = {"result": "not_run"}
    job_status_counts: dict[str, int | str] = {}
    if database_path.is_file():
        try:
            uri = database_path.as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
                connection.execute("PRAGMA query_only=ON")
                quick_check = {
                    "result": "ok",
                    "rows": [row[0] for row in connection.execute("PRAGMA quick_check")],
                }
                try:
                    job_status_counts = {
                        str(status): int(count)
                        for status, count in connection.execute(
                            "SELECT status, COUNT(*) FROM download_jobs GROUP BY status"
                        )
                    }
                except sqlite3.Error as error:
                    job_status_counts = {"query_error": type(error).__name__}
        except Exception as error:
            quick_check = {"result": "error", "error_type": type(error).__name__}

    capture = {
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "artifact_directory": str(run_directory),
        "data_root": str(data_root),
        "temporary_inventory": {
            "exact": True,
            "root": str(temporary_root),
            "bytes": temporary_bytes,
            "file_count": temporary_count,
        },
        "data_root_files": files,
        "database_quick_check_read_only": quick_check,
        "download_job_status_counts_read_only": job_status_counts,
        "note": "Capture is read-only against preserved run data; no cleanup performed.",
    }
    _atomic_json_write(run_directory / "watchdog_final_capture.json", capture)
    print(json.dumps(capture, ensure_ascii=False, indent=2))
    return 0 if quick_check.get("result") == "ok" else 2


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--capture-final":
        return _capture_final(Path(sys.argv[2]))
    if len(sys.argv) != 1:
        raise SystemExit("usage: run_k116_diagnostic_2x100.py [--capture-final RUN_DIR]")
    if os.name != "nt":
        raise SystemExit("This target-platform diagnostic requires Windows.")

    _install_harness_instrumentation()
    try:
        return smoke.main()
    finally:
        _RECORDER.finish()


if __name__ == "__main__":
    raise SystemExit(main())
