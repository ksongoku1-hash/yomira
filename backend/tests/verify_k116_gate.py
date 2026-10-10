"""Evaluate the ADR-0103 K116 gate from preserved, offline run evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import zipfile
from pathlib import Path
from typing import Any, cast

PAGE_COUNT = 100
PAGE_BYTES = 65_536
MAX_WORKING_SET_BYTES = 300_000_000
MAX_TEMPORARY_BYTES = 2_147_483_648
REQUIRED_ARTIFACTS = (
    "metrics.json",
    "resource_observation.json",
    "exact_final_inventory.json",
    "reopen_verification.json",
    "outer_run_metadata.json",
    "provenance_before.json",
    "provenance_after.json",
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return cast(dict[str, Any], value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _under(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    path.relative_to(root.resolve())
    return path


def verify(run_directory: Path) -> dict[str, Any]:
    """Return PASS only when every ADR-0103 criterion has preserved evidence."""
    run_directory = run_directory.resolve()
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    missing = [name for name in REQUIRED_ARTIFACTS if not (run_directory / name).is_file()]
    database = run_directory / "data" / "database" / "yomira.sqlite3"
    if not database.is_file():
        missing.append("data/database/yomira.sqlite3")
    add("required_artifacts", not missing, f"missing={missing}")
    if missing:
        return {
            "k116_gate_result": "NO PASS",
            "run_directory": str(run_directory),
            "checks": checks,
        }

    try:
        metrics = _read_json(run_directory / "metrics.json")
        resources = _read_json(run_directory / "resource_observation.json")
        inventory = _read_json(run_directory / "exact_final_inventory.json")
        reopened = _read_json(run_directory / "reopen_verification.json")
        outer = _read_json(run_directory / "outer_run_metadata.json")
        before = _read_json(run_directory / "provenance_before.json")
        after = _read_json(run_directory / "provenance_after.json")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        add("valid_json", False, f"{type(error).__name__}: {error}")
        return {
            "k116_gate_result": "NO PASS",
            "run_directory": str(run_directory),
            "checks": checks,
        }
    add("valid_json", True, "all required JSON artifacts parsed")

    observed = metrics.get("observed", {})
    phase = observed.get("phase_timings", {})
    jobs = metrics.get("integrity", {}).get("jobs", [])
    job_ids = metrics.get("job_ids", [])
    statuses = observed.get("job_statuses_after_dispatcher_stop", {})
    transfer_ok = (
        metrics.get("fixture_pipeline_result") == "PASS"
        and metrics.get("failure_type") is None
        and phase.get("transport_calls") == 2 * PAGE_COUNT
        and phase.get("transport_success_bytes") == 2 * PAGE_COUNT * PAGE_BYTES
        and phase.get("transport_failure_count") == 0
        and len(jobs) == 2
        and len(job_ids) == 2
        and set(statuses) == set(job_ids)
        and all(statuses.get(job_id) == "COMPLETED" for job_id in job_ids)
        and all(
            job.get("job_id") in job_ids
            and job.get("status") == "COMPLETED"
            and job.get("page_count") == PAGE_COUNT
            and job.get("duplicate_reference_preserved") is True
            and len(job.get("pages", [])) == PAGE_COUNT
            and all(page.get("status") == "VALIDATED" for page in job.get("pages", []))
            for job in jobs
        )
    )
    add(
        "workload_and_transfers",
        transfer_ok,
        "2 jobs × 100 validated occurrences; zero transfer failures",
    )

    host_peaks = metrics.get("fixture_peak_requests_by_host", {})
    concurrency_ok = (
        metrics.get("fixture_global_peak_concurrent_requests") == 2
        and observed.get("fixture_peak_concurrent_requests") == 2
        and len(host_peaks) == 2
        and bool(host_peaks)
        and all(value == 1 for value in host_peaks.values())
        and all(value == 1 for value in observed.get("fixture_peak_requests_by_host", {}).values())
    )
    add("concurrency_caps", concurrency_ok, f"global=2, per-host peaks={host_peaks}")

    content_ok = transfer_ok
    content_detail = "CBZ members, page hashes, SQLite integrity and manifests verified"
    try:
        data_root = Path(metrics["data_root"]).resolve()
        if data_root != (run_directory / "data").resolve():
            raise ValueError("metrics data_root does not point to this run's data directory")
        temporary_root = data_root / "temp"
        for job in jobs:
            pages = job["pages"]
            references = [page["remote_reference"] for page in pages]
            if len(set(references)) >= PAGE_COUNT:
                raise ValueError("duplicate page occurrence was not demonstrated")
            for ordinal, page in enumerate(pages, 1):
                if page.get("ordinal") != ordinal or page.get("size_bytes") != PAGE_BYTES:
                    raise ValueError(f"invalid page metadata at ordinal {ordinal}")
                page_path = _under(
                    data_root,
                    str(Path("temp") / page["temporary_path"]),
                )
                if page_path.stat().st_size != PAGE_BYTES or _sha256(page_path) != page["sha256"]:
                    raise ValueError(f"page bytes differ from manifest at ordinal {ordinal}")

            job_temporary_root = temporary_root / "jobs" / job["job_id"]
            if not job_temporary_root.is_dir():
                raise ValueError(f"temporary files are missing for job {job['job_id']}")
            job_temporary_bytes = sum(
                path.stat().st_size for path in job_temporary_root.rglob("*") if path.is_file()
            )
            if job_temporary_bytes > MAX_TEMPORARY_BYTES:
                raise ValueError(f"job {job['job_id']} exceeds the temporary byte ceiling")

            cbz_path = _under(data_root, job["cbz_relative_path"])
            if (
                cbz_path.stat().st_size != job["cbz_size_bytes"]
                or _sha256(cbz_path) != job["cbz_sha256"]
            ):
                raise ValueError("CBZ size or SHA-256 differs from preserved evidence")
            expected_members = ["ComicInfo.xml", *(f"{i:03}.png" for i in range(1, PAGE_COUNT + 1))]
            with zipfile.ZipFile(cbz_path) as archive:
                if archive.namelist() != expected_members or archive.testzip() is not None:
                    raise ValueError("CBZ members, order, or CRC is invalid")
                for page, member in zip(pages, expected_members[1:], strict=True):
                    if hashlib.sha256(archive.read(member)).hexdigest() != page["sha256"]:
                        raise ValueError(f"CBZ page hash differs at {member}")

        database_uri = database.as_uri() + "?mode=ro"
        with sqlite3.connect(database_uri, uri=True, timeout=2.0) as connection:
            connection.execute("PRAGMA query_only=ON")
            quick = [row[0] for row in connection.execute("PRAGMA quick_check")]
            foreign_key_errors = list(connection.execute("PRAGMA foreign_key_check"))
            db_jobs = dict(connection.execute("SELECT job_id, status FROM download_jobs"))
            page_counts = dict(
                connection.execute("SELECT job_id, COUNT(*) FROM download_pages GROUP BY job_id")
            )
            invalid_counts = dict(
                connection.execute(
                    "SELECT job_id, COUNT(*) FROM download_pages "
                    "WHERE status != 'VALIDATED' GROUP BY job_id"
                )
            )
        if quick != ["ok"] or foreign_key_errors or set(db_jobs) != set(job_ids):
            raise ValueError("SQLite quick_check, foreign keys, or job membership failed")
        if any(
            db_jobs[job_id] != "COMPLETED"
            or page_counts.get(job_id) != PAGE_COUNT
            or invalid_counts.get(job_id, 0)
            for job_id in job_ids
        ):
            raise ValueError("SQLite job/page rows disagree with completed manifest evidence")
        if inventory.get("inventory_is_exact") is not True:
            raise ValueError("final temporary inventory is not marked exact")
        if inventory.get("temporary_bytes", MAX_TEMPORARY_BYTES + 1) > MAX_TEMPORARY_BYTES:
            raise ValueError("final temporary bytes exceed the approved per-job ceiling")
        actual_temporary_files = [path for path in temporary_root.rglob("*") if path.is_file()]
        actual_temporary_bytes = sum(path.stat().st_size for path in actual_temporary_files)
        if (
            inventory.get("temporary_bytes") != actual_temporary_bytes
            or inventory.get("temporary_file_count") != len(actual_temporary_files)
            or inventory.get("temporary_bytes") != observed.get("final_temporary_bytes")
        ):
            raise ValueError("exact final inventory differs from sampled final temporary bytes")
    except (OSError, KeyError, TypeError, ValueError, sqlite3.Error, zipfile.BadZipFile) as error:
        content_ok = False
        content_detail = f"{type(error).__name__}: {error}"
    add("cbz_sqlite_and_page_integrity", content_ok, content_detail)

    reopened_jobs = {
        job.get("job_id"): job for run in reopened.get("runs", []) for job in run.get("jobs", [])
    }
    reopen_ok = (
        reopened.get("verification_process_is_separate_from_smoke_process") is True
        and reopened.get("verified_runs") == 1
        and reopened.get("verified_jobs") == 2
        and all(
            run.get("database_closed_by_smoke_before_this_process") is True
            for run in reopened.get("runs", [])
        )
        and all(
            job_id in reopened_jobs
            and reopened_jobs[job_id].get("status") == "COMPLETED"
            and reopened_jobs[job_id].get("page_read_after_reopen") == PAGE_COUNT
            and reopened_jobs[job_id].get("read_page_size_bytes") == PAGE_BYTES
            and reopened_jobs[job_id].get("matches_preclose_integrity_evidence") is True
            and reopened_jobs[job_id].get("matches_durable_manifest_checksum") is True
            and reopened_jobs[job_id].get("read_page_sha256")
            == next(job["pages"][-1]["sha256"] for job in jobs if job["job_id"] == job_id)
            for job_id in job_ids
        )
    )
    add(
        "separate_process_reopen",
        reopen_ok,
        "last page and durable hash checked after DB close/reopen",
    )

    sampler_errors = observed.get("metric_sampling_errors", [])
    working_set = observed.get("process_peak_working_set_bytes", 0)
    temporary_peak = observed.get(
        "sampled_peak_temporary_bytes",
        (2 * MAX_TEMPORARY_BYTES) + 1,
    )
    resources_ok = (
        resources == observed
        and isinstance(sampler_errors, list)
        and not sampler_errors
        and observed.get("metric_samples", 0) > 0
        and 0 < working_set < MAX_WORKING_SET_BYTES
        and 0 <= temporary_peak <= 2 * MAX_TEMPORARY_BYTES
    )
    add(
        "resource_bounds_and_sampler",
        resources_ok,
        (
            f"peak_working_set={working_set}, peak_temp_bytes={temporary_peak}, "
            f"sampler_errors={sampler_errors}"
        ),
    )

    before_files = before.get("files", [])
    after_files = after.get("files", [])
    before_map = {entry.get("path"): entry.get("sha256") for entry in before_files}
    after_map = {entry.get("path"): entry.get("sha256") for entry in after_files}
    provenance_ok = (
        before.get("phase") == "before"
        and after.get("phase") == "after"
        and before.get("file_count") == len(before_files)
        and after.get("file_count") == len(after_files)
        and before_map == after_map
        and before.get("source_tree_sha256") == after.get("source_tree_sha256")
        and before.get("file_count", 0) >= 170
        and outer.get("runner_exit_code") == 0
        and outer.get("source_tree_equal") is True
        and outer.get("file_difference_count") == 0
        and bool(outer.get("command"))
        and outer.get("fixture_internet_used") is False
        and outer.get("workload") == "offline loopback fixture, 2 jobs x 100 pages x 65536 bytes"
        and metrics.get("host_platform") == "win32"
        and metrics.get("python_version", "").startswith("3.14.7")
        and {
            "backend/app/services/download_worker.py",
            "backend/tests/run_k116_loopback_smoke.py",
            "backend/tests/run_k116_approved_gate.py",
            "backend/tests/run_k116_diagnostic_2x100.py",
            "backend/tests/verify_k116_reopen.py",
            "backend/tests/verify_k116_gate.py",
            "docs/adr/ADR-0103-k116-v1-acceptance.md",
        }.issubset(before_map)
        and all(len(value) == 64 for value in before_map.values())
    )
    add(
        "provenance_and_reproducibility",
        provenance_ok,
        (
            f"files={len(before_files)}, "
            f"tree_sha256={before.get('source_tree_sha256')}, "
            f"command={outer.get('command')}"
        ),
    )

    passed = all(check["passed"] for check in checks)
    return {
        "k116_gate_result": "PASS" if passed else "NO PASS",
        "adr": "ADR-0103",
        "run_directory": str(run_directory),
        "checks": checks,
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_k116_gate.py RUN_DIRECTORY")
    run_directory = Path(sys.argv[1]).resolve()
    report = verify(run_directory)
    report_path = run_directory / "k116_gate_evaluation.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"report_path": str(report_path), **report}, ensure_ascii=False, indent=2))
    return 0 if report["k116_gate_result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
