"""Run Windows Portable Device COM work outside the API process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Literal, NoReturn, cast
from uuid import UUID

from app.domain.asset import Asset, AssetType
from app.errors import ErrorCode
from app.services.usb_export import ExportFailure, ExportTarget
from app.services.wpd_export import WindowsWpdBackend, WpdExportService
from app.tracing import create_trace_id, current_trace_id

WPD_ENUMERATION_TIMEOUT_SECONDS = 120.0
WPD_EXPORT_TIMEOUT_SECONDS = 60 * 60.0
MAX_WORKER_REQUEST_BYTES = 64 * 1024
MAX_WORKER_RESPONSE_BYTES = 2 * 1024 * 1024

_SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


class IsolatedWpdClient:
    """Proxy all WPD work through a bounded, short-lived helper process."""

    def __init__(
        self,
        *,
        worker_command: Sequence[str] | None = None,
        runner: _SubprocessRunner = subprocess.run,
        enumeration_timeout_seconds: float = WPD_ENUMERATION_TIMEOUT_SECONDS,
        export_timeout_seconds: float = WPD_EXPORT_TIMEOUT_SECONDS,
    ) -> None:
        if worker_command is not None and (
            not worker_command or any(type(item) is not str or not item for item in worker_command)
        ):
            raise ValueError("worker_command must contain non-empty strings")
        if not callable(runner):
            raise TypeError("runner must be callable")
        if enumeration_timeout_seconds <= 0 or export_timeout_seconds <= 0:
            raise ValueError("worker timeouts must be positive")
        self._worker_command = tuple(worker_command) if worker_command is not None else None
        self._runner = runner
        self._enumeration_timeout_seconds = enumeration_timeout_seconds
        self._export_timeout_seconds = export_timeout_seconds
        self._lock = Lock()
        self._progress: dict[str, dict[str, object]] = {}

    def progress_for_trace(self, trace_id: str) -> dict[str, object] | None:
        progress = self._progress.get(trace_id)
        return dict(progress) if progress else None

    def list_targets(self) -> tuple[ExportTarget, ...]:
        trace_id = current_trace_id() or create_trace_id()
        response = self._request(
            {"action": "list_targets", "trace_id": trace_id},
            timeout=self._enumeration_timeout_seconds,
            trace_id=trace_id,
            failure_code=ErrorCode.EXP_DEVICE_DISCONNECTED,
            failure_retryable=True,
        )
        raw_values = response.get("targets")
        if type(raw_values) is not list:
            self._raise_worker_failure(
                ErrorCode.EXP_DEVICE_DISCONNECTED,
                "worker_response",
                trace_id=trace_id,
                retryable=True,
                cause_type="InvalidTargetList",
            )
        values = cast(list[object], raw_values)
        if len(values) > 1024:
            self._raise_worker_failure(
                ErrorCode.EXP_DEVICE_DISCONNECTED,
                "worker_response",
                trace_id=trace_id,
                retryable=True,
                cause_type="InvalidTargetList",
            )
        try:
            return tuple(_target_from_payload(item) for item in values)
        except TypeError, ValueError:
            self._raise_worker_failure(
                ErrorCode.EXP_DEVICE_DISCONNECTED,
                "worker_response",
                trace_id=trace_id,
                retryable=True,
                cause_type="InvalidTarget",
            )

    def export_asset(
        self,
        asset: Asset,
        *,
        source_path: Path,
        target: ExportTarget,
        export_id: UUID,
        trace_id: str,
    ) -> Literal["EXPORTED", "SKIPPED"]:
        response = self._request(
            {
                "action": "export_asset",
                "asset": _asset_to_payload(asset),
                "source_path": str(source_path),
                "target": _target_to_payload(target),
                "export_id": str(export_id),
                "trace_id": trace_id,
            },
            timeout=self._export_timeout_seconds,
            trace_id=trace_id,
            failure_code=ErrorCode.EXP_COPY_FAILED,
            failure_retryable=False,
        )
        status = response.get("status")
        if status not in {"EXPORTED", "SKIPPED"}:
            self._raise_worker_failure(
                ErrorCode.EXP_COPY_FAILED,
                "worker_response",
                trace_id=trace_id,
                retryable=False,
                cause_type="InvalidExportStatus",
            )
        return cast(Literal["EXPORTED", "SKIPPED"], status)

    def _request(
        self,
        request: dict[str, object],
        *,
        timeout: float,
        trace_id: str,
        failure_code: ErrorCode,
        failure_retryable: bool,
    ) -> dict[str, Any]:
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_WORKER_REQUEST_BYTES:
            self._raise_worker_failure(
                failure_code,
                "worker_request",
                trace_id=trace_id,
                retryable=False,
                cause_type="RequestTooLarge",
            )

        command = self._command()
        arguments: dict[str, object] = {
            "input": encoded,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "check": False,
            "timeout": timeout,
            "cwd": None if getattr(sys, "frozen", False) else _backend_root(),
        }
        if os.name == "nt":
            arguments["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            with self._lock:
                if self._runner is subprocess.run:
                    completed = self._run_streaming(command, arguments, request, trace_id)
                else:
                    completed = self._runner(command, **arguments)
        except subprocess.TimeoutExpired:
            self._raise_worker_failure(
                failure_code,
                "worker_timeout",
                trace_id=trace_id,
                retryable=failure_retryable,
                cause_type="TimeoutExpired",
            )
        except OSError as error:
            self._raise_worker_failure(
                failure_code,
                "worker_start",
                trace_id=trace_id,
                retryable=failure_retryable,
                cause_type=type(error).__name__,
            )

        stdout = completed.stdout or ""
        if len(stdout.encode("utf-8")) > MAX_WORKER_RESPONSE_BYTES:
            self._raise_worker_failure(
                failure_code,
                "worker_response",
                trace_id=trace_id,
                retryable=False,
                cause_type="ResponseTooLarge",
            )
        if completed.returncode != 0:
            is_access_violation = completed.returncode in {
                0xC0000005,
                -1073741819,
            }
            cause_type = (
                "NativeWorkerExitC0000005"
                if is_access_violation
                else f"WorkerExit{completed.returncode}"
            )
            self._raise_worker_failure(
                failure_code,
                "worker_crash",
                trace_id=trace_id,
                retryable=False,
                cause_type=cause_type,
            )

        try:
            response = _object_dict(json.loads(stdout))
        except TypeError, ValueError:
            self._raise_worker_failure(
                failure_code,
                "worker_response",
                trace_id=trace_id,
                retryable=False,
                cause_type="InvalidWorkerResponse",
            )
        if response is None:
            self._raise_worker_failure(
                failure_code,
                "worker_response",
                trace_id=trace_id,
                retryable=False,
                cause_type="InvalidWorkerResponse",
            )
        if response.get("ok") is True:
            return response

        error = _object_dict(response.get("error"))
        if error is None:
            self._raise_worker_failure(
                failure_code,
                "worker_response",
                trace_id=trace_id,
                retryable=False,
                cause_type="InvalidWorkerError",
            )
        raw_retryable = error.get("retryable")
        self._raise_worker_failure(
            _safe_error_code(error.get("code"), fallback=failure_code),
            _safe_string(error.get("stage"), maximum=64, fallback="worker_error"),
            trace_id=trace_id,
            retryable=raw_retryable if isinstance(raw_retryable, bool) else failure_retryable,
            cause_type=_safe_string(error.get("cause_type"), maximum=64, fallback="WorkerError"),
            staging_name=_safe_optional_string(error.get("staging_name")),
        )

    def _command(self) -> tuple[str, ...]:
        if self._worker_command is not None:
            return self._worker_command
        if getattr(sys, "frozen", False):
            return (sys.executable, "--wpd-worker")
        # Embedded Python ignores PYTHONPATH; preserve the running app's import paths.
        import_paths = list(dict.fromkeys(sys.path))
        bootstrap = (
            f"import sys; sys.path[:] = {import_paths!r}; "
            "from app.services.wpd_worker import run_worker; raise SystemExit(run_worker())"
        )
        return (sys.executable, "-c", bootstrap)

    def _run_streaming(
        self,
        command: Sequence[str],
        arguments: dict[str, object],
        request: dict[str, object],
        trace_id: str,
    ) -> subprocess.CompletedProcess[str]:
        output: list[str] = []
        invalid = False
        size = 0
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            cwd=None if getattr(sys, "frozen", False) else _backend_root(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert process.stdout is not None and process.stdin is not None
        stdout = process.stdout

        def read_output() -> None:
            nonlocal invalid, size
            try:
                while line := stdout.readline(MAX_WORKER_RESPONSE_BYTES + 1):
                    if len(line.encode("utf-8")) > MAX_WORKER_RESPONSE_BYTES:
                        raise ValueError("Worker line too large")
                    frame = _object_dict(json.loads(line))
                    if frame is not None and "progress" in frame:
                        progress = _object_dict(frame["progress"])
                        asset = _object_dict(request.get("asset"))
                        if (
                            progress is None
                            or asset is None
                            or progress.get("stage")
                            not in {
                                "verify_source",
                                "copy",
                                "verify_staging",
                                "resume_staging",
                                "destination_verify",
                                "publish",
                                "verify_final",
                            }
                            or type(progress.get("processed_bytes")) is not int
                            or type(progress.get("total_bytes")) is not int
                            or progress["total_bytes"] != asset.get("size_bytes")
                            or not 0
                            <= cast(int, progress["processed_bytes"])
                            <= cast(int, progress["total_bytes"])
                            <= 8 * 1024**3
                        ):
                            raise ValueError("Invalid worker progress")
                        self._progress[trace_id] = progress
                    else:
                        size += len(line.encode("utf-8"))
                        if size > MAX_WORKER_RESPONSE_BYTES:
                            raise ValueError("Worker response too large")
                        output.append(line)
            except OSError, ValueError:
                invalid = True
                process.kill()

        reader = Thread(target=read_output, daemon=True)
        reader.start()
        try:
            process.stdin.write(str(arguments["input"]))
            process.stdin.close()
            process.wait(timeout=cast(float, arguments["timeout"]))
            reader.join(timeout=5)
            if reader.is_alive() or invalid:
                return subprocess.CompletedProcess(command, 0, "invalid worker response")
            return subprocess.CompletedProcess(command, process.returncode, "".join(output))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            reader.join(timeout=5)
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()
            self._progress.pop(trace_id, None)

    @staticmethod
    def _raise_worker_failure(
        code: ErrorCode,
        stage: str,
        *,
        trace_id: str,
        retryable: bool,
        cause_type: str,
        staging_name: str | None = None,
    ) -> NoReturn:
        raise ExportFailure(
            code,
            stage,
            trace_id=trace_id,
            retryable=retryable,
            staging_name=staging_name,
            cause_type=cause_type,
        )


def run_worker() -> int:
    """Serve one bounded request, streaming progress before its final JSON result."""
    raw_request = sys.stdin.buffer.read(MAX_WORKER_REQUEST_BYTES + 1)
    if len(raw_request) > MAX_WORKER_REQUEST_BYTES:
        response = _worker_error(
            ErrorCode.EXP_COPY_FAILED,
            "worker_request",
            trace_id=create_trace_id(),
            retryable=False,
            cause_type="RequestTooLarge",
        )
    else:
        try:
            request = json.loads(raw_request.decode("utf-8"))
        except UnicodeDecodeError, ValueError:
            response = _worker_error(
                ErrorCode.EXP_COPY_FAILED,
                "worker_request",
                trace_id=create_trace_id(),
                retryable=False,
                cause_type="InvalidWorkerRequest",
            )
        else:
            response = _execute_worker_request(request)
    encoded_response = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded_response) > MAX_WORKER_RESPONSE_BYTES:
        encoded_response = json.dumps(
            _worker_error(
                ErrorCode.EXP_COPY_FAILED,
                "worker_response",
                trace_id=create_trace_id(),
                retryable=False,
                cause_type="ResponseTooLarge",
            ),
            separators=(",", ":"),
        ).encode("utf-8")
    sys.stdout.buffer.write(encoded_response + b"\n")
    sys.stdout.buffer.flush()
    return 0


def _execute_worker_request(request_value: object) -> dict[str, object]:
    request = _object_dict(request_value)
    if request is None:
        return _worker_error(
            ErrorCode.EXP_COPY_FAILED,
            "worker_request",
            trace_id=create_trace_id(),
            retryable=False,
            cause_type="InvalidWorkerRequest",
        )
    action = request.get("action")
    trace_id = _safe_string(request.get("trace_id"), maximum=64, fallback=create_trace_id())
    last_stage = ""
    last_emit = 0.0

    def report_progress(stage: str, processed_bytes: int, total_bytes: int) -> None:
        nonlocal last_stage, last_emit
        now = time.monotonic()
        if stage == last_stage and processed_bytes != total_bytes and now - last_emit < 1:
            return
        last_stage, last_emit = stage, now
        sys.stdout.write(
            json.dumps(
                {
                    "progress": {
                        "stage": stage,
                        "processed_bytes": processed_bytes,
                        "total_bytes": total_bytes,
                    }
                }
            )
            + "\n"
        )
        sys.stdout.flush()

    failure_code = (
        ErrorCode.EXP_DEVICE_DISCONNECTED if action == "list_targets" else ErrorCode.EXP_COPY_FAILED
    )
    try:
        backend = WindowsWpdBackend()
        if action == "list_targets":
            return {
                "ok": True,
                "targets": [_target_to_payload(target) for target in backend.list_targets()],
            }
        if action == "export_asset":
            asset_value = _object_dict(request.get("asset"))
            if asset_value is None:
                raise ValueError("Invalid worker asset.")
            source_path_value = request.get("source_path")
            if type(source_path_value) is not str:
                raise ValueError("Invalid worker source path.")
            source_path = Path(source_path_value)
            if not source_path.is_absolute():
                raise ValueError("Worker source path must be absolute.")
            target = _target_from_payload(request.get("target"))
            export_id_value = request.get("export_id")
            if type(export_id_value) is not str:
                raise ValueError("Invalid worker export ID.")
            result = WpdExportService(backend, progress_callback=report_progress).export_asset(
                _asset_from_payload(asset_value),
                source_path=source_path,
                target=target,
                export_id=UUID(export_id_value),
                trace_id=trace_id,
            )
            return {"ok": True, "status": result}
        return _worker_error(
            ErrorCode.EXP_COPY_FAILED,
            "worker_request",
            trace_id=trace_id,
            retryable=False,
            cause_type="UnsupportedWorkerAction",
        )
    except ExportFailure as error:
        return _worker_error(
            error.code,
            error.stage,
            trace_id=error.trace_id,
            retryable=error.retryable,
            cause_type=error.cause_type or type(error).__name__,
            staging_name=error.staging_name,
        )
    except Exception as error:
        return _worker_error(
            failure_code,
            "worker_runtime",
            trace_id=trace_id,
            retryable=False,
            cause_type=type(error).__name__,
        )


def _worker_error(
    code: ErrorCode,
    stage: str,
    *,
    trace_id: str,
    retryable: bool,
    cause_type: str,
    staging_name: str | None = None,
) -> dict[str, object]:
    error: dict[str, object] = {
        "code": code.value,
        "stage": stage[:64],
        "trace_id": trace_id,
        "retryable": retryable,
        "cause_type": cause_type[:64],
    }
    if staging_name is not None:
        error["staging_name"] = staging_name[:255]
    return {"ok": False, "error": error}


def _asset_to_payload(asset: Asset) -> dict[str, object]:
    return {
        "asset_id": str(asset.asset_id),
        "asset_type": asset.asset_type.value,
        "relative_path": asset.relative_path,
        "sha256": asset.sha256,
        "size_bytes": asset.size_bytes,
        "created_at": asset.created_at.isoformat(),
        "verified_at": asset.verified_at.isoformat() if asset.verified_at is not None else None,
    }


def _asset_from_payload(value: dict[str, object]) -> Asset:
    created_at = value.get("created_at")
    verified_at = value.get("verified_at")
    if type(created_at) is not str or (verified_at is not None and type(verified_at) is not str):
        raise ValueError("Invalid worker asset timestamps.")
    return Asset(
        asset_id=UUID(_required_string(value, "asset_id")),
        asset_type=AssetType(_required_string(value, "asset_type")),
        relative_path=_required_string(value, "relative_path"),
        sha256=_required_string(value, "sha256"),
        size_bytes=_required_int(value, "size_bytes"),
        created_at=datetime.fromisoformat(created_at),
        verified_at=(datetime.fromisoformat(verified_at) if isinstance(verified_at, str) else None),
    )


def _target_to_payload(target: ExportTarget) -> dict[str, object]:
    return {
        "target_id": target.target_id,
        "mount_path": str(target.mount_path) if target.mount_path is not None else None,
        "display_name": target.display_name,
        "file_system": target.file_system,
        "total_bytes": target.total_bytes,
        "free_bytes": target.free_bytes,
        "transport": target.transport,
        "portable_device_id": target.portable_device_id,
        "storage_object_id": target.storage_object_id,
        "storage_persistent_id": target.storage_persistent_id,
    }


def _target_from_payload(value: object) -> ExportTarget:
    target_value = _object_dict(value)
    if target_value is None:
        raise ValueError("Invalid worker target.")
    transport = _required_string(target_value, "transport")
    if transport not in {"volume", "wpd"}:
        raise ValueError("Invalid worker target transport.")
    mount_path = target_value.get("mount_path")
    return ExportTarget(
        target_id=_required_string(target_value, "target_id"),
        mount_path=Path(mount_path) if isinstance(mount_path, str) else None,
        display_name=_required_string(target_value, "display_name"),
        file_system=_required_string(target_value, "file_system"),
        total_bytes=_required_int(target_value, "total_bytes"),
        free_bytes=_required_int(target_value, "free_bytes"),
        transport=cast(Literal["volume", "wpd"], transport),
        portable_device_id=_optional_string(target_value.get("portable_device_id")),
        storage_object_id=_optional_string(target_value.get("storage_object_id")),
        storage_persistent_id=_optional_string(target_value.get("storage_persistent_id")),
    )


def _object_dict(value: object) -> dict[str, object] | None:
    if type(value) is not dict:
        return None
    candidate = cast(dict[object, object], value)
    result: dict[str, object] = {}
    for key, item in candidate.items():
        if type(key) is not str:
            return None
        result[key] = item
    return result


def _required_string(value: dict[str, object], name: str) -> str:
    result = value.get(name)
    if type(result) is not str:
        raise ValueError(f"Invalid worker field: {name}.")
    return result


def _required_int(value: dict[str, object], name: str) -> int:
    result = value.get(name)
    if type(result) is not int:
        raise ValueError(f"Invalid worker field: {name}.")
    return result


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is str:
        return value
    raise ValueError("Invalid optional worker field.")


def _safe_optional_string(value: object) -> str | None:
    return value if type(value) is str else None


def _safe_string(value: object, *, maximum: int, fallback: str) -> str:
    return value[:maximum] if type(value) is str and value else fallback


def _safe_error_code(value: object, *, fallback: ErrorCode) -> ErrorCode:
    if type(value) is str:
        try:
            return ErrorCode(value)
        except ValueError:
            pass
    return fallback


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[2]


if __name__ == "__main__":
    raise SystemExit(run_worker())
