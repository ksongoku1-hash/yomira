"""Bounded, recoverable persistence for download manifests and page files."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import logging
import os
import re
import stat
import threading
from _thread import RLock as RLockType
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast
from uuid import UUID, uuid4

from app.domain.download_page import validate_temporary_path
from app.domain.job_manifest import (
    MAX_MANIFEST_BYTES,
    DownloadJobManifest,
    manifest_from_bytes,
    manifest_to_bytes,
)
from app.errors import ErrorCode
from app.persistence.filesystem_retry import (
    RetryingFileStream,
    close_read_handle,
    is_transient_lock_error,
    retry_filesystem_operation,
    retry_lstat,
)
from app.persistence.storage_gate import (
    SharedStorageGate,
    StorageGateClosedError,
    is_disk_full_os_error,
)
from app.persistence.windows_paths import (
    DataRootIdentity,
    DataRootIdentityError,
    is_absolute_windows_path,
    windows_path_key,
)
from app.tracing import create_trace_id, current_trace_id

logger = logging.getLogger("yomira.manifest_store")

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ABSOLUTE_PATH_UTF16_UNITS = 259
_JOB_MUTEX_TIMEOUT_MS = 5_000
_MAX_MANIFEST_GENERATION = (1 << 63) - 1
_MAX_RECOVERY_ENTRIES = 4
_MAX_RECOVERY_BYTES = 32 * 1024 * 1024
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_DISK_FULL_WINERRORS = frozenset({39, 112})


class ManifestStoreError(RuntimeError):
    """Safe, machine-readable persistence error without user paths or payloads."""

    def __init__(self, code: ErrorCode, stage: str) -> None:
        self.code = code
        self.stage = stage
        self.retryable = False
        super().__init__(f"Manifest persistence failed during {stage}.")


@dataclass(frozen=True, slots=True)
class ManifestWriteReservation:
    """One-use authorization for one manifest publication under one job lock."""

    job_id: UUID
    expected_current_generation: int
    new_generation: int
    current_sha256: str
    current_size_bytes: int
    new_sha256: str
    new_size_bytes: int
    target_sha256: str | None
    target_size_bytes: int | None
    retained_backup_sha256: str | None
    issuer_nonce: UUID
    token_id: UUID
    acquisition_id: UUID
    thread_id: int


def _fail(
    code: ErrorCode,
    stage: str,
    *,
    job_id: UUID | None = None,
    cause: BaseException | None = None,
    cleanup_cause: BaseException | None = None,
) -> NoReturn:
    """Log stable diagnostic fields only; never log a full filesystem path."""
    trace_id = current_trace_id() or create_trace_id()
    extra: dict[str, object] = {
        "component": "manifest_store",
        "error_code": code.value,
        "retryable": False,
        "stage": stage,
        "trace_id": trace_id,
    }
    if job_id is not None:
        extra["job_id"] = str(job_id)
    if cause is not None:
        extra["cause_type"] = type(cause).__name__
    if cleanup_cause is not None:
        extra["cleanup_cause_type"] = type(cleanup_cause).__name__
    logger.error("manifest_store.operation_failed", extra=extra)
    raise ManifestStoreError(code, stage) from None


def _temporary_page_io_error_code(error: OSError) -> ErrorCode:
    if (
        error.errno == errno.ENOSPC
        or getattr(error, "winerror", None) in _DISK_FULL_WINERRORS
    ):
        return ErrorCode.SYS_DISK_BUDGET_EXCEEDED
    return ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE


def _utf16_units(path: Path) -> int:
    try:
        return len(str(path).encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        raise ValueError("The path contains invalid Unicode.") from None


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _lstat_or_none(
    path: Path,
    *,
    job_id: UUID | None = None,
    trace_id: str | None = None,
    validate_path: Callable[[], None] | None = None,
    expected_parent_identity: tuple[int, int] | None = None,
) -> os.stat_result | None:
    try:
        return retry_lstat(
            path,
            component="manifest_store",
            stage="path_lstat",
            trace_id=trace_id or current_trace_id() or create_trace_id(),
            job_id=str(job_id) if job_id is not None else None,
            validate_path=validate_path,
            expected_parent_identity=expected_parent_identity,
        )
    except FileNotFoundError:
        return None
    except OSError:
        raise


def _read_manifest_bytes(
    path: Path,
    *,
    max_bytes: int,
    expected_identity: tuple[int, int] | None = None,
    expected_size: int | None = None,
    expected_parent_identity: tuple[int, int] | None = None,
    job_id: UUID | None = None,
    trace_id: str | None = None,
) -> bytes:
    """Read a bounded, stable file; lock retries reopen and restart at byte zero."""
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    stable_trace_id = trace_id or current_trace_id() or create_trace_id()
    parent_identity = _directory_identity(
        path.parent,
        job_id=job_id,
        trace_id=stable_trace_id,
    )
    if (
        parent_identity is None
        or (
            expected_parent_identity is not None
            and parent_identity != expected_parent_identity
        )
    ):
        raise OSError(errno.EIO, "manifest parent identity changed")
    info = _lstat_or_none(
        path,
        job_id=job_id,
        trace_id=stable_trace_id,
        expected_parent_identity=parent_identity,
    )
    if info is None:
        raise FileNotFoundError(errno.ENOENT, "manifest candidate is absent")
    if _is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
        raise OSError(errno.ELOOP, "manifest candidate is not a regular file")
    identity = _managed_file_identity(info)
    size = info.st_size
    if (
        identity is None
        or (expected_identity is not None and identity != expected_identity)
        or (expected_size is not None and size != expected_size)
    ):
        raise OSError(errno.EIO, "manifest candidate identity changed")

    def snapshot_matches() -> bool:
        try:
            return (
                _managed_file_state(
                    path,
                    job_id=job_id,
                    trace_id=stable_trace_id,
                    expected_parent_identity=parent_identity,
                )
                == (identity, size)
                and _directory_identity(
                    path.parent,
                    job_id=job_id,
                    trace_id=stable_trace_id,
                )
                == parent_identity
            )
        except OSError:
            return False

    def read_from_start() -> bytes:
        if not snapshot_matches():
            raise OSError(errno.EIO, "manifest candidate changed before read")
        stream = path.open("rb")
        try:
            stream = stream.__enter__()
        except BaseException as enter_error:
            close_error = close_read_handle(
                stream,
                component="manifest_store",
                stage="manifest_read_enter_close",
                trace_id=stable_trace_id,
                job_id=str(job_id) if job_id is not None else None,
            )
            try:
                definitely_closed = stream.closed is True
            except BaseException:
                definitely_closed = False
            if not definitely_closed:
                failure = close_error or OSError(
                    errno.EIO, "Manifest read handle close is unverified"
                )
                raise failure from enter_error
            raise enter_error
        operation_error: BaseException | None = None
        value: bytes | None = None
        closed: os.stat_result | None = None
        try:
            opened = os.fstat(stream.fileno())
            if (
                _is_reparse_point(opened)
                or not stat.S_ISREG(opened.st_mode)
                or _managed_file_identity(opened) != identity
                or opened.st_size != size
            ):
                raise OSError(errno.EIO, "manifest candidate changed before read")
            value = stream.read(max_bytes + 1)
            closed = os.fstat(stream.fileno())
        except BaseException as error:
            operation_error = error
        close_error = close_read_handle(
            stream,
            component="manifest_store",
            stage="manifest_read_close",
            trace_id=stable_trace_id,
            job_id=str(job_id) if job_id is not None else None,
        )
        try:
            definitely_closed = stream.closed is True
        except BaseException:
            definitely_closed = False
        if not definitely_closed:
            raise close_error or OSError(errno.EIO, "Manifest read handle close is unverified")
        if close_error is not None and (
            operation_error is None
            or not isinstance(close_error, OSError)
            or not is_transient_lock_error(close_error)
        ):
            raise close_error
        if operation_error is not None:
            raise operation_error
        if close_error is not None:
            raise close_error
        if value is None or closed is None:
            raise OSError(errno.EIO, "manifest read produced no stable result")
        if (
            _is_reparse_point(closed)
            or not stat.S_ISREG(closed.st_mode)
            or _managed_file_identity(closed) != identity
            or closed.st_size != size
            or not snapshot_matches()
        ):
            raise OSError(errno.EIO, "manifest candidate changed during read")
        return value

    value = retry_filesystem_operation(
        read_from_start,
        unchanged=snapshot_matches,
        component="manifest_store",
        stage="manifest_read",
        trace_id=stable_trace_id,
        job_id=str(job_id) if job_id is not None else None,
    )
    if len(value) > max_bytes:
        raise ValueError("manifest exceeds its configured byte bound")
    return value


def _managed_file_identity(info: os.stat_result) -> tuple[int, int] | None:
    file_id = getattr(info, "st_ino", 0)
    if type(info.st_dev) is not int or type(file_id) is not int or file_id == 0:
        return None
    return info.st_dev, file_id


def _directory_identity(
    path: Path,
    *,
    job_id: UUID | None = None,
    trace_id: str | None = None,
    expected_parent_identity: tuple[int, int] | None = None,
) -> tuple[int, int] | None:
    try:
        info = retry_lstat(
            path,
            component="manifest_store",
            stage="directory_identity_stat",
            trace_id=trace_id or current_trace_id() or create_trace_id(),
            job_id=str(job_id) if job_id is not None else None,
            expected_parent_identity=expected_parent_identity,
        )
    except OSError:
        return None
    if _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
        return None
    return _managed_file_identity(info)


def _managed_file_state(
    path: Path,
    *,
    job_id: UUID | None = None,
    trace_id: str | None = None,
    expected_parent_identity: tuple[int, int] | None = None,
) -> tuple[tuple[int, int], int] | None:
    info = retry_lstat(
        path,
        component="manifest_store",
        stage="managed_file_state_stat",
        trace_id=trace_id or current_trace_id() or create_trace_id(),
        job_id=str(job_id) if job_id is not None else None,
        expected_parent_identity=expected_parent_identity,
    )
    if _is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
        return None
    identity = _managed_file_identity(info)
    if identity is None or type(info.st_size) is not int:
        return None
    return identity, info.st_size


def _manifest_file_matches(
    path: Path,
    *,
    identity: tuple[int, int],
    expected_bytes: bytes,
    job_id: UUID | None = None,
    trace_id: str | None = None,
) -> bool:
    stable_trace_id = trace_id or current_trace_id() or create_trace_id()
    try:
        parent_identity = _directory_identity(
            path.parent,
            job_id=job_id,
            trace_id=stable_trace_id,
        )
        info = _lstat_or_none(
            path,
            job_id=job_id,
            trace_id=stable_trace_id,
            expected_parent_identity=parent_identity,
        )
        if (
            parent_identity is None
            or info is None
            or _is_reparse_point(info)
            or not stat.S_ISREG(info.st_mode)
            or _managed_file_identity(info) != identity
            or info.st_size != len(expected_bytes)
        ):
            return False
        if job_id is None and trace_id is None:
            value = _read_manifest_bytes(
                path,
                max_bytes=MAX_MANIFEST_BYTES,
                expected_identity=identity,
                expected_size=len(expected_bytes),
                expected_parent_identity=parent_identity,
            )
        else:
            value = _read_manifest_bytes(
                path,
                max_bytes=MAX_MANIFEST_BYTES,
                expected_identity=identity,
                expected_size=len(expected_bytes),
                expected_parent_identity=parent_identity,
                job_id=job_id,
                trace_id=stable_trace_id,
            )
        return value == expected_bytes
    except Exception:
        return False


def _path_is_absent(
    path: Path,
    *,
    job_id: UUID | None = None,
    trace_id: str | None = None,
    expected_parent_identity: tuple[int, int] | None = None,
) -> bool:
    try:
        retry_lstat(
            path,
            component="manifest_store",
            stage="path_absence_stat",
            trace_id=trace_id or current_trace_id() or create_trace_id(),
            job_id=str(job_id) if job_id is not None else None,
            expected_parent_identity=expected_parent_identity,
        )
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _move_file_ex_win32(source_path: Path, destination_path: Path) -> None:
    """Publish a new file without replace/copy flags on Windows."""
    if os.name == "nt" and hasattr(ctypes, "WinDLL"):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        move_file = kernel32.MoveFileExW  # type: ignore[attr-defined]
        move_file.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        move_file.restype = ctypes.c_int
        # Flags deliberately remain zero: no REPLACE_EXISTING and no COPY_ALLOWED.
        if not move_file(str(source_path), str(destination_path), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return

    # A same-volume hard-link creation is atomic and fails if the destination
    # exists; removing the staging name after that does not expose partial data.
    os.link(source_path, destination_path)
    source_path.unlink()


def _replace_file_win32(
    replaced_path: Path,
    replacement_path: Path,
    backup_path: Path,
) -> None:
    """Atomically replace a manifest while retaining its old bytes as backup."""
    if os.name == "nt" and hasattr(ctypes, "WinDLL"):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        replace_file = kernel32.ReplaceFileW  # type: ignore[attr-defined]
        replace_file.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        replace_file.restype = ctypes.c_int
        if not replace_file(
            str(replaced_path),
            str(replacement_path),
            str(backup_path),
            0,
            None,
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return

    # Link the previous target first. A crash before os.replace leaves the old
    # destination plus both complete recovery candidates, never a missing
    # destination. The final os.replace is atomic within this directory.
    os.link(replaced_path, backup_path)
    os.replace(replacement_path, replaced_path)


def _data_root_key(data_root: Path | str) -> str:
    return windows_path_key(os.fspath(data_root))


def _validate_root_argument(
    data_root: Path | str,
    data_root_identity: DataRootIdentity | None,
    *,
    job_id: UUID | None = None,
) -> DataRootIdentity:
    if not isinstance(data_root_identity, DataRootIdentity):
        _fail(
            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
            "root_identity",
            job_id=job_id,
        )
    try:
        requested_path = Path(data_root)
        canonical_path = data_root_identity.canonical_path
        if (
            not is_absolute_windows_path(requested_path)
            or not is_absolute_windows_path(canonical_path)
            or _data_root_key(requested_path) != _data_root_key(canonical_path)
        ):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "root_identity",
                job_id=job_id,
            )
    except ManifestStoreError:
        raise
    except Exception as error:
        _fail(
            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
            "root_identity",
            job_id=job_id,
            cause=error,
        )
    return data_root_identity


def _assert_root_identity(
    identity: DataRootIdentity,
    *,
    job_id: UUID | None = None,
    stage: str = "root_identity",
) -> None:
    try:
        identity.assert_current()
    except (DataRootIdentityError, OSError, RuntimeError) as error:
        _fail(
            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
            stage,
            job_id=job_id,
            cause=error,
        )


def _assert_path_length(path: Path, *, stage: str, job_id: UUID | None = None) -> None:
    try:
        count = _utf16_units(path)
    except (ValueError, UnicodeError) as error:
        _fail(
            ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
            stage,
            job_id=job_id,
            cause=error,
        )
    if count > _MAX_ABSOLUTE_PATH_UTF16_UNITS:
        logger.error(
            "manifest_store.path_limit_exceeded",
            extra={
                "component": "manifest_store",
                "error_code": ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE.value,
                "retryable": False,
                "stage": stage,
                "path_utf16_units": count,
                "trace_id": current_trace_id() or create_trace_id(),
                **({"job_id": str(job_id)} if job_id is not None else {}),
            },
        )
        raise ManifestStoreError(ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE, stage)


def _assert_no_reparse(
    path: Path,
    *,
    expected: str | None,
    allow_missing: bool,
    stage: str,
    identity: DataRootIdentity,
    job_id: UUID | None = None,
    trace_id: str | None = None,
    expected_parent_identity: tuple[int, int] | None = None,
) -> os.stat_result | None:
    stable_trace_id = trace_id or current_trace_id() or create_trace_id()

    def validate_root() -> None:
        _assert_root_identity(
            identity,
            job_id=job_id,
            stage="root_identity",
        )

    try:
        if expected_parent_identity is not None:
            info = _lstat_or_none(
                path,
                job_id=job_id,
                trace_id=stable_trace_id,
                validate_path=validate_root,
                expected_parent_identity=expected_parent_identity,
            )
        else:
            try:
                relative = path.relative_to(identity.canonical_path)
            except ValueError:
                relative = None
            if relative is None or any(part in {".", ".."} for part in relative.parts):
                info = _lstat_or_none(
                    path,
                    job_id=job_id,
                    trace_id=stable_trace_id,
                    validate_path=validate_root,
                )
            else:
                current = identity.canonical_path
                info = _lstat_or_none(
                    current,
                    job_id=job_id,
                    trace_id=stable_trace_id,
                    validate_path=validate_root,
                )
                if (
                    info is None
                    or _is_reparse_point(info)
                    or not stat.S_ISDIR(info.st_mode)
                ):
                    _fail(
                        ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
                        stage,
                        job_id=job_id,
                    )
                parent_identity = _managed_file_identity(info)
                for index, component in enumerate(relative.parts):
                    current = current / component
                    info = _lstat_or_none(
                        current,
                        job_id=job_id,
                        trace_id=stable_trace_id,
                        validate_path=validate_root,
                        expected_parent_identity=parent_identity,
                    )
                    if info is None:
                        break
                    if index < len(relative.parts) - 1:
                        if _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
                            _fail(
                                ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
                                stage,
                                job_id=job_id,
                            )
                        parent_identity = _managed_file_identity(info)
    except OSError as error:
        _fail(
            ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
            stage,
            job_id=job_id,
            cause=error,
        )
    if info is None:
        if allow_missing:
            return None
        _fail(
            ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
            stage,
            job_id=job_id,
        )
    if _is_reparse_point(info):
        _fail(
            ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
            stage,
            job_id=job_id,
        )
    if expected == "directory" and not stat.S_ISDIR(info.st_mode):
        _fail(
            ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
            stage,
            job_id=job_id,
        )
    if expected == "file" and not stat.S_ISREG(info.st_mode):
        _fail(
            ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
            stage,
            job_id=job_id,
        )
    return info


def _directory_path_state(
    identity: DataRootIdentity,
    path: Path,
    *,
    create: bool,
    stage: str,
    job_id: UUID | None,
    storage_gate: SharedStorageGate | None = None,
    trace_id: str | None = None,
    expected_parent_identity: tuple[int, int] | None = None,
) -> os.stat_result:
    stable_trace_id = trace_id or current_trace_id() or create_trace_id()
    parent_identity = expected_parent_identity
    if parent_identity is None:
        parent_info = _assert_no_reparse(
            path.parent,
            expected="directory",
            allow_missing=False,
            stage=stage,
            identity=identity,
            job_id=job_id,
            trace_id=stable_trace_id,
        )
        if parent_info is None:
            _fail(
                ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
                stage,
                job_id=job_id,
            )
        parent_identity = _managed_file_identity(parent_info)
        if parent_identity is None:
            _fail(
                ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
                stage,
                job_id=job_id,
            )
    info = _assert_no_reparse(
        path,
        expected=None,
        allow_missing=True,
        stage=stage,
        identity=identity,
        job_id=job_id,
        trace_id=stable_trace_id,
        expected_parent_identity=parent_identity,
    )
    if info is not None:
        if not stat.S_ISDIR(info.st_mode):
            _fail(
                ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
                stage,
                job_id=job_id,
            )
        return info
    if not create:
        _fail(
            ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
            stage,
            job_id=job_id,
        )
    retry_lstat_budget_trace = stable_trace_id

    def directory_parent_is_unchanged() -> bool:
        _assert_root_identity(identity, job_id=job_id, stage=stage)
        current_parent = _lstat_or_none(
            path.parent,
            job_id=job_id,
            trace_id=retry_lstat_budget_trace,
        )
        if (
            current_parent is None
            or _is_reparse_point(current_parent)
            or not stat.S_ISDIR(current_parent.st_mode)
            or _managed_file_identity(current_parent) != parent_identity
        ):
            return False
        return (
            _lstat_or_none(
                path,
                job_id=job_id,
                trace_id=retry_lstat_budget_trace,
                validate_path=lambda: _assert_root_identity(
                    identity,
                    job_id=job_id,
                    stage=stage,
                ),
                expected_parent_identity=parent_identity,
            )
            is None
        )

    def directory_was_created() -> bool:
        try:
            _assert_root_identity(identity, job_id=job_id, stage=stage)
            current_parent = _lstat_or_none(
                path.parent,
                job_id=job_id,
                trace_id=retry_lstat_budget_trace,
            )
            current = _lstat_or_none(
                path,
                job_id=job_id,
                trace_id=retry_lstat_budget_trace,
                validate_path=lambda: _assert_root_identity(
                    identity,
                    job_id=job_id,
                    stage=stage,
                ),
                expected_parent_identity=parent_identity,
            )
        except Exception:
            return False
        return (
            current_parent is not None
            and not _is_reparse_point(current_parent)
            and stat.S_ISDIR(current_parent.st_mode)
            and _managed_file_identity(current_parent) == parent_identity
            and current is not None
            and not _is_reparse_point(current)
            and stat.S_ISDIR(current.st_mode)
        )

    def mkdir_once() -> None:
        _assert_root_identity(identity, job_id=job_id, stage=stage)
        try:
            if storage_gate is None:
                path.mkdir()
            else:
                with storage_gate.mutation():
                    try:
                        path.mkdir()
                    except OSError as error:
                        if is_disk_full_os_error(error):
                            storage_gate.close("FILESYSTEM_DISK_FULL")
                        raise
        except FileExistsError:
            # A racing directory creation is acceptable only if it is the
            # exact directory under the captured parent.
            if directory_was_created():
                return
            raise

    try:
        retry_filesystem_operation(
            mkdir_once,
            unchanged=directory_parent_is_unchanged,
            applied=directory_was_created,
            component="manifest_store",
            stage=stage,
            trace_id=stable_trace_id,
            job_id=str(job_id) if job_id is not None else None,
        )
    except StorageGateClosedError:
        raise
    except OSError as error:
        if storage_gate is not None and is_disk_full_os_error(error):
            storage_gate.close("FILESYSTEM_DISK_FULL")
        _fail(
            ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
            stage,
            job_id=job_id,
            cause=error,
        )
    created_info = _assert_no_reparse(
        path,
        expected="directory",
        allow_missing=False,
        stage=stage,
        identity=identity,
        job_id=job_id,
        trace_id=stable_trace_id,
        expected_parent_identity=parent_identity,
    )
    if created_info is None:
        _fail(
            ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
            stage,
            job_id=job_id,
        )
    return created_info


def _manifest_mutex_name(identity: DataRootIdentity, job_id: UUID) -> str:
    material = (
        identity.volume_serial.to_bytes(8, byteorder="big", signed=False)
        + identity.file_id
        + job_id.bytes
    )
    return "Global\\Yomira.JobManifest." + hashlib.sha256(material).hexdigest()


class _HeldMutex:
    __slots__ = (
        "depth",
        "handle",
        "thread_lock",
        "recovery_active",
        "acquisition_id",
        "reservation_ids",
        "consumed_reservation_ids",
    )

    def __init__(
        self,
        *,
        handle: int | None,
        thread_lock: RLockType | None,
    ) -> None:
        self.depth = 1
        self.handle = handle
        self.thread_lock = thread_lock
        self.recovery_active = False
        self.acquisition_id = uuid4()
        self.reservation_ids: set[UUID] = set()
        self.consumed_reservation_ids: set[UUID] = set()


_thread_state = threading.local()
_fallback_locks_guard = threading.Lock()
_fallback_locks: dict[str, RLockType] = {}


def _thread_holds() -> dict[str, _HeldMutex]:
    holds: object = getattr(_thread_state, "manifest_mutex_holds", None)
    if not isinstance(holds, dict):
        holds = {}
        _thread_state.manifest_mutex_holds = holds
    return cast(dict[str, _HeldMutex], holds)


def _named_mutex_create_and_wait(name: str) -> int:
    if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
        raise OSError("The named Win32 mutex is unavailable.")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    create_mutex = kernel32.CreateMutexW  # type: ignore[attr-defined]
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    handle = create_mutex(None, 0, name)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        raise ctypes.WinError(ctypes.get_last_error())
    wait_for_single_object = kernel32.WaitForSingleObject  # type: ignore[attr-defined]
    wait_for_single_object.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    wait_for_single_object.restype = ctypes.c_uint32
    result = int(wait_for_single_object(handle, _JOB_MUTEX_TIMEOUT_MS))
    if result in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
        return int(handle)

    close_handle = kernel32.CloseHandle  # type: ignore[attr-defined]
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    close_handle(handle)
    if result == _WAIT_TIMEOUT:
        raise TimeoutError("The job mutex wait timed out.")
    if result == _WAIT_FAILED:
        raise OSError("WaitForSingleObject failed for the job mutex.")
    raise OSError("WaitForSingleObject returned an unsupported job mutex result.")


def _named_mutex_release_and_close(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    release_mutex = kernel32.ReleaseMutex  # type: ignore[attr-defined]
    release_mutex.argtypes = (ctypes.c_void_p,)
    release_mutex.restype = ctypes.c_int
    released = bool(release_mutex(handle))
    release_error = ctypes.get_last_error() if not released else 0
    close_handle = kernel32.CloseHandle  # type: ignore[attr-defined]
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    closed = bool(close_handle(handle))
    if not released:
        raise ctypes.WinError(release_error)
    if not closed:
        raise ctypes.WinError(ctypes.get_last_error())


class JobManifestStore:
    """Persist one bounded manifest per job with CAS and recoverable publication."""

    def __init__(
        self,
        data_root: Path | str,
        *,
        data_root_identity: DataRootIdentity | None,
        recovery_guard: object,
        storage_gate: SharedStorageGate | None = None,
    ) -> None:
        identity = _validate_root_argument(data_root, data_root_identity)
        if not callable(recovery_guard):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "recovery_guard_configuration",
            )
        self.data_root_identity = identity
        self.data_root = identity.canonical_path
        self._data_root_file_identity: tuple[int, int] | None = None
        self._recovery_guard = recovery_guard
        if storage_gate is not None and type(storage_gate) is not SharedStorageGate:
            raise TypeError("storage_gate must be a SharedStorageGate or None")
        self.storage_gate = storage_gate
        self._store_nonce = uuid4()
        self._reservation_guard = threading.Lock()
        self._issued_reservations: dict[UUID, ManifestWriteReservation] = {}
        self._consumed_reservations: dict[UUID, ManifestWriteReservation] = {}

    def _target_path(self, job_id: UUID) -> Path:
        if type(job_id) is not UUID or job_id.version != 4:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_validation",
            )
        return self.data_root / "database" / "manifests" / f"{job_id}.json"

    def _get_data_root_file_identity(self, job_id: UUID) -> tuple[int, int]:
        if self._data_root_file_identity is None:
            root_info = _assert_no_reparse(
                self.data_root,
                expected="directory",
                allow_missing=False,
                stage="root_identity",
                identity=self.data_root_identity,
                job_id=job_id,
            )
            root_identity = (
                _managed_file_identity(root_info) if root_info is not None else None
            )
            if root_identity is None:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "root_identity",
                    job_id=job_id,
                )
            self._data_root_file_identity = root_identity
        return self._data_root_file_identity

    @contextmanager
    def lock_job(self, job_id: UUID) -> Generator[None]:
        target = self._target_path(job_id)
        del target  # Validate the UUID before forming or waiting on its mutex.
        _assert_root_identity(self.data_root_identity, job_id=job_id)
        mutex_name = _manifest_mutex_name(self.data_root_identity, job_id)
        holds = _thread_holds()
        current = holds.get(mutex_name)
        if current is not None:
            if current.recovery_active:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "recovery_guard_reentry",
                    job_id=job_id,
                )
            current.depth += 1
            try:
                yield
            finally:
                current.depth -= 1
            return

        handle: int | None = None
        thread_lock: RLockType | None = None
        if os.name == "nt" and hasattr(ctypes, "WinDLL"):
            try:
                handle = _named_mutex_create_and_wait(mutex_name)
            except Exception as error:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "job_mutex_wait",
                    job_id=job_id,
                    cause=error,
                )
        else:
            # The product runtime is Windows. This process-local adapter keeps
            # isolated persistence tests usable on hosts without Win32; it is
            # never selected by the packaged Windows application.
            with _fallback_locks_guard:
                thread_lock = _fallback_locks.setdefault(mutex_name, RLockType())
            try:
                if not thread_lock.acquire(timeout=_JOB_MUTEX_TIMEOUT_MS / 1_000):
                    raise TimeoutError("The test-host job mutex wait timed out.")
            except Exception as error:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "job_mutex_wait",
                    job_id=job_id,
                    cause=error,
                )

        held = _HeldMutex(handle=handle, thread_lock=thread_lock)
        holds[mutex_name] = held
        try:
            # The volume can be removed or replaced while the named mutex is
            # pending. Revalidate after acquisition so recovery never touches
            # a different root that now occupies the same path.
            _assert_root_identity(self.data_root_identity, job_id=job_id)
            held.recovery_active = True
            try:
                self._recovery_guard(job_id)  # type: ignore[operator]
            except Exception as error:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "recovery_guard",
                    job_id=job_id,
                    cause=error,
                )
            finally:
                held.recovery_active = False
            yield
        finally:
            self._expire_reservations(held)
            holds.pop(mutex_name, None)
            try:
                if held.handle is not None:
                    _named_mutex_release_and_close(held.handle)
                elif held.thread_lock is not None:
                    held.thread_lock.release()
            except Exception as error:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "job_mutex_release",
                    job_id=job_id,
                    cause=error,
                )

    def _held_mutex(self, job_id: UUID) -> _HeldMutex:
        mutex_name = _manifest_mutex_name(self.data_root_identity, job_id)
        held = _thread_holds().get(mutex_name)
        if held is None or held.depth < 1 or held.recovery_active:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "job_mutex_required",
                job_id=job_id,
            )
        return held

    def assert_write_reservation_issued(
        self,
        reservation: ManifestWriteReservation,
    ) -> None:
        """Validate the exact, unconsumed token while its job lock is held."""
        if type(reservation) is not ManifestWriteReservation:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "reservation_required",
            )
        held = self._held_mutex(reservation.job_id)
        with self._reservation_guard:
            issued = self._issued_reservations.get(reservation.token_id)
            if (
                issued is not reservation
                or reservation.issuer_nonce != self._store_nonce
                or reservation.acquisition_id != held.acquisition_id
                or reservation.thread_id != threading.get_ident()
                or reservation.token_id not in held.reservation_ids
            ):
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "reservation_invalid",
                    job_id=reservation.job_id,
                )

    def _expire_reservations(self, held: _HeldMutex) -> None:
        with self._reservation_guard:
            for token_id in held.reservation_ids:
                self._issued_reservations.pop(token_id, None)
            held.reservation_ids.clear()
            for token_id in held.consumed_reservation_ids:
                self._consumed_reservations.pop(token_id, None)
            held.consumed_reservation_ids.clear()

    def _consume_reservation(
        self,
        reservation: ManifestWriteReservation | None,
        job_id: UUID,
    ) -> None:
        if type(reservation) is not ManifestWriteReservation:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "reservation_required",
                job_id=job_id,
            )
        held = self._held_mutex(job_id)
        token_id = reservation.token_id
        with self._reservation_guard:
            issued = self._issued_reservations.get(token_id)
            if (
                issued is not reservation
                or reservation.issuer_nonce != self._store_nonce
                or reservation.job_id != job_id
                or reservation.acquisition_id != held.acquisition_id
                or reservation.thread_id != threading.get_ident()
                or token_id not in held.reservation_ids
            ):
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "reservation_invalid",
                    job_id=job_id,
                )
            del self._issued_reservations[token_id]
            held.reservation_ids.discard(token_id)
            self._consumed_reservations[token_id] = reservation
            held.consumed_reservation_ids.add(token_id)

    def _require_consumed_reservation(
        self,
        reservation: ManifestWriteReservation,
    ) -> None:
        if type(reservation) is not ManifestWriteReservation:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "reservation_invalid",
            )
        held = self._held_mutex(reservation.job_id)
        with self._reservation_guard:
            consumed = self._consumed_reservations.get(reservation.token_id)
            if (
                consumed is not reservation
                or reservation.issuer_nonce != self._store_nonce
                or reservation.acquisition_id != held.acquisition_id
                or reservation.thread_id != threading.get_ident()
                or reservation.token_id not in held.consumed_reservation_ids
            ):
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "reservation_invalid",
                    job_id=reservation.job_id,
                )

    @staticmethod
    def _serialize(manifest: DownloadJobManifest) -> tuple[bytes, str]:
        try:
            serialized = manifest_to_bytes(manifest)
            parsed = manifest_from_bytes(serialized)
        except Exception as error:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_serialization",
                cause=error,
            )
        if parsed.job_id != manifest.job_id:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_validation",
                job_id=manifest.job_id,
            )
        return serialized, hashlib.sha256(serialized).hexdigest()

    def _prepare_manifest_directories(
        self,
        job_id: UUID,
    ) -> tuple[Path, os.stat_result]:
        database_directory = self.data_root / "database"
        manifests_directory = database_directory / "manifests"
        trace_id = current_trace_id() or create_trace_id()
        database_info = _directory_path_state(
            self.data_root_identity,
            database_directory,
            create=True,
            stage="manifest_directory",
            job_id=job_id,
            storage_gate=self.storage_gate,
            trace_id=trace_id,
            expected_parent_identity=self._get_data_root_file_identity(job_id),
        )
        database_identity = _managed_file_identity(database_info)
        if database_identity is None:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_directory",
                job_id=job_id,
            )
        manifests_info = _directory_path_state(
            self.data_root_identity,
            manifests_directory,
            create=True,
            stage="manifest_directory",
            job_id=job_id,
            storage_gate=self.storage_gate,
            trace_id=trace_id,
            expected_parent_identity=database_identity,
        )
        return manifests_directory, manifests_info

    def _require_existing_manifest_directories(self, job_id: UUID) -> Path:
        database_directory = self.data_root / "database"
        manifests_directory = database_directory / "manifests"
        expected_parent_identity = self._get_data_root_file_identity(job_id)
        for directory in (database_directory, manifests_directory):
            info = _assert_no_reparse(
                directory,
                expected=None,
                allow_missing=True,
                stage="manifest_directory",
                identity=self.data_root_identity,
                job_id=job_id,
                expected_parent_identity=expected_parent_identity,
            )
            if info is None:
                _fail(
                    ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                    "manifest_snapshot",
                    job_id=job_id,
                )
            if not stat.S_ISDIR(info.st_mode):
                _fail(
                    ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
                    "manifest_directory",
                    job_id=job_id,
                )
            expected_parent_identity = _managed_file_identity(info)
        return manifests_directory

    def _read_target(
        self,
        target: Path,
        job_id: UUID,
        *,
        expected_parent_identity: tuple[int, int] | None = None,
    ) -> bytes:
        if expected_parent_identity is None:
            manifests_info = self._assert_manifest_directories_safe(job_id)
            if manifests_info is not None:
                expected_parent_identity = _managed_file_identity(manifests_info)
        _assert_no_reparse(
            target,
            expected="file",
            allow_missing=False,
            stage="manifest_read",
            identity=self.data_root_identity,
            job_id=job_id,
            expected_parent_identity=expected_parent_identity,
        )
        try:
            return _read_manifest_bytes(
                target,
                max_bytes=MAX_MANIFEST_BYTES,
                expected_parent_identity=expected_parent_identity,
            )
        except FileNotFoundError as error:
            raise error
        except Exception as error:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_read",
                job_id=job_id,
                cause=error,
            )

    def _check_expected_snapshot(
        self,
        target: Path,
        expected_sha256: str,
        job_id: UUID,
    ) -> bytes:
        try:
            target_info = _assert_no_reparse(
                target,
                expected="file",
                allow_missing=True,
                stage="manifest_snapshot",
                identity=self.data_root_identity,
                job_id=job_id,
            )
            if target_info is None:
                _fail(
                    ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                    "manifest_snapshot",
                    job_id=job_id,
                )
            current_bytes = _read_manifest_bytes(target, max_bytes=MAX_MANIFEST_BYTES)
        except FileNotFoundError:
            _fail(
                ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                "manifest_snapshot",
                job_id=job_id,
            )
        except ManifestStoreError:
            raise
        except Exception as error:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_snapshot",
                job_id=job_id,
                cause=error,
            )

        current_digest = hashlib.sha256(current_bytes).hexdigest()
        if current_digest != expected_sha256:
            _fail(
                ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                "manifest_snapshot",
                job_id=job_id,
            )
        try:
            current_manifest = manifest_from_bytes(current_bytes)
        except Exception as error:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_snapshot_validation",
                job_id=job_id,
                cause=error,
            )
        if current_manifest.job_id != job_id:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_snapshot_validation",
                job_id=job_id,
            )
        return current_bytes

    def _create_staging_file(
        self,
        directory: Path,
        serialized: bytes,
        expected_digest: str,
        manifest: DownloadJobManifest,
        *,
        generation: int,
        first_candidate: Path,
        expected_parent_identity: tuple[int, int],
    ) -> Path:
        job_id = manifest.job_id
        for attempt in range(16):
            staging_path = (
                first_candidate
                if attempt == 0
                else _new_staging_path(directory, generation)
            )
            if staging_path.parent != directory or staging_path == self._target_path(job_id):
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "staging_path",
                    job_id=job_id,
                )
            _assert_path_length(staging_path, stage="staging_path", job_id=job_id)
            _assert_root_identity(self.data_root_identity, job_id=job_id)
            existing = _assert_no_reparse(
                staging_path,
                expected=None,
                allow_missing=True,
                stage="staging_path",
                identity=self.data_root_identity,
                job_id=job_id,
                expected_parent_identity=expected_parent_identity,
            )
            if existing is not None:
                continue
            def write_staging_file(staging_target: Path = staging_path) -> None:
                _assert_root_identity(self.data_root_identity, job_id=job_id)
                with RetryingFileStream.create(
                    staging_target,
                    component="manifest_store",
                    stage="manifest_staging",
                    trace_id=current_trace_id() or create_trace_id(),
                    job_id=str(job_id),
                    storage_gate=self.storage_gate,
                    validate_path=lambda: _assert_root_identity(
                        self.data_root_identity, job_id=job_id
                    ),
                ) as stream:
                    offset = 0
                    while offset < len(serialized):
                        _assert_root_identity(self.data_root_identity, job_id=job_id)
                        written = stream.write(serialized[offset:])
                        if type(written) is not int or written <= 0:
                            raise OSError("The manifest staging write was incomplete.")
                        offset += written
                    _assert_root_identity(self.data_root_identity, job_id=job_id)
                    stream.sync()

            try:
                if self.storage_gate is None:
                    write_staging_file()
                else:
                    with self.storage_gate.mutation():
                        try:
                            write_staging_file()
                        except OSError as error:
                            if is_disk_full_os_error(error):
                                self.storage_gate.close("FILESYSTEM_DISK_FULL")
                            raise
            except FileExistsError:
                continue
            except StorageGateClosedError:
                raise
            except Exception as error:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "staging_write",
                    job_id=job_id,
                    cause=error,
                )

            _assert_no_reparse(
                staging_path,
                expected="file",
                allow_missing=False,
                stage="staging_verify",
                identity=self.data_root_identity,
                job_id=job_id,
                expected_parent_identity=expected_parent_identity,
            )
            try:
                staged_bytes = _read_manifest_bytes(
                    staging_path,
                    max_bytes=MAX_MANIFEST_BYTES,
                    expected_parent_identity=expected_parent_identity,
                    job_id=job_id,
                )
                staged_manifest = manifest_from_bytes(staged_bytes)
            except Exception as error:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "staging_verify",
                    job_id=job_id,
                    cause=error,
                )
            if (
                hashlib.sha256(staged_bytes).hexdigest() != expected_digest
                or staged_bytes != serialized
                or staged_manifest.job_id != manifest.job_id
            ):
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "staging_verify",
                    job_id=job_id,
                )
            return staging_path
        _fail(
            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
            "staging_path",
            job_id=job_id,
        )

    def _recovery_directory(self, job_id: UUID) -> Path:
        return self.data_root / "database" / "manifests" / ".recovery" / str(job_id)

    def _assert_recovery_namespace_expected(
        self,
        job_id: UUID,
        expected_backups: tuple[tuple[int, str], ...],
        *,
        allow_older_generations_before: int | None = None,
        expected_manifests_identity: tuple[int, int] | None = None,
    ) -> dict[int, tuple[Path, int]]:
        """Verify an exact, bounded set of generation-bound backup files."""
        recovery_root = self.data_root / "database" / "manifests" / ".recovery"
        directory = recovery_root / str(job_id)
        _assert_path_length(recovery_root, stage="recovery_directory", job_id=job_id)
        _assert_path_length(directory, stage="recovery_directory", job_id=job_id)
        _assert_root_identity(self.data_root_identity, job_id=job_id)
        if expected_manifests_identity is None:
            manifests_info = self._assert_manifest_directories_safe(job_id)
            if manifests_info is not None:
                expected_manifests_identity = _managed_file_identity(manifests_info)
        root_info = _assert_no_reparse(
            recovery_root,
            expected=None,
            allow_missing=True,
            stage="recovery_namespace",
            identity=self.data_root_identity,
            job_id=job_id,
            expected_parent_identity=expected_manifests_identity,
        )
        if root_info is None:
            if expected_backups:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "recovery_namespace_missing_backup",
                    job_id=job_id,
                )
            return {}
        if not stat.S_ISDIR(root_info.st_mode):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "recovery_namespace",
                job_id=job_id,
            )
        info = _assert_no_reparse(
            directory,
            expected=None,
            allow_missing=True,
            stage="recovery_namespace",
            identity=self.data_root_identity,
            job_id=job_id,
            expected_parent_identity=_managed_file_identity(root_info),
        )
        if info is None:
            if expected_backups:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "recovery_namespace_missing_backup",
                    job_id=job_id,
                )
            return {}
        if not stat.S_ISDIR(info.st_mode):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "recovery_namespace",
                job_id=job_id,
            )
        expected = dict(expected_backups)
        if len(expected) != len(expected_backups) or any(
            type(generation) is not int
            or generation < 1
            or type(digest) is not str
            or _SHA256_PATTERN.fullmatch(digest) is None
            for generation, digest in expected.items()
        ):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "recovery_namespace_expectation",
                job_id=job_id,
            )
        found: dict[int, tuple[Path, int]] = {}
        count = 0
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > _MAX_RECOVERY_ENTRIES:
                        _fail(
                            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                            "recovery_budget",
                            job_id=job_id,
                        )
                    match = re.fullmatch(
                        r"\.bak-(\d{20})-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                        entry.name,
                    )
                    if match is None:
                        _fail(
                            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                            "recovery_namespace_unattributed",
                            job_id=job_id,
                        )
                    generation = int(match.group(1))
                    try:
                        nonce = UUID(match.group(2))
                    except ValueError:
                        _fail(
                            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                            "recovery_namespace_unattributed",
                            job_id=job_id,
                        )
                    if (
                        nonce.version != 4
                        or str(nonce) != match.group(2)
                        or generation in found
                    ):
                        _fail(
                            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                            "recovery_namespace_unattributed",
                            job_id=job_id,
                        )
                    if generation not in expected and (
                        allow_older_generations_before is None
                        or generation >= allow_older_generations_before
                    ):
                        _fail(
                            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                            "recovery_namespace_unattributed",
                            job_id=job_id,
                        )
                    backup_path = directory / entry.name
                    _assert_no_reparse(
                        backup_path,
                        expected="file",
                        allow_missing=False,
                        stage="recovery_backup",
                        identity=self.data_root_identity,
                        job_id=job_id,
                        expected_parent_identity=_managed_file_identity(info),
                    )
                    try:
                        backup_bytes = _read_manifest_bytes(
                            backup_path,
                            max_bytes=MAX_MANIFEST_BYTES,
                        )
                        backup_manifest = manifest_from_bytes(backup_bytes)
                    except Exception as error:
                        _fail(
                            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                            "recovery_backup_verify",
                            job_id=job_id,
                            cause=error,
                        )
                    expected_digest = expected.get(generation)
                    if backup_manifest.job_id != job_id or (
                        expected_digest is not None
                        and hashlib.sha256(backup_bytes).hexdigest() != expected_digest
                    ):
                        _fail(
                            ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                            "recovery_backup_digest",
                            job_id=job_id,
                        )
                    found[generation] = (backup_path, len(backup_bytes))
        except ManifestStoreError:
            raise
        except OSError as error:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "recovery_namespace",
                job_id=job_id,
                cause=error,
            )
        if not expected.keys() <= found.keys() or (
            allow_older_generations_before is None
            and found.keys() != expected.keys()
        ):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "recovery_namespace_missing_backup",
                job_id=job_id,
            )
        if sum(size for _, size in found.values()) > _MAX_RECOVERY_BYTES:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "recovery_budget",
                job_id=job_id,
            )
        return found

    def _assert_recovery_namespace_empty(self, job_id: UUID) -> None:
        self._assert_recovery_namespace_expected(job_id, ())

    def _unlink_recovery_backup(
        self,
        path: Path,
        *,
        stage: str,
        job_id: UUID,
    ) -> None:
        try:
            info = _lstat_or_none(path)
            if info is None or _is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
                raise OSError("Recovery backup changed before cleanup")
            identity = _managed_file_identity(info)
            if identity is None:
                raise OSError("Recovery backup identity is unavailable")
            parent_identity = _directory_identity(path.parent)
            if parent_identity is None:
                raise OSError("Recovery backup parent identity is unavailable")
            expected_bytes = _read_manifest_bytes(path, max_bytes=MAX_MANIFEST_BYTES)

            def namespace_matches() -> bool:
                try:
                    self.data_root_identity.assert_current()
                except Exception:
                    return False
                return _directory_identity(path.parent) == parent_identity

            def prestate_matches() -> bool:
                return namespace_matches() and _manifest_file_matches(
                    path,
                    identity=identity,
                    expected_bytes=expected_bytes,
                )

            def applied_state_matches() -> bool:
                return namespace_matches() and _path_is_absent(path)

            def unlink_backup() -> None:
                self.data_root_identity.assert_current()
                if self.storage_gate is None:
                    if not prestate_matches():
                        raise OSError(errno.EIO, "Recovery backup pre-state changed")
                    path.unlink()
                    return
                with self.storage_gate.mutation():
                    try:
                        if not prestate_matches():
                            raise OSError(
                                errno.EIO,
                                "Recovery backup pre-state changed",
                            )
                        path.unlink()
                    except OSError as error:
                        if is_disk_full_os_error(error):
                            self.storage_gate.close("FILESYSTEM_DISK_FULL")
                        raise

            retry_filesystem_operation(
                unlink_backup,
                unchanged=prestate_matches,
                applied=applied_state_matches,
                component="manifest_store",
                stage=stage,
                trace_id=current_trace_id() or create_trace_id(),
                job_id=str(job_id),
            )
        except StorageGateClosedError:
            raise
        except Exception as error:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                stage,
                job_id=job_id,
                cause=error,
            )

    def prune_obsolete_backups(
        self,
        job_id: UUID,
        *,
        current_generation: int,
        current_sha256: str,
        current_size_bytes: int,
        previous_sha256: str | None,
    ) -> None:
        """Retry safe cleanup after a confirmed publication closed its DB CAS."""
        self._held_mutex(job_id)
        if (
            type(current_generation) is not int
            or current_generation < 1
            or type(current_sha256) is not str
            or _SHA256_PATTERN.fullmatch(current_sha256) is None
            or type(current_size_bytes) is not int
            or not 1 <= current_size_bytes <= MAX_MANIFEST_BYTES
            or (
                current_generation > 1
                and previous_sha256 is None
            )
            or (
                previous_sha256 is not None
                and (
                    type(previous_sha256) is not str
                    or _SHA256_PATTERN.fullmatch(previous_sha256) is None
                )
            )
        ):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "confirmed_state_validation",
                job_id=job_id,
            )

        target = self._target_path(job_id)
        expected_current_backup = (
            ((current_generation, previous_sha256),)
            if previous_sha256 is not None
            else ()
        )
        expected = self._assert_recovery_namespace_expected(
            job_id,
            expected_current_backup,
            allow_older_generations_before=current_generation,
        )
        current_bytes = self._read_target(target, job_id)
        if (
            hashlib.sha256(current_bytes).hexdigest() != current_sha256
            or len(current_bytes) != current_size_bytes
        ):
            _fail(
                ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                "confirmed_target_snapshot",
                job_id=job_id,
            )
        try:
            current_manifest = manifest_from_bytes(current_bytes)
        except Exception as error:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "confirmed_target_validation",
                job_id=job_id,
                cause=error,
            )
        if current_manifest.job_id != job_id:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "confirmed_target_validation",
                job_id=job_id,
            )

        for generation in sorted(expected):
            if generation >= current_generation:
                continue
            # Recheck the authority and the current rollback immediately before
            # each deletion. Unknown or malformed candidates fail closed.
            current_bytes = self._read_target(target, job_id)
            if (
                hashlib.sha256(current_bytes).hexdigest() != current_sha256
                or len(current_bytes) != current_size_bytes
            ):
                _fail(
                    ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                    "confirmed_target_snapshot",
                    job_id=job_id,
                )
            try:
                current_manifest = manifest_from_bytes(current_bytes)
            except Exception as error:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "confirmed_target_validation",
                    job_id=job_id,
                    cause=error,
                )
            if current_manifest.job_id != job_id:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "confirmed_target_validation",
                    job_id=job_id,
                )
            latest = self._assert_recovery_namespace_expected(
                job_id,
                expected_current_backup,
                allow_older_generations_before=current_generation,
            )
            obsolete_path = latest[generation][0]
            _assert_no_reparse(
                obsolete_path,
                expected="file",
                allow_missing=False,
                stage="obsolete_backup_cleanup",
                identity=self.data_root_identity,
                job_id=job_id,
            )
            self._unlink_recovery_backup(
                obsolete_path,
                stage="obsolete_backup_cleanup",
                job_id=job_id,
            )
            _assert_no_reparse(
                obsolete_path,
                expected=None,
                allow_missing=True,
                stage="obsolete_backup_cleanup_verify",
                identity=self.data_root_identity,
                job_id=job_id,
            )

    def _assert_manifest_directories_safe(self, job_id: UUID) -> os.stat_result | None:
        database_directory = self.data_root / "database"
        manifests_directory = database_directory / "manifests"
        database_info = _assert_no_reparse(
            database_directory,
            expected=None,
            allow_missing=True,
            stage="manifest_directory",
            identity=self.data_root_identity,
            job_id=job_id,
            expected_parent_identity=self._get_data_root_file_identity(job_id),
        )
        if database_info is None:
            return None
        if not stat.S_ISDIR(database_info.st_mode):
            _fail(
                ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
                "manifest_directory",
                job_id=job_id,
            )
        manifests_info = _assert_no_reparse(
            manifests_directory,
            expected=None,
            allow_missing=True,
            stage="manifest_directory",
            identity=self.data_root_identity,
            job_id=job_id,
            expected_parent_identity=_managed_file_identity(database_info),
        )
        if manifests_info is None:
            return None
        if not stat.S_ISDIR(manifests_info.st_mode):
            _fail(
                ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
                "manifest_directory",
                job_id=job_id,
            )
        return manifests_info

    def reserve_write(
        self,
        *,
        job_id: UUID,
        expected_current_generation: int,
        current_sha256: str,
        current_size_bytes: int,
        new_sha256: str,
        new_size_bytes: int,
        retained_backup_sha256: str | None = None,
    ) -> ManifestWriteReservation:
        """Preflight one publication; the caller must already hold ``lock_job``."""
        self._target_path(job_id)
        held = self._held_mutex(job_id)
        if (
            type(expected_current_generation) is not int
            or expected_current_generation < 0
            or expected_current_generation >= _MAX_MANIFEST_GENERATION
        ):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "generation_validation",
                job_id=job_id,
            )
        for digest, stage in (
            (current_sha256, "current_digest_validation"),
            (new_sha256, "new_digest_validation"),
        ):
            if type(digest) is not str or _SHA256_PATTERN.fullmatch(digest) is None:
                _fail(ErrorCode.DL_MANIFEST_PERSIST_FAILED, stage, job_id=job_id)
        if (
            type(current_size_bytes) is not int
            or not 0 <= current_size_bytes <= MAX_MANIFEST_BYTES
            or type(new_size_bytes) is not int
            or not 0 <= new_size_bytes <= MAX_MANIFEST_BYTES
        ):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_size_validation",
                job_id=job_id,
            )
        if retained_backup_sha256 is not None and (
            expected_current_generation < 1
            or type(retained_backup_sha256) is not str
            or _SHA256_PATTERN.fullmatch(retained_backup_sha256) is None
        ):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "retained_backup_validation",
                job_id=job_id,
            )

        new_generation = expected_current_generation + 1
        target = self._target_path(job_id)
        recovery_directory = self._recovery_directory(job_id)
        _assert_path_length(target, stage="manifest_path", job_id=job_id)
        _assert_path_length(recovery_directory, stage="recovery_directory", job_id=job_id)
        _assert_root_identity(self.data_root_identity, job_id=job_id)
        manifests_info = self._assert_manifest_directories_safe(job_id)
        directories_exist = manifests_info is not None
        manifests_identity = (
            _managed_file_identity(manifests_info)
            if manifests_info is not None
            else None
        )
        target_info = _assert_no_reparse(
            target,
            expected="file",
            allow_missing=True,
            stage="manifest_snapshot",
            identity=self.data_root_identity,
            job_id=job_id,
            expected_parent_identity=manifests_identity,
        )
        target_sha256: str | None = None
        target_size_bytes: int | None = None
        if target_info is not None:
            if not directories_exist:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_snapshot",
                    job_id=job_id,
                )
            try:
                target_bytes = _read_manifest_bytes(target, max_bytes=MAX_MANIFEST_BYTES)
                current_manifest = manifest_from_bytes(target_bytes)
            except Exception as error:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_snapshot_validation",
                    job_id=job_id,
                    cause=error,
                )
            target_sha256 = hashlib.sha256(target_bytes).hexdigest()
            target_size_bytes = len(target_bytes)
            if current_manifest.job_id != job_id:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_snapshot_validation",
                    job_id=job_id,
                )
            if target_sha256 != current_sha256 or target_size_bytes != current_size_bytes:
                _fail(
                    ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                    "manifest_snapshot",
                    job_id=job_id,
                )
        elif expected_current_generation > 0:
            _fail(
                ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                "confirmed_manifest_missing",
                job_id=job_id,
            )

        retained_generation = expected_current_generation
        retained = self._assert_recovery_namespace_expected(
            job_id,
            (
                ((retained_generation, retained_backup_sha256),)
                if retained_backup_sha256 is not None
                else ()
            ),
            expected_manifests_identity=manifests_identity,
        )
        retained_size_bytes = retained.get(retained_generation, (target, 0))[1]
        entry_count = (1 if target_info is None else 2) + len(retained)
        recovery_bytes = (
            new_size_bytes + (target_size_bytes or 0) + retained_size_bytes
        )
        if entry_count > _MAX_RECOVERY_ENTRIES or recovery_bytes > _MAX_RECOVERY_BYTES:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "recovery_budget",
                job_id=job_id,
            )

        token_id = uuid4()
        reservation = ManifestWriteReservation(
            job_id=job_id,
            expected_current_generation=expected_current_generation,
            new_generation=new_generation,
            current_sha256=current_sha256,
            current_size_bytes=current_size_bytes,
            new_sha256=new_sha256,
            new_size_bytes=new_size_bytes,
            target_sha256=target_sha256,
            target_size_bytes=target_size_bytes,
            retained_backup_sha256=retained_backup_sha256,
            issuer_nonce=self._store_nonce,
            token_id=token_id,
            acquisition_id=held.acquisition_id,
            thread_id=threading.get_ident(),
        )
        with self._reservation_guard:
            self._issued_reservations[token_id] = reservation
            held.reservation_ids.add(token_id)
        return reservation

    def save(
        self,
        manifest: DownloadJobManifest,
        *,
        reservation: ManifestWriteReservation | None = None,
    ) -> str:
        if type(manifest) is not DownloadJobManifest:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_validation",
            )
        self._consume_reservation(reservation, manifest.job_id)
        if reservation is None:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "reservation_required",
                job_id=manifest.job_id,
            )
        serialized, new_digest = self._serialize(manifest)
        if (
            new_digest != reservation.new_sha256
            or len(serialized) != reservation.new_size_bytes
        ):
            _fail(
                ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                "reservation_manifest_mismatch",
                job_id=manifest.job_id,
            )
        target = self._target_path(manifest.job_id)
        recovery_directory = self._recovery_directory(manifest.job_id)
        staging_path = _new_staging_path(recovery_directory, reservation.new_generation)
        backup_path: Path | None = None
        if reservation.target_sha256 is not None:
            backup_path = _new_backup_path(recovery_directory, reservation.new_generation)
            _validate_backup_path(target, backup_path, manifest.job_id)
            _assert_path_length(backup_path, stage="backup_path", job_id=manifest.job_id)
        _assert_path_length(target, stage="manifest_path", job_id=manifest.job_id)
        _assert_path_length(staging_path, stage="staging_path", job_id=manifest.job_id)
        _assert_path_length(
            recovery_directory.parent,
            stage="recovery_directory",
            job_id=manifest.job_id,
        )

        _assert_root_identity(self.data_root_identity, job_id=manifest.job_id)
        manifests_info = self._assert_manifest_directories_safe(manifest.job_id)
        manifests_identity = (
            _managed_file_identity(manifests_info)
            if manifests_info is not None
            else None
        )
        target_info = _assert_no_reparse(
            target,
            expected="file",
            allow_missing=True,
            stage="manifest_snapshot",
            identity=self.data_root_identity,
            job_id=manifest.job_id,
            expected_parent_identity=manifests_identity,
        )
        if reservation.target_sha256 is None:
            if target_info is not None:
                _fail(
                    ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                    "manifest_snapshot",
                    job_id=manifest.job_id,
                )
        else:
            if target_info is None:
                _fail(
                    ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                    "manifest_snapshot",
                    job_id=manifest.job_id,
                )
            current_bytes = self._read_target(
                target,
                manifest.job_id,
                expected_parent_identity=manifests_identity,
            )
            if (
                hashlib.sha256(current_bytes).hexdigest() != reservation.target_sha256
                or len(current_bytes) != reservation.target_size_bytes
            ):
                _fail(
                    ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                    "manifest_snapshot",
                    job_id=manifest.job_id,
                )
        retained_generation = reservation.expected_current_generation
        retained_backups = self._assert_recovery_namespace_expected(
            manifest.job_id,
            (
                ((retained_generation, reservation.retained_backup_sha256),)
                if reservation.retained_backup_sha256 is not None
                else ()
            ),
            expected_manifests_identity=manifests_identity,
        )
        retained_size_bytes = retained_backups.get(retained_generation, (target, 0))[1]
        recovery_entries = (1 if target_info is None else 2) + len(retained_backups)
        recovery_bytes = (
            len(serialized)
            + (reservation.target_size_bytes or 0)
            + retained_size_bytes
        )
        if (
            recovery_entries > _MAX_RECOVERY_ENTRIES
            or recovery_bytes > _MAX_RECOVERY_BYTES
        ):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "recovery_budget",
                job_id=manifest.job_id,
            )

        manifests_directory, prepared_manifests_info = self._prepare_manifest_directories(
            manifest.job_id
        )
        prepared_manifests_identity = _managed_file_identity(prepared_manifests_info)
        recovery_root = manifests_directory / ".recovery"
        recovery_trace_id = current_trace_id() or create_trace_id()
        recovery_root_info = _directory_path_state(
            self.data_root_identity,
            recovery_root,
            create=True,
            stage="recovery_directory",
            job_id=manifest.job_id,
            storage_gate=self.storage_gate,
            trace_id=recovery_trace_id,
            expected_parent_identity=prepared_manifests_identity,
        )
        recovery_root_identity = _managed_file_identity(recovery_root_info)
        recovery_directory_info = _directory_path_state(
            self.data_root_identity,
            recovery_directory,
            create=True,
            stage="recovery_directory",
            job_id=manifest.job_id,
            storage_gate=self.storage_gate,
            trace_id=recovery_trace_id,
            expected_parent_identity=recovery_root_identity,
        )
        recovery_directory_identity = _managed_file_identity(recovery_directory_info)
        if recovery_directory_identity is None:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "recovery_directory",
                job_id=manifest.job_id,
            )
        self._assert_recovery_namespace_expected(
            manifest.job_id,
            (
                ((retained_generation, reservation.retained_backup_sha256),)
                if reservation.retained_backup_sha256 is not None
                else ()
            ),
            expected_manifests_identity=manifests_identity,
        )
        staging_path = self._create_staging_file(
            recovery_directory,
            serialized,
            new_digest,
            manifest,
            generation=reservation.new_generation,
            first_candidate=staging_path,
            expected_parent_identity=recovery_directory_identity,
        )

        if reservation.target_sha256 is None:
            destination_info = _assert_no_reparse(
                target,
                expected=None,
                allow_missing=True,
                stage="manifest_destination",
                identity=self.data_root_identity,
                job_id=manifest.job_id,
                expected_parent_identity=manifests_identity,
            )
            if destination_info is not None:
                _fail(
                    ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                    "manifest_destination",
                    job_id=manifest.job_id,
                )
            staging_info = _assert_no_reparse(
                staging_path,
                expected="file",
                allow_missing=False,
                stage="staging_publish_snapshot",
                identity=self.data_root_identity,
                job_id=manifest.job_id,
            )
            staging_identity = _managed_file_identity(cast(os.stat_result, staging_info))
            if staging_identity is None:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "staging_publish_snapshot",
                    job_id=manifest.job_id,
                )
            staging_parent_identity = _directory_identity(staging_path.parent)
            target_parent_identity = _directory_identity(target.parent)
            if staging_parent_identity is None or target_parent_identity is None:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_publish_namespace",
                    job_id=manifest.job_id,
                )

            def publish_namespace_matches() -> bool:
                try:
                    self.data_root_identity.assert_current()
                except Exception:
                    return False
                return (
                    _directory_identity(staging_path.parent)
                    == staging_parent_identity
                    and _directory_identity(target.parent) == target_parent_identity
                )

            def publish_prestate_matches() -> bool:
                return (
                    publish_namespace_matches()
                    and _manifest_file_matches(
                        staging_path,
                        identity=staging_identity,
                        expected_bytes=serialized,
                    )
                    and _path_is_absent(target)
                )

            def publish_applied_state_matches() -> bool:
                return (
                    publish_namespace_matches()
                    and _manifest_file_matches(
                        target,
                        identity=staging_identity,
                        expected_bytes=serialized,
                    )
                    and _path_is_absent(staging_path)
                )

            def publish_initial_manifest() -> None:
                _assert_root_identity(self.data_root_identity, job_id=manifest.job_id)
                if self.storage_gate is None:
                    if not publish_prestate_matches():
                        raise OSError(errno.EIO, "Manifest publish pre-state changed")
                    _move_file_ex_win32(staging_path, target)
                    return
                with self.storage_gate.mutation():
                    try:
                        if not publish_prestate_matches():
                            raise OSError(
                                errno.EIO,
                                "Manifest publish pre-state changed",
                            )
                        _move_file_ex_win32(staging_path, target)
                    except OSError as error:
                        if is_disk_full_os_error(error):
                            self.storage_gate.close("FILESYSTEM_DISK_FULL")
                        raise

            try:
                retry_filesystem_operation(
                    publish_initial_manifest,
                    unchanged=publish_prestate_matches,
                    applied=publish_applied_state_matches,
                    component="manifest_store",
                    stage="manifest_publish",
                    trace_id=current_trace_id() or create_trace_id(),
                    job_id=str(manifest.job_id),
                )
            except StorageGateClosedError:
                raise
            except Exception as error:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_publish",
                    job_id=manifest.job_id,
                    cause=error,
                )
        else:
            assert backup_path is not None
            if target_info is None:
                _fail(
                    ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                    "manifest_snapshot",
                    job_id=manifest.job_id,
                )
            current_bytes = self._read_target(
                target,
                manifest.job_id,
                expected_parent_identity=manifests_identity,
            )
            if (
                hashlib.sha256(current_bytes).hexdigest() != reservation.target_sha256
                or len(current_bytes) != reservation.target_size_bytes
            ):
                _fail(
                    ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                    "manifest_snapshot",
                    job_id=manifest.job_id,
                )
            backup_info = _assert_no_reparse(
                backup_path,
                expected=None,
                allow_missing=True,
                stage="backup_candidate",
                identity=self.data_root_identity,
                job_id=manifest.job_id,
                expected_parent_identity=recovery_directory_identity,
            )
            if backup_info is not None:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "backup_candidate",
                    job_id=manifest.job_id,
                )
            staging_info = _assert_no_reparse(
                staging_path,
                expected="file",
                allow_missing=False,
                stage="staging_replace_snapshot",
                identity=self.data_root_identity,
                job_id=manifest.job_id,
                expected_parent_identity=recovery_directory_identity,
            )
            staging_identity = _managed_file_identity(cast(os.stat_result, staging_info))
            current_info = _assert_no_reparse(
                target,
                expected="file",
                allow_missing=False,
                stage="manifest_replace_snapshot",
                identity=self.data_root_identity,
                job_id=manifest.job_id,
                expected_parent_identity=manifests_identity,
            )
            current_identity = _managed_file_identity(cast(os.stat_result, current_info))
            if staging_identity is None or current_identity is None:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_replace_snapshot",
                    job_id=manifest.job_id,
                )
            target_parent_identity = _directory_identity(target.parent)
            staging_parent_identity = _directory_identity(staging_path.parent)
            backup_parent_identity = _directory_identity(backup_path.parent)
            if (
                target_parent_identity is None
                or staging_parent_identity is None
                or backup_parent_identity is None
            ):
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_replace_namespace",
                    job_id=manifest.job_id,
                )

            def replace_namespace_matches() -> bool:
                try:
                    self.data_root_identity.assert_current()
                except Exception:
                    return False
                return (
                    _directory_identity(target.parent) == target_parent_identity
                    and _directory_identity(staging_path.parent)
                    == staging_parent_identity
                    and _directory_identity(backup_path.parent) == backup_parent_identity
                )

            def replace_prestate_matches() -> bool:
                return (
                    replace_namespace_matches()
                    and _manifest_file_matches(
                        target,
                        identity=current_identity,
                        expected_bytes=current_bytes,
                    )
                    and _manifest_file_matches(
                        staging_path,
                        identity=staging_identity,
                        expected_bytes=serialized,
                    )
                    and _path_is_absent(backup_path)
                )

            def replace_applied_state_matches() -> bool:
                return (
                    replace_namespace_matches()
                    and _manifest_file_matches(
                        target,
                        identity=staging_identity,
                        expected_bytes=serialized,
                    )
                    and _manifest_file_matches(
                        backup_path,
                        identity=current_identity,
                        expected_bytes=current_bytes,
                    )
                    and _path_is_absent(staging_path)
                )

            def replace_manifest() -> None:
                _assert_root_identity(self.data_root_identity, job_id=manifest.job_id)
                if self.storage_gate is None:
                    if not replace_prestate_matches():
                        raise OSError(errno.EIO, "Manifest replace pre-state changed")
                    _replace_file_win32(target, staging_path, backup_path)
                    return
                with self.storage_gate.mutation():
                    try:
                        if not replace_prestate_matches():
                            raise OSError(
                                errno.EIO,
                                "Manifest replace pre-state changed",
                            )
                        _replace_file_win32(target, staging_path, backup_path)
                    except OSError as error:
                        if is_disk_full_os_error(error):
                            self.storage_gate.close("FILESYSTEM_DISK_FULL")
                        raise

            try:
                retry_filesystem_operation(
                    replace_manifest,
                    unchanged=replace_prestate_matches,
                    applied=replace_applied_state_matches,
                    component="manifest_store",
                    stage="manifest_replace",
                    trace_id=current_trace_id() or create_trace_id(),
                    job_id=str(manifest.job_id),
                )
            except StorageGateClosedError:
                raise
            except Exception as error:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_replace",
                    job_id=manifest.job_id,
                    cause=error,
                )

            try:
                _assert_no_reparse(
                    backup_path,
                    expected="file",
                    allow_missing=False,
                    stage="backup_verify",
                    identity=self.data_root_identity,
                    job_id=manifest.job_id,
                    expected_parent_identity=recovery_directory_identity,
                )
                backup_bytes = _read_manifest_bytes(
                    backup_path,
                    max_bytes=MAX_MANIFEST_BYTES,
                    expected_parent_identity=recovery_directory_identity,
                    job_id=manifest.job_id,
                )
                backup_manifest = manifest_from_bytes(backup_bytes)
            except Exception as error:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "backup_verify",
                    job_id=manifest.job_id,
                    cause=error,
                )
            if (
                hashlib.sha256(backup_bytes).hexdigest() != reservation.target_sha256
                or len(backup_bytes) != reservation.target_size_bytes
                or backup_manifest.job_id != manifest.job_id
            ):
                _fail(
                    ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                    "backup_digest_verify",
                    job_id=manifest.job_id,
                )

        destination_bytes = self._read_target(
            target,
            manifest.job_id,
            expected_parent_identity=manifests_identity,
        )
        self._verify_published_manifest(destination_bytes, manifest, new_digest)
        return new_digest

    @staticmethod
    def _verify_published_manifest(
        serialized: bytes,
        expected_manifest: DownloadJobManifest,
        expected_digest: str,
    ) -> None:
        actual_digest = hashlib.sha256(serialized).hexdigest()
        try:
            published = manifest_from_bytes(serialized)
        except Exception as error:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_destination_verify",
                job_id=expected_manifest.job_id,
                cause=error,
            )
        if (
            actual_digest != expected_digest
            or published.job_id != expected_manifest.job_id
            or published != expected_manifest
        ):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_destination_verify",
                job_id=expected_manifest.job_id,
            )

    def verify_published(
        self,
        reservation: ManifestWriteReservation,
    ) -> dict[int, tuple[Path, int]]:
        """Reverify the published target and all backups while the job lock is held."""
        self._require_consumed_reservation(reservation)
        target = self._target_path(reservation.job_id)
        manifests_info = self._assert_manifest_directories_safe(reservation.job_id)
        manifests_identity = (
            _managed_file_identity(manifests_info) if manifests_info is not None else None
        )
        target_bytes = self._read_target(
            target,
            reservation.job_id,
            expected_parent_identity=manifests_identity,
        )
        target_digest = hashlib.sha256(target_bytes).hexdigest()
        if (
            target_digest != reservation.new_sha256
            or len(target_bytes) != reservation.new_size_bytes
        ):
            _fail(
                ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                "manifest_publication_verify",
                job_id=reservation.job_id,
            )
        try:
            target_manifest = manifest_from_bytes(target_bytes)
        except Exception as error:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_publication_verify",
                job_id=reservation.job_id,
                cause=error,
            )
        if target_manifest.job_id != reservation.job_id:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_publication_verify",
                job_id=reservation.job_id,
            )

        expected_backups: list[tuple[int, str]] = []
        if reservation.retained_backup_sha256 is not None:
            expected_backups.append(
                (
                    reservation.expected_current_generation,
                    reservation.retained_backup_sha256,
                )
            )
        if reservation.target_sha256 is not None:
            expected_backups.append(
                (reservation.new_generation, reservation.target_sha256)
            )
        backups = self._assert_recovery_namespace_expected(
            reservation.job_id,
            tuple(expected_backups),
            expected_manifests_identity=manifests_identity,
        )
        if reservation.target_sha256 is not None:
            backup = backups.get(reservation.new_generation)
            if (
                backup is None
                or backup[1] != reservation.target_size_bytes
            ):
                _fail(
                    ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                    "manifest_backup_verify",
                    job_id=reservation.job_id,
                )
        return backups

    def read_verified_recovery_snapshot(
        self,
        expected_manifest: DownloadJobManifest,
        *,
        generation: int | None,
        manifest_sha256: str | None,
        manifest_size_bytes: int | None,
        previous_sha256: str | None,
    ) -> DownloadJobManifest | None:
        """Read the exact DB-projected manifest and its recovery namespace.

        The caller supplies a detached SQLite snapshot and must hold this job's
        mutex. This method keeps path, marker, byte, and backup verification
        behind the persistence boundary instead of exposing internal helpers.
        """
        if type(expected_manifest) is not DownloadJobManifest:
            raise TypeError("expected_manifest must be a DownloadJobManifest")
        job_id = expected_manifest.job_id
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("expected_manifest must identify a UUIDv4 job")
        target = self._target_path(job_id)

        if generation is None:
            if (
                expected_manifest.pages
                or manifest_sha256 is not None
                or manifest_size_bytes is not None
                or previous_sha256 is not None
            ):
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_recovery_snapshot",
                    job_id=job_id,
                )
            target_info = _assert_no_reparse(
                target,
                expected=None,
                allow_missing=True,
                stage="manifest_recovery_snapshot",
                identity=self.data_root_identity,
                job_id=job_id,
            )
            if target_info is not None:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_recovery_snapshot",
                    job_id=job_id,
                )
            self._assert_recovery_namespace_expected(job_id, ())
            return None

        if (
            type(generation) is not int
            or generation < 1
            or not expected_manifest.pages
            or type(manifest_sha256) is not str
            or _SHA256_PATTERN.fullmatch(manifest_sha256) is None
            or type(manifest_size_bytes) is not int
            or not 1 <= manifest_size_bytes <= MAX_MANIFEST_BYTES
            or (
                previous_sha256 is not None
                and (
                    type(previous_sha256) is not str
                    or _SHA256_PATTERN.fullmatch(previous_sha256) is None
                )
            )
            or (generation > 1 and previous_sha256 is None)
        ):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_recovery_snapshot",
                job_id=job_id,
            )

        target_info = _assert_no_reparse(
            target,
            expected="file",
            allow_missing=False,
            stage="manifest_recovery_snapshot",
            identity=self.data_root_identity,
            job_id=job_id,
        )
        if target_info is None:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_recovery_snapshot",
                job_id=job_id,
            )
        target_bytes = self._read_target(target, job_id)
        canonical_bytes = manifest_to_bytes(expected_manifest)
        digest = hashlib.sha256(canonical_bytes).hexdigest()
        self._verify_published_manifest(target_bytes, expected_manifest, digest)
        if (
            target_bytes != canonical_bytes
            or digest != manifest_sha256
            or len(canonical_bytes) != manifest_size_bytes
        ):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_recovery_snapshot",
                job_id=job_id,
            )

        expected_backups = (
            ((generation, previous_sha256),)
            if previous_sha256 is not None
            else ()
        )
        self._assert_recovery_namespace_expected(job_id, expected_backups)
        return manifest_from_bytes(target_bytes)

    def replay_pending_recovery_snapshot(
        self,
        expected_manifest: DownloadJobManifest,
        *,
        generation: int,
        manifest_sha256: str,
        manifest_size_bytes: int,
        previous_sha256: str | None,
    ) -> DownloadJobManifest:
        """Replay a marker-authorized manifest publication after process exit.

        The caller must hold the per-job mutex and pass the detached SQLite
        projection and pending marker captured from one closed read session.
        """
        if type(expected_manifest) is not DownloadJobManifest:
            raise TypeError("expected_manifest must be a DownloadJobManifest")
        job_id = expected_manifest.job_id
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("expected_manifest must identify a UUIDv4 job")
        held = self._held_mutex(job_id)
        del held  # Validate mutex ownership before filesystem inspection.
        if (
            type(generation) is not int
            or generation < 1
            or type(manifest_sha256) is not str
            or _SHA256_PATTERN.fullmatch(manifest_sha256) is None
            or type(manifest_size_bytes) is not int
            or not 1 <= manifest_size_bytes <= MAX_MANIFEST_BYTES
            or (
                previous_sha256 is not None
                and (
                    type(previous_sha256) is not str
                    or _SHA256_PATTERN.fullmatch(previous_sha256) is None
                )
            )
            or (generation == 1 and previous_sha256 is not None)
            or (generation > 1 and previous_sha256 is None)
            or not expected_manifest.pages
        ):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_recovery_replay",
                job_id=job_id,
            )

        serialized, digest = self._serialize(expected_manifest)
        if digest != manifest_sha256 or len(serialized) != manifest_size_bytes:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_recovery_replay",
                job_id=job_id,
            )

        target = self._target_path(job_id)
        target_info = _assert_no_reparse(
            target,
            expected="file",
            allow_missing=True,
            stage="manifest_recovery_replay",
            identity=self.data_root_identity,
            job_id=job_id,
        )
        target_bytes: bytes | None = (
            self._read_target(target, job_id) if target_info is not None else None
        )
        target_digest = (
            hashlib.sha256(target_bytes).hexdigest()
            if target_bytes is not None
            else None
        )

        if target_bytes is not None and target_bytes == serialized:
            retained_backup_sha256 = self._pending_retained_backup_sha256(
                job_id,
                pending_generation=generation,
                allow_pending_generation_backup=previous_sha256 is not None,
            )
            expected_backups: tuple[tuple[int, str], ...] = ()
            if retained_backup_sha256 is not None:
                expected_backups += ((generation - 1, retained_backup_sha256),)
            if previous_sha256 is not None:
                expected_backups += ((generation, previous_sha256),)
            self._assert_recovery_namespace_expected(job_id, expected_backups)
            self._verify_published_manifest(target_bytes, expected_manifest, digest)
            return expected_manifest

        if previous_sha256 is None:
            if target_bytes is not None:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_recovery_replay_target",
                    job_id=job_id,
                )
            current_sha256 = "0" * 64
            current_size_bytes = 0
        else:
            if target_bytes is None or target_digest != previous_sha256:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_recovery_replay_target",
                    job_id=job_id,
                )
            try:
                previous_manifest = manifest_from_bytes(target_bytes)
            except Exception as error:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_recovery_replay_target",
                    job_id=job_id,
                    cause=error,
                )
            if previous_manifest.job_id != job_id:
                _fail(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_recovery_replay_target",
                    job_id=job_id,
                )
            current_sha256 = previous_sha256
            current_size_bytes = len(target_bytes)

        retained_backup_sha256 = self._pending_retained_backup_sha256(
            job_id,
            pending_generation=generation,
            allow_pending_generation_backup=False,
        )

        reservation = self.reserve_write(
            job_id=job_id,
            expected_current_generation=generation - 1,
            current_sha256=current_sha256,
            current_size_bytes=current_size_bytes,
            new_sha256=digest,
            new_size_bytes=len(serialized),
            retained_backup_sha256=retained_backup_sha256,
        )
        published_sha256 = self.save(expected_manifest, reservation=reservation)
        if published_sha256 != digest:
            _fail(
                ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                "manifest_recovery_replay",
                job_id=job_id,
            )
        self.verify_published(reservation)
        return expected_manifest

    def _pending_retained_backup_sha256(
        self,
        job_id: UUID,
        *,
        pending_generation: int,
        allow_pending_generation_backup: bool,
    ) -> str | None:
        """Identify only the valid retained predecessor of a pending generation."""
        retained_generation = pending_generation - 1
        if retained_generation < 2:
            return None

        recovery_directory = self._recovery_directory(job_id)
        _assert_root_identity(self.data_root_identity, job_id=job_id)
        directory_info = _assert_no_reparse(
            recovery_directory,
            expected="directory",
            allow_missing=True,
            stage="manifest_recovery_retained_backup",
            identity=self.data_root_identity,
            job_id=job_id,
        )
        if directory_info is None:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_recovery_retained_backup_missing",
                job_id=job_id,
            )

        allowed_generations = {retained_generation}
        if allow_pending_generation_backup:
            allowed_generations.add(pending_generation)
        candidates: list[Path] = []
        found_generations: set[int] = set()
        try:
            with os.scandir(recovery_directory) as entries:
                for count, entry in enumerate(entries, start=1):
                    if count > _MAX_RECOVERY_ENTRIES:
                        _fail(
                            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                            "recovery_budget",
                            job_id=job_id,
                        )
                    match = re.fullmatch(
                        r"\.bak-(\d{20})-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                        entry.name,
                    )
                    if match is None:
                        _fail(
                            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                            "recovery_namespace_unattributed",
                            job_id=job_id,
                        )
                    backup_generation = int(match.group(1))
                    nonce = UUID(match.group(2))
                    if (
                        nonce.version != 4
                        or str(nonce) != match.group(2)
                        or backup_generation not in allowed_generations
                        or backup_generation in found_generations
                    ):
                        _fail(
                            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                            "recovery_namespace_unattributed",
                            job_id=job_id,
                        )
                    found_generations.add(backup_generation)
                    if backup_generation == retained_generation:
                        candidates.append(recovery_directory / entry.name)
        except ManifestStoreError:
            raise
        except OSError as error:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_recovery_retained_backup",
                job_id=job_id,
                cause=error,
            )

        if len(candidates) != 1:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_recovery_retained_backup_missing",
                job_id=job_id,
            )
        retained_path = candidates[0]
        _assert_no_reparse(
            retained_path,
            expected="file",
            allow_missing=False,
            stage="manifest_recovery_retained_backup",
            identity=self.data_root_identity,
            job_id=job_id,
        )
        try:
            retained_bytes = _read_manifest_bytes(
                retained_path,
                max_bytes=MAX_MANIFEST_BYTES,
            )
            retained_manifest = manifest_from_bytes(retained_bytes)
            retained_canonical_bytes = manifest_to_bytes(retained_manifest)
        except Exception as error:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_recovery_retained_backup_verify",
                job_id=job_id,
                cause=error,
            )
        if (
            retained_manifest.job_id != job_id
            or retained_canonical_bytes != retained_bytes
        ):
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "manifest_recovery_retained_backup_verify",
                job_id=job_id,
            )
        return hashlib.sha256(retained_bytes).hexdigest()

    def prune_retained_backup(
        self,
        reservation: ManifestWriteReservation,
    ) -> None:
        """Remove the older verified backup after the caller closes the DB CAS."""
        self._require_consumed_reservation(reservation)
        verified_backups = self.verify_published(reservation)
        retained_digest = reservation.retained_backup_sha256
        if retained_digest is None:
            return

        retained_generation = reservation.expected_current_generation
        retained = verified_backups.get(retained_generation)
        if retained is None:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "retained_backup_cleanup",
                job_id=reservation.job_id,
            )
        retained_path, _retained_size = retained
        manifests_info = self._assert_manifest_directories_safe(reservation.job_id)
        manifests_identity = (
            _managed_file_identity(manifests_info)
            if manifests_info is not None
            else None
        )
        if manifests_identity is None:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "retained_backup_cleanup_root",
                job_id=reservation.job_id,
            )
        recovery_root_info = _assert_no_reparse(
            retained_path.parent.parent,
            expected="directory",
            allow_missing=False,
            stage="retained_backup_cleanup_root",
            identity=self.data_root_identity,
            job_id=reservation.job_id,
            expected_parent_identity=manifests_identity,
        )
        recovery_root_identity = _managed_file_identity(
            cast(os.stat_result, recovery_root_info)
        )
        if recovery_root_identity is None:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "retained_backup_cleanup_root",
                job_id=reservation.job_id,
            )
        recovery_directory_info = _assert_no_reparse(
            retained_path.parent,
            expected="directory",
            allow_missing=False,
            stage="retained_backup_cleanup_directory",
            identity=self.data_root_identity,
            job_id=reservation.job_id,
            expected_parent_identity=recovery_root_identity,
        )
        recovery_directory_identity = _managed_file_identity(
            cast(os.stat_result, recovery_directory_info)
        )
        if recovery_directory_identity is None:
            _fail(
                ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                "retained_backup_cleanup_directory",
                job_id=reservation.job_id,
            )
        _assert_root_identity(self.data_root_identity, job_id=reservation.job_id)
        _assert_no_reparse(
            retained_path,
            expected="file",
            allow_missing=False,
            stage="retained_backup_cleanup",
            identity=self.data_root_identity,
            job_id=reservation.job_id,
            expected_parent_identity=recovery_directory_identity,
        )
        self._unlink_recovery_backup(
            retained_path,
            stage="retained_backup_cleanup",
            job_id=reservation.job_id,
        )
        _assert_no_reparse(
            retained_path,
            expected=None,
            allow_missing=True,
            stage="retained_backup_cleanup_verify",
            identity=self.data_root_identity,
            job_id=reservation.job_id,
            expected_parent_identity=recovery_directory_identity,
        )
        expected_current_backup = (
            ((reservation.new_generation, reservation.target_sha256),)
            if reservation.target_sha256 is not None
            else ()
        )
        self._assert_recovery_namespace_expected(
            reservation.job_id,
            expected_current_backup,
        )


def _new_staging_path(directory: Path, generation: int) -> Path:
    return directory / f".stg-{generation:020d}-{uuid4()}"


def _new_backup_path(directory: Path, generation: int) -> Path:
    return directory / f".bak-{generation:020d}-{uuid4()}"


def _validate_backup_path(target: Path, backup_path: object, job_id: UUID) -> None:
    if not isinstance(backup_path, Path):
        _fail(
            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
            "backup_path",
            job_id=job_id,
        )
    expected_directory = target.parent / ".recovery" / str(job_id)
    if backup_path.parent != expected_directory or backup_path == target:
        _fail(
            ErrorCode.DL_MANIFEST_PERSIST_FAILED,
            "backup_path",
            job_id=job_id,
        )


class TemporaryPageStore:
    """Create reserved temporary page files exclusively below ``data_root/temp``."""

    def __init__(
        self,
        data_root: Path | str,
        *,
        data_root_identity: DataRootIdentity | None,
        storage_gate: SharedStorageGate | None = None,
    ) -> None:
        identity = _validate_root_argument(data_root, data_root_identity)
        self.data_root_identity = identity
        self.data_root = identity.canonical_path
        if storage_gate is not None and type(storage_gate) is not SharedStorageGate:
            raise TypeError("storage_gate must be a SharedStorageGate or None")
        self.storage_gate = storage_gate if storage_gate is not None else SharedStorageGate()

    def _target_path(self, temporary_path: str) -> Path:
        if type(temporary_path) is not str:
            _fail(ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE, "temporary_path_validation")
        try:
            validate_temporary_path(temporary_path)
        except Exception as error:
            _fail(
                ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
                "temporary_path_validation",
                cause=error,
            )
        target = self.data_root / "temp"
        for component in temporary_path.split("/"):
            target = target / component
        _assert_path_length(target, stage="temporary_path_length")
        return target

    @contextmanager
    def create(
        self,
        temporary_path: str,
        *,
        job_id: UUID | None = None,
        trace_id: str | None = None,
    ) -> Generator[RetryingFileStream]:
        stable_trace_id = trace_id or current_trace_id() or create_trace_id()
        target = self._target_path(temporary_path)
        root = self.data_root / "temp"
        parent_paths: list[Path] = [root]
        cursor = root
        components = temporary_path.split("/")
        for component in components[:-1]:
            cursor = cursor / component
            parent_paths.append(cursor)

        target_parent_info: os.stat_result | None = None
        for directory in parent_paths:
            target_parent_info = _directory_path_state(
                self.data_root_identity,
                directory,
                create=True,
                stage="temporary_path_directory",
                job_id=job_id,
                storage_gate=self.storage_gate,
                trace_id=stable_trace_id,
            )
        target_parent_identity = (
            _managed_file_identity(target_parent_info)
            if target_parent_info is not None
            else None
        )

        existing = _assert_no_reparse(
            target,
            expected=None,
            allow_missing=True,
            stage="temporary_path_destination",
            identity=self.data_root_identity,
            job_id=job_id,
            trace_id=stable_trace_id,
            expected_parent_identity=target_parent_identity,
        )
        if existing is not None:
            _fail(
                ErrorCode.DL_TEMPORARY_PATH_CONFLICT,
                "temporary_path_conflict",
            )

        _assert_root_identity(self.data_root_identity, stage="root_identity")
        with self.storage_gate.mutation():
            try:
                stream = RetryingFileStream.create(
                    target,
                    component="download_transport",
                    stage="temporary_page",
                    trace_id=stable_trace_id,
                    job_id=str(job_id) if job_id is not None else None,
                    storage_gate=self.storage_gate,
                    validate_path=lambda: _assert_root_identity(
                        self.data_root_identity,
                        stage="root_identity",
                        job_id=job_id,
                    ),
                )
            except FileExistsError:
                _fail(
                    ErrorCode.DL_TEMPORARY_PATH_CONFLICT,
                    "temporary_path_conflict",
                )
            except OSError as error:
                if is_disk_full_os_error(error):
                    self.storage_gate.close("FILESYSTEM_DISK_FULL")
                _fail(
                    _temporary_page_io_error_code(error),
                    "temporary_path_create",
                    cause=error,
                )

            try:
                yield stream
            except OSError as error:
                if is_disk_full_os_error(error):
                    self.storage_gate.close("FILESYSTEM_DISK_FULL")
                try:
                    stream.close()
                except BaseException as cleanup_error:
                    if isinstance(cleanup_error, OSError) and is_disk_full_os_error(
                        cleanup_error
                    ):
                        self.storage_gate.close("FILESYSTEM_DISK_FULL")
                    error.add_note(
                        "Temporary page stream cleanup failed after write error "
                        f"({type(cleanup_error).__name__})."
                    )
                    _fail(
                        _temporary_page_io_error_code(error),
                        "temporary_path_write",
                        cause=error,
                        cleanup_cause=cleanup_error,
                    )
                _fail(
                    _temporary_page_io_error_code(error),
                    "temporary_path_write",
                    cause=error,
                )
            except BaseException as operation_error:
                try:
                    stream.close()
                except BaseException as cleanup_error:
                    if isinstance(cleanup_error, OSError) and is_disk_full_os_error(
                        cleanup_error
                    ):
                        self.storage_gate.close("FILESYSTEM_DISK_FULL")
                    operation_error.add_note(
                        "Temporary page stream cleanup failed during abort "
                        f"({type(cleanup_error).__name__})."
                    )
                raise
            else:
                try:
                    _assert_root_identity(self.data_root_identity, stage="root_identity")
                    stream.close()
                except OSError as error:
                    if is_disk_full_os_error(error):
                        self.storage_gate.close("FILESYSTEM_DISK_FULL")
                    _fail(
                        _temporary_page_io_error_code(error),
                        "temporary_path_close",
                        cause=error,
                    )
