"""Run the ADR-0103 K116 fixture smoke with before/after source provenance."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
TESTS_ROOT = BACKEND_ROOT / "tests"
PROVENANCE_FILES = (
    "backend/tests/mock_manga_server.py",
    "backend/tests/run_k116_diagnostic_2x100.py",
    "backend/tests/run_k116_loopback_smoke.py",
    "backend/tests/run_k116_approved_gate.py",
    "backend/tests/support/sqlite.py",
    "backend/tests/test_k116_metric_sampler.py",
    "backend/tests/test_k116_smoke_asset_path.py",
    "backend/tests/test_k116_gate_evaluation.py",
    "backend/tests/verify_k116_reopen.py",
    "backend/tests/verify_k116_gate.py",
    "backend/requirements-package.lock",
    "backend/requirements.lock",
    "docs/adr/ADR-0083-page-fetch-and-v1-resume.md",
    "docs/adr/ADR-0086-v1-page-resource-limits.md",
    "docs/adr/ADR-0088-v1-cbz-size-limit.md",
    "docs/adr/ADR-0103-k116-v1-acceptance.md",
    "docs/YOMIRA_MASTER_EXECUTION_PLAN_V1_SCOPE.md",
    "docs/YOMIRA_V1_SPEC_1.1.md",
)
WORKLOAD = "offline loopback fixture, 2 jobs x 100 pages x 65536 bytes"


def _source_snapshot(phase: str) -> dict[str, Any]:
    source_paths = {path for path in (BACKEND_ROOT / "app").rglob("*.py")}
    source_paths.update(REPOSITORY_ROOT / name for name in PROVENANCE_FILES)
    files: list[dict[str, Any]] = []
    for path in sorted(source_paths, key=lambda item: item.relative_to(REPOSITORY_ROOT).as_posix()):
        if not path.is_file():
            raise FileNotFoundError(f"required provenance source is missing: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append(
            {
                "path": path.relative_to(REPOSITORY_ROOT).as_posix(),
                "sha256": digest,
                "size_bytes": path.stat().st_size,
            }
        )
    canonical = json.dumps(files, ensure_ascii=False, separators=(",", ":")).encode()
    version = sys.getwindowsversion()
    return {
        "phase": phase,
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "repository_root": str(REPOSITORY_ROOT),
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "windows_version": (
            f"Microsoft Windows NT {version.major}.{version.minor}."
            f"{version.build}.{version.platform}"
        ),
        "file_count": len(files),
        "source_tree_sha256": hashlib.sha256(canonical).hexdigest().upper(),
        "files": files,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    if os.name != "nt":
        raise SystemExit("The ADR-0103 gate runner requires Windows.")
    if len(sys.argv) != 2:
        raise SystemExit("usage: run_k116_approved_gate.py NEW_ARTIFACT_DIRECTORY")

    artifact_root = Path(sys.argv[1]).resolve()
    if artifact_root.exists():
        raise SystemExit(f"Refusing to reuse existing evidence directory: {artifact_root}")
    artifact_root.mkdir(parents=True)
    before = _source_snapshot("before")
    before_path = artifact_root / "provenance_before.json"
    _write_json(before_path, before)

    environment = os.environ.copy()
    environment["YOMIRA_K116_BASETEMP"] = str(artifact_root)
    runner = TESTS_ROOT / "run_k116_diagnostic_2x100.py"
    command = [sys.executable, str(runner)]
    runner_result = subprocess.run(command, cwd=BACKEND_ROOT, env=environment, check=False)
    run_directories = sorted(artifact_root.glob("k116-loopback-*"))
    if len(run_directories) != 1:
        print(f"Expected one preserved run directory; found {len(run_directories)}.")
        return runner_result.returncode or 2
    run_directory = run_directories[0]
    shutil.copy2(before_path, run_directory / before_path.name)

    after = _source_snapshot("after")
    after_path = run_directory / "provenance_after.json"
    _write_json(after_path, after)
    before_hashes = {item["path"]: item["sha256"] for item in before["files"]}
    after_hashes = {item["path"]: item["sha256"] for item in after["files"]}
    differences = sum(
        before_hashes.get(path) != after_hashes.get(path)
        for path in before_hashes.keys() | after_hashes.keys()
    )
    reopen_command = [
        sys.executable,
        str(TESTS_ROOT / "verify_k116_reopen.py"),
        str(artifact_root),
    ]
    _write_json(
        run_directory / "outer_run_metadata.json",
        {
            "runner_exit_code": runner_result.returncode,
            "command": subprocess.list2cmdline([sys.executable, str(__file__), str(artifact_root)]),
            "runner_command": subprocess.list2cmdline(command),
            "reopen_command": subprocess.list2cmdline(reopen_command),
            "artifact_directory": str(run_directory),
            "source_tree_sha256_before": before["source_tree_sha256"],
            "source_tree_sha256_after": after["source_tree_sha256"],
            "source_tree_equal": before_hashes == after_hashes,
            "file_difference_count": differences,
            "workload": WORKLOAD,
            "watchdog_seconds": 600,
            "fixture_internet_used": False,
        },
    )

    reopen_result = subprocess.run(reopen_command, cwd=BACKEND_ROOT, check=False)
    reopen_path = artifact_root / "reopen_verification.json"
    if reopen_path.is_file():
        shutil.copy2(reopen_path, run_directory / reopen_path.name)
    else:
        print("Separate-process reopen verifier did not produce its report.")

    verifier = TESTS_ROOT / "verify_k116_gate.py"
    gate_result = subprocess.run(
        [sys.executable, str(verifier), str(run_directory)],
        cwd=BACKEND_ROOT,
        check=False,
    )
    if reopen_result.returncode or gate_result.returncode:
        return gate_result.returncode or reopen_result.returncode
    return runner_result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
