from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from support.sqlite import open_test_database, reopen_test_database

import app.persistence.filesystem_retry as filesystem_retry_module
import app.services.download_dispatcher as dispatcher_module
from app import lifecycle
from app.connectors.mock import MockConnector
from app.domain.download_job import DownloadJob, DownloadJobStatus
from app.domain.download_page import DownloadPage
from app.factory import create_app
from app.persistence.database import SQLiteDatabase
from app.persistence.disk_budget import (
    CBZ_RESERVATION_BYTES,
    MINIMUM_RESERVED_BYTES,
    PAGE_TEMP_RESERVATION_BYTES,
    DiskBudgetManager,
)
from app.persistence.filesystem_retry import RetryingFileStream
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.repositories.download_job import DownloadJobRepository
from app.persistence.repositories.download_page import DownloadPageRepository
from app.persistence.storage_gate import SharedStorageGate, StorageGateClosedError
from app.runtime import RuntimeState
from app.services.cbz_builder import (
    MAX_CBZ_BYTES_V1,
    CBZBuilder,
    CBZBuildError,
    ComicInfoMetadata,
    ValidatedPage,
)
from app.services.download_dispatcher import DownloadDispatcher
from app.services.download_worker import DownloadWorker
from app.services.job_recovery import RecoveryCoordinator, WorkerQuiescenceAck
from app.services.jobs import DownloadJobService
from app.services.page_manifest_writer import PageManifestWriter
from app.services.resource_monitor import ResourceMonitorService
from app.settings import Settings
from app.single_instance import WindowsMutexGuard


def _cbz_inputs(
    tmp_path: Path, gate: SharedStorageGate
) -> tuple[CBZBuilder, ValidatedPage, list[tuple[Path, Path]]]:
    page_path = tmp_path / "page.png"
    with Image.new("RGB", (8, 6), "blue") as image:
        image.save(page_path, format="PNG")
    content = page_path.read_bytes()
    page = ValidatedPage(1, page_path, len(content), hashlib.sha256(content).hexdigest())
    publication_calls: list[tuple[Path, Path]] = []

    class PublicationProbe:
        def publish_no_replace(self, source: Path, destination: Path) -> None:
            publication_calls.append((source, destination))
            gate.ensure_open()
            raise AssertionError("a failed CBZ must never reach publication")

    builder = CBZBuilder(
        publisher=PublicationProbe(),
        max_archive_bytes=MAX_CBZ_BYTES_V1,
        storage_gate=gate,
    )
    return builder, page, publication_calls


def test_cbz_stops_new_writes_and_sync_after_another_worker_closes_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = SharedStorageGate()
    builder, page, publication_calls = _cbz_inputs(tmp_path, gate)
    destination = tmp_path / "chapter.cbz"
    temporary = tmp_path / "chapter.cbz.tmp"
    real_open = Path.open
    real_fsync = os.fsync
    post_close_operations: list[str] = []
    size_at_close: int | None = None

    class ClosingGateStream:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def write(self, value: bytes) -> int:
            nonlocal size_at_close
            if gate.closed:
                post_close_operations.append("write")
            written = int(self.stream.write(value))
            if not gate.closed:
                size_at_close = temporary.stat().st_size
                gate.close("SQLITE_FULL")
            return written

        def flush(self) -> None:
            if gate.closed:
                post_close_operations.append("flush")
            self.stream.flush()

        def __getattr__(self, name: str) -> Any:
            return getattr(self.stream, name)

    def tracked_fsync(file_descriptor: int) -> None:
        if gate.closed:
            post_close_operations.append("fsync")
        real_fsync(file_descriptor)

    def patch_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        stream = real_open(path, mode, *args, **kwargs)
        if path == temporary and mode in {"x+b", "r+b"}:
            return ClosingGateStream(stream)
        return stream

    monkeypatch.setattr(Path, "open", patch_open)
    monkeypatch.setattr(os, "fsync", tracked_fsync)
    with pytest.raises(StorageGateClosedError):
        builder.build(
            destination=destination,
            metadata=ComicInfoMetadata(title="K251"),
            pages=(page,),
            job_id=uuid4(),
        )

    assert post_close_operations == []
    assert gate.active_mutations == 0
    assert temporary.is_file()
    assert temporary.stat().st_size == size_at_close
    assert not destination.exists()
    assert publication_calls == []
    assert hashlib.sha256(page.path.read_bytes()).hexdigest() == page.sha256


def test_cbz_lock_retry_never_replays_a_write_after_gate_closes_during_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = SharedStorageGate()
    builder, page, publication_calls = _cbz_inputs(tmp_path, gate)
    destination = tmp_path / "chapter.cbz"
    temporary = tmp_path / "chapter.cbz.tmp"
    real_open = Path.open
    write_attempts = 0
    waits: list[float] = []

    class LockBeforeWrite(OSError):
        def __init__(self) -> None:
            super().__init__("injected lock before any bytes change")
            self.winerror = 32

    class LockOnceStream:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def write(self, value: bytes) -> int:
            nonlocal write_attempts
            write_attempts += 1
            if write_attempts == 1:
                raise LockBeforeWrite()
            return int(self.stream.write(value))

        def __getattr__(self, name: str) -> Any:
            return getattr(self.stream, name)

    def patch_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        stream = real_open(path, mode, *args, **kwargs)
        if path == temporary and mode in {"x+b", "r+b"}:
            return LockOnceStream(stream)
        return stream

    def close_gate_during_backoff(delay: float) -> None:
        waits.append(delay)
        gate.close("SQLITE_FULL")

    monkeypatch.setattr(Path, "open", patch_open)
    monkeypatch.setattr(filesystem_retry_module.time, "sleep", close_gate_during_backoff)
    with pytest.raises(StorageGateClosedError):
        builder.build(
            destination=destination,
            metadata=ComicInfoMetadata(title="K251"),
            pages=(page,),
            job_id=uuid4(),
        )

    assert write_attempts == 1
    assert waits == [0.1]
    assert gate.active_mutations == 0
    assert temporary.is_file()
    assert temporary.stat().st_size == 0
    assert publication_calls == []
    assert not destination.exists()


@pytest.mark.parametrize("prior_write_failure", (False, True))
def test_cbz_close_disk_full_closes_gate_even_with_an_original_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_write_failure: bool,
) -> None:
    gate = SharedStorageGate()
    builder, page, publication_calls = _cbz_inputs(tmp_path, gate)
    destination = tmp_path / "chapter.cbz"
    temporary = tmp_path / "chapter.cbz.tmp"
    real_close = RetryingFileStream.close
    close_calls = 0

    def disk_full_on_close(stream: RetryingFileStream) -> None:
        nonlocal close_calls
        if not stream.closed:
            close_calls += 1
            real_close(stream)
            raise OSError(errno.ENOSPC, "injected disk full while closing CBZ")
        real_close(stream)

    def denied_write(_stream: RetryingFileStream, _value: object) -> int:
        raise PermissionError(errno.EACCES, "injected original write failure")

    monkeypatch.setattr(RetryingFileStream, "close", disk_full_on_close)
    if prior_write_failure:
        monkeypatch.setattr(RetryingFileStream, "write", denied_write)

    with pytest.raises(CBZBuildError) as failure:
        builder.build(
            destination=destination,
            metadata=ComicInfoMetadata(title="K251"),
            pages=(page,),
            job_id=uuid4(),
        )

    assert gate.closed_reason == "FILESYSTEM_DISK_FULL"
    assert gate.active_mutations == 0
    assert close_calls == 1
    assert failure.value.cause_type == ("PermissionError" if prior_write_failure else "OSError")
    assert temporary.is_file()
    assert not destination.exists()
    assert publication_calls == []
    assert hashlib.sha256(page.path.read_bytes()).hexdigest() == page.sha256


@pytest.mark.parametrize("payload_prefix", (b"\x89PNG\r\n\x1a\n", b"<?xml"))
def test_cbz_member_disk_full_preserves_primary_error_when_cleanup_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload_prefix: bytes,
) -> None:
    gate = SharedStorageGate()
    builder, page, publication_calls = _cbz_inputs(tmp_path, gate)
    destination = tmp_path / "chapter.cbz"
    temporary = tmp_path / "chapter.cbz.tmp"
    real_open = Path.open
    real_fsync = os.fsync
    injected = False
    post_close_operations: list[str] = []

    class DiskFullPayloadStream:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def write(self, value: bytes) -> int:
            nonlocal injected
            if gate.closed:
                post_close_operations.append("write")
            if not injected and value.startswith(payload_prefix):
                injected = True
                self.stream.write(value[: len(payload_prefix)])
                raise OSError(errno.ENOSPC, "injected member payload disk full")
            return int(self.stream.write(value))

        def flush(self) -> None:
            if gate.closed:
                post_close_operations.append("flush")
            self.stream.flush()

        def __getattr__(self, name: str) -> Any:
            return getattr(self.stream, name)

    def patch_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        stream = real_open(path, mode, *args, **kwargs)
        if path == temporary and mode in {"x+b", "r+b"}:
            return DiskFullPayloadStream(stream)
        return stream

    def tracked_fsync(file_descriptor: int) -> None:
        if gate.closed:
            post_close_operations.append("fsync")
        real_fsync(file_descriptor)

    monkeypatch.setattr(Path, "open", patch_open)
    monkeypatch.setattr(os, "fsync", tracked_fsync)
    with pytest.raises(CBZBuildError) as failure:
        builder.build(
            destination=destination,
            metadata=ComicInfoMetadata(title="K251"),
            pages=(page,),
            job_id=uuid4(),
        )

    assert injected is True
    assert failure.value.cause_type == "OSError"
    assert gate.closed_reason == "FILESYSTEM_DISK_FULL"
    assert gate.active_mutations == 0
    assert post_close_operations == []
    assert temporary.is_file() and temporary.stat().st_size > 0
    assert not destination.exists()
    assert publication_calls == []
    assert hashlib.sha256(page.path.read_bytes()).hexdigest() == page.sha256


def test_existing_artifacts_reserve_only_remaining_growth_at_exact_floor(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    free_bytes = [0]
    capacity = 1 << 50
    budget = DiskBudgetManager(
        database,
        disk_usage_provider=lambda _path: SimpleNamespace(
            total=capacity,
            used=capacity - free_bytes[0],
            free=free_bytes[0],
        ),
    )
    try:
        baseline = budget.snapshot()
        assert baseline.recovery_demand_bytes is not None
        free_bytes[0] = baseline.recovery_demand_bytes + MINIMUM_RESERVED_BYTES + 12
        job_id = uuid4()
        budget.reserve(
            job_id,
            existing_page_bytes=PAGE_TEMP_RESERVATION_BYTES - 5,
            existing_cbz_bytes=CBZ_RESERVATION_BYTES - 7,
        )
        assert budget.snapshot().reserved_bytes == 12
        assert budget.snapshot().safe is True

        def write_pages() -> int:
            free_bytes[0] -= 5
            return 5

        def write_cbz() -> int:
            free_bytes[0] -= 7
            return 7

        assert (
            budget.write_reserved(job_id, category="pages", requested_bytes=5, write=write_pages)
            == 5
        )
        assert (
            budget.write_reserved(job_id, category="cbz", requested_bytes=7, write=write_cbz) == 7
        )
        assert budget.snapshot().reserved_bytes == 0
        assert budget.snapshot().safe is True
        assert budget.release(job_id, quiescent=True) is True
    finally:
        database.close()


@pytest.mark.parametrize("failure", ("stale_scan", "oversized_inventory", "unknown_volume"))
def test_unverifiable_inventory_or_volume_leaves_job_unclaimed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    async def exercise() -> None:
        database = open_test_database(tmp_path)
        MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
        connector = MockConnector()
        summary = DownloadJobService(database).create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=connector.normalize_metadata(
                await connector.get_series("mock-series-1")
            ),
            chapters=await connector.get_chapters("mock-series-1"),
            selected_remote_ids=("mock-chapter-1",),
        )[0]

        def disk_usage(_path: Path) -> SimpleNamespace:
            if failure == "unknown_volume":
                raise OSError("volume is unavailable")
            return SimpleNamespace(total=1 << 50, used=0, free=1 << 50)

        budget = DiskBudgetManager(database, disk_usage_provider=disk_usage)

        class Runner:
            calls = 0

            def existing_artifact_bytes_for_budget(self, _job_id: UUID) -> tuple[int, int]:
                if failure == "stale_scan":
                    raise OSError("artifact namespace changed during scan")
                if failure == "oversized_inventory":
                    return PAGE_TEMP_RESERVATION_BYTES + 1, 0
                return 0, 0

            async def run_job(self, job_id: UUID) -> object:
                del job_id
                self.calls += 1
                raise AssertionError("an unverifiable inventory must never reach a worker")

        runner = Runner()
        dispatcher = DownloadDispatcher(
            database=database,
            worker=runner,
            recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
            disk_budget_manager=budget,
        )
        try:
            await dispatcher._dispatch_available()  # pyright: ignore[reportPrivateUsage]
            await asyncio.sleep(0)
            assert runner.calls == 0
            assert dispatcher.active_job_ids == ()
            assert budget.snapshot().reserved_bytes == 0
            with database.read_session() as session:
                current = DownloadJobRepository().get(session, summary.job.job_id)
            assert current is not None
            assert current == summary.job
            assert current.status is DownloadJobStatus.QUEUED
            if failure == "unknown_volume":
                assert database.storage_gate.closed_reason == "DISK_BUDGET_UNSAFE"
            denials = [
                record
                for record in caplog.records
                if record.message
                in {
                    "job.dispatcher.disk_inventory_failed",
                    "job.dispatcher.disk_reservation_denied",
                }
            ]
            assert len(denials) == 1
            assert getattr(denials[0], "trace_id", None)
            assert getattr(denials[0], "job_id", None) == str(summary.job.job_id)
        finally:
            database.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("integrity_failure", ("foreign_key", "quick_check"))
def test_integrity_failure_preserves_data_keeps_gate_closed_and_has_a_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    integrity_failure: str,
) -> None:
    database = open_test_database(tmp_path)
    with sqlite3.connect(database.path) as connection:
        connection.execute("CREATE TABLE budget_parent(id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE budget_child(id INTEGER PRIMARY KEY, "
            "parent_id INTEGER REFERENCES budget_parent(id))"
        )
        if integrity_failure == "quick_check":
            connection.execute("INSERT INTO budget_parent VALUES(999)")
        connection.execute("INSERT INTO budget_child VALUES(1, 999)")

    real_exec = Connection.exec_driver_sql

    def invalid_quick_check(
        connection: Connection, statement: str, *args: Any, **kwargs: Any
    ) -> Any:
        if statement == "PRAGMA quick_check":
            return SimpleNamespace(all=lambda: [("injected SQLite page inconsistency",)])
        return real_exec(connection, statement, *args, **kwargs)

    if integrity_failure == "quick_check":
        monkeypatch.setattr(Connection, "exec_driver_sql", invalid_quick_check)
    caplog.set_level(logging.INFO, logger="yomira.database")

    class Runner:
        def existing_artifact_bytes_for_budget(self, _job_id: UUID) -> tuple[int, int]:
            return 0, 0

        async def run_job(self, job_id: UUID) -> object:
            del job_id
            raise AssertionError("recovery checks cannot start a worker")

    budget = DiskBudgetManager(
        database,
        disk_usage_provider=lambda _path: SimpleNamespace(total=1 << 50, used=0, free=1 << 50),
    )
    dispatcher = DownloadDispatcher(
        database=database,
        worker=Runner(),
        recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
        disk_budget_manager=budget,
    )
    database.storage_gate.close("SQLITE_FULL")
    try:
        assert budget.snapshot().safe is True
        recovered = asyncio.run(
            dispatcher._try_recover_storage_gate()  # pyright: ignore[reportPrivateUsage]
        )
        assert recovered is False
        assert database.storage_gate.closed_reason == "SQLITE_FULL"
        with database.exclusive_connection() as connection:
            assert real_exec(connection, "PRAGMA quick_check").scalar_one() == "ok"
            assert len(connection.exec_driver_sql("PRAGMA foreign_key_check").all()) == (
                1 if integrity_failure == "foreign_key" else 0
            )
            assert (
                connection.exec_driver_sql("SELECT parent_id FROM budget_child").scalar_one() == 999
            )
            assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 0
        assert dispatcher.active_job_ids == ()
        rejections = [
            record for record in caplog.records if getattr(record, "admission_safe", None) is False
        ]
        assert len(rejections) == 1
        assert rejections[0].levelno >= logging.ERROR
        assert getattr(rejections[0], "error_code", None)
        assert getattr(rejections[0], "stage", None) == "recovery_integrity_check"
        assert getattr(rejections[0], "trace_id", None)
    finally:
        database.close()


def test_budget_becoming_unknown_after_integrity_checks_prevents_reopening(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    measurement_calls = 0

    def disk_usage(_path: Path) -> SimpleNamespace:
        nonlocal measurement_calls
        measurement_calls += 1
        if measurement_calls > 1:
            raise OSError("volume vanished after SQLite checks")
        return SimpleNamespace(total=1 << 50, used=0, free=1 << 50)

    class Runner:
        def existing_artifact_bytes_for_budget(self, _job_id: UUID) -> tuple[int, int]:
            return 0, 0

        async def run_job(self, job_id: UUID) -> object:
            del job_id
            raise AssertionError("recovery checks cannot start a worker")

    dispatcher = DownloadDispatcher(
        database=database,
        worker=Runner(),
        recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
        disk_budget_manager=DiskBudgetManager(database, disk_usage_provider=disk_usage),
    )
    database.storage_gate.close("FILESYSTEM_DISK_FULL")
    try:
        recovered = asyncio.run(
            dispatcher._try_recover_storage_gate()  # pyright: ignore[reportPrivateUsage]
        )
        assert recovered is False
        assert measurement_calls == 2
        assert database.storage_gate.closed_reason == "FILESYSTEM_DISK_FULL"
        assert dispatcher.active_job_ids == ()
    finally:
        database.close()


async def _queued_job(database: SQLiteDatabase, data_root: Path) -> DownloadJob:
    MigrationRunner(database, data_root / "backups").upgrade_to_head()
    connector = MockConnector()
    return (
        DownloadJobService(database)
        .create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=connector.normalize_metadata(
                await connector.get_series("mock-series-1")
            ),
            chapters=await connector.get_chapters("mock-series-1"),
            selected_remote_ids=("mock-chapter-1",),
        )[0]
        .job
    )


def _sqlite_full_error() -> OperationalError:
    with sqlite3.connect(":memory:") as probe:
        probe.execute("PRAGMA max_page_count=2")
        probe.execute("CREATE TABLE disk_full_probe(payload BLOB)")
        try:
            probe.execute("INSERT INTO disk_full_probe VALUES(zeroblob(1048576))")
        except sqlite3.OperationalError as native_error:
            assert getattr(native_error, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL
            return OperationalError("injected recovery CAS", (), native_error)
    raise AssertionError("the isolated SQLite probe must produce SQLITE_FULL")


@pytest.mark.parametrize("failed_cas", (None, "interrupt", "resume"))
def test_verified_early_job_requeues_after_disk_recovery_and_retains_failed_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_cas: str | None,
) -> None:
    async def exercise() -> None:
        database = open_test_database(tmp_path)
        queued = await _queued_job(database, tmp_path)
        repository = DownloadJobRepository()
        with database.write_session() as session:
            active = repository.transition_status(
                session,
                current_job=queued,
                target_status=DownloadJobStatus.FETCHING_METADATA,
                changed_at=queued.updated_at,
            )
        assert active is not None
        recovery = RecoveryCoordinator(database, database.data_root_identity)
        pending_page = DownloadPage.new_local(queued.job_id, 1, "mock-page-1")
        publication = PageManifestWriter(
            database, manifest_store=recovery.manifest_store
        ).replace_pending_pages(queued.job_id, (pending_page,))
        assert publication.publication_pending is False
        manifest_path = tmp_path / "database" / "manifests" / f"{queued.job_id}.json"
        manifest_bytes = manifest_path.read_bytes()
        failure_count = 0
        real_interrupt = recovery.repository.interrupt_active_job
        real_resume = recovery.repository.resume_interrupted_job_for_safe_retry

        def interrupt_with_one_failure(
            session: Session,
            *,
            current_job: DownloadJob,
            interrupted_at: datetime,
            recovery_blocked: bool | None = None,
        ) -> DownloadJob | None:
            nonlocal failure_count
            if failed_cas == "interrupt" and failure_count == 0:
                failure_count += 1
                raise _sqlite_full_error()
            return real_interrupt(
                session,
                current_job=current_job,
                interrupted_at=interrupted_at,
                recovery_blocked=recovery_blocked,
            )

        def resume_with_one_failure(
            session: Session, *, current_job: DownloadJob, resumed_at: datetime
        ) -> DownloadJob | None:
            nonlocal failure_count
            if failed_cas == "resume" and failure_count == 0:
                failure_count += 1
                raise _sqlite_full_error()
            return real_resume(session, current_job=current_job, resumed_at=resumed_at)

        monkeypatch.setattr(recovery.repository, "interrupt_active_job", interrupt_with_one_failure)
        monkeypatch.setattr(
            recovery.repository, "resume_interrupted_job_for_safe_retry", resume_with_one_failure
        )

        class Runner:
            def existing_artifact_bytes_for_budget(self, _job_id: UUID) -> tuple[int, int]:
                return 0, 0

            async def run_job(self, job_id: UUID) -> object:
                del job_id
                raise AssertionError("recovery must requeue before admitting another worker")

        budget = DiskBudgetManager(
            database,
            disk_usage_provider=lambda _path: SimpleNamespace(total=1 << 50, used=0, free=1 << 50),
        )
        dispatcher = DownloadDispatcher(
            database=database,
            worker=Runner(),
            recovery_coordinator=recovery,
            disk_budget_manager=budget,
        )
        pending_ids = dispatcher._storage_gate_quiesced_job_ids  # pyright: ignore[reportPrivateUsage]
        pending_ids.add(queued.job_id)
        database.storage_gate.close("SQLITE_FULL")
        try:
            if failed_cas is not None:
                assert await dispatcher._try_recover_storage_gate() is False  # pyright: ignore[reportPrivateUsage]
                assert database.storage_gate.closed is True
                assert pending_ids == {queued.job_id}
                assert failure_count == 1
                with database.read_session() as session:
                    preserved = repository.get(session, queued.job_id)
                assert preserved is not None
                assert preserved.status is (
                    DownloadJobStatus.FETCHING_METADATA
                    if failed_cas == "interrupt"
                    else DownloadJobStatus.INTERRUPTED
                )
                assert preserved.cancel_requested_at is None
                assert manifest_path.read_bytes() == manifest_bytes

            assert await dispatcher._try_recover_storage_gate() is True  # pyright: ignore[reportPrivateUsage]
            assert database.storage_gate.closed is False
            assert pending_ids == set()
            with database.read_session() as session:
                resumed = repository.get(session, queued.job_id)
            assert resumed is not None
            assert resumed.status is DownloadJobStatus.QUEUED
            assert resumed.recovery_blocked is False
            assert resumed.interrupted_from_status is DownloadJobStatus.FETCHING_METADATA
            assert resumed.cancel_requested_at is None
            assert manifest_path.read_bytes() == manifest_bytes
            assert dispatcher.active_job_ids == ()
        finally:
            database.close()

    asyncio.run(exercise())


def test_unsafe_disk_monitor_checks_at_30_second_intervals_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = open_test_database(tmp_path)
    clock = [0.0]
    measurement_times: list[float] = []
    wake_times = iter((29.0, 30.0, 59.0, 60.0))

    def disk_usage(_path: Path) -> SimpleNamespace:
        measurement_times.append(clock[0])
        return SimpleNamespace(total=1 << 50, used=(1 << 50) - 1, free=1)

    class Runner:
        def existing_artifact_bytes_for_budget(self, _job_id: UUID) -> tuple[int, int]:
            raise AssertionError("unsafe monitor must not inspect or claim queued jobs")

        async def run_job(self, job_id: UUID) -> object:
            del job_id
            raise AssertionError("unsafe monitor must not start workers")

    dispatcher = DownloadDispatcher(
        database=database,
        worker=Runner(),
        recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
        disk_budget_manager=DiskBudgetManager(database, disk_usage_provider=disk_usage),
    )

    async def advance_clock() -> None:
        try:
            clock[0] = next(wake_times)
        except StopIteration:
            dispatcher.request_stop()

    def unexpected_integrity_check() -> bool:
        raise AssertionError("insufficient space must stop before SQLite recovery")

    def unexpected_write() -> None:
        raise AssertionError("an unsafe monitor must not open a mutation session")

    monkeypatch.setattr(dispatcher_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(dispatcher, "_wait_for_wakeup", advance_clock)
    monkeypatch.setattr(database, "verify_recovery_integrity", unexpected_integrity_check)
    monkeypatch.setattr(database, "write_session", unexpected_write)
    monkeypatch.setattr(database, "quiesced_recovery_write_session", unexpected_write)
    try:
        asyncio.run(dispatcher._run())  # pyright: ignore[reportPrivateUsage]
        assert measurement_times == [0.0, 0.0, 30.0, 60.0]
        assert database.storage_gate.closed_reason == "DISK_BUDGET_UNSAFE"
        assert database.storage_gate.active_mutations == 0
        assert dispatcher.active_job_ids == ()
    finally:
        database.close()


@pytest.mark.parametrize("invalid_ack", ("wrong_type", "different_job", "open_gate"))
def test_quiesced_recovery_ack_rejects_invalid_authority_before_storage_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_ack: str,
) -> None:
    database = open_test_database(tmp_path)
    recovery = RecoveryCoordinator(database, database.data_root_identity)
    job_id = uuid4()
    ack: object = (
        object()
        if invalid_ack == "wrong_type"
        else WorkerQuiescenceAck(uuid4() if invalid_ack == "different_job" else job_id)
    )
    if invalid_ack != "open_gate":
        database.storage_gate.close("SQLITE_FULL")

    def unexpected_storage_access() -> None:
        raise AssertionError("an invalid acknowledgement cannot read or mutate storage")

    monkeypatch.setattr(database, "read_session", unexpected_storage_access)
    monkeypatch.setattr(database, "write_session", unexpected_storage_access)
    try:
        with pytest.raises(TypeError if invalid_ack == "wrong_type" else ValueError):
            recovery.recover_job(job_id, quiescence_ack=cast(WorkerQuiescenceAck, ack))
        assert database.storage_gate.active_mutations == 0
        assert not (tmp_path / "database" / "manifests" / f"{job_id}.json").exists()
    finally:
        database.close()


@pytest.mark.parametrize("unsafe_restart", ("late_stage", "tampered_manifest", "cancellation"))
def test_storage_recovery_preserves_unsafe_jobs_without_requeueing(
    tmp_path: Path,
    unsafe_restart: str,
) -> None:
    async def exercise() -> None:
        database = open_test_database(tmp_path)
        queued = await _queued_job(database, tmp_path)
        repository = DownloadJobRepository()
        with database.write_session() as session:
            current = repository.transition_status(
                session,
                current_job=queued,
                target_status=DownloadJobStatus.FETCHING_METADATA,
                changed_at=queued.updated_at,
            )
        assert current is not None
        recovery = RecoveryCoordinator(database, database.data_root_identity)
        PageManifestWriter(database, manifest_store=recovery.manifest_store).replace_pending_pages(
            queued.job_id, (DownloadPage.new_local(queued.job_id, 1, "mock-page-1"),)
        )
        manifest_path = tmp_path / "database" / "manifests" / f"{queued.job_id}.json"
        if unsafe_restart == "late_stage":
            for target in (
                DownloadJobStatus.DOWNLOADING,
                DownloadJobStatus.VALIDATING,
                DownloadJobStatus.PROCESSING,
                DownloadJobStatus.PACKAGING,
            ):
                with database.write_session() as session:
                    current = repository.transition_status(
                        session,
                        current_job=current,
                        target_status=target,
                        changed_at=current.updated_at,
                    )
                assert current is not None
        elif unsafe_restart == "tampered_manifest":
            manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
        else:
            with database.write_session() as session:
                current = repository.transition_status(
                    session,
                    current_job=current,
                    target_status=DownloadJobStatus.DOWNLOADING,
                    changed_at=current.updated_at,
                )
            assert current is not None
            with database.write_session() as session:
                current = repository.request_cancellation(
                    session, current_job=current, requested_at=current.updated_at
                )
            assert current is not None
        manifest_bytes = manifest_path.read_bytes()

        class Runner:
            def existing_artifact_bytes_for_budget(self, _job_id: UUID) -> tuple[int, int]:
                return 0, 0

            async def run_job(self, job_id: UUID) -> object:
                del job_id
                raise AssertionError("unsafe evidence must not be dispatched")

        dispatcher = DownloadDispatcher(
            database=database,
            worker=Runner(),
            recovery_coordinator=recovery,
            disk_budget_manager=DiskBudgetManager(
                database,
                disk_usage_provider=lambda _path: SimpleNamespace(
                    total=1 << 50, used=0, free=1 << 50
                ),
            ),
        )
        dispatcher._storage_gate_quiesced_job_ids.add(queued.job_id)  # pyright: ignore[reportPrivateUsage]
        database.storage_gate.close("SQLITE_FULL")
        try:
            assert await dispatcher._try_recover_storage_gate() is True  # pyright: ignore[reportPrivateUsage]
            with database.read_session() as session:
                preserved = repository.get(session, queued.job_id)
            assert preserved is not None
            assert preserved.status is DownloadJobStatus.INTERRUPTED
            assert preserved.recovery_blocked is True
            assert preserved.interrupted_from_status is current.status
            assert preserved.cancel_requested_at == current.cancel_requested_at
            assert manifest_path.read_bytes() == manifest_bytes
            assert dispatcher.active_job_ids == ()
        finally:
            database.close()

    asyncio.run(exercise())


def test_startup_recovers_existing_part_before_inventory_reservation_and_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    partial_bytes = b"preserved-page-attempt"
    database = open_test_database(tmp_path)
    queued = asyncio.run(_queued_job(database, tmp_path))
    repository = DownloadJobRepository()
    with database.write_session() as session:
        active = repository.transition_status(
            session,
            current_job=queued,
            target_status=DownloadJobStatus.FETCHING_METADATA,
            changed_at=queued.updated_at,
        )
    assert active is not None
    recovery = RecoveryCoordinator(database, database.data_root_identity)
    writer = PageManifestWriter(database, manifest_store=recovery.manifest_store)
    page = DownloadPage.new_local(queued.job_id, 1, "mock-page-1")
    writer.replace_pending_pages(queued.job_id, (page,))
    with database.write_session() as session:
        active = repository.transition_status(
            session,
            current_job=active,
            target_status=DownloadJobStatus.DOWNLOADING,
            changed_at=active.updated_at,
        )
    assert active is not None
    attempt_path = DownloadPageRepository.attempt_path(queued.job_id, 1, 1)
    writer.publish_snapshot(
        queued.job_id,
        (replace(page, status="DOWNLOADING", temporary_path=attempt_path, attempts=1),),
    )
    with recovery.temporary_page_store.create(attempt_path) as stream:
        stream.write(partial_bytes)
    partial_path = tmp_path / "temp" / attempt_path
    manifest_path = tmp_path / "database" / "manifests" / f"{queued.job_id}.json"
    manifest_bytes = manifest_path.read_bytes()
    database.close()

    target_app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = cast(RuntimeState, target_app.state.runtime)
    real_reconcile = RecoveryCoordinator.reconcile_startup
    real_inventory = DownloadWorker.existing_artifact_bytes_for_budget
    real_reserve = DiskBudgetManager.reserve

    class NoopInstanceGuard(WindowsMutexGuard):
        def acquire(self) -> None:
            return

        def release(self) -> None:
            return

    def budget_for_startup(current_database: SQLiteDatabase) -> DiskBudgetManager:
        return DiskBudgetManager(
            current_database,
            disk_usage_provider=lambda _path: SimpleNamespace(total=1 << 50, used=0, free=1 << 50),
        )

    def recovered_before_dispatch(coordinator: RecoveryCoordinator, *, batch_size: int = 64) -> int:
        assert events == []
        recovered_count = real_reconcile(coordinator, batch_size=batch_size)
        assert recovered_count == 1
        events.append("recovered")
        return recovered_count

    def inventoried_after_recovery(worker: DownloadWorker, job_id: UUID) -> tuple[int, int]:
        assert events == ["recovered"]
        inventory = real_inventory(worker, job_id)
        assert inventory == (len(partial_bytes), 0)
        events.append("inventoried")
        return inventory

    def reserved_after_inventory(
        budget: DiskBudgetManager,
        job_id: UUID,
        *,
        existing_page_bytes: int = 0,
        existing_cbz_bytes: int = 0,
    ) -> None:
        assert events == ["recovered", "inventoried"]
        assert existing_page_bytes == len(partial_bytes)
        assert existing_cbz_bytes == 0
        real_reserve(
            budget,
            job_id,
            existing_page_bytes=existing_page_bytes,
            existing_cbz_bytes=existing_cbz_bytes,
        )
        events.append("reserved")

    async def skip_sampling(_monitor: ResourceMonitorService) -> None:
        return

    def admitted(_monitor: ResourceMonitorService) -> bool:
        return True

    async def exercise() -> None:
        claim_finished = asyncio.Event()

        async def claim_without_network(worker: DownloadWorker, job_id: UUID) -> object:
            assert events == ["recovered", "inventoried", "reserved"]
            budget = runtime.disk_budget_manager
            current_database = runtime.database
            assert budget is not None and current_database is not None
            assert budget.snapshot().reserved_bytes == (
                PAGE_TEMP_RESERVATION_BYTES + CBZ_RESERVATION_BYTES - len(partial_bytes)
            )
            assert runtime.download_transport is not None
            assert runtime.download_dispatcher is not None
            assert runtime.download_transport._disk_budget_manager is budget  # pyright: ignore[reportPrivateUsage]
            assert runtime.download_dispatcher._disk_budget_manager is budget  # pyright: ignore[reportPrivateUsage]
            assert worker._cbz_builder._disk_budget_manager is budget  # pyright: ignore[reportPrivateUsage]
            assert worker._data_root_identity is current_database.data_root_identity  # pyright: ignore[reportPrivateUsage]
            with current_database.read_session() as session:
                recovered = repository.get(session, job_id)
            assert recovered is not None
            assert recovered.status is DownloadJobStatus.QUEUED
            assert recovered.recovery_blocked is False
            claimed = await worker._transition(  # pyright: ignore[reportPrivateUsage]
                recovered, DownloadJobStatus.FETCHING_METADATA
            )
            events.append("claimed")
            await worker._transition(claimed, DownloadJobStatus.FAILED)  # pyright: ignore[reportPrivateUsage]
            claim_finished.set()
            return object()

        monkeypatch.setattr(DownloadWorker, "run_job", claim_without_network)
        async with target_app.router.lifespan_context(target_app):
            await asyncio.wait_for(claim_finished.wait(), timeout=5)
            assert partial_path.read_bytes() == partial_bytes
            assert manifest_path.read_bytes() == manifest_bytes
            assert events == ["recovered", "inventoried", "reserved", "claimed"]

    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)
    monkeypatch.setattr(lifecycle, "DiskBudgetManager", budget_for_startup)
    monkeypatch.setattr(RecoveryCoordinator, "reconcile_startup", recovered_before_dispatch)
    monkeypatch.setattr(
        DownloadWorker, "existing_artifact_bytes_for_budget", inventoried_after_recovery
    )
    monkeypatch.setattr(DiskBudgetManager, "reserve", reserved_after_inventory)
    monkeypatch.setattr(ResourceMonitorService, "start", skip_sampling)
    monkeypatch.setattr(ResourceMonitorService, "admission_allowed", property(admitted))
    asyncio.run(exercise())
    assert runtime.ready is False
    assert partial_path.read_bytes() == partial_bytes
    assert manifest_path.read_bytes() == manifest_bytes


def test_startup_resumes_verified_interrupted_job_left_between_recovery_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = open_test_database(tmp_path)
    queued = asyncio.run(_queued_job(database, tmp_path))
    repository = DownloadJobRepository()
    with database.write_session() as session:
        active = repository.transition_status(
            session,
            current_job=queued,
            target_status=DownloadJobStatus.FETCHING_METADATA,
            changed_at=queued.updated_at,
        )
    assert active is not None
    recovery = RecoveryCoordinator(database, database.data_root_identity)
    pending_page = DownloadPage.new_local(queued.job_id, 1, "mock-page-1")
    PageManifestWriter(database, manifest_store=recovery.manifest_store).replace_pending_pages(
        queued.job_id, (pending_page,)
    )
    manifest_path = tmp_path / "database" / "manifests" / f"{queued.job_id}.json"
    manifest_bytes = manifest_path.read_bytes()
    database.storage_gate.close("SQLITE_FULL")
    recovery.interrupt_quiesced_job(
        queued.job_id,
        active,
        quiescence_ack=WorkerQuiescenceAck(queued.job_id),
        recovery_blocked=False,
        storage_gate_quiesced=True,
    )
    with database.read_session() as session:
        interrupted = repository.get(session, queued.job_id)
    assert interrupted is not None
    assert interrupted.status is DownloadJobStatus.INTERRUPTED
    assert interrupted.recovery_blocked is False
    database.close()
    database = reopen_test_database(database)
    restarted_recovery = RecoveryCoordinator(database, database.data_root_identity)

    def unexpected_interruption(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("an already interrupted row must never get another interrupt CAS")

    monkeypatch.setattr(
        restarted_recovery.repository, "interrupt_active_job", unexpected_interruption
    )
    try:
        assert restarted_recovery.reconcile_startup() == 0
        with database.read_session() as session:
            resumed = repository.get(session, queued.job_id)
            assert resumed is not None
            manifest = DownloadPageRepository().project_manifest(session, resumed)
        assert resumed.status is DownloadJobStatus.QUEUED
        assert resumed.recovery_blocked is False
        assert resumed.interrupted_from_status is DownloadJobStatus.FETCHING_METADATA
        assert resumed.cancel_requested_at is None
        assert manifest.pages == (pending_page,)
        assert manifest_path.read_bytes() == manifest_bytes
    finally:
        database.close()
