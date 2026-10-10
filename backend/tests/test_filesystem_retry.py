from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import app.persistence.filesystem_retry as retry_module
from app.persistence.filesystem_retry import (
    RetryingFileStream,
    retry_filesystem_operation,
    retry_lstat,
)


class _Win32LockError(OSError):
    def __init__(self, code: int) -> None:
        super().__init__(f"injected Win32 error {code}")
        self.winerror = code


class _WriteProxy:
    def __init__(
        self,
        stream: Any,
        write_action: Callable[[bytes], int] | None = None,
    ) -> None:
        self._stream = stream
        self._write_action = write_action

    def write(self, value: bytes) -> int:
        if self._write_action is not None:
            action, self._write_action = self._write_action, None
            return action(value)
        return self._stream.write(value)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _make_stream(path: Path, *, sleep: Callable[[float], None]) -> RetryingFileStream:
    return RetryingFileStream.create(
        path,
        component="test",
        stage="append",
        trace_id="trace-test",
        sleep=sleep,
    )


def test_mutation_retry_revalidates_prestate_after_wait_before_second_attempt() -> None:
    prestate_unchanged = True
    calls = 0
    sleeps: list[float] = []

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise _Win32LockError(32)

    def wait_and_change_prestate(delay: float) -> None:
        nonlocal prestate_unchanged
        sleeps.append(delay)
        prestate_unchanged = False

    with pytest.raises(_Win32LockError):
        retry_filesystem_operation(
            operation,
            unchanged=lambda: prestate_unchanged,
            component="test",
            stage="rename",
            trace_id="trace-test",
            sleep=wait_and_change_prestate,
        )

    assert calls == 1
    assert sleeps == [0.1]


def test_append_write_retries_lock_after_a_real_reopen_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "page.part"
    real_open = Path.open
    reopen_failures = 1
    writes: list[bytes] = []
    sleeps: list[float] = []

    def fail_initial_write(payload: bytes) -> int:
        writes.append(payload)
        raise _Win32LockError(32)

    def patched_open(
        self: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        nonlocal reopen_failures
        if self == path and mode == "r+b" and reopen_failures:
            reopen_failures -= 1
            raise _Win32LockError(33)
        stream = real_open(self, mode, *args, **kwargs)
        if self == path and mode == "x+b":
            return _WriteProxy(stream, fail_initial_write)
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    with _make_stream(path, sleep=sleeps.append) as stream:
        assert stream.write(b"page-bytes") == len(b"page-bytes")

    assert writes == [b"page-bytes"]
    assert path.read_bytes() == b"page-bytes"
    assert sleeps == [0.1, 0.2]


def test_append_write_shares_retry_budget_across_proof_and_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "shared-write-budget.part"
    real_open = Path.open
    real_lstat = Path.lstat
    proof_stat_locks = 0
    proof_armed = False
    reopen_locks = 1
    sleeps: list[float] = []

    def fail_initial_write(_payload: bytes) -> int:
        nonlocal proof_armed
        proof_armed = True
        raise _Win32LockError(32)

    def patched_open(
        candidate: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        nonlocal reopen_locks
        if candidate == path and mode == "r+b" and reopen_locks:
            reopen_locks -= 1
            raise _Win32LockError(33)
        stream = real_open(candidate, mode, *args, **kwargs)
        if candidate == path and mode == "x+b":
            return _WriteProxy(stream, fail_initial_write)
        return stream

    def lock_proof_stats(candidate: Path) -> os.stat_result:
        nonlocal proof_stat_locks
        if candidate == path and proof_armed and proof_stat_locks < 2:
            proof_stat_locks += 1
            raise _Win32LockError(32)
        return real_lstat(candidate)

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(Path, "lstat", lock_proof_stats)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    with _make_stream(path, sleep=sleeps.append) as stream:
        assert stream.write(b"single-bounded-write") == len(b"single-bounded-write")

    assert proof_stat_locks == 2
    assert reopen_locks == 0
    assert sleeps == [0.1, 0.2, 0.4, 0.8]
    assert sum(sleeps) <= 1.5
    assert path.read_bytes() == b"single-bounded-write"


def test_append_write_preserves_partial_mutation_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "partial.part"
    real_open = Path.open
    writes = 0
    sleeps: list[float] = []

    def partial_then_lock(stream: Any, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        stream.write(payload[:3])  # type: ignore[attr-defined]
        raise _Win32LockError(32)

    def patched_open(
        self: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        stream = real_open(self, mode, *args, **kwargs)
        if self == path and mode == "x+b":
            return _WriteProxy(stream, lambda payload: partial_then_lock(stream, payload))
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    with pytest.raises(_Win32LockError):
        with _make_stream(path, sleep=sleeps.append) as stream:
            stream.write(b"page-bytes")

    assert writes == 1
    assert path.read_bytes() == b"pag"
    assert sleeps == []


def test_append_write_does_not_retry_after_path_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "identity-changed.part"
    replacement = tmp_path / "replacement.part"
    real_open = Path.open
    writes = 0
    sleeps: list[float] = []

    def replace_path_then_lock(stream: Any, _payload: bytes) -> int:
        nonlocal writes
        writes += 1
        replacement.write_bytes(b"external-candidate")
        stream.close()  # type: ignore[attr-defined]
        path.unlink()
        replacement.replace(path)
        raise _Win32LockError(32)

    def patched_open(
        self: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        stream = real_open(self, mode, *args, **kwargs)
        if self == path and mode == "x+b":
            return _WriteProxy(
                stream,
                lambda payload: replace_path_then_lock(stream, payload),
            )
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    with pytest.raises(_Win32LockError):
        with _make_stream(path, sleep=sleeps.append) as stream:
            stream.write(b"page-bytes")

    assert writes == 1
    assert path.read_bytes() == b"external-candidate"
    assert sleeps == []


def test_append_write_treats_exact_payload_after_lock_error_as_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "applied.part"
    real_open = Path.open
    writes = 0
    sleeps: list[float] = []

    def write_then_lock(stream: Any, payload: bytes) -> int:
        nonlocal writes
        writes += 1
        stream.write(payload)  # type: ignore[attr-defined]
        raise _Win32LockError(36)

    def patched_open(
        self: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        stream = real_open(self, mode, *args, **kwargs)
        if self == path and mode == "x+b":
            return _WriteProxy(stream, lambda payload: write_then_lock(stream, payload))
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    with _make_stream(path, sleep=sleeps.append) as stream:
        assert stream.write(b"page-bytes") == len(b"page-bytes")

    assert writes == 1
    assert path.read_bytes() == b"page-bytes"
    assert sleeps == []


def test_reopen_failures_share_the_write_retry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "locked-reopen.part"
    real_open = Path.open
    open_failures = 0
    sleeps: list[float] = []

    def fail_initial_write(_payload: bytes) -> int:
        raise _Win32LockError(32)

    def patched_open(
        self: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        nonlocal open_failures
        if self == path and mode == "r+b":
            open_failures += 1
            raise _Win32LockError(32)
        stream = real_open(self, mode, *args, **kwargs)
        if self == path and mode == "x+b":
            return _WriteProxy(stream, fail_initial_write)
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    with pytest.raises(_Win32LockError):
        with _make_stream(path, sleep=sleeps.append) as stream:
            stream.write(b"page-bytes")

    assert open_failures == 4
    assert sleeps == [0.1, 0.2, 0.4, 0.8]
    assert path.read_bytes() == b""


def test_permanent_filesystem_error_is_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "denied.part"
    real_open = Path.open
    writes = 0
    sleeps: list[float] = []

    def denied(_payload: bytes) -> int:
        nonlocal writes
        writes += 1
        error = OSError("injected access denied")
        error.winerror = 5  # type: ignore[attr-defined]
        raise error

    def patched_open(
        self: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        stream = real_open(self, mode, *args, **kwargs)
        if self == path and mode == "x+b":
            return _WriteProxy(stream, denied)
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    with pytest.raises(OSError) as error:
        with _make_stream(path, sleep=sleeps.append) as stream:
            stream.write(b"page-bytes")

    assert getattr(error.value, "winerror", None) == 5
    assert writes == 1
    assert path.read_bytes() == b""
    assert sleeps == []


def test_initialization_failure_closes_newly_created_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "init-failure.part"
    real_open = Path.open
    opened_streams: list[Any] = []

    def patched_open(
        self: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        stream = real_open(self, mode, *args, **kwargs)
        if self == path and mode == "x+b":
            opened_streams.append(stream)
        return stream

    def fail_fstat(_descriptor: int) -> os.stat_result:
        raise OSError("injected identity inspection failure")

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(retry_module.os, "fstat", fail_fstat)
    with pytest.raises(OSError, match="identity inspection"):
        _make_stream(path, sleep=lambda _delay: None)

    assert len(opened_streams) == 1
    assert opened_streams[0].closed is True
    assert path.exists()


def test_exclusive_create_does_not_treat_existing_symlink_as_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "real.part"
    target.write_bytes(b"original")
    link = tmp_path / "linked.part"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {type(error).__name__}")
    real_open = Path.open
    opens = 0
    sleeps: list[float] = []

    def patched_open(
        self: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        nonlocal opens
        if self == link and mode == "x+b":
            opens += 1
            raise _Win32LockError(32)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", patched_open)
    with pytest.raises(_Win32LockError):
        _make_stream(link, sleep=sleeps.append)

    assert opens == 1
    assert link.is_symlink()
    assert target.read_bytes() == b"original"
    assert sleeps == []


def test_exclusive_create_retries_lock_only_when_path_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "create-lock.part"
    real_open = Path.open
    opens = 0
    sleeps: list[float] = []

    def patched_open(
        self: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        nonlocal opens
        if self == path and mode == "x+b":
            opens += 1
            if opens == 1:
                raise _Win32LockError(32)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", patched_open)
    with _make_stream(path, sleep=sleeps.append) as stream:
        stream.write(b"created")

    assert opens == 2
    assert path.read_bytes() == b"created"
    assert sleeps == [0.1]


def test_exclusive_create_stops_if_parent_identity_changes_during_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "pages"
    parent.mkdir()
    moved_parent = tmp_path / "pages-before-replacement"
    path = parent / "page.part"
    real_open = Path.open
    opens = 0
    sleeps: list[float] = []

    def patched_open(
        candidate: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        nonlocal opens
        if candidate == path and mode == "x+b":
            opens += 1
            if opens == 1:
                raise _Win32LockError(32)
        return real_open(candidate, mode, *args, **kwargs)

    def replace_parent_during_wait(delay: float) -> None:
        sleeps.append(delay)
        parent.rename(moved_parent)
        parent.mkdir()

    monkeypatch.setattr(Path, "open", patched_open)
    with pytest.raises(_Win32LockError):
        _make_stream(path, sleep=replace_parent_during_wait)

    assert opens == 1
    assert sleeps == [0.1]
    assert not path.exists()
    assert not (moved_parent / path.name).exists()


def test_path_range_read_does_not_retry_when_handle_close_is_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "unverified-close.part"
    content = b"read payload that must not be retried with an open handle"
    path.write_bytes(content)
    identity = RetryingFileStream.identity_of(path.lstat())
    assert identity is not None
    real_open = Path.open
    opened: list[Any] = []
    sleeps: list[float] = []

    class ReadProxy:
        def __init__(self, stream: Any) -> None:
            self._stream = stream
            self.closed = False
            opened.append(self)

        def __enter__(self) -> ReadProxy:
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def read(self, size: int = -1) -> bytes:
            self._stream.read(3)  # type: ignore[attr-defined]
            raise _Win32LockError(32)

        def close(self) -> None:
            # Model a failed close that does not prove the handle was released.
            raise _Win32LockError(32)

        def cleanup(self) -> None:
            self._stream.close()  # type: ignore[attr-defined]
            self.closed = True

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patched_open(
        candidate: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        stream = real_open(candidate, mode, *args, **kwargs)
        if candidate == path and mode == "rb":
            return ReadProxy(stream)
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    try:
        result = retry_module._path_range_equals(  # pyright: ignore[reportPrivateUsage]
            path,
            identity,
            len(content),
            0,
            content,
        )
    finally:
        for stream in opened:
            stream.cleanup()  # type: ignore[attr-defined]

    assert result is False
    assert len(opened) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("write_kind", "expected_bytes", "expected_sleeps"),
    [
        ("unchanged", b"abXYef", [0.1]),
        ("partial", b"abXdef", []),
        ("applied", b"abXYef", []),
    ],
)
def test_seekable_overwrite_classifies_lock_outcome_without_replaying_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_kind: str,
    expected_bytes: bytes,
    expected_sleeps: list[float],
) -> None:
    path = tmp_path / f"seek-{write_kind}.part"
    real_open = Path.open
    second_writes = 0
    sleeps: list[float] = []

    class SeekableWriteProxy:
        def __init__(self, stream: Any) -> None:
            self._stream = stream
            self._write_count = 0

        def write(self, payload: bytes) -> int:
            nonlocal second_writes
            self._write_count += 1
            if self._write_count == 1:
                return self._stream.write(payload)  # type: ignore[attr-defined]
            second_writes += 1
            if write_kind == "partial":
                self._stream.write(payload[:1])  # type: ignore[attr-defined]
            elif write_kind == "applied":
                self._stream.write(payload)  # type: ignore[attr-defined]
            raise _Win32LockError(32)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patched_open(
        self: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        stream = real_open(self, mode, *args, **kwargs)
        if self == path and mode == "x+b":
            return SeekableWriteProxy(stream)
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    if write_kind == "partial":
        with pytest.raises(_Win32LockError):
            with _make_stream(path, sleep=sleeps.append) as stream:
                stream.write(b"abcdef")
                stream.seek(2)
                stream.write(b"XY")
    else:
        with _make_stream(path, sleep=sleeps.append) as stream:
            stream.write(b"abcdef")
            stream.seek(2)
            assert stream.write(b"XY") == 2

    assert second_writes == 1
    assert path.read_bytes() == expected_bytes
    assert sleeps == expected_sleeps


def test_path_identity_stat_retries_transient_lock_with_stable_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "stat-probe.part"
    path.write_bytes(b"preserved candidate")
    expected = path.lstat()
    real_lstat = Path.lstat
    calls = 0
    sleeps: list[float] = []

    def lock_once(candidate: Path) -> os.stat_result:
        nonlocal calls
        if candidate == path:
            calls += 1
            if calls == 1:
                raise _Win32LockError(32)
        return real_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", lock_once)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    observed = retry_module._path_identity_size(path)  # pyright: ignore[reportPrivateUsage]

    assert observed == (
        RetryingFileStream.identity_of(expected),
        expected.st_size,
    )
    assert calls == 2
    assert sleeps == [0.1]
    assert path.read_bytes() == b"preserved candidate"


@pytest.mark.parametrize("probe_kind", ["fstat", "read"])
def test_append_snapshot_retries_transient_probe_after_closing_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_kind: str,
) -> None:
    path = tmp_path / f"snapshot-{probe_kind}.part"
    sleeps: list[float] = []
    closed_at_sleep: list[bool] = []

    class ReadLockProxy:
        def __init__(self, stream: Any) -> None:
            self._stream = stream
            self._lock_once = True

        @property
        def closed(self) -> bool:
            return self._stream.closed  # type: ignore[attr-defined]

        def read(self, size: int = -1) -> bytes:
            if self._lock_once:
                self._lock_once = False
                raise _Win32LockError(32)
            return self._stream.read(size)  # type: ignore[attr-defined]

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    with _make_stream(path, sleep=lambda delay: sleeps.append(delay)) as stream:
        stream.write(b"seed")
        stream.seek(0)
        original_handle = stream._stream  # pyright: ignore[reportPrivateUsage]
        assert original_handle is not None

        def record_closed_handle(delay: float) -> None:
            sleeps.append(delay)
            closed_at_sleep.append(original_handle.closed)

        stream._sleep = record_closed_handle  # pyright: ignore[reportPrivateUsage]
        if probe_kind == "fstat":
            real_fstat = os.fstat
            fstat_calls = 0
            handle_fd = original_handle.fileno()

            def lock_fstat_once(fd: int) -> os.stat_result:
                nonlocal fstat_calls
                if fd == handle_fd:
                    fstat_calls += 1
                    if fstat_calls == 1:
                        raise _Win32LockError(32)
                return real_fstat(fd)

            monkeypatch.setattr(retry_module.os, "fstat", lock_fstat_once)
        else:
            stream._stream = ReadLockProxy(original_handle)  # type: ignore[assignment]

        assert stream.write(b"replace") == len(b"replace")

    assert sleeps == [0.1]
    assert closed_at_sleep == [True]
    assert path.read_bytes() == b"replace"


def test_state_only_operation_retries_fstat_after_closing_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state-only-fstat.part"
    sleeps: list[float] = []
    closed_at_sleep: list[bool] = []

    with _make_stream(path, sleep=sleeps.append) as stream:
        stream.write(b"must stay intact")
        original_handle = stream._stream  # pyright: ignore[reportPrivateUsage]
        assert original_handle is not None
        real_fstat = os.fstat
        handle_fd = original_handle.fileno()
        fstat_calls = 0

        def lock_fstat_once(fd: int) -> os.stat_result:
            nonlocal fstat_calls
            if fd == handle_fd:
                fstat_calls += 1
                if fstat_calls == 1:
                    raise _Win32LockError(32)
            return real_fstat(fd)

        def record_closed_handle(delay: float) -> None:
            sleeps.append(delay)
            closed_at_sleep.append(original_handle.closed)

        stream._sleep = record_closed_handle  # pyright: ignore[reportPrivateUsage]
        monkeypatch.setattr(retry_module.os, "fstat", lock_fstat_once)

        stream.flush()

    assert sleeps == [0.1]
    assert closed_at_sleep == [True]
    assert path.read_bytes() == b"must stay intact"


@pytest.mark.parametrize("operation", ["flush", "fsync"])
def test_state_operation_retries_transient_flush_or_fsync_lock_safely(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / f"{operation}-retry.part"
    payload = b"durable bytes must remain exact"
    real_open = Path.open
    opened_handles: list[Any] = []
    operation_calls = 0
    sleeps: list[float] = []
    closed_during_sleep: list[bool] = []

    class FlushLockProxy:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        def flush(self) -> None:
            nonlocal operation_calls
            if operation == "flush":
                operation_calls += 1
                if operation_calls == 1:
                    raise _Win32LockError(32)
            self._stream.flush()  # type: ignore[attr-defined]

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patched_open(
        candidate: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        stream = real_open(candidate, mode, *args, **kwargs)
        if candidate == path and mode in {"x+b", "r+b"}:
            proxy = FlushLockProxy(stream)
            opened_handles.append(proxy)
            return proxy
        return stream

    real_fsync = os.fsync

    def lock_fsync_once(descriptor: int) -> None:
        nonlocal operation_calls
        if operation == "fsync":
            operation_calls += 1
            if operation_calls == 1:
                raise _Win32LockError(33)
        real_fsync(descriptor)

    def record_closed_handle(delay: float) -> None:
        sleeps.append(delay)
        closed_during_sleep.append(opened_handles[-1].closed)  # type: ignore[attr-defined]

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(retry_module.os, "fsync", lock_fsync_once)
    caplog.set_level(logging.WARNING, logger="yomira.filesystem_retry")

    identity_before: tuple[int, int] | None = None
    with RetryingFileStream.create(
        path,
        component="test_component",
        stage="state_operation",
        trace_id="trace-safe-state-op",
        job_id="job-safe-state-op",
        sleep=record_closed_handle,
    ) as stream:
        assert stream.write(payload) == len(payload)
        identity_before = RetryingFileStream.identity_of(path.lstat())
        original_handle = opened_handles[0]
        if operation == "flush":
            stream.flush()
        else:
            stream.sync()

    assert identity_before is not None
    identity_after = RetryingFileStream.identity_of(path.lstat())
    assert operation_calls == 2
    assert sleeps == [0.1]
    assert closed_during_sleep == [True]
    assert path.read_bytes() == payload
    assert identity_after == identity_before
    assert original_handle.closed  # type: ignore[attr-defined]
    records = [
        record
        for record in caplog.records
        if record.getMessage() == "filesystem_lock_retry"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.__dict__["trace_id"] == "trace-safe-state-op"
    assert record.__dict__["job_id"] == "job-safe-state-op"
    assert record.__dict__["stage"] == f"state_operation_{operation}"
    assert record.__dict__["native_error_code"] in {32, 33}
    assert record.__dict__["attempt"] == 1
    assert "path" not in record.__dict__
    assert "payload" not in record.__dict__


@pytest.mark.parametrize("operation", ["flush", "fsync"])
def test_state_operation_persistent_lock_exhausts_four_retries_without_mutation(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / f"{operation}-persistent.part"
    payload = b"preserve this exact prestate"
    real_open = Path.open
    opened_handles: list[Any] = []
    operation_calls = 0
    sleeps: list[float] = []
    closed_during_sleep: list[bool] = []

    class PersistentFlushLockProxy:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        def flush(self) -> None:
            nonlocal operation_calls
            if operation == "flush":
                operation_calls += 1
                raise _Win32LockError(36)
            self._stream.flush()  # type: ignore[attr-defined]

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patched_open(
        candidate: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        stream = real_open(candidate, mode, *args, **kwargs)
        if candidate == path and mode in {"x+b", "r+b"}:
            proxy = PersistentFlushLockProxy(stream)
            opened_handles.append(proxy)
            return proxy
        return stream

    real_fsync = os.fsync

    def lock_fsync_always(descriptor: int) -> None:
        nonlocal operation_calls
        if operation == "fsync":
            operation_calls += 1
            raise _Win32LockError(36)
        real_fsync(descriptor)

    def record_closed_handle(delay: float) -> None:
        sleeps.append(delay)
        closed_during_sleep.append(opened_handles[-1].closed)  # type: ignore[attr-defined]

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(retry_module.os, "fsync", lock_fsync_always)
    caplog.set_level(logging.ERROR, logger="yomira.filesystem_retry")

    stream = RetryingFileStream.create(
        path,
        component="test_component",
        stage="persistent_state_operation",
        trace_id="trace-safe-persistent",
        job_id="job-safe-persistent",
        sleep=record_closed_handle,
    )
    assert stream.write(payload) == len(payload)
    identity_before = RetryingFileStream.identity_of(path.lstat())
    with pytest.raises(_Win32LockError) as error:
        if operation == "flush":
            stream.flush()
        else:
            stream.sync()
    stream.close()

    identity_after = RetryingFileStream.identity_of(path.lstat())
    assert error.value.winerror == 36
    assert operation_calls == 5
    assert sleeps == [0.1, 0.2, 0.4, 0.8]
    assert closed_during_sleep == [True, True, True, True]
    assert path.read_bytes() == payload
    assert identity_after == identity_before
    exhausted = [
        record
        for record in caplog.records
        if record.getMessage() == "filesystem_lock_retries_exhausted"
    ]
    assert len(exhausted) == 1
    record = exhausted[0]
    assert record.__dict__["trace_id"] == "trace-safe-persistent"
    assert record.__dict__["job_id"] == "job-safe-persistent"
    assert record.__dict__["stage"] == f"persistent_state_operation_{operation}"
    assert record.__dict__["native_error_code"] == 36
    assert record.__dict__["attempt"] == 5
    assert record.__dict__["retryable"] is False
    assert "path" not in record.__dict__
    assert "payload" not in record.__dict__


def test_retry_lstat_retries_lock_while_capturing_parent_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "candidate.part"
    path.write_bytes(b"candidate")
    parent = path.parent
    real_lstat = Path.lstat
    parent_lstat_calls = 0
    sleeps: list[float] = []

    def lock_parent_once(candidate: Path) -> os.stat_result:
        nonlocal parent_lstat_calls
        if candidate == parent:
            parent_lstat_calls += 1
            if parent_lstat_calls == 1:
                raise _Win32LockError(32)
        return real_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", lock_parent_once)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    observed = retry_lstat(
        path,
        component="test",
        stage="candidate_stat",
        trace_id="trace-test",
    )

    assert observed.st_size == len(b"candidate")
    assert parent_lstat_calls >= 2
    assert sleeps == [0.1]


def test_retry_lstat_retries_parent_identity_probe_during_unchanged_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "candidate.part"
    path.write_bytes(b"candidate")
    parent = path.parent
    real_lstat = Path.lstat
    parent_lstat_calls = 0
    candidate_lstat_calls = 0
    sleeps: list[float] = []

    def lock_probes_once(candidate: Path) -> os.stat_result:
        nonlocal parent_lstat_calls, candidate_lstat_calls
        if candidate == parent:
            parent_lstat_calls += 1
            if parent_lstat_calls == 2:
                raise _Win32LockError(33)
        if candidate == path:
            candidate_lstat_calls += 1
            if candidate_lstat_calls == 1:
                raise _Win32LockError(32)
        return real_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", lock_probes_once)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    observed = retry_lstat(
        path,
        component="test",
        stage="candidate_stat",
        trace_id="trace-test",
    )

    assert observed.st_size == len(b"candidate")
    assert parent_lstat_calls >= 3
    assert candidate_lstat_calls >= 2
    assert sleeps == [0.1, 0.2]


def test_retry_lstat_uses_verified_expected_parent_without_rewalking_ancestors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path.joinpath(*(f"level-{index}" for index in range(7)))
    parent.mkdir(parents=True)
    path = parent / "candidate.part"
    path.write_bytes(b"candidate")
    parent_info = retry_lstat(
        parent,
        component="test",
        stage="trusted_parent_snapshot",
        trace_id="trace-test",
    )
    parent_identity = RetryingFileStream.identity_of(parent_info)
    assert parent_identity is not None
    assert len(path.parts) >= 10

    real_lstat = Path.lstat
    observed_paths: list[Path] = []

    def count_lstat(candidate: Path) -> os.stat_result:
        observed_paths.append(candidate)
        return real_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", count_lstat)

    observed = retry_lstat(
        path,
        component="test",
        stage="candidate_stat",
        trace_id="trace-test",
        expected_parent_identity=parent_identity,
    )

    assert observed.st_size == len(b"candidate")
    # Exact parent precondition, candidate query, and parent postcondition.
    # Ancestors were already proven when the caller captured parent_identity.
    assert observed_paths == [parent, path, parent]


def test_retry_lstat_with_verified_parent_checks_root_around_candidate_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "candidate.part"
    path.write_bytes(b"candidate")
    parent_identity = RetryingFileStream.identity_of(Path.lstat(tmp_path))
    assert parent_identity is not None

    real_lstat = Path.lstat
    candidate_lstat_calls = 0
    root_check_positions: list[int] = []

    def count_candidate_lstat(candidate: Path) -> os.stat_result:
        nonlocal candidate_lstat_calls
        if candidate == path:
            candidate_lstat_calls += 1
        return real_lstat(candidate)

    def count_root_check() -> None:
        root_check_positions.append(candidate_lstat_calls)

    monkeypatch.setattr(Path, "lstat", count_candidate_lstat)

    observed = retry_lstat(
        path,
        component="test",
        stage="candidate_stat",
        trace_id="trace-test",
        validate_path=count_root_check,
        expected_parent_identity=parent_identity,
    )

    assert observed.st_size == len(b"candidate")
    assert root_check_positions == [0, 1]


def test_retry_lstat_expected_parent_mismatch_fails_before_candidate_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "candidate.part"
    path.write_bytes(b"candidate")
    real_lstat = Path.lstat
    candidate_calls = 0

    def track_candidate(candidate: Path) -> os.stat_result:
        nonlocal candidate_calls
        if candidate == path:
            candidate_calls += 1
        return real_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", track_candidate)

    with pytest.raises(OSError, match="parent directory identity"):
        retry_lstat(
            path,
            component="test",
            stage="candidate_stat",
            trace_id="trace-test",
            expected_parent_identity=(123, 456),
        )

    assert candidate_calls == 0


def test_retry_lstat_expected_parent_swap_after_candidate_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "candidate.part"
    path.write_bytes(b"candidate")
    parent = path.parent
    real_lstat = Path.lstat
    parent_identity = RetryingFileStream.identity_of(real_lstat(parent))
    assert parent_identity is not None
    candidate_seen = False

    def swap_after_candidate(candidate: Path) -> os.stat_result:
        nonlocal candidate_seen
        info = real_lstat(candidate)
        if candidate == path:
            candidate_seen = True
        elif candidate == parent and candidate_seen:
            return cast(
                os.stat_result,
                SimpleNamespace(
                    st_dev=info.st_dev,
                    st_file_attributes=getattr(info, "st_file_attributes", 0) or 0,
                    st_ino=info.st_ino + 1,
                    st_mode=info.st_mode,
                ),
            )
        return info

    monkeypatch.setattr(Path, "lstat", swap_after_candidate)

    with pytest.raises(OSError, match="parent directory identity"):
        retry_lstat(
            path,
            component="test",
            stage="candidate_stat",
            trace_id="trace-test",
            expected_parent_identity=parent_identity,
        )


def test_retry_lstat_expected_reparse_parent_fails_closed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"directory symlink creation unavailable: {type(error).__name__}")
    path = alias / "candidate.part"
    (target / "candidate.part").write_bytes(b"candidate")
    link_identity = RetryingFileStream.identity_of(Path.lstat(alias))
    assert link_identity is not None

    with pytest.raises(OSError, match="parent directory identity"):
        retry_lstat(
            path,
            component="test",
            stage="candidate_stat",
            trace_id="trace-test",
            expected_parent_identity=link_identity,
        )


def test_retry_lstat_expected_reparse_parent_attribute_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "candidate.part"
    path.write_bytes(b"candidate")
    parent_identity = RetryingFileStream.identity_of(Path.lstat(tmp_path))
    assert parent_identity is not None
    real_reparse_check = retry_module._is_reparse_or_not_directory  # pyright: ignore[reportPrivateUsage]
    candidate_calls = 0

    def report_parent_as_reparse(info: os.stat_result) -> bool:
        if info.st_ino == parent_identity[1]:
            return True
        return real_reparse_check(info)

    real_lstat = Path.lstat

    def count_candidate(candidate: Path) -> os.stat_result:
        nonlocal candidate_calls
        if candidate == path:
            candidate_calls += 1
        return real_lstat(candidate)

    monkeypatch.setattr(retry_module, "_is_reparse_or_not_directory", report_parent_as_reparse)
    monkeypatch.setattr(Path, "lstat", count_candidate)

    with pytest.raises(OSError, match="parent directory identity"):
        retry_lstat(
            path,
            component="test",
            stage="candidate_stat",
            trace_id="trace-test",
            expected_parent_identity=parent_identity,
        )

    assert candidate_calls == 0


@pytest.mark.parametrize("include_validator", [False, True])
def test_retry_lstat_retries_volume_anchor_with_available_identity_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_validator: bool,
) -> None:
    anchor = Path(tmp_path.anchor)
    real_lstat = Path.lstat
    expected_identity = RetryingFileStream.identity_of(real_lstat(anchor))
    assert expected_identity is not None
    anchor_lstat_calls = 0
    validations = 0
    sleeps: list[float] = []
    retry_budget = retry_module._RetryBudget()  # pyright: ignore[reportPrivateUsage]

    def lock_anchor_once(candidate: Path) -> os.stat_result:
        nonlocal anchor_lstat_calls
        if candidate == anchor:
            anchor_lstat_calls += 1
            if anchor_lstat_calls == 1:
                raise _Win32LockError(32)
        return real_lstat(candidate)

    def validate_anchor_identity() -> None:
        nonlocal validations
        validations += 1
        observed = RetryingFileStream.identity_of(real_lstat(anchor))
        if observed != expected_identity:
            raise OSError("volume anchor identity changed")

    monkeypatch.setattr(Path, "lstat", lock_anchor_once)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    observed = retry_lstat(
        anchor,
        component="test",
        stage="volume_anchor_stat",
        trace_id="trace-test",
        validate_path=validate_anchor_identity if include_validator else None,
        sleep=sleeps.append,
        retry_budget=retry_budget,
    )

    assert RetryingFileStream.identity_of(observed) == expected_identity
    assert anchor_lstat_calls == 2
    assert validations >= 3 if include_validator else validations == 0
    assert retry_budget.retries_used == 1
    assert sleeps == [0.1]


def test_path_range_snapshot_retries_from_zero_after_closing_locked_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "range-probe.part"
    content = b"0123456789-snapshot-bytes-abcdefghijklmnopqrstuvwxyz"
    path.write_bytes(content)
    identity = RetryingFileStream.identity_of(path.lstat())
    assert identity is not None
    real_open = Path.open
    opens = 0
    starts: list[int] = []
    closed: list[bool] = []
    sleeps: list[float] = []
    offset = 10
    expected = content[offset : offset + 16]

    class ReadProxy:
        def __init__(self, stream: Any, *, fail: bool) -> None:
            self._stream = stream
            self._fail = fail
            self._read_started = False

        def __enter__(self) -> ReadProxy:
            closed.append(False)
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def read(self, size: int = -1) -> bytes:
            if not self._read_started:
                starts.append(self._stream.tell())  # type: ignore[attr-defined]
                self._read_started = True
            if self._fail:
                self._fail = False
                self._stream.read(3)  # type: ignore[attr-defined]
                raise _Win32LockError(33)
            return self._stream.read(size)  # type: ignore[attr-defined]

        def close(self) -> None:
            self._stream.close()  # type: ignore[attr-defined]
            closed[-1] = True

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patched_open(
        candidate: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        nonlocal opens
        stream = real_open(candidate, mode, *args, **kwargs)
        if candidate == path and mode == "rb":
            opens += 1
            return ReadProxy(stream, fail=opens == 1)
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    assert retry_module._path_range_equals(  # pyright: ignore[reportPrivateUsage]
        path,
        identity,
        len(content),
        offset,
        expected,
    )

    assert opens == 2
    assert starts == [offset, offset]
    assert closed == [True, True]
    assert sleeps == [0.1]


def test_path_range_retries_parent_lock_during_closed_handle_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "range-parent-proof.part"
    content = b"range proof must restart from its captured offset"
    path.write_bytes(content)
    identity = RetryingFileStream.identity_of(path.lstat())
    assert identity is not None
    parent = path.parent
    real_lstat = Path.lstat
    real_open = Path.open
    parent_lock_armed = False
    parent_lstat_locks = 0
    read_opens = 0
    starts: list[int] = []
    closed: list[bool] = []
    sleeps: list[float] = []
    offset = 7
    expected = content[offset : offset + 18]

    class ReadLockProxy:
        def __init__(self, stream: Any, *, fail: bool) -> None:
            self._stream = stream
            self._fail = fail

        def __enter__(self) -> ReadLockProxy:
            closed.append(False)
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def read(self, size: int = -1) -> bytes:
            nonlocal parent_lock_armed
            starts.append(self._stream.tell())  # type: ignore[attr-defined]
            if self._fail:
                self._fail = False
                self._stream.read(2)  # type: ignore[attr-defined]
                parent_lock_armed = True
                raise _Win32LockError(32)
            return self._stream.read(size)  # type: ignore[attr-defined]

        def close(self) -> None:
            self._stream.close()  # type: ignore[attr-defined]
            closed[-1] = True

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def lock_parent_during_proof(candidate: Path) -> os.stat_result:
        nonlocal parent_lock_armed, parent_lstat_locks
        if candidate == parent and parent_lock_armed:
            parent_lock_armed = False
            parent_lstat_locks += 1
            raise _Win32LockError(33)
        return real_lstat(candidate)

    def patched_open(
        candidate: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> object:
        nonlocal read_opens
        stream = real_open(candidate, mode, *args, **kwargs)
        if candidate == path and mode == "rb":
            read_opens += 1
            stream.seek(offset)
            return ReadLockProxy(stream, fail=read_opens == 1)
        return stream

    monkeypatch.setattr(Path, "lstat", lock_parent_during_proof)
    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    assert retry_module._path_range_equals(  # pyright: ignore[reportPrivateUsage]
        path,
        identity,
        len(content),
        offset,
        expected,
    )

    assert parent_lstat_locks == 1
    assert read_opens == 2
    assert starts == [offset, offset]
    assert closed == [True, True]
    assert sleeps == [0.1, 0.2]
