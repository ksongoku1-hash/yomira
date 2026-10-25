from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from io import BytesIO, TextIOWrapper
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from threading import Thread
from uuid import uuid4

import pytest

from app.domain.asset import Asset, AssetType
from app.errors import ErrorCode
from app.services.usb_export import ExportFailure, ExportTarget
from app.services.wpd_worker import IsolatedWpdClient, run_worker


class _WorkerProtocolClient(IsolatedWpdClient):
    def request_progress(self) -> dict[str, object]:
        return self._request(
            {"asset": {"size_bytes": 3}},
            timeout=5,
            trace_id="progress-test",
            failure_code=ErrorCode.EXP_COPY_FAILED,
            failure_retryable=False,
        )

    def command(self) -> tuple[str, ...]:
        return self._command()


@pytest.mark.parametrize("processed", [2, -1, 4, True])
def test_streamed_progress_is_visible_then_cleared(tmp_path: Path, processed: object) -> None:
    script = tmp_path / "progress.py"
    script.write_text(
        "import json,sys,time\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'progress': {'stage': 'copy', "
        f"'processed_bytes': {processed!r}, 'total_bytes': 3}}}}), flush=True)\n"
        "time.sleep(0.5)\n"
        "print(json.dumps({'ok': True, 'status': 'EXPORTED'}), flush=True)\n",
        encoding="utf-8",
    )
    client = _WorkerProtocolClient(worker_command=(sys.executable, str(script)))
    results: list[object] = []

    def invoke() -> None:
        try:
            results.append(client.request_progress())
        except ExportFailure as error:
            results.append(error)

    thread = Thread(target=invoke)
    thread.start()
    deadline = time.monotonic() + 3
    while (
        not results
        and client.progress_for_trace("progress-test") is None
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    if type(processed) is int and processed == 2:
        assert client.progress_for_trace("progress-test") == {
            "stage": "copy",
            "processed_bytes": 2,
            "total_bytes": 3,
        }
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert client.progress_for_trace("progress-test") is None
    if type(processed) is int and processed == 2:
        assert results == [{"ok": True, "status": "EXPORTED"}]
    else:
        assert isinstance(results[0], ExportFailure)


def test_streaming_worker_timeout_terminates_child(tmp_path: Path) -> None:
    script = tmp_path / "timeout.py"
    marker = tmp_path / "orphan"
    script.write_text(
        f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch()",
        encoding="utf-8",
    )
    client = IsolatedWpdClient(
        worker_command=(sys.executable, str(script)),
        enumeration_timeout_seconds=0.1,
    )
    with pytest.raises(ExportFailure) as failure:
        client.list_targets()
    assert failure.value.stage == "worker_timeout"
    time.sleep(1.1)
    assert not marker.exists()


def test_default_worker_preserves_runtime_import_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    package = tmp_path / "app" / "services"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "wpd_worker.py").write_text(
        "def run_worker():\n    print('runtime import paths preserved')\n    return 0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path, *(sys.path * 100)])
    command = _WorkerProtocolClient().command()
    assert len(command[-1]) < 32_000
    # -I reproduces an interpreter that ignores cwd and PYTHONPATH.
    result = subprocess.run(
        (command[0], "-I", *command[1:]),
        cwd=tmp_path.parent,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "runtime import paths preserved"


def test_worker_protocol_uses_utf8_for_unicode_windows_strings() -> None:
    target_payload = {
        "target_id": "wpd:reader-id",
        "mount_path": None,
        "display_name": "Lector español 日本語",
        "file_system": "MTP/WPD",
        "total_bytes": 100,
        "free_bytes": 90,
        "transport": "wpd",
        "portable_device_id": "device-1",
        "storage_object_id": "storage-1",
        "storage_persistent_id": "persistent-1",
    }
    invocation: dict[str, object] = {}

    def run_worker(command: Sequence[str], **arguments: object) -> CompletedProcess[str]:
        invocation.update(arguments)
        return CompletedProcess(
            args=list(command),
            returncode=0,
            stdout=json.dumps({"ok": True, "targets": [target_payload]}, ensure_ascii=False),
            stderr="",
        )

    client = IsolatedWpdClient(worker_command=("worker",), runner=run_worker)

    targets = client.list_targets()

    assert invocation.get("encoding") == "utf-8"
    assert targets[0].display_name == "Lector español 日本語"


def test_target_enumeration_worker_reports_device_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class DisconnectedBackend:
        def list_targets(self) -> tuple[ExportTarget, ...]:
            raise OSError("device disconnected")

    monkeypatch.setattr("app.services.wpd_worker.WindowsWpdBackend", DisconnectedBackend)
    input_stream = TextIOWrapper(
        BytesIO(b'{"action":"list_targets","trace_id":"trace-1"}'),
        encoding="utf-8",
    )
    output_buffer = BytesIO()
    output_stream = TextIOWrapper(output_buffer, encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", input_stream)
    monkeypatch.setattr(sys, "stdout", output_stream)

    assert run_worker() == 0
    output_stream.flush()
    response = json.loads(output_buffer.getvalue().decode("utf-8"))

    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == ErrorCode.EXP_DEVICE_DISCONNECTED.value


def test_access_violation_is_reported_as_a_terminal_native_worker_crash() -> None:
    def run_crashed_worker(command: Sequence[str], **arguments: object) -> CompletedProcess[str]:
        return CompletedProcess(args=list(command), returncode=0xC0000005, stdout="", stderr="")

    client = IsolatedWpdClient(worker_command=("worker",), runner=run_crashed_worker)

    with pytest.raises(ExportFailure) as raised:
        client.list_targets()

    assert raised.value.stage == "worker_crash"
    assert raised.value.retryable is False
    assert raised.value.cause_type == "NativeWorkerExitC0000005"


def test_target_enumeration_timeout_is_retryable() -> None:
    def timeout_worker(command: Sequence[str], **_arguments: object) -> CompletedProcess[str]:
        raise TimeoutExpired(cmd=list(command), timeout=1.0)

    client = IsolatedWpdClient(worker_command=("worker",), runner=timeout_worker)

    with pytest.raises(ExportFailure) as raised:
        client.list_targets()

    assert raised.value.code is ErrorCode.EXP_DEVICE_DISCONNECTED
    assert raised.value.stage == "worker_timeout"
    assert raised.value.retryable is True


def test_native_worker_failure_is_terminal_and_does_not_kill_the_api_process(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "private-device-id-must-not-be-logged"
    worker_script = tmp_path / "crash_worker.py"
    worker_script.write_text(
        f"import os, sys\nsys.stderr.write({marker!r})\nsys.stdin.read()\nos._exit(17)\n",
        encoding="utf-8",
    )
    client = IsolatedWpdClient(worker_command=(sys.executable, str(worker_script)))
    asset = Asset(
        asset_id=uuid4(),
        asset_type=AssetType.CBZ,
        relative_path="library/chapters/chapter.cbz",
        sha256="a" * 64,
        size_bytes=3,
        created_at=datetime.now(UTC),
        verified_at=datetime.now(UTC),
    )
    target = ExportTarget(
        target_id="wpd:opaque-target",
        mount_path=None,
        display_name="Reader",
        file_system="MTP/WPD",
        total_bytes=100,
        free_bytes=90,
        transport="wpd",
        portable_device_id=marker,
        storage_object_id="private-storage-object",
        storage_persistent_id="private-persistent-id",
    )
    trace_id = "6df23532-5984-411b-b2da-880788da4dff"

    with pytest.raises(ExportFailure) as raised:
        client.export_asset(
            asset,
            source_path=tmp_path / "chapter.cbz",
            target=target,
            export_id=uuid4(),
            trace_id=trace_id,
        )

    assert raised.value.code is ErrorCode.EXP_COPY_FAILED
    assert raised.value.stage == "worker_crash"
    assert raised.value.retryable is False
    assert raised.value.trace_id == trace_id
    assert raised.value.cause_type == "WorkerExit17"
    assert marker not in caplog.text
    assert os.getpid() > 0


def test_worker_timeout_fails_closed_without_claiming_export_success(
    tmp_path: Path,
) -> None:
    worker_script = tmp_path / "slow_worker.py"
    worker_script.write_text(
        "import time\ntime.sleep(5)\n",
        encoding="utf-8",
    )
    client = IsolatedWpdClient(
        worker_command=(sys.executable, str(worker_script)),
        export_timeout_seconds=0.05,
    )
    asset = Asset(
        asset_id=uuid4(),
        asset_type=AssetType.CBZ,
        relative_path="library/chapters/chapter.cbz",
        sha256="b" * 64,
        size_bytes=3,
        created_at=datetime.now(UTC),
        verified_at=datetime.now(UTC),
    )
    target = ExportTarget(
        target_id="wpd:opaque-target",
        mount_path=None,
        display_name="Reader",
        file_system="MTP/WPD",
        total_bytes=100,
        free_bytes=90,
        transport="wpd",
        portable_device_id="private-device-id",
        storage_object_id="private-storage-object",
        storage_persistent_id="private-persistent-id",
    )

    with pytest.raises(ExportFailure) as raised:
        client.export_asset(
            asset,
            source_path=tmp_path / "chapter.cbz",
            target=target,
            export_id=uuid4(),
            trace_id="841e0dc3-3fd9-49e3-b898-05036dba12a3",
        )

    assert raised.value.code is ErrorCode.EXP_COPY_FAILED
    assert raised.value.stage == "worker_timeout"
    assert raised.value.retryable is False
    assert raised.value.cause_type == "TimeoutExpired"
