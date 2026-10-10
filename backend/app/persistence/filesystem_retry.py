"""Bounded Windows filesystem-lock retries that preserve file candidates."""

from __future__ import annotations

import errno
import logging
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TypeVar, cast

from app.persistence.storage_gate import SharedStorageGate, is_disk_full_os_error
from app.tracing import create_trace_id

logger = logging.getLogger("yomira.filesystem_retry")

WIN32_TRANSIENT_LOCK_ERRORS = frozenset({32, 33, 36})
ADR_0087_RETRY_DELAYS_SECONDS = (0.1, 0.2, 0.4, 0.8)
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)

T = TypeVar("T")
FilesystemOperation = Callable[[], T]
UnchangedProof = Callable[[], bool]
AppliedProof = Callable[[], bool]
PathValidator = Callable[[], None]
SleepOperation = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class _WriteSnapshot:
    identity: tuple[int, int]
    size: int
    position: int
    previous_bytes: bytes

    def result_size(self, payload_size: int) -> int:
        return max(self.size, self.position + payload_size)


@dataclass(slots=True)
class _RetryBudget:
    retries_used: int = 0


def native_winerror(error: BaseException) -> int | None:
    value = getattr(error, "winerror", None)
    return value if type(value) is int else None


def is_transient_lock_error(error: BaseException) -> bool:
    return native_winerror(error) in WIN32_TRANSIENT_LOCK_ERRORS


def _run_storage_mutation[T](
    storage_gate: SharedStorageGate | None,
    operation: Callable[[], T],
) -> T:
    if storage_gate is None:
        return operation()
    with storage_gate.mutation():
        try:
            return operation()
        except OSError as error:
            if is_disk_full_os_error(error):
                storage_gate.close("FILESYSTEM_DISK_FULL")
            raise


def retry_filesystem_operation[T](
    operation: FilesystemOperation[T],
    *,
    unchanged: UnchangedProof,
    component: str,
    stage: str,
    trace_id: str,
    job_id: str | None = None,
    applied: AppliedProof | None = None,
    applied_result: T | None = None,
    retry_budget: _RetryBudget | None = None,
    sleep: SleepOperation | None = None,
) -> T:
    """Retry an operation only while its caller proves its pre-state remains exact."""
    budget = retry_budget or _RetryBudget()
    while True:
        try:
            return operation()
        except OSError as error:
            winerror = native_winerror(error)
            if winerror not in WIN32_TRANSIENT_LOCK_ERRORS:
                raise
            try:
                still_unchanged = unchanged()
            except Exception as proof_error:
                error.add_note(
                    "Filesystem retry stopped because its unchanged-state proof "
                    f"failed ({type(proof_error).__name__})."
                )
                raise error from None
            if not still_unchanged:
                if applied is not None:
                    try:
                        operation_was_applied = applied()
                    except Exception as proof_error:
                        error.add_note(
                            "Filesystem retry stopped because its applied-state "
                            "proof failed "
                            f"({type(proof_error).__name__})."
                        )
                        operation_was_applied = False
                    if operation_was_applied:
                        _log_applied(
                            component=component,
                            stage=stage,
                            trace_id=trace_id,
                            job_id=job_id,
                            winerror=winerror,
                            attempt=budget.retries_used + 1,
                        )
                        return cast(T, applied_result)
                _log_stopped(
                    component=component,
                    stage=stage,
                    trace_id=trace_id,
                    job_id=job_id,
                    winerror=winerror,
                    attempt=budget.retries_used + 1,
                    reason="pre_state_not_proven_unchanged",
                )
                error.add_note(
                    "Filesystem retry stopped because the operation's pre-state "
                    "could not be proven unchanged."
                )
                raise
            if budget.retries_used >= len(ADR_0087_RETRY_DELAYS_SECONDS):
                _log_exhausted(
                    component=component,
                    stage=stage,
                    trace_id=trace_id,
                    job_id=job_id,
                    winerror=winerror,
                    attempts=budget.retries_used + 1,
                )
                raise
            delay = ADR_0087_RETRY_DELAYS_SECONDS[budget.retries_used]
            _log_retry(
                component=component,
                stage=stage,
                trace_id=trace_id,
                job_id=job_id,
                winerror=winerror,
                attempt=budget.retries_used + 1,
                delay=delay,
            )
            try:
                (sleep or time.sleep)(delay)
            except Exception as sleep_error:
                error.add_note(
                    "Filesystem retry stopped because its wait failed "
                    f"({type(sleep_error).__name__})."
                )
                raise error from None
            budget.retries_used += 1
            # A proof made before sleeping is stale once control returns: a
            # different process may have changed the candidate or its parent
            # namespace during the wait. Re-prove the exact pre-state before
            # allowing another mutation attempt.
            try:
                still_unchanged = unchanged()
            except Exception as proof_error:
                error.add_note(
                    "Filesystem retry stopped because its post-wait "
                    "unchanged-state proof failed "
                    f"({type(proof_error).__name__})."
                )
                raise error from None
            if not still_unchanged:
                if applied is not None:
                    try:
                        operation_was_applied = applied()
                    except Exception as proof_error:
                        error.add_note(
                            "Filesystem retry stopped because its post-wait "
                            "applied-state proof failed "
                            f"({type(proof_error).__name__})."
                        )
                        operation_was_applied = False
                    if operation_was_applied:
                        _log_applied(
                            component=component,
                            stage=stage,
                            trace_id=trace_id,
                            job_id=job_id,
                            winerror=winerror,
                            attempt=budget.retries_used + 1,
                        )
                        return cast(T, applied_result)
                _log_stopped(
                    component=component,
                    stage=stage,
                    trace_id=trace_id,
                    job_id=job_id,
                    winerror=winerror,
                    attempt=budget.retries_used + 1,
                    reason="pre_state_changed_during_wait",
                )
                error.add_note(
                    "Filesystem retry stopped because the operation's "
                    "pre-state changed during the wait."
                )
                raise error from None


def retry_lstat(
    path: Path,
    *,
    component: str,
    stage: str,
    trace_id: str,
    job_id: str | None = None,
    validate_path: PathValidator | None = None,
    sleep: SleepOperation | None = None,
    retry_budget: _RetryBudget | None = None,
    expected_parent_identity: tuple[int, int] | None = None,
) -> os.stat_result:
    """Retry an ``lstat`` while its exact parent directory keeps the same ID.

    ``expected_parent_identity`` is a previously captured identity from a
    caller that already validated the parent path's ancestry. In that case,
    this function probes only the exact parent before and after the candidate
    lstat and validates the data root immediately around the candidate probe.
    Without it, the parent identity is captured using the full safe ancestor
    walk first.
    """
    budget = retry_budget or _RetryBudget()
    parent = path.parent
    # A volume root has no independent parent namespace to establish a stable
    # identity against. lstat is read-only, so its transient retry can restart
    # the query; use an external validator when the caller has one.
    if parent == path:
        if validate_path is not None:
            validate_path()

        def anchor_is_unchanged() -> bool:
            if validate_path is not None:
                validate_path()
            return True

        return retry_filesystem_operation(
            path.lstat,
            unchanged=anchor_is_unchanged,
            component=component,
            stage=stage,
            trace_id=trace_id,
            job_id=job_id,
            retry_budget=budget,
            sleep=sleep,
        )

    parent_identity = expected_parent_identity
    if parent_identity is None:
        if validate_path is not None:
            validate_path()
        parent_info = retry_lstat(
            parent,
            component=component,
            stage=f"{stage}_parent_snapshot",
            trace_id=trace_id,
            job_id=job_id,
            validate_path=validate_path,
            sleep=sleep,
            retry_budget=budget,
        )
        parent_identity = RetryingFileStream.identity_of(parent_info)
    if (
        parent_identity is None
    ):
        raise OSError("Cannot prove the candidate's parent directory identity")

    def assert_parent_identity() -> None:
        current_parent = parent.lstat()
        if (
            _is_reparse_or_not_directory(current_parent)
            or RetryingFileStream.identity_of(current_parent) != parent_identity
        ):
            raise OSError("Cannot prove the candidate's parent directory identity")

    def prove_parent_identity() -> None:
        if validate_path is not None:
            validate_path()
        assert_parent_identity()
        if validate_path is not None:
            validate_path()

    def parent_is_unchanged() -> bool:
        try:
            prove_parent_identity()
        except OSError:
            return False
        return True

    def lstat_with_parent_proof() -> os.stat_result:
        assert_parent_identity()
        if validate_path is not None:
            validate_path()
        observed = path.lstat()
        if validate_path is not None:
            validate_path()
        # A successful read is only usable if the namespace still resolves
        # through the same exact parent after the candidate probe.
        assert_parent_identity()
        return observed

    return retry_filesystem_operation(
        lstat_with_parent_proof,
        unchanged=parent_is_unchanged,
        component=component,
        stage=stage,
        trace_id=trace_id,
        job_id=job_id,
        retry_budget=budget,
        sleep=sleep,
    )


def close_read_handle(
    stream: BinaryIO,
    *,
    component: str,
    stage: str,
    trace_id: str,
    job_id: str | None = None,
) -> BaseException | None:
    """Close a read handle and report errors only after checking its state.

    A caller may retry a transient close error only if ``closed`` proves that
    the prior handle is gone. An unverified handle is converted to a permanent
    I/O error so an outer retry loop cannot reopen the file beside it.
    """
    close_error: BaseException | None = None
    try:
        stream.close()
    except BaseException as error:
        close_error = error

    try:
        definitely_closed = stream.closed is True
    except BaseException:
        definitely_closed = False

    if definitely_closed:
        return close_error

    winerror = native_winerror(close_error) if close_error is not None else None
    logger.error(
        "filesystem_read_handle_close_unverified",
        extra={
            "component": component,
            "trace_id": trace_id or create_trace_id(),
            "job_id": job_id,
            "stage": stage,
            "native_error_code": winerror,
            "cause_type": type(close_error).__name__ if close_error else None,
            "retryable": False,
        },
    )
    failure = OSError(errno.EIO, "Read handle close could not be verified")
    if close_error is not None:
        failure.add_note(
            "The prior read handle did not report a closed state "
            f"({type(close_error).__name__})."
        )
    return failure


def _is_reparse_or_not_directory(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return (
        stat.S_ISLNK(info.st_mode)
        or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
        or not stat.S_ISDIR(info.st_mode)
    )


class RetryingFileStream:
    """Unbuffered writer with bounded, pre-state-verified Windows lock retries.

    On a failed write, the handle is closed before the same file ID, size, and
    affected byte range are re-read. A retry happens only when the operation's
    exact pre-state is still present. If the complete payload is present despite
    the error, the write is treated as applied; partial or other changes are
    preserved and surfaced without replay.
    """

    def __init__(
        self,
        path: Path,
        stream: BinaryIO,
        *,
        component: str,
        stage: str,
        trace_id: str,
        job_id: str | None = None,
        validate_path: PathValidator | None = None,
        sleep: SleepOperation | None = None,
        storage_gate: SharedStorageGate | None = None,
    ) -> None:
        self._path = path
        self._stream: BinaryIO | None = stream
        self._component = component
        self._stage = stage
        self._trace_id = trace_id or create_trace_id()
        self._job_id = job_id
        self._validate_path = validate_path
        self._sleep = sleep
        self._storage_gate = storage_gate
        self._closed = False
        try:
            info = os.fstat(stream.fileno())
            identity = self.identity_of(info)
            self._known_size = info.st_size
            self._position = stream.tell()
            if self._known_size != 0 or self._position != 0:
                raise ValueError(
                    "RetryingFileStream requires a newly created empty file"
                )
            if identity is None:
                raise OSError("Filesystem identity is unavailable for safe retry")
            self._identity = identity
        except BaseException as initialization_error:
            try:
                self._close_stream(stream)
            except BaseException as close_error:
                initialization_error.add_note(
                    "New file stream cleanup failed "
                    f"({type(close_error).__name__})."
                )
            raise

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        component: str,
        stage: str,
        trace_id: str,
        job_id: str | None = None,
        validate_path: PathValidator | None = None,
        sleep: SleepOperation | None = None,
        storage_gate: SharedStorageGate | None = None,
    ) -> RetryingFileStream:
        stable_trace_id = trace_id or create_trace_id()
        retry_budget = _RetryBudget()
        if validate_path is not None:
            validate_path()
        parent_info = retry_lstat(
            path.parent,
            component=component,
            stage=f"{stage}_parent_snapshot",
            trace_id=stable_trace_id,
            job_id=job_id,
            validate_path=validate_path,
            retry_budget=retry_budget,
            sleep=sleep,
        )
        parent_identity = cls.identity_of(parent_info)
        if parent_identity is None or _is_reparse_or_not_directory(parent_info):
            raise OSError("Cannot prove the exclusive-create parent identity")

        def creation_prestate_is_unchanged() -> bool:
            if validate_path is not None:
                validate_path()
            try:
                current_parent = retry_lstat(
                    path.parent,
                    component=component,
                    stage=f"{stage}_parent_proof",
                    trace_id=stable_trace_id,
                    job_id=job_id,
                    validate_path=validate_path,
                    retry_budget=retry_budget,
                    sleep=sleep,
                )
            except OSError:
                return False
            return (
                not _is_reparse_or_not_directory(current_parent)
                and cls.identity_of(current_parent) == parent_identity
                and _path_is_absent(
                    path,
                    component=component,
                    stage=f"{stage}_absence_proof",
                    trace_id=stable_trace_id,
                    job_id=job_id,
                    validate_path=validate_path,
                    retry_budget=retry_budget,
                    sleep=sleep,
                    expected_parent_identity=parent_identity,
                )
            )

        if not creation_prestate_is_unchanged():
            try:
                retry_lstat(
                    path,
                    component=component,
                    stage=f"{stage}_destination_check",
                    trace_id=stable_trace_id,
                    job_id=job_id,
                    validate_path=validate_path,
                    retry_budget=retry_budget,
                    sleep=sleep,
                    expected_parent_identity=parent_identity,
                )
            except FileNotFoundError:
                raise OSError("Cannot prove the exclusive-create pre-state") from None
            raise FileExistsError(errno.EEXIST, "Exclusive-create candidate exists")
        stream = retry_filesystem_operation(
            lambda: _run_storage_mutation(
                storage_gate, lambda: path.open("x+b", buffering=0)
            ),
            unchanged=creation_prestate_is_unchanged,
            component=component,
            stage=stage,
            trace_id=stable_trace_id,
            job_id=job_id,
            retry_budget=retry_budget,
            sleep=sleep,
        )
        return cls(
            path,
            stream,
            component=component,
            stage=stage,
            trace_id=stable_trace_id,
            job_id=job_id,
            validate_path=validate_path,
            sleep=sleep,
            storage_gate=storage_gate,
        )

    @property
    def name(self) -> str:
        return str(self._path)

    @property
    def closed(self) -> bool:
        return self._closed

    def writable(self) -> bool:
        return not self._closed

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return not self._closed

    def tell(self) -> int:
        return self._position

    def fileno(self) -> int:
        stream = self._require_open()
        return stream.fileno()

    def write(self, value: object) -> int:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError("write value must be bytes-like")
        payload = bytes(cast(bytes | bytearray | memoryview[int], value))
        if not payload:
            self._require_open()
            return 0

        retry_budget = _RetryBudget()
        while True:
            stream = self._ensure_open()
            try:
                snapshot = self._capture_write_snapshot(stream, len(payload))
            except OSError as error:
                winerror = native_winerror(error)
                if winerror not in WIN32_TRANSIENT_LOCK_ERRORS:
                    raise
                expected_state = (self._identity, self._known_size)
                self._close_after_failed_operation(error)
                unchanged = self._path_matches_state(
                    expected_state, retry_budget=retry_budget
                )
                if not self._wait_for_retry(
                    error,
                    operation_stage=f"{self._stage}_write_snapshot",
                    retry_budget=retry_budget,
                    unchanged=unchanged,
                ):
                    raise
                if not self._path_matches_state(
                    expected_state, retry_budget=retry_budget
                ):
                    _log_stopped(
                        component=self._component,
                        stage=f"{self._stage}_write_snapshot",
                        trace_id=self._trace_id,
                        job_id=self._job_id,
                        winerror=winerror,
                        attempt=retry_budget.retries_used + 1,
                        reason="pre_state_changed_during_wait",
                    )
                    error.add_note(
                        "Filesystem retry stopped because the file changed during "
                        "the write-snapshot wait."
                    )
                    raise
                self._reopen_for_state_operation(expected_state, retry_budget)
                continue
            try:
                written = _run_storage_mutation(
                    self._storage_gate, lambda current=stream: current.write(payload)
                )
            except OSError as error:
                winerror = native_winerror(error)
                if winerror not in WIN32_TRANSIENT_LOCK_ERRORS:
                    raise
                self._close_after_failed_operation(error)
                status = self._classify_write_after_error(
                    snapshot, payload, retry_budget=retry_budget
                )
                if status == "applied":
                    self._position = snapshot.position + len(payload)
                    self._known_size = snapshot.result_size(len(payload))
                    _log_applied(
                        component=self._component,
                        stage=f"{self._stage}_write",
                        trace_id=self._trace_id,
                        job_id=self._job_id,
                        winerror=winerror,
                        attempt=retry_budget.retries_used + 1,
                    )
                    return len(payload)
                if status != "unchanged":
                    _log_stopped(
                        component=self._component,
                        stage=f"{self._stage}_write",
                        trace_id=self._trace_id,
                        job_id=self._job_id,
                        winerror=winerror,
                        attempt=retry_budget.retries_used + 1,
                        reason="write_range_changed_ambiguously",
                    )
                    error.add_note(
                        "Filesystem retry stopped because the write range or file "
                        "identity changed; the candidate was preserved."
                    )
                    raise
                if not self._wait_for_retry(
                    error,
                    operation_stage=f"{self._stage}_write",
                    retry_budget=retry_budget,
                    unchanged=True,
                ):
                    raise
                self._open_for_write_retry(snapshot, payload, error, retry_budget)
                continue
            if type(written) is not int or not 0 < written <= len(payload):
                raise OSError("File write returned an invalid byte count")
            self._position = snapshot.position + written
            self._known_size = max(self._known_size, self._position)
            return written

    def flush(self) -> None:
        self._perform_state_only_operation(
            lambda stream: stream.flush(),
            operation_stage=f"{self._stage}_flush",
            mutating=True,
        )

    def sync(self) -> None:
        self.flush()
        self._perform_state_only_operation(
            lambda stream: os.fsync(stream.fileno()),
            operation_stage=f"{self._stage}_fsync",
            mutating=True,
        )

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        result = self._perform_state_only_operation(
            lambda stream: stream.seek(offset, whence),
            operation_stage=f"{self._stage}_seek",
        )
        if type(result) is not int or result < 0:
            raise OSError("File seek returned an invalid position")
        self._position = result
        return result

    def close(self) -> None:
        if self._closed:
            return
        stream = self._stream
        self._stream = None
        self._closed = True
        if stream is not None:
            self._close_stream(stream)

    def _close_stream(self, stream: BinaryIO) -> None:
        # Closing an unbuffered handle remains allowed after admission stops.
        try:
            stream.close()
        except OSError as error:
            if self._storage_gate is not None and is_disk_full_os_error(error):
                self._storage_gate.close("FILESYSTEM_DISK_FULL")
            raise

    def __enter__(self) -> RetryingFileStream:
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, _tb: object) -> bool:
        try:
            self.close()
        except BaseException as close_error:
            if exc is not None:
                exc.add_note(
                    "Append stream close failed during exception cleanup "
                    f"({type(close_error).__name__})."
                )
                return False
            raise
        return False

    def _perform_state_only_operation(
        self,
        operation: Callable[[BinaryIO], T],
        *,
        operation_stage: str,
        mutating: bool = False,
    ) -> T:
        retry_budget = _RetryBudget()
        while True:
            stream = self._ensure_open()
            try:
                before_state = self._state_from_stream(stream)
            except OSError as error:
                winerror = native_winerror(error)
                if winerror not in WIN32_TRANSIENT_LOCK_ERRORS:
                    raise
                expected_state = (self._identity, self._known_size)
                self._close_after_failed_operation(error)
                unchanged = self._path_matches_state(
                    expected_state, retry_budget=retry_budget
                )
                if not self._wait_for_retry(
                    error,
                    operation_stage=operation_stage,
                    retry_budget=retry_budget,
                    unchanged=unchanged,
                ):
                    raise
                if not self._path_matches_state(
                    expected_state, retry_budget=retry_budget
                ):
                    _log_stopped(
                        component=self._component,
                        stage=operation_stage,
                        trace_id=self._trace_id,
                        job_id=self._job_id,
                        winerror=winerror,
                        attempt=retry_budget.retries_used + 1,
                        reason="pre_state_changed_during_wait",
                    )
                    error.add_note(
                        "Filesystem retry stopped because the file changed during "
                        "the state-snapshot wait."
                    )
                    raise
                self._reopen_for_state_operation(expected_state, retry_budget)
                continue
            try:
                result = (
                    _run_storage_mutation(
                        self._storage_gate, lambda current=stream: operation(current)
                    )
                    if mutating
                    else operation(stream)
                )
                self._position = stream.tell()
                self._known_size = max(self._known_size, before_state[1])
                return result
            except OSError as error:
                winerror = native_winerror(error)
                if winerror not in WIN32_TRANSIENT_LOCK_ERRORS:
                    raise
                self._close_after_failed_operation(error)
                unchanged = self._path_matches_state(
                    before_state, retry_budget=retry_budget
                )
                if not self._wait_for_retry(
                    error,
                    operation_stage=operation_stage,
                    retry_budget=retry_budget,
                    unchanged=unchanged,
                ):
                    raise
                self._reopen_for_state_operation(before_state, retry_budget)

    def _wait_for_retry(
        self,
        error: OSError,
        *,
        operation_stage: str,
        retry_budget: _RetryBudget,
        unchanged: bool,
    ) -> bool:
        winerror = native_winerror(error)
        if winerror not in WIN32_TRANSIENT_LOCK_ERRORS:
            return False
        if not unchanged:
            _log_stopped(
                component=self._component,
                stage=operation_stage,
                trace_id=self._trace_id,
                job_id=self._job_id,
                winerror=winerror,
                attempt=retry_budget.retries_used + 1,
                reason="pre_state_not_proven_unchanged",
            )
            error.add_note(
                "Filesystem retry stopped because the file's pre-state could "
                "not be proven unchanged."
            )
            return False
        if retry_budget.retries_used >= len(ADR_0087_RETRY_DELAYS_SECONDS):
            _log_exhausted(
                component=self._component,
                stage=operation_stage,
                trace_id=self._trace_id,
                job_id=self._job_id,
                winerror=winerror,
                attempts=retry_budget.retries_used + 1,
            )
            return False
        delay = ADR_0087_RETRY_DELAYS_SECONDS[retry_budget.retries_used]
        _log_retry(
            component=self._component,
            stage=operation_stage,
            trace_id=self._trace_id,
            job_id=self._job_id,
            winerror=winerror,
            attempt=retry_budget.retries_used + 1,
            delay=delay,
        )
        try:
            (self._sleep or time.sleep)(delay)
        except Exception as sleep_error:
            error.add_note(
                "Filesystem retry stopped because its wait failed "
                f"({type(sleep_error).__name__})."
            )
            return False
        retry_budget.retries_used += 1
        return True

    def _ensure_open(self) -> BinaryIO:
        stream = self._stream
        if stream is not None:
            return stream
        expected_state = (self._identity, self._known_size)
        return self._reopen_for_state_operation(expected_state)

    def _reopen_for_state_operation(
        self,
        expected_state: tuple[tuple[int, int], int],
        retry_budget: _RetryBudget | None = None,
    ) -> BinaryIO:
        stream = retry_filesystem_operation(
            lambda: self._open_update_stream(expected_state),
            unchanged=lambda: self._path_matches_state(
                expected_state, retry_budget=retry_budget
            ),
            component=self._component,
            stage=f"{self._stage}_reopen",
            trace_id=self._trace_id,
            job_id=self._job_id,
            retry_budget=retry_budget,
            sleep=self._sleep,
        )
        self._stream = stream
        return stream

    def _open_update_stream(
        self,
        expected_state: tuple[tuple[int, int], int],
    ) -> BinaryIO:
        self._validate()
        stream = self._path.open("r+b", buffering=0)
        try:
            info = os.fstat(stream.fileno())
            if (self.identity_of(info), info.st_size) != expected_state:
                raise OSError("Filesystem candidate changed before a retry")
            stream.seek(self._position)
        except BaseException as validation_error:
            try:
                self._close_stream(stream)
            except BaseException as close_error:
                validation_error.add_note(
                    "Reopened file stream cleanup failed "
                    f"({type(close_error).__name__})."
                )
            raise
        self._stream = stream
        return stream

    def _open_for_write_retry(
        self,
        snapshot: _WriteSnapshot,
        payload: bytes,
        operation_error: OSError,
        retry_budget: _RetryBudget,
    ) -> None:
        expected_state = (snapshot.identity, snapshot.size)

        def open_checked() -> BinaryIO:
            stream = self._open_update_stream(expected_state)
            try:
                if not self._write_range_matches_stream(
                    stream, snapshot, unchanged=True
                ):
                    operation_error.add_note(
                        "Filesystem retry stopped because the write range changed "
                        "after its first unchanged-state proof."
                    )
                    raise operation_error
                return stream
            except BaseException as open_error:
                if self._stream is stream:
                    self._stream = None
                try:
                    self._close_stream(stream)
                except BaseException as close_error:
                    open_error.add_note(
                        "Reopened file stream cleanup failed "
                        f"({type(close_error).__name__})."
                    )
                raise

        self._stream = retry_filesystem_operation(
            open_checked,
            unchanged=lambda: self._classify_write_after_close(
                snapshot, payload, retry_budget=retry_budget
            )
            == "unchanged",
            component=self._component,
            stage=f"{self._stage}_reopen",
            trace_id=self._trace_id,
            job_id=self._job_id,
            retry_budget=retry_budget,
            sleep=self._sleep,
        )
        self._position = snapshot.position

    def _capture_write_snapshot(
        self,
        stream: BinaryIO,
        write_size: int,
    ) -> _WriteSnapshot:
        position = stream.tell()
        info = os.fstat(stream.fileno())
        identity = self.identity_of(info)
        if identity is None or identity != self._identity:
            raise OSError("Filesystem handle identity changed before write")
        size = info.st_size
        overlap_size = max(0, min(write_size, size - position))
        stream.seek(position)
        previous = stream.read(overlap_size) if overlap_size else b""
        if len(previous) != overlap_size:
            stream.seek(position)
            raise OSError("Could not snapshot the write range")
        stream.seek(position)
        return _WriteSnapshot(
            identity=identity,
            size=size,
            position=position,
            previous_bytes=previous,
        )

    def _classify_write_after_error(
        self,
        snapshot: _WriteSnapshot,
        payload: bytes,
        *,
        retry_budget: _RetryBudget | None = None,
    ) -> str:
        return self._classify_write_after_close(
            snapshot, payload, retry_budget=retry_budget
        )

    def _classify_write_after_close(
        self,
        snapshot: _WriteSnapshot,
        payload: bytes,
        *,
        retry_budget: _RetryBudget | None = None,
    ) -> str:
        state = _path_identity_size(
            self._path,
            retry_budget=retry_budget,
            component=self._component,
            trace_id=self._trace_id,
            job_id=self._job_id,
            validate_path=self._validate_path,
        )
        if state is None or state[0] != snapshot.identity:
            return "ambiguous"
        unchanged = state[1] == snapshot.size and _path_range_equals(
            self._path,
            snapshot.identity,
            snapshot.size,
            snapshot.position,
            snapshot.previous_bytes,
            retry_budget=retry_budget,
            component=self._component,
            stage=f"{self._stage}_classify_read",
            trace_id=self._trace_id,
            job_id=self._job_id,
            validate_path=self._validate_path,
        )
        expected_size = snapshot.result_size(len(payload))
        payload_matches = state[1] == expected_size and _path_range_equals(
            self._path,
            snapshot.identity,
            expected_size,
            snapshot.position,
            payload,
            retry_budget=retry_budget,
            component=self._component,
            stage=f"{self._stage}_classify_read",
            trace_id=self._trace_id,
            job_id=self._job_id,
            validate_path=self._validate_path,
        )
        if snapshot.position > snapshot.size and payload_matches:
            payload_matches = _path_range_equals(
                self._path,
                snapshot.identity,
                expected_size,
                snapshot.size,
                bytes(snapshot.position - snapshot.size),
                retry_budget=retry_budget,
                component=self._component,
                stage=f"{self._stage}_classify_read",
                trace_id=self._trace_id,
                job_id=self._job_id,
                validate_path=self._validate_path,
            )
        if payload_matches:
            return "applied"
        return "unchanged" if unchanged else "ambiguous"

    def _write_range_matches_stream(
        self,
        stream: BinaryIO,
        snapshot: _WriteSnapshot,
        *,
        unchanged: bool,
    ) -> bool:
        expected = snapshot.previous_bytes if unchanged else b""
        stream.seek(snapshot.position)
        actual = stream.read(len(expected)) if expected else b""
        stream.seek(snapshot.position)
        return actual == expected

    def _state_from_stream(self, stream: BinaryIO) -> tuple[tuple[int, int], int]:
        info = os.fstat(stream.fileno())
        identity = self.identity_of(info)
        if identity is None:
            raise OSError("Filesystem identity is unavailable")
        return identity, info.st_size

    def _path_matches_state(
        self,
        expected_state: tuple[tuple[int, int], int],
        *,
        retry_budget: _RetryBudget | None = None,
    ) -> bool:
        try:
            self._validate()
            return (
                _path_identity_size(
                    self._path,
                    retry_budget=retry_budget,
                    component=self._component,
                    trace_id=self._trace_id,
                    job_id=self._job_id,
                    validate_path=self._validate_path,
                )
                == expected_state
            )
        except Exception:
            return False

    def _close_after_failed_operation(self, operation_error: OSError) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            self._close_stream(stream)
        except BaseException as close_error:
            operation_error.add_note(
                "Filesystem retry stopped because closing the previous handle "
                f"failed ({type(close_error).__name__})."
            )
            raise operation_error from close_error

    def _validate(self) -> None:
        if self._validate_path is not None:
            self._validate_path()

    def _require_open(self) -> BinaryIO:
        if self._closed or self._stream is None:
            raise ValueError("I/O operation on closed append stream")
        return self._stream

    @staticmethod
    def identity_of(info: os.stat_result) -> tuple[int, int] | None:
        file_id = getattr(info, "st_ino", 0)
        if type(info.st_dev) is not int or type(file_id) is not int or file_id == 0:
            return None
        return info.st_dev, file_id


def _path_identity_size(
    path: Path,
    *,
    retry_budget: _RetryBudget | None = None,
    expected_parent_identity: tuple[int, int] | None = None,
    component: str = "filesystem_retry",
    stage: str = "path_identity_stat",
    trace_id: str | None = None,
    job_id: str | None = None,
    validate_path: PathValidator | None = None,
    sleep: SleepOperation | None = None,
) -> tuple[tuple[int, int], int] | None:
    stable_trace_id = trace_id or create_trace_id()
    try:
        info = retry_lstat(
            path,
            component=component,
            stage=stage,
            trace_id=stable_trace_id,
            job_id=job_id,
            validate_path=validate_path,
            sleep=sleep,
            retry_budget=retry_budget,
            expected_parent_identity=expected_parent_identity,
        )
    except FileNotFoundError:
        return None
    attributes = getattr(info, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(info.st_mode)
        or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
        or not stat.S_ISREG(info.st_mode)
    ):
        return None
    identity = RetryingFileStream.identity_of(info)
    if identity is None or type(info.st_size) is not int:
        return None
    return identity, info.st_size


def _path_is_absent(
    path: Path,
    *,
    component: str = "filesystem_retry",
    stage: str = "path_absence_stat",
    trace_id: str | None = None,
    job_id: str | None = None,
    validate_path: PathValidator | None = None,
    retry_budget: _RetryBudget | None = None,
    sleep: SleepOperation | None = None,
    expected_parent_identity: tuple[int, int] | None = None,
) -> bool:
    stable_trace_id = trace_id or create_trace_id()
    try:
        retry_lstat(
            path,
            component=component,
            stage=stage,
            trace_id=stable_trace_id,
            job_id=job_id,
            validate_path=validate_path,
            sleep=sleep,
            retry_budget=retry_budget,
            expected_parent_identity=expected_parent_identity,
        )
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _path_range_equals(
    path: Path,
    expected_identity: tuple[int, int],
    expected_size: int,
    offset: int,
    expected: bytes,
    *,
    retry_budget: _RetryBudget | None = None,
    component: str = "filesystem_retry",
    stage: str = "path_range_read",
    trace_id: str | None = None,
    job_id: str | None = None,
    validate_path: PathValidator | None = None,
    sleep: SleepOperation | None = None,
) -> bool:
    if offset < 0 or offset + len(expected) > expected_size:
        return False
    budget = retry_budget or _RetryBudget()
    stable_trace_id = trace_id or create_trace_id()
    try:
        parent_info = retry_lstat(
            path.parent,
            component=component,
            stage=f"{stage}_parent_stat",
            trace_id=stable_trace_id,
            job_id=job_id,
            validate_path=validate_path,
            sleep=sleep,
            retry_budget=budget,
        )
        parent_identity = RetryingFileStream.identity_of(parent_info)
        if parent_identity is None or _is_reparse_or_not_directory(parent_info):
            return False
        if _path_identity_size(
            path,
            retry_budget=budget,
            component=component,
            stage=f"{stage}_identity_stat",
            trace_id=stable_trace_id,
            job_id=job_id,
            validate_path=validate_path,
            sleep=sleep,
            expected_parent_identity=parent_identity,
        ) != (expected_identity, expected_size):
            return False
    except OSError:
        return False

    def snapshot_matches() -> bool:
        if validate_path is not None:
            validate_path()
        try:
            current_parent = retry_lstat(
                path.parent,
                component=component,
                stage=f"{stage}_parent_proof",
                trace_id=stable_trace_id,
                job_id=job_id,
                validate_path=validate_path,
                sleep=sleep,
                retry_budget=budget,
            )
            current = retry_lstat(
                path,
                component=component,
                stage=f"{stage}_file_proof",
                trace_id=stable_trace_id,
                job_id=job_id,
                validate_path=validate_path,
                sleep=sleep,
                retry_budget=budget,
                expected_parent_identity=parent_identity,
            )
        except OSError:
            return False
        return (
            not _is_reparse_or_not_directory(current_parent)
            and RetryingFileStream.identity_of(current_parent) == parent_identity
            and not _is_reparse_or_not_regular(current)
            and RetryingFileStream.identity_of(current) == expected_identity
            and current.st_size == expected_size
        )

    def read_range() -> bool:
        stream = path.open("rb", buffering=0)
        try:
            stream = stream.__enter__()
        except BaseException as enter_error:
            close_error = close_read_handle(
                stream,
                component=component,
                stage=f"{stage}_enter_close",
                trace_id=stable_trace_id,
                job_id=job_id,
            )
            try:
                definitely_closed = stream.closed is True
            except BaseException:
                definitely_closed = False
            if not definitely_closed:
                failure = close_error or OSError(
                    errno.EIO, "Read handle close is unverified"
                )
                raise failure from enter_error
            raise enter_error
        result = False
        operation_error: BaseException | None = None
        closed_info: os.stat_result | None = None
        try:
            opened = os.fstat(stream.fileno())
            if (
                _is_reparse_or_not_regular(opened)
                or RetryingFileStream.identity_of(opened) != expected_identity
                or opened.st_size != expected_size
            ):
                result = False
            else:
                stream.seek(offset)
                remaining = len(expected)
                actual = bytearray()
                while remaining:
                    chunk = stream.read(remaining)
                    if not chunk:
                        break
                    actual.extend(chunk)
                    remaining -= len(chunk)
                result = remaining == 0 and bytes(actual) == expected
                closed_info = os.fstat(stream.fileno())
        except BaseException as error:
            operation_error = error
        close_error = close_read_handle(
            stream,
            component=component,
            stage=f"{stage}_close",
            trace_id=stable_trace_id,
            job_id=job_id,
        )
        try:
            definitely_closed = stream.closed is True
        except BaseException:
            definitely_closed = False
        if not definitely_closed:
            raise close_error or OSError(errno.EIO, "Read handle close is unverified")
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
        if not result or closed_info is None:
            return False
        return (
            not _is_reparse_or_not_regular(closed_info)
            and RetryingFileStream.identity_of(closed_info) == expected_identity
            and closed_info.st_size == expected_size
            and snapshot_matches()
        )

    try:
        return retry_filesystem_operation(
            read_range,
            unchanged=snapshot_matches,
            component=component,
            stage=stage,
            trace_id=stable_trace_id,
            job_id=job_id,
            retry_budget=budget,
            sleep=sleep,
        )
    except OSError:
        return False


def _is_reparse_or_not_regular(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return (
        stat.S_ISLNK(info.st_mode)
        or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
        or not stat.S_ISREG(info.st_mode)
    )


def _log_retry(
    *,
    component: str,
    stage: str,
    trace_id: str,
    job_id: str | None,
    winerror: int,
    attempt: int,
    delay: float,
) -> None:
    logger.warning(
        "filesystem_lock_retry",
        extra={
            "component": component,
            "trace_id": trace_id or create_trace_id(),
            "job_id": job_id,
            "stage": stage,
            "native_error_code": winerror,
            "attempt": attempt,
            "retry_delay_seconds": delay,
        },
    )


def _log_exhausted(
    *,
    component: str,
    stage: str,
    trace_id: str,
    job_id: str | None,
    winerror: int,
    attempts: int,
) -> None:
    logger.error(
        "filesystem_lock_retries_exhausted",
        extra={
            "component": component,
            "trace_id": trace_id or create_trace_id(),
            "job_id": job_id,
            "stage": stage,
            "native_error_code": winerror,
            "attempt": attempts,
            "retryable": False,
        },
    )


def _log_stopped(
    *,
    component: str,
    stage: str,
    trace_id: str,
    job_id: str | None,
    winerror: int,
    attempt: int,
    reason: str,
) -> None:
    logger.error(
        "filesystem_lock_retry_stopped",
        extra={
            "component": component,
            "trace_id": trace_id or create_trace_id(),
            "job_id": job_id,
            "stage": stage,
            "native_error_code": winerror,
            "attempt": attempt,
            "retryable": False,
            "reason": reason,
        },
    )


def _log_applied(
    *,
    component: str,
    stage: str,
    trace_id: str,
    job_id: str | None,
    winerror: int,
    attempt: int,
) -> None:
    logger.warning(
        "filesystem_operation_applied_with_error",
        extra={
            "component": component,
            "trace_id": trace_id or create_trace_id(),
            "job_id": job_id,
            "stage": stage,
            "native_error_code": winerror,
            "attempt": attempt,
            "mutation_proven": True,
            "retryable": False,
        },
    )
