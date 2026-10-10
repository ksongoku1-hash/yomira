from __future__ import annotations

from pathlib import Path
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest
from support.sqlite import sqlite_test_context

from app.persistence.database import SQLiteDatabase
from app.persistence.disk_budget import (
    CBZ_RESERVATION_BYTES,
    MINIMUM_RESERVED_BYTES,
    PAGE_TEMP_RESERVATION_BYTES,
    DiskBudgetManager,
    DiskBudgetUnavailableError,
)
from app.persistence.storage_gate import SharedStorageGate, StorageGateClosedError


def _manager(
    tmp_path: Path,
    free_bytes: list[int | None],
) -> tuple[DiskBudgetManager, SQLiteDatabase]:
    context = sqlite_test_context(tmp_path)
    database = context.open()

    def disk_usage(_path: Path) -> SimpleNamespace:
        free = free_bytes[0]
        if free is None:
            raise OSError("volume capacity is unavailable")
        total = 1 << 50
        return SimpleNamespace(total=total, used=total - free, free=free)

    return DiskBudgetManager(database, disk_usage_provider=disk_usage), database


def test_disk_reservation_accepts_exact_threshold_and_rejects_one_byte_below(
    tmp_path: Path,
) -> None:
    free_bytes: list[int | None] = [0]
    manager, database = _manager(tmp_path, free_bytes)
    try:
        initial = manager.snapshot()
        assert initial.recovery_demand_bytes is not None
        maximum_job_bytes = PAGE_TEMP_RESERVATION_BYTES + CBZ_RESERVATION_BYTES
        exact_threshold = (
            initial.recovery_demand_bytes + MINIMUM_RESERVED_BYTES + maximum_job_bytes
        )

        free_bytes[0] = exact_threshold
        manager.reserve(uuid4())
        assert manager.snapshot().safe is True

        free_bytes[0] = exact_threshold - 1
        with pytest.raises(DiskBudgetUnavailableError):
            manager.reserve(uuid4())
        assert manager.snapshot().safe is False
    finally:
        database.close()


def test_whole_manga_reservation_keeps_download_limits_and_cannot_overallocate(
    tmp_path: Path,
) -> None:
    gib = 1024 * 1024 * 1024
    free_bytes: list[int | None] = [24 * gib]
    manager, database = _manager(tmp_path, free_bytes)
    try:
        manager.reserve_bounded_operation(uuid4(), page_bytes=8 * gib, cbz_bytes=8 * gib)
        assert manager.snapshot().reserved_bytes == 16 * gib
        with pytest.raises(DiskBudgetUnavailableError):
            manager.reserve_bounded_operation(uuid4(), page_bytes=8 * gib, cbz_bytes=8 * gib)
        with pytest.raises(DiskBudgetUnavailableError):
            manager.reserve(uuid4(), existing_page_bytes=3 * gib)
        with pytest.raises(DiskBudgetUnavailableError):
            manager.reserve_bounded_operation(uuid4(), page_bytes=8 * gib + 1)
    finally:
        database.close()


def test_concurrent_reservations_cannot_overallocate_one_volume(tmp_path: Path) -> None:
    free_bytes: list[int | None] = [0]
    manager, database = _manager(tmp_path, free_bytes)
    start = Barrier(3)
    results: list[bool] = []
    try:
        baseline = manager.snapshot()
        assert baseline.recovery_demand_bytes is not None
        one_job = PAGE_TEMP_RESERVATION_BYTES + CBZ_RESERVATION_BYTES
        free_bytes[0] = (
            baseline.recovery_demand_bytes
            + MINIMUM_RESERVED_BYTES
            + one_job
            + one_job
            - 1
        )

        def reserve() -> None:
            start.wait()
            try:
                manager.reserve(uuid4())
            except DiskBudgetUnavailableError:
                results.append(False)
            else:
                results.append(True)

        workers = [Thread(target=reserve), Thread(target=reserve)]
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join(timeout=5)

        assert all(not worker.is_alive() for worker in workers)
        assert sorted(results) == [False, True]
        assert manager.snapshot().reserved_bytes == one_job
    finally:
        database.close()


def test_confirmed_writes_consume_reservation_and_ambiguous_writes_keep_it(
    tmp_path: Path,
) -> None:
    free_bytes: list[int | None] = [0]
    manager, database = _manager(tmp_path, free_bytes)
    job_id = uuid4()
    try:
        snapshot = manager.snapshot()
        assert snapshot.recovery_demand_bytes is not None
        free_bytes[0] = (
            snapshot.recovery_demand_bytes
            + MINIMUM_RESERVED_BYTES
            + PAGE_TEMP_RESERVATION_BYTES
            + CBZ_RESERVATION_BYTES
        )
        manager.reserve(job_id)

        assert (
            manager.write_reserved(
                job_id,
                category="pages",
                requested_bytes=5,
                write=lambda: 5,
            )
            == 5
        )
        reserved_after_success = manager.snapshot().reserved_bytes
        assert reserved_after_success == PAGE_TEMP_RESERVATION_BYTES + CBZ_RESERVATION_BYTES - 5

        def ambiguous_write() -> int:
            raise OSError("write result is ambiguous")

        with pytest.raises(OSError, match="ambiguous"):
            manager.write_reserved(
                job_id,
                category="pages",
                requested_bytes=7,
                write=ambiguous_write,
            )
        assert manager.snapshot().reserved_bytes == reserved_after_success

        with pytest.raises(RuntimeError):
            manager.release(job_id, quiescent=False)
        with database.storage_gate.mutation(), pytest.raises(RuntimeError):
            manager.release(job_id, quiescent=True)
        assert manager.snapshot().reserved_bytes == reserved_after_success
        manager.release(job_id, quiescent=True)
        assert manager.snapshot().reserved_bytes == 0
    finally:
        database.close()


def test_in_flight_write_holds_budget_without_blocking_other_job_admission(
    tmp_path: Path,
) -> None:
    free_bytes: list[int | None] = [0]
    manager, database = _manager(tmp_path, free_bytes)
    job_id = uuid4()
    first_started = Event()
    release_first = Event()
    second_denied = Event()
    second_write_called = Event()
    release_denied = Event()
    requested_bytes = PAGE_TEMP_RESERVATION_BYTES * 3 // 4
    errors: list[BaseException] = []
    first: Thread | None = None
    second: Thread | None = None
    try:
        baseline = manager.snapshot()
        assert baseline.recovery_demand_bytes is not None
        free_bytes[0] = (
            baseline.recovery_demand_bytes
            + MINIMUM_RESERVED_BYTES
            + PAGE_TEMP_RESERVATION_BYTES
            + CBZ_RESERVATION_BYTES
        )
        manager.reserve(job_id)

        def ambiguous_slow_write() -> int:
            first_started.set()
            try:
                manager.release(job_id, quiescent=True)
            except RuntimeError:
                release_denied.set()
            if not release_first.wait(timeout=5):
                raise TimeoutError("test did not release the first write")
            raise OSError("ambiguous write result")

        def first_writer() -> None:
            try:
                manager.write_reserved(
                    job_id,
                    category="pages",
                    requested_bytes=requested_bytes,
                    write=ambiguous_slow_write,
                )
            except OSError as error:
                errors.append(error)

        def second_writer() -> None:
            try:
                manager.write_reserved(
                    job_id,
                    category="pages",
                    requested_bytes=requested_bytes,
                    write=lambda: second_write_called.set() or requested_bytes,
                )
            except DiskBudgetUnavailableError:
                second_denied.set()

        first = Thread(target=first_writer)
        second = Thread(target=second_writer)
        first.start()
        assert first_started.wait(timeout=2)
        assert manager.snapshot().reserved_bytes == (
            PAGE_TEMP_RESERVATION_BYTES + CBZ_RESERVATION_BYTES
        )
        assert release_denied.is_set()
        second.start()
        second.join(timeout=2)

        assert not second.is_alive()
        assert second_denied.is_set()
        assert not second_write_called.is_set()
    finally:
        release_first.set()
        if first is not None:
            first.join(timeout=5)
        if second is not None:
            second.join(timeout=5)
        database.close()

    assert first is not None
    assert second is not None
    assert not first.is_alive()
    assert not second.is_alive()
    assert len(errors) == 1
    assert manager.snapshot().reserved_bytes == (
        PAGE_TEMP_RESERVATION_BYTES + CBZ_RESERVATION_BYTES
    )


def test_unknown_volume_measurement_denies_reservation(tmp_path: Path) -> None:
    manager, database = _manager(tmp_path, [None])
    try:
        assert manager.snapshot().safe is False
        with pytest.raises(DiskBudgetUnavailableError):
            manager.reserve(uuid4())
        assert manager.snapshot().reserved_bytes == 0
    finally:
        database.close()


def test_cbz_zip_backpatch_consumes_only_physical_file_growth(tmp_path: Path) -> None:
    from app.services.cbz_builder import (
        _SizeBoundedZipStream,  # pyright: ignore[reportPrivateUsage]
    )

    free_bytes: list[int | None] = [0]
    manager, database = _manager(tmp_path, free_bytes)
    job_id = uuid4()
    path = tmp_path / "backpatch.cbz.tmp"
    try:
        snapshot = manager.snapshot()
        assert snapshot.recovery_demand_bytes is not None
        tokens = PAGE_TEMP_RESERVATION_BYTES + CBZ_RESERVATION_BYTES
        free_bytes[0] = (
            snapshot.recovery_demand_bytes
            + MINIMUM_RESERVED_BYTES
            + tokens
        )
        manager.reserve(job_id)
        with path.open("w+b", buffering=0) as raw:
            stream = _SizeBoundedZipStream(
                raw,
                max_bytes=CBZ_RESERVATION_BYTES,
                disk_budget_manager=manager,
                job_id=job_id,
            )
            assert stream.write(b"12345678") == 8
            after_append = manager.snapshot().reserved_bytes
            assert after_append == tokens - 8
            stream.seek(0)
            assert stream.write(b"abcd") == 4
            assert manager.snapshot().reserved_bytes == after_append
            stream.close()
    finally:
        database.close()


def test_storage_gate_reopens_only_after_mutations_are_quiescent() -> None:
    gate = SharedStorageGate()
    with gate.mutation():
        gate.close("FILESYSTEM_DISK_FULL")
        assert gate.reopen_after_recovery() is False
        assert gate.closed is True

    assert gate.closed is True
    assert gate.reopen_after_recovery() is True
    assert gate.closed is False

    with gate.mutation():
        pass


def test_storage_gate_closed_state_still_rejects_normal_mutations() -> None:
    gate = SharedStorageGate()
    gate.close("SQLITE_FULL")

    with pytest.raises(StorageGateClosedError):
        with gate.mutation():
            pass
