"""Windows same-volume atomic file promotion with no-replace semantics."""

from __future__ import annotations

import ctypes
import hashlib
import logging
import math
import os
import stat
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol
from uuid import UUID

from app.persistence.filesystem_retry import (
    RetryingFileStream,
    close_read_handle,
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

logger = logging.getLogger("yomira.atomic_file_publisher")
_current_job_id: ContextVar[str | None] = ContextVar(
    "atomic_file_publisher_job_id", default=None
)

_WIN32_RETRYABLE_LOCK_ERRORS = frozenset({32, 33, 36})
_MOVE_FILE_FLAGS = 0
_READ_CHUNK_SIZE = 64 * 1024
_ADR_0087_RETRY_DELAYS_SECONDS = (0.1, 0.2, 0.4, 0.8)
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


class AtomicFilePublishError(RuntimeError):
    """Sanitized publication failure with stage and native error attribution."""

    def __init__(
        self,
        stage: str,
        *,
        trace_id: str,
        attempt: int,
        winerror: int | None = None,
        cause_type: str | None = None,
        mutation_proven: bool = False,
        job_id: str | None = None,
    ) -> None:
        self.stage = stage
        self.trace_id = trace_id
        self.attempt = attempt
        self.winerror = winerror
        self.cause_type = cause_type
        self.mutation_proven = mutation_proven
        self.job_id = job_id
        super().__init__(f"Atomic file publication failed during {stage}.")


class RetryPolicy(Protocol):
    """Explicit bounded delay policy consulted only for approved Win32 locks."""

    def delay_seconds(
        self,
        *,
        retry_index: int,
        elapsed_seconds: float,
    ) -> float | None:
        """Return the next wait, or None when the retry budget is exhausted."""


@dataclass(frozen=True, slots=True)
class ExponentialLockRetryPolicy:
    """Finite exponential wait schedule; all limits are explicit at construction."""

    retry_delays_seconds: tuple[float, ...]

    def __post_init__(self) -> None:
        if type(self.retry_delays_seconds) is not tuple:
            raise TypeError("retry_delays_seconds must be a tuple")
        if any(
            type(delay) not in {float, int}
            or not math.isfinite(delay)
            or delay <= 0
            for delay in self.retry_delays_seconds
        ):
            raise ValueError("retry delays must be positive finite numbers")
        if any(
            not math.isclose(later, earlier * 2, rel_tol=0.0, abs_tol=1e-12)
            for earlier, later in zip(
                self.retry_delays_seconds,
                self.retry_delays_seconds[1:],
                strict=False,
            )
        ):
            raise ValueError("retry delays must double after every retry")

    @property
    def max_retries(self) -> int:
        return len(self.retry_delays_seconds)

    def delay_seconds(
        self,
        *,
        retry_index: int,
        elapsed_seconds: float,
    ) -> float | None:
        if type(retry_index) is not int or retry_index < 0:
            raise ValueError("retry_index must be a non-negative integer")
        if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must be finite and non-negative")
        if retry_index >= len(self.retry_delays_seconds):
            return None
        return float(self.retry_delays_seconds[retry_index])


ADR_0087_RETRY_POLICY = ExponentialLockRetryPolicy(
    retry_delays_seconds=_ADR_0087_RETRY_DELAYS_SECONDS
)

MoveFileOperation = Callable[[Path, Path, int], None]
SleepOperation = Callable[[float], None]
MonotonicClock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    device: int
    file_id: int
    size_bytes: int
    sha256: str


class AtomicFilePublisher:
    """Promote an owned regular file inside one identified data root.

    The two-argument ``publish_no_replace`` method is compatible with the CBZ
    builder protocol. The publisher hashes the source before the rename and
    verifies the same file identity, size and bytes at the destination after it.
    Retry policy, sleep, and monotonic clock are mandatory constructor inputs;
    no implicit timing or retry budget is selected here.
    """

    def __init__(
        self,
        *,
        data_root_identity: DataRootIdentity,
        retry_policy: RetryPolicy,
        sleep: SleepOperation,
        clock: MonotonicClock,
        move_file: MoveFileOperation | None = None,
        storage_gate: SharedStorageGate | None = None,
    ) -> None:
        if type(data_root_identity) is not DataRootIdentity:
            raise TypeError("data_root_identity must be a DataRootIdentity")
        if not callable(getattr(retry_policy, "delay_seconds", None)):
            raise TypeError("retry_policy must implement delay_seconds")
        if not callable(sleep):
            raise TypeError("sleep must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if move_file is not None and not callable(move_file):
            raise TypeError("move_file must be callable when provided")
        if storage_gate is not None and type(storage_gate) is not SharedStorageGate:
            raise TypeError("storage_gate must be a SharedStorageGate or None")
        self.data_root_identity = data_root_identity
        self._retry_policy = retry_policy
        self._sleep = sleep
        self._clock = clock
        self._move_file = move_file if move_file is not None else _move_file_ex_win32
        self._storage_gate = storage_gate

    def publish_no_replace(
        self,
        source: Path,
        destination: Path,
        *,
        job_id: UUID | str | None = None,
    ) -> None:
        """Atomically move a verified source to an absent target without replace."""
        token = _current_job_id.set(_safe_job_id(job_id))
        trace_id = current_trace_id() or create_trace_id()
        try:
            try:
                self._publish(source, destination, trace_id=trace_id)
            except AtomicFilePublishError as error:
                self._log_failure(error)
                raise
            except StorageGateClosedError:
                raise
            except Exception as error:
                failure = AtomicFilePublishError(
                    "publication_internal_failure",
                    trace_id=trace_id,
                    attempt=1,
                    cause_type=type(error).__name__,
                    job_id=_current_job_id.get(),
                )
                self._log_failure(failure)
                raise failure from None
        finally:
            _current_job_id.reset(token)

    def _publish(self, source: Path, destination: Path, *, trace_id: str) -> None:
        source_path, _source_parts = self._managed_path(source, "source", trace_id)
        destination_path, _destination_parts = self._managed_path(
            destination, "destination", trace_id
        )
        if windows_path_key(source_path) == windows_path_key(destination_path):
            self._raise(
                "source_destination_identical",
                trace_id=trace_id,
                attempt=1,
                mutation_proven=True,
            )
        destination_info = self._inspect_managed_path(
            destination_path,
            role="destination",
            trace_id=trace_id,
            allow_missing_leaf=True,
            allow_existing_destination=False,
        )
        if destination_info is not None:
            self._raise(
                "destination_conflict",
                trace_id=trace_id,
                attempt=1,
                mutation_proven=True,
            )
        source_snapshot = self._hash_managed_file(
            source_path,
            role="source",
            trace_id=trace_id,
            attempt=1,
        )
        started_at = self._read_clock(trace_id=trace_id, attempt=1)
        retries_used = 0
        attempt = 1

        while True:
            self._assert_source_unchanged(
                source_path,
                source_snapshot,
                trace_id=trace_id,
                attempt=attempt,
            )
            target_info = self._inspect_managed_path(
                destination_path,
                role="destination",
                trace_id=trace_id,
                allow_missing_leaf=True,
                allow_existing_destination=False,
                attempt=attempt,
            )
            if target_info is not None:
                self._raise(
                    "destination_conflict",
                    trace_id=trace_id,
                    attempt=attempt,
                    mutation_proven=True,
                )
            self._assert_root_current(trace_id=trace_id, attempt=attempt)

            try:
                if self._storage_gate is None:
                    self._move_file(source_path, destination_path, _MOVE_FILE_FLAGS)
                else:
                    with self._storage_gate.mutation():
                        try:
                            self._move_file(source_path, destination_path, _MOVE_FILE_FLAGS)
                        except OSError as error:
                            if is_disk_full_os_error(error):
                                self._storage_gate.close("FILESYSTEM_DISK_FULL")
                            raise
            except StorageGateClosedError:
                raise
            except Exception as native_error:
                winerror = _native_winerror(native_error)
                if (
                    winerror in _WIN32_RETRYABLE_LOCK_ERRORS
                    and self._prove_failed_operation_applied(
                        source_path,
                        destination_path,
                        source_snapshot,
                        trace_id=trace_id,
                        attempt=attempt,
                    )
                ):
                    self._verify_success(
                        source_path,
                        destination_path,
                        source_snapshot,
                        trace_id=trace_id,
                        attempt=attempt,
                    )
                    return
                if not self._prove_failed_operation_unchanged(
                    source_path,
                    destination_path,
                    source_snapshot,
                    trace_id=trace_id,
                    attempt=attempt,
                ):
                    self._raise(
                        "mutation_ambiguous",
                        trace_id=trace_id,
                        attempt=attempt,
                        winerror=_native_winerror(native_error),
                        cause_type=type(native_error).__name__,
                        mutation_proven=False,
                    )

                if winerror not in _WIN32_RETRYABLE_LOCK_ERRORS:
                    self._raise(
                        "move_failed",
                        trace_id=trace_id,
                        attempt=attempt,
                        winerror=winerror,
                        cause_type=type(native_error).__name__,
                        mutation_proven=True,
                    )

                elapsed = self._elapsed_seconds(started_at, trace_id=trace_id, attempt=attempt)
                try:
                    delay = self._retry_policy.delay_seconds(
                        retry_index=retries_used,
                        elapsed_seconds=elapsed,
                    )
                except Exception as policy_error:
                    self._raise(
                        "retry_policy_failed",
                        trace_id=trace_id,
                        attempt=attempt,
                        winerror=winerror,
                        cause_type=type(policy_error).__name__,
                        mutation_proven=True,
                    )
                if delay is None:
                    self._raise(
                        "move_failed",
                        trace_id=trace_id,
                        attempt=attempt,
                        winerror=winerror,
                        cause_type=type(native_error).__name__,
                        mutation_proven=True,
                    )
                if (
                    type(delay) not in {int, float}
                    or not math.isfinite(delay)
                    or delay < 0
                ):
                    self._raise(
                        "retry_policy_invalid",
                        trace_id=trace_id,
                        attempt=attempt,
                        winerror=winerror,
                        mutation_proven=True,
                    )
                self._log_retry(
                    trace_id=trace_id,
                    attempt=attempt,
                    winerror=winerror,
                    elapsed_seconds=elapsed,
                )
                try:
                    self._sleep(float(delay))
                except Exception as sleep_error:
                    self._raise(
                        "retry_wait_failed",
                        trace_id=trace_id,
                        attempt=attempt,
                        winerror=winerror,
                        cause_type=type(sleep_error).__name__,
                        mutation_proven=True,
                    )
                retries_used += 1
                attempt += 1
                continue

            self._verify_success(
                source_path,
                destination_path,
                source_snapshot,
                trace_id=trace_id,
                attempt=attempt,
            )
            return

    def _managed_path(
        self,
        path: object,
        role: str,
        trace_id: str,
    ) -> tuple[Path, tuple[str, ...]]:
        if not isinstance(path, Path):
            self._raise(
                "path_invalid",
                trace_id=trace_id,
                attempt=1,
                mutation_proven=True,
            )
        if not is_absolute_windows_path(path):
            self._raise(
                "path_invalid",
                trace_id=trace_id,
                attempt=1,
                mutation_proven=True,
            )
        try:
            absolute = Path(os.path.abspath(path))
            root = self.data_root_identity.canonical_path
            root_key = windows_path_key(root).rstrip("\\")
            absolute_key = windows_path_key(absolute)
            if not absolute_key.startswith(f"{root_key}\\"):
                self._raise(
                    "path_outside_data_root",
                    trace_id=trace_id,
                    attempt=1,
                    mutation_proven=True,
                )
            relative = absolute.relative_to(root)
            if not relative.parts:
                self._raise(
                    "path_invalid",
                    trace_id=trace_id,
                    attempt=1,
                    mutation_proven=True,
                )
            return absolute, tuple(relative.parts)
        except AtomicFilePublishError:
            raise
        except Exception as error:
            self._raise(
                "path_invalid",
                trace_id=trace_id,
                attempt=1,
                cause_type=type(error).__name__,
                mutation_proven=True,
            )

    def _inspect_managed_path(
        self,
        path: Path,
        *,
        role: str,
        trace_id: str,
        allow_missing_leaf: bool,
        allow_existing_destination: bool,
        attempt: int = 1,
    ) -> os.stat_result | None:
        self._assert_root_current(trace_id=trace_id, attempt=attempt)
        root = self.data_root_identity.canonical_path

        def validate_root() -> None:
            self._assert_root_current(trace_id=trace_id, attempt=attempt)

        try:
            root_info = retry_lstat(
                root,
                component="atomic_file_publisher",
                stage="root_stat",
                trace_id=trace_id,
                job_id=_current_job_id.get(),
                validate_path=validate_root,
                sleep=self._sleep,
            )
        except Exception as error:
            self._raise(
                "root_unavailable",
                trace_id=trace_id,
                attempt=attempt,
                cause_type=type(error).__name__,
                mutation_proven=False,
            )
        if self._is_reparse_point(root_info):
            self._raise(
                "root_is_reparse_point",
                trace_id=trace_id,
                attempt=attempt,
                mutation_proven=False,
            )
        if not stat.S_ISDIR(root_info.st_mode):
            self._raise(
                "root_not_directory",
                trace_id=trace_id,
                attempt=attempt,
                mutation_proven=False,
            )
        # ``st_dev`` is the filesystem's device token for comparing entries.
        # It need not use the same numeric representation as the Win32
        # FILE_ID_INFO volume serial captured by DataRootIdentity.
        root_device = root_info.st_dev

        try:
            relative = path.relative_to(root)
        except ValueError as error:
            self._raise(
                "path_outside_data_root",
                trace_id=trace_id,
                attempt=attempt,
                cause_type=type(error).__name__,
                mutation_proven=True,
            )
        parts = relative.parts
        if not parts:
            self._raise(
                "path_invalid",
                trace_id=trace_id,
                attempt=attempt,
                mutation_proven=True,
            )

        current = root
        expected_parent_identity = RetryingFileStream.identity_of(root_info)
        info: os.stat_result | None = None
        for index, component in enumerate(parts):
            current = current / component
            is_leaf = index == len(parts) - 1
            try:
                info = retry_lstat(
                    current,
                    component="atomic_file_publisher",
                    stage="managed_path_stat",
                    trace_id=trace_id,
                    job_id=_current_job_id.get(),
                    validate_path=validate_root,
                    sleep=self._sleep,
                    expected_parent_identity=expected_parent_identity,
                )
            except FileNotFoundError as error:
                if is_leaf and allow_missing_leaf:
                    return None
                self._raise(
                    "source_unavailable" if role == "source" else "destination_parent_unavailable",
                    trace_id=trace_id,
                    attempt=attempt,
                    cause_type=type(error).__name__,
                    mutation_proven=True,
                )
            except Exception as error:
                self._raise(
                    "path_inspection_failed",
                    trace_id=trace_id,
                    attempt=attempt,
                    cause_type=type(error).__name__,
                    mutation_proven=False,
                )

            if self._is_reparse_point(info):
                stage = (
                    "source_not_regular"
                    if role == "source" and is_leaf
                    else "destination_reparse_point"
                    if role == "destination" and is_leaf
                    else "path_reparse_point"
                )
                self._raise(
                    stage,
                    trace_id=trace_id,
                    attempt=attempt,
                    mutation_proven=True,
                )
            if info.st_dev != root_device:
                self._raise(
                    "same_volume_required",
                    trace_id=trace_id,
                    attempt=attempt,
                    mutation_proven=True,
                )
            if not is_leaf and not stat.S_ISDIR(info.st_mode):
                self._raise(
                    "path_parent_not_directory",
                    trace_id=trace_id,
                    attempt=attempt,
                    mutation_proven=True,
                )
            if is_leaf and role == "source" and not stat.S_ISREG(info.st_mode):
                self._raise(
                    "source_not_regular",
                    trace_id=trace_id,
                    attempt=attempt,
                    mutation_proven=True,
                )
            if is_leaf and role == "destination" and not allow_existing_destination:
                self._raise(
                    "destination_conflict",
                    trace_id=trace_id,
                    attempt=attempt,
                    mutation_proven=True,
                )
            expected_parent_identity = RetryingFileStream.identity_of(info)
        if info is None:
            self._raise(
                "path_invalid",
                trace_id=trace_id,
                attempt=attempt,
                mutation_proven=True,
            )
        return info

    def _hash_managed_file(
        self,
        path: Path,
        *,
        role: str,
        trace_id: str,
        attempt: int,
    ) -> _FileSnapshot:
        def validate_root() -> None:
            self._assert_root_current(trace_id=trace_id, attempt=attempt)

        def lstat_managed(candidate: Path, *, stage: str) -> os.stat_result:
            return retry_lstat(
                candidate,
                component="atomic_file_publisher",
                stage=stage,
                trace_id=trace_id,
                job_id=_current_job_id.get(),
                validate_path=validate_root,
                sleep=self._sleep,
            )

        before = self._inspect_managed_path(
            path,
            role=role,
            trace_id=trace_id,
            allow_missing_leaf=False,
            allow_existing_destination=True,
            attempt=attempt,
        )
        if before is None or not stat.S_ISREG(before.st_mode):
            self._raise(
                "source_not_regular" if role == "source" else "published_artifact_invalid",
                trace_id=trace_id,
                attempt=attempt,
                mutation_proven=True,
            )

        parent_before = lstat_managed(path.parent, stage="hash_parent_stat")
        if (
            self._is_reparse_point(parent_before)
            or not stat.S_ISDIR(parent_before.st_mode)
        ):
            self._raise(
                "source_changed_before_read",
                trace_id=trace_id,
                attempt=attempt,
                mutation_proven=True,
            )
        parent_identity = (parent_before.st_dev, parent_before.st_ino)

        def read_snapshot_is_unchanged() -> bool:
            try:
                self.data_root_identity.assert_current()
                current_parent = lstat_managed(
                    path.parent,
                    stage="hash_parent_snapshot",
                )
                current_file = lstat_managed(
                    path,
                    stage="hash_file_snapshot",
                )
            except Exception:
                return False
            return (
                not self._is_reparse_point(current_parent)
                and stat.S_ISDIR(current_parent.st_mode)
                and (current_parent.st_dev, current_parent.st_ino) == parent_identity
                and not self._is_reparse_point(current_file)
                and stat.S_ISREG(current_file.st_mode)
                and self._same_file(before, current_file)
            )

        def read_and_hash_from_zero() -> _FileSnapshot:
            hasher = hashlib.sha256()
            byte_count = 0
            opened_stream = path.open("rb")
            try:
                stream = opened_stream.__enter__()
            except BaseException as enter_error:
                close_error = close_read_handle(
                    opened_stream,
                    component="atomic_file_publisher",
                    stage=f"{role}_hash_enter_close",
                    trace_id=trace_id,
                    job_id=_current_job_id.get(),
                )
                try:
                    definitely_closed = opened_stream.closed is True
                except BaseException:
                    definitely_closed = False
                if not definitely_closed:
                    failure = close_error or OSError(
                        "Hash read handle close is unverified"
                    )
                    raise failure from enter_error
                close_is_transient = (
                    isinstance(close_error, OSError)
                    and _native_winerror(close_error) in _WIN32_RETRYABLE_LOCK_ERRORS
                )
                enter_is_transient = (
                    isinstance(enter_error, OSError)
                    and _native_winerror(enter_error) in _WIN32_RETRYABLE_LOCK_ERRORS
                )
                if close_error is not None and enter_is_transient and not close_is_transient:
                    raise close_error from enter_error
                raise

            operation_error: BaseException | None = None
            closed_snapshot: os.stat_result | None = None
            try:
                opened = os.fstat(stream.fileno())
                if not self._same_file(before, opened) or self._is_reparse_point(opened):
                    self._raise(
                        "source_changed_before_read",
                        trace_id=trace_id,
                        attempt=attempt,
                        mutation_proven=True,
                    )
                while chunk := stream.read(_READ_CHUNK_SIZE):
                    hasher.update(chunk)
                    byte_count += len(chunk)
                closed_snapshot = os.fstat(stream.fileno())
            except BaseException as error:
                operation_error = error

            close_error = close_read_handle(
                stream,
                component="atomic_file_publisher",
                stage=f"{role}_hash_close",
                trace_id=trace_id,
                job_id=_current_job_id.get(),
            )
            try:
                definitely_closed = stream.closed is True
            except BaseException:
                definitely_closed = False
            if not definitely_closed:
                raise close_error or OSError(
                    "Hash read handle close is unverified"
                )
            close_is_transient = (
                isinstance(close_error, OSError)
                and _native_winerror(close_error) in _WIN32_RETRYABLE_LOCK_ERRORS
            )
            operation_is_transient = (
                isinstance(operation_error, OSError)
                and _native_winerror(operation_error) in _WIN32_RETRYABLE_LOCK_ERRORS
            )
            if close_error is not None and (
                operation_error is None
                or (operation_is_transient and not close_is_transient)
            ):
                raise close_error
            if operation_error is not None:
                raise operation_error
            if close_error is not None:
                raise close_error
            if closed_snapshot is None:
                raise OSError("Hash read produced no stable snapshot")

            after = self._inspect_managed_path(
                path,
                role=role,
                trace_id=trace_id,
                allow_missing_leaf=False,
                allow_existing_destination=True,
                attempt=attempt,
            )
            current_parent = lstat_managed(path.parent, stage="hash_parent_postread")
            if (
                after is None
                or not self._same_file(before, closed_snapshot)
                or not self._same_file(before, after)
                or byte_count != before.st_size
                or byte_count != closed_snapshot.st_size
                or self._is_reparse_point(current_parent)
                or not stat.S_ISDIR(current_parent.st_mode)
                or (current_parent.st_dev, current_parent.st_ino) != parent_identity
            ):
                self._raise(
                    "source_changed_during_read",
                    trace_id=trace_id,
                    attempt=attempt,
                    mutation_proven=False,
                )
            return _FileSnapshot(
                device=before.st_dev,
                file_id=before.st_ino,
                size_bytes=byte_count,
                sha256=hasher.hexdigest(),
            )

        try:
            return retry_filesystem_operation(
                read_and_hash_from_zero,
                unchanged=read_snapshot_is_unchanged,
                component="atomic_file_publisher",
                stage=f"{role}_hash_read",
                trace_id=trace_id,
                job_id=_current_job_id.get(),
                sleep=self._sleep,
            )
        except AtomicFilePublishError:
            raise
        except Exception as error:
            self._raise(
                "source_read_failed" if role == "source" else "published_read_failed",
                trace_id=trace_id,
                attempt=attempt,
                cause_type=type(error).__name__,
                winerror=_native_winerror(error),
                mutation_proven=True,
            )

    def _assert_source_unchanged(
        self,
        source: Path,
        expected: _FileSnapshot,
        *,
        trace_id: str,
        attempt: int,
    ) -> None:
        observed = self._hash_managed_file(
            source,
            role="source",
            trace_id=trace_id,
            attempt=attempt,
        )
        if observed != expected:
            self._raise(
                "source_changed_before_move",
                trace_id=trace_id,
                attempt=attempt,
                mutation_proven=True,
            )

    def _prove_failed_operation_unchanged(
        self,
        source: Path,
        destination: Path,
        expected: _FileSnapshot,
        *,
        trace_id: str,
        attempt: int,
    ) -> bool:
        try:
            self._assert_root_current(trace_id=trace_id, attempt=attempt)
            target = self._inspect_managed_path(
                destination,
                role="destination",
                trace_id=trace_id,
                allow_missing_leaf=True,
                allow_existing_destination=True,
                attempt=attempt,
            )
            if target is not None:
                return False
            observed = self._hash_managed_file(
                source,
                role="source",
                trace_id=trace_id,
                attempt=attempt,
            )
            return observed == expected
        except Exception:
            return False

    def _prove_failed_operation_applied(
        self,
        source: Path,
        destination: Path,
        expected: _FileSnapshot,
        *,
        trace_id: str,
        attempt: int,
    ) -> bool:
        """Recognize a completed move after a transient native error, without replay."""
        try:
            self._assert_root_current(trace_id=trace_id, attempt=attempt)
            source_after = self._inspect_managed_path(
                source,
                role="source",
                trace_id=trace_id,
                allow_missing_leaf=True,
                allow_existing_destination=True,
                attempt=attempt,
            )
            destination_after = self._inspect_managed_path(
                destination,
                role="destination",
                trace_id=trace_id,
                allow_missing_leaf=True,
                allow_existing_destination=True,
                attempt=attempt,
            )
            if source_after is not None or destination_after is None:
                return False
            observed = self._hash_managed_file(
                destination,
                role="destination",
                trace_id=trace_id,
                attempt=attempt,
            )
            return observed == expected
        except Exception:
            return False

    def _verify_success(
        self,
        source: Path,
        destination: Path,
        expected: _FileSnapshot,
        *,
        trace_id: str,
        attempt: int,
    ) -> None:
        try:
            self._assert_root_current(trace_id=trace_id, attempt=attempt)
            source_after = self._inspect_managed_path(
                source,
                role="source",
                trace_id=trace_id,
                allow_missing_leaf=True,
                allow_existing_destination=True,
                attempt=attempt,
            )
            target_after = self._inspect_managed_path(
                destination,
                role="destination",
                trace_id=trace_id,
                allow_missing_leaf=False,
                allow_existing_destination=True,
                attempt=attempt,
            )
            if source_after is not None or target_after is None:
                self._raise(
                    "publication_mutation_ambiguous",
                    trace_id=trace_id,
                    attempt=attempt,
                    mutation_proven=False,
                )
            published = self._hash_managed_file(
                destination,
                role="destination",
                trace_id=trace_id,
                attempt=attempt,
            )
        except AtomicFilePublishError as error:
            if error.stage == "publication_mutation_ambiguous":
                raise
            self._raise(
                "publication_verification_failed",
                trace_id=trace_id,
                attempt=attempt,
                winerror=error.winerror,
                cause_type=error.cause_type,
                mutation_proven=False,
            )
        if published != expected:
            self._raise(
                "publication_content_mismatch",
                trace_id=trace_id,
                attempt=attempt,
                mutation_proven=False,
            )

    def _read_clock(self, *, trace_id: str, attempt: int) -> float:
        try:
            value = self._clock()
        except Exception as error:
            self._raise(
                "clock_failed",
                trace_id=trace_id,
                attempt=attempt,
                cause_type=type(error).__name__,
                mutation_proven=True,
            )
        if type(value) not in {float, int} or not math.isfinite(value):
            self._raise(
                "clock_invalid",
                trace_id=trace_id,
                attempt=attempt,
                mutation_proven=True,
            )
        return float(value)

    def _elapsed_seconds(
        self,
        started_at: float,
        *,
        trace_id: str,
        attempt: int,
    ) -> float:
        elapsed = self._read_clock(trace_id=trace_id, attempt=attempt) - started_at
        if not math.isfinite(elapsed) or elapsed < 0:
            self._raise(
                "clock_not_monotonic",
                trace_id=trace_id,
                attempt=attempt,
                mutation_proven=True,
            )
        return elapsed

    def _assert_root_current(self, *, trace_id: str, attempt: int) -> None:
        try:
            self.data_root_identity.assert_current()
        except (DataRootIdentityError, OSError, RuntimeError) as error:
            self._raise(
                "root_identity_invalid",
                trace_id=trace_id,
                attempt=attempt,
                cause_type=type(error).__name__,
                mutation_proven=False,
            )

    def _raise(
        self,
        stage: str,
        *,
        trace_id: str,
        attempt: int,
        winerror: int | None = None,
        cause_type: str | None = None,
        mutation_proven: bool = False,
    ) -> NoReturn:
        raise AtomicFilePublishError(
            stage,
            trace_id=trace_id,
            attempt=attempt,
            winerror=winerror,
            cause_type=cause_type,
            mutation_proven=mutation_proven,
            job_id=_current_job_id.get(),
        )

    @staticmethod
    def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
        return (
            first.st_dev == second.st_dev
            and first.st_ino == second.st_ino
            and first.st_size == second.st_size
        )

    @staticmethod
    def _is_reparse_point(info: os.stat_result) -> bool:
        attributes = getattr(info, "st_file_attributes", 0)
        return stat.S_ISLNK(info.st_mode) or bool(
            attributes & _FILE_ATTRIBUTE_REPARSE_POINT
        )

    @staticmethod
    def _log_retry(
        *,
        trace_id: str,
        attempt: int,
        winerror: int,
        elapsed_seconds: float,
    ) -> None:
        logger.warning(
            "atomic_file_publish.retrying",
            extra={
                "component": "atomic_file_publisher",
                "stage": "move_file",
                "trace_id": trace_id,
                **(
                    {"job_id": _current_job_id.get()}
                    if _current_job_id.get() is not None
                    else {}
                ),
                "attempt": attempt,
                "native_error_code": winerror,
                "duration": elapsed_seconds,
            },
        )

    @staticmethod
    def _log_failure(error: AtomicFilePublishError) -> None:
        extra: dict[str, object] = {
            "component": "atomic_file_publisher",
            "stage": error.stage,
            "trace_id": error.trace_id,
            "attempt": error.attempt,
            "mutation_proven": error.mutation_proven,
        }
        if error.job_id is not None:
            extra["job_id"] = error.job_id
        if error.winerror is not None:
            extra["native_error_code"] = error.winerror
        if error.cause_type is not None:
            extra["cause_type"] = error.cause_type
        logger.error("atomic_file_publish.failed", extra=extra)


def _move_file_ex_win32(source: Path, destination: Path, flags: int) -> None:
    """Call MoveFileExW with flags 0: same-volume move, never replace/copy."""
    if flags != _MOVE_FILE_FLAGS:
        raise ValueError("MoveFileExW flags must be zero")
    if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
        raise OSError("MoveFileExW is unavailable on this platform.")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    move_file = kernel32.MoveFileExW  # type: ignore[attr-defined]
    move_file.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
    move_file.restype = ctypes.c_int
    if not move_file(str(source), str(destination), _MOVE_FILE_FLAGS):
        raise ctypes.WinError(ctypes.get_last_error())


def _native_winerror(error: BaseException) -> int | None:
    value = getattr(error, "winerror", None)
    return value if type(value) is int else None


def _safe_job_id(value: UUID | str | None) -> str | None:
    if isinstance(value, UUID):
        return str(value) if value.version == 4 else None
    if type(value) is not str:
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    canonical = str(parsed)
    return canonical if parsed.version == 4 and canonical == value.lower() else None
