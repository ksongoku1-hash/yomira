from __future__ import annotations

import ctypes
import hashlib
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.persistence import atomic_file_publisher as publisher_module
from app.persistence.atomic_file_publisher import (
    ADR_0087_RETRY_POLICY,
    AtomicFilePublisher,
    AtomicFilePublishError,
    RetryPolicy,
)
from app.persistence.filesystem_retry import RetryingFileStream
from app.persistence.storage_gate import SharedStorageGate
from app.persistence.windows_paths import DataRootIdentity


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.value += delay


class _Move:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path, int]] = []

    def __call__(self, source: Path, destination: Path, flags: int) -> None:
        self.calls.append((source, destination, flags))
        os.link(source, destination)
        source.unlink()


class _RecordingPolicy:
    def __init__(self, delegate: RetryPolicy) -> None:
        self.delegate = delegate
        self.calls: list[tuple[int, float]] = []

    def delay_seconds(self, *, retry_index: int, elapsed_seconds: float) -> float | None:
        self.calls.append((retry_index, elapsed_seconds))
        return self.delegate.delay_seconds(
            retry_index=retry_index,
            elapsed_seconds=elapsed_seconds,
        )


def _publisher(
    root: Path,
    move: Callable[[Path, Path, int], None],
    clock: _Clock,
    *,
    policy: RetryPolicy = ADR_0087_RETRY_POLICY,
) -> AtomicFilePublisher:
    identity = DataRootIdentity.capture(root)
    return AtomicFilePublisher(
        data_root_identity=identity,
        retry_policy=policy,
        sleep=clock.sleep,
        clock=clock,
        move_file=move,
    )


def _winerror(code: int) -> OSError:
    return ctypes.WinError(code)


def _source(root: Path, name: str = "source.part") -> tuple[Path, bytes, str]:
    path = root / name
    content = b"verified source bytes\x00\xff" * 17
    path.write_bytes(content)
    return path, content, hashlib.sha256(content).hexdigest()


def test_publishes_verified_file_atomically_without_replacing_and_uses_zero_flags(
    tmp_path: Path,
) -> None:
    source, content, digest = _source(tmp_path)
    destination = tmp_path / "published.cbz"
    clock = _Clock()
    move = _Move()
    publisher = _publisher(tmp_path, move, clock)

    publisher.publish_no_replace(source, destination)

    assert move.calls == [(source, destination, 0)]
    assert not source.exists()
    assert destination.read_bytes() == content
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == digest
    assert clock.sleeps == []


def test_managed_path_inspection_reuses_verified_parent_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = tmp_path / "jobs"
    nested.mkdir()
    source = nested / "source.part"
    source.write_bytes(b"verified")
    destination = nested / "published.cbz"
    publisher = _publisher(tmp_path, _Move(), _Clock())
    real_retry_lstat = publisher_module.retry_lstat
    observed: list[tuple[Path, object, bool]] = []

    def record_retry_lstat(path: Path, **kwargs: Any) -> os.stat_result:
        observed.append(
            (
                path,
                kwargs.get("expected_parent_identity"),
                callable(kwargs.get("validate_path")),
            )
        )
        return real_retry_lstat(path, **kwargs)

    monkeypatch.setattr(publisher_module, "retry_lstat", record_retry_lstat)

    publisher.publish_no_replace(source, destination)

    root_identity = RetryingFileStream.identity_of(tmp_path.lstat())
    nested_identity = RetryingFileStream.identity_of(nested.lstat())
    assert root_identity is not None
    assert nested_identity is not None
    assert any(
        path == nested and parent == root_identity and validates
        for path, parent, validates in observed
    )
    assert any(
        path == source and parent == nested_identity and validates
        for path, parent, validates in observed
    )
    assert not source.exists()
    assert destination.read_bytes() == b"verified"


def test_source_stat_retries_transient_lock_without_changing_candidate_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, content, _digest = _source(tmp_path)
    destination = tmp_path / "published.cbz"
    clock = _Clock()
    move = _Move()
    publisher = _publisher(tmp_path, move, clock)
    real_lstat = Path.lstat
    source_stat_calls = 0

    def lock_once(candidate: Path) -> os.stat_result:
        nonlocal source_stat_calls
        if candidate == source:
            source_stat_calls += 1
            if source_stat_calls == 1:
                raise _winerror(32)
        return real_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", lock_once)

    publisher.publish_no_replace(source, destination)

    assert source_stat_calls >= 2
    assert clock.sleeps == [0.1]
    assert not source.exists()
    assert destination.read_bytes() == content


@pytest.mark.skipif(os.name != "nt", reason="production publisher uses MoveFileExW")
def test_default_publisher_uses_native_move_file_ex_w(tmp_path: Path) -> None:
    source, content, _digest = _source(tmp_path)
    destination = tmp_path / "published.cbz"
    clock = _Clock()
    publisher = AtomicFilePublisher(
        data_root_identity=DataRootIdentity.capture(tmp_path),
        retry_policy=ADR_0087_RETRY_POLICY,
        sleep=clock.sleep,
        clock=clock,
    )

    publisher.publish_no_replace(source, destination)

    assert not source.exists()
    assert destination.read_bytes() == content
    assert clock.sleeps == []


def test_existing_destination_is_preserved_and_native_move_is_not_called(
    tmp_path: Path,
) -> None:
    source, content, _digest = _source(tmp_path)
    destination = tmp_path / "published.cbz"
    existing = b"do not overwrite"
    destination.write_bytes(existing)
    clock = _Clock()
    move = _Move()
    publisher = _publisher(tmp_path, move, clock)

    with pytest.raises(AtomicFilePublishError) as error:
        publisher.publish_no_replace(source, destination)

    assert error.value.stage == "destination_conflict"
    assert source.read_bytes() == content
    assert destination.read_bytes() == existing
    assert move.calls == []
    assert clock.sleeps == []


def test_only_win32_lock_codes_retry_after_proving_source_and_destination_unchanged(
    tmp_path: Path,
) -> None:
    for winerror in (32, 33, 36):
        case_root = tmp_path / str(winerror)
        case_root.mkdir()
        source, content, _digest = _source(case_root)
        destination = case_root / "published.cbz"
        clock = _Clock()
        move = _Move()
        calls = 0

        def transient_then_publish(
            src: Path,
            dst: Path,
            flags: int,
            *,
            expected_content: bytes = content,
            native_lock_error: int = winerror,
            operation: _Move = move,
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                assert src.read_bytes() == expected_content
                assert not dst.exists()
                raise _winerror(native_lock_error)
            operation(src, dst, flags)

        publisher = _publisher(case_root, transient_then_publish, clock)
        publisher.publish_no_replace(source, destination)

        assert calls == 2
        assert clock.sleeps == [0.1]
        assert destination.read_bytes() == content
        assert not source.exists()
        assert move.calls == [(source, destination, 0)]


def test_hash_read_retries_from_zero_after_closing_transiently_locked_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, content, digest = _source(tmp_path)
    clock = _Clock()
    publisher = _publisher(tmp_path, _Move(), clock)
    real_open = Path.open
    handles: list[dict[str, bool]] = []
    starts: list[int] = []
    open_calls = 0

    class ReadProxy:
        def __init__(self, stream: object, *, fail: bool) -> None:
            self._stream = stream
            self._fail = fail
            self.closed = False
            self._read_started = False

        def __enter__(self) -> ReadProxy:
            if handles:
                assert handles[-1]["closed"]
            handles.append({"closed": False})
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def read(self, size: int = -1) -> bytes:
            if not self._read_started:
                starts.append(self._stream.tell())  # type: ignore[attr-defined]
                self._read_started = True
            if self._fail:
                self._fail = False
                self._stream.read(7)  # type: ignore[attr-defined]
                raise _winerror(32)
            return self._stream.read(size)  # type: ignore[attr-defined]

        def close(self) -> None:
            self._stream.close()  # type: ignore[attr-defined]
            self.closed = True
            handles[-1]["closed"] = True

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patched_open(
        path: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal open_calls
        stream = real_open(path, mode, *args, **kwargs)
        if path == source and mode == "rb":
            open_calls += 1
            return ReadProxy(stream, fail=open_calls == 1)
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    observed = publisher._hash_managed_file(  # pyright: ignore[reportPrivateUsage]
        source,
        role="source",
        trace_id="trace-hash-retry",
        attempt=1,
    )

    assert observed.sha256 == digest
    assert observed.size_bytes == len(content)
    assert open_calls == 2
    assert starts == [0, 0]
    assert all(handle["closed"] for handle in handles)
    assert clock.sleeps == [0.1]


def test_hash_read_does_not_retry_after_unverified_handle_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _content, _digest = _source(tmp_path)
    clock = _Clock()
    publisher = _publisher(tmp_path, _Move(), clock)
    real_open = Path.open
    handles: list[object] = []
    open_calls = 0

    class ReadProxy:
        def __init__(self, stream: object) -> None:
            self._stream = stream
            self.closed = False

        def __enter__(self) -> ReadProxy:
            handles.append(self)
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def read(self, size: int = -1) -> bytes:
            self._stream.read(7)  # type: ignore[attr-defined]
            raise _winerror(32)

        def close(self) -> None:
            raise _winerror(32)

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
    ) -> Any:
        nonlocal open_calls
        stream = real_open(candidate, mode, *args, **kwargs)
        if candidate == source and mode == "rb":
            open_calls += 1
            return ReadProxy(stream)
        return stream

    monkeypatch.setattr(Path, "open", patched_open)

    try:
        with pytest.raises(AtomicFilePublishError):
            publisher._hash_managed_file(  # pyright: ignore[reportPrivateUsage]
                source,
                role="source",
                trace_id="trace-unverified-close",
                attempt=1,
            )
    finally:
        for handle in handles:
            handle.cleanup()  # type: ignore[attr-defined]

    assert open_calls == 1
    assert len(handles) == 1
    assert clock.sleeps == []


def test_hash_retry_diagnostic_includes_supplied_job_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source, content, _digest = _source(tmp_path)
    destination = tmp_path / "hash-retry-published.cbz"
    clock = _Clock()
    publisher = _publisher(tmp_path, _Move(), clock)
    real_open = Path.open
    handles: list[dict[str, bool]] = []
    open_calls = 0
    job_id = str(UUID("8b25dcca-e824-4aeb-9e0d-06b3d3f94932"))

    class ReadProxy:
        def __init__(self, stream: object, *, fail: bool) -> None:
            self._stream = stream
            self._fail = fail
            self.closed = False

        def __enter__(self) -> ReadProxy:
            if handles:
                assert handles[-1]["closed"]
            handles.append({"closed": False})
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def read(self, size: int = -1) -> bytes:
            if self._fail:
                self._fail = False
                self._stream.read(7)  # type: ignore[attr-defined]
                raise _winerror(32)
            return self._stream.read(size)  # type: ignore[attr-defined]

        def close(self) -> None:
            self._stream.close()  # type: ignore[attr-defined]
            self.closed = True
            handles[-1]["closed"] = True

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patched_open(
        candidate: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal open_calls
        stream = real_open(candidate, mode, *args, **kwargs)
        if candidate == source and mode == "rb":
            open_calls += 1
            return ReadProxy(stream, fail=open_calls == 1)
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    caplog.set_level(logging.WARNING, logger="yomira.filesystem_retry")

    publisher.publish_no_replace(source, destination, job_id=job_id)

    retry_records = [
        record
        for record in caplog.records
        if record.name == "yomira.filesystem_retry"
        and record.msg == "filesystem_lock_retry"
        and getattr(record, "component", None) == "atomic_file_publisher"
        and getattr(record, "stage", None) == "source_hash_read"
    ]
    assert len(retry_records) == 1
    assert retry_records[0].__dict__["job_id"] == job_id
    assert len(handles) == 3
    assert all(handle["closed"] for handle in handles)
    assert clock.sleeps == [0.1]
    assert destination.read_bytes() == content


@pytest.mark.parametrize("winerror", (5, 112, 39, 123))
def test_non_lock_errors_are_not_retried(
    tmp_path: Path,
    winerror: int,
) -> None:
    source, content, _digest = _source(tmp_path)
    destination = tmp_path / "published.cbz"
    clock = _Clock()
    calls = 0

    def fail_without_mutation(src: Path, dst: Path, flags: int) -> None:
        nonlocal calls
        calls += 1
        raise _winerror(winerror)

    publisher = _publisher(tmp_path, fail_without_mutation, clock)
    with pytest.raises(AtomicFilePublishError) as error:
        publisher.publish_no_replace(source, destination)

    assert error.value.stage == "move_failed"
    assert error.value.winerror == winerror
    assert calls == 1
    assert source.read_bytes() == content
    assert not destination.exists()
    assert clock.sleeps == []


def test_disk_full_promotion_closes_shared_gate_and_preserves_source(
    tmp_path: Path,
) -> None:
    source, content, _digest = _source(tmp_path)
    destination = tmp_path / "published.cbz"
    clock = _Clock()
    gate = SharedStorageGate()

    def disk_full(_source: Path, _destination: Path, _flags: int) -> None:
        raise _winerror(112)

    publisher = AtomicFilePublisher(
        data_root_identity=DataRootIdentity.capture(tmp_path),
        retry_policy=ADR_0087_RETRY_POLICY,
        sleep=clock.sleep,
        clock=clock,
        move_file=disk_full,
        storage_gate=gate,
    )

    with pytest.raises(AtomicFilePublishError) as error:
        publisher.publish_no_replace(source, destination)

    assert gate.closed
    assert error.value.winerror == 112
    assert source.read_bytes() == content
    assert not destination.exists()
    assert clock.sleeps == []


def test_persistent_lock_uses_exactly_the_approved_four_retries(
    tmp_path: Path,
) -> None:
    source, content, _digest = _source(tmp_path)
    destination = tmp_path / "published.cbz"
    clock = _Clock()
    calls = 0

    def persistently_locked(_src: Path, _dst: Path, _flags: int) -> None:
        nonlocal calls
        calls += 1
        raise _winerror(32)

    publisher = _publisher(tmp_path, persistently_locked, clock)
    with pytest.raises(AtomicFilePublishError) as error:
        publisher.publish_no_replace(source, destination)

    assert error.value.stage == "move_failed"
    assert error.value.winerror == 32
    assert calls == 5
    assert clock.sleeps == [0.1, 0.2, 0.4, 0.8]
    assert sum(clock.sleeps) == pytest.approx(1.5)
    assert source.read_bytes() == content
    assert not destination.exists()


def test_lock_error_after_move_was_applied_is_verified_as_success(
    tmp_path: Path,
) -> None:
    source, content, _digest = _source(tmp_path)
    destination = tmp_path / "published.cbz"
    clock = _Clock()
    calls = 0

    def move_then_raise(src: Path, dst: Path, flags: int) -> None:
        nonlocal calls
        calls += 1
        assert flags == 0
        os.link(src, dst)
        src.unlink()
        raise _winerror(32)

    publisher = _publisher(tmp_path, move_then_raise, clock)
    publisher.publish_no_replace(source, destination)

    assert calls == 1
    assert not source.exists()
    assert destination.read_bytes() == content
    assert clock.sleeps == []


def test_lock_error_after_source_bytes_change_is_ambiguous_and_never_retried(
    tmp_path: Path,
) -> None:
    source, _content, _digest = _source(tmp_path)
    destination = tmp_path / "published.cbz"
    clock = _Clock()
    calls = 0

    def mutate_source_then_raise(src: Path, _dst: Path, _flags: int) -> None:
        nonlocal calls
        calls += 1
        src.write_bytes(b"changed after failed system operation")
        raise _winerror(33)

    publisher = _publisher(tmp_path, mutate_source_then_raise, clock)
    with pytest.raises(AtomicFilePublishError) as error:
        publisher.publish_no_replace(source, destination)

    assert error.value.stage == "mutation_ambiguous"
    assert calls == 1
    assert source.read_bytes() == b"changed after failed system operation"
    assert not destination.exists()
    assert clock.sleeps == []


def test_policy_exhaustion_and_clock_sleep_are_injected_explicitly(
    tmp_path: Path,
) -> None:
    source, content, _digest = _source(tmp_path)
    destination = tmp_path / "published.cbz"
    clock = _Clock()
    recording = _RecordingPolicy(ADR_0087_RETRY_POLICY)
    calls = 0

    def fail_twice_then_succeed(src: Path, dst: Path, flags: int) -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise _winerror(36)
        os.link(src, dst)
        src.unlink()

    publisher = _publisher(tmp_path, fail_twice_then_succeed, clock, policy=recording)
    publisher.publish_no_replace(source, destination)

    assert calls == 3
    assert [index for index, _elapsed in recording.calls] == [0, 1]
    assert clock.sleeps == [0.1, 0.2]
    assert [elapsed for _index, elapsed in recording.calls] == [0.0, 0.1]
    assert destination.read_bytes() == content


def test_source_outside_root_is_rejected_before_native_move(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "outside.part"
    outside.write_bytes(b"outside")
    destination = root / "published.cbz"
    clock = _Clock()
    move = _Move()
    publisher = _publisher(root, move, clock)

    with pytest.raises(AtomicFilePublishError) as error:
        publisher.publish_no_replace(outside, destination)

    assert error.value.stage == "path_outside_data_root"
    assert outside.read_bytes() == b"outside"
    assert move.calls == []


def test_symlink_source_is_rejected_without_following_it(tmp_path: Path) -> None:
    target, _content, _digest = _source(tmp_path, "target.bin")
    symlink = tmp_path / "source.part"
    try:
        symlink.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {type(error).__name__}")
    clock = _Clock()
    move = _Move()
    publisher = _publisher(tmp_path, move, clock)

    with pytest.raises(AtomicFilePublishError) as error:
        publisher.publish_no_replace(symlink, tmp_path / "published.cbz")

    assert error.value.stage == "source_not_regular"
    assert target.exists()
    assert symlink.is_symlink()
    assert move.calls == []


def test_error_log_has_trace_and_native_code_but_no_paths(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source, _content, _digest = _source(tmp_path)
    destination = tmp_path / "private-name.cbz"
    clock = _Clock()

    def denied(_src: Path, _dst: Path, _flags: int) -> None:
        raise _winerror(5)

    publisher = _publisher(tmp_path, denied, clock)
    with pytest.raises(AtomicFilePublishError) as error:
        publisher.publish_no_replace(source, destination)

    assert error.value.trace_id
    assert any(
        getattr(record, "trace_id", None) == error.value.trace_id
        for record in caplog.records
    )
    assert any(
        getattr(record, "native_error_code", None) == 5
        for record in caplog.records
    )
    assert str(tmp_path) not in caplog.text
    assert "private-name.cbz" not in caplog.text


def test_retry_diagnostic_includes_canonical_job_id_when_supplied(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source, content, _digest = _source(tmp_path)
    destination = tmp_path / "published-with-job-id.cbz"
    clock = _Clock()
    move = _Move()
    move_calls = 0
    job_id = str(UUID("8b25dcca-e824-4aeb-9e0d-06b3d3f94932"))

    def transient_then_move(src: Path, dst: Path, flags: int) -> None:
        nonlocal move_calls
        move_calls += 1
        if move_calls == 1:
            raise _winerror(32)
        move(src, dst, flags)

    publisher = _publisher(tmp_path, transient_then_move, clock)
    caplog.set_level(logging.WARNING, logger="yomira.atomic_file_publisher")

    publisher.publish_no_replace(source, destination, job_id=job_id)

    retry_records = [
        record
        for record in caplog.records
        if record.name == "yomira.atomic_file_publisher"
        and record.msg == "atomic_file_publish.retrying"
    ]
    assert len(retry_records) == 1
    assert retry_records[0].__dict__["job_id"] == job_id
    assert destination.read_bytes() == content
    assert str(tmp_path) not in repr(retry_records[0].__dict__)


@pytest.mark.parametrize(
    ("job_id", "expected_job_id"),
    [
        ("8b25dcca-e824-4aeb-9e0d-06b3d3f94932", "8b25dcca-e824-4aeb-9e0d-06b3d3f94932"),
        ("private-path-fragment", None),
    ],
)
def test_failure_diagnostic_propagates_only_sanitized_job_id(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    job_id: str,
    expected_job_id: str | None,
) -> None:
    source, _content, _digest = _source(tmp_path)
    destination = tmp_path / "failure-job-id.cbz"
    clock = _Clock()

    def denied(_src: Path, _dst: Path, _flags: int) -> None:
        raise _winerror(5)

    publisher = _publisher(tmp_path, denied, clock)
    caplog.set_level(logging.ERROR, logger="yomira.atomic_file_publisher")

    with pytest.raises(AtomicFilePublishError) as caught:
        publisher.publish_no_replace(source, destination, job_id=job_id)

    failure_records = [
        record
        for record in caplog.records
        if record.name == "yomira.atomic_file_publisher"
        and record.msg == "atomic_file_publish.failed"
    ]
    assert caught.value.job_id == expected_job_id
    assert len(failure_records) == 1
    if expected_job_id is None:
        assert not hasattr(failure_records[0], "job_id")
        assert "private-path-fragment" not in caplog.text
    else:
        assert failure_records[0].__dict__["job_id"] == expected_job_id

