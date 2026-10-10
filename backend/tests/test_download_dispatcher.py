from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from support.sqlite import open_test_database

from app.connectors.mock import MockConnector
from app.domain.download_job import DownloadJob, DownloadJobStatus
from app.domain.job_pause import JobPauseController
from app.persistence.disk_budget import (
    CBZ_RESERVATION_BYTES,
    MINIMUM_RESERVED_BYTES,
    PAGE_TEMP_RESERVATION_BYTES,
    DiskBudgetManager,
)
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.repositories.download_job import DownloadJobRepository
from app.services.job_recovery import RecoveryCoordinator, WorkerQuiescenceAck
from app.services.jobs import DownloadJobService


def test_source_failure_holds_the_remaining_queue_until_connection_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.errors import ErrorCode
    from app.services.download_dispatcher import DownloadDispatcher
    from app.services.download_worker import DownloadWorkerResult

    clock = [100.0]
    monkeypatch.setattr("app.services.download_dispatcher.monotonic", lambda: clock[0])

    async def exercise() -> None:
        database = open_test_database(tmp_path)
        MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
        connector = MockConnector()
        template = (await connector.get_chapters("mock-series-1"))[0]
        jobs = DownloadJobService(database).create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=connector.normalize_metadata(
                await connector.get_series("mock-series-1")
            ),
            chapters=(
                template,
                replace(template, remote_id="mock-chapter-2", chapter_sort_key=(2,)),
            ),
            selected_remote_ids=("mock-chapter-1", "mock-chapter-2"),
        )

        class Runner:
            available = False
            calls: list[UUID] = []
            probes = 0

            async def run_job(self, job_id: UUID) -> object:
                self.calls.append(job_id)
                summary = next(item for item in jobs if item.job.job_id == job_id)
                with database.write_session() as session:
                    from app.persistence.models.download_job import DownloadJobRow

                    session.get(DownloadJobRow, str(job_id)).status = "FAILED"
                return DownloadWorkerResult(
                    replace(summary.job, status=DownloadJobStatus.FAILED),
                    summary.series_id,
                    summary.chapter_id,
                    0,
                    None,
                    ErrorCode.SRC_CONNECTOR_FAILED.value,
                    "source",
                    "trace-test",
                    retryable=True,
                )

            async def source_available(self, _job_id: UUID) -> bool:
                self.probes += 1
                return self.available

        runner = Runner()
        dispatcher = DownloadDispatcher(
            database=database,
            worker=runner,
            recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
        )
        try:
            await dispatcher._dispatch_available()
            await asyncio.gather(*dispatcher._active.values())
            dispatcher._discard_completed_tasks()
            assert len(runner.calls) == 1
            assert dispatcher.source_wait == (1, 30)
            await dispatcher._dispatch_available()
            assert runner.probes == 0 and not dispatcher.active_job_ids
            clock[0] += 31
            await dispatcher._dispatch_available()
            assert runner.probes == 1 and not dispatcher.active_job_ids
            assert DownloadJobService(database).list_jobs().states["QUEUED"] == 1
            runner.available = True
            clock[0] += 31
            await dispatcher._dispatch_available()
            assert runner.probes == 2 and len(dispatcher.active_job_ids) == 1
            await asyncio.gather(*dispatcher._active.values())
            assert len(runner.calls) == 2
        finally:
            database.close()

    asyncio.run(exercise())


def test_dispatcher_runs_job_and_drains_before_stopping(tmp_path: Path) -> None:
    asyncio.run(_exercise_dispatcher_drain(tmp_path))


def test_unhandled_active_worker_failure_is_interrupted_and_recovery_blocked(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_unhandled_failure(tmp_path))


def test_dispatcher_blocks_admission_and_registers_jobs_with_pause_controller(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_backpressure_admission(tmp_path))


def test_storage_gate_blocks_dispatch_of_queued_jobs(tmp_path: Path) -> None:
    asyncio.run(_exercise_storage_gate_admission(tmp_path))


def test_storage_gate_interrupts_active_job_only_after_worker_ack(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_storage_gate_quiescence(tmp_path))


def test_storage_gate_waits_for_all_workers_before_any_interruption_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_exercise_global_storage_gate_quiescence(tmp_path, monkeypatch))


def test_storage_gate_retains_quiesced_jobs_after_permit_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_exercise_quiescence_timeout_retains_pending_job(tmp_path, monkeypatch))


def test_dispatcher_reserves_disk_before_start_and_releases_after_worker_quiescence(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_disk_reservation_lifecycle(tmp_path))


def test_dispatcher_keeps_queued_job_when_disk_reservation_is_unavailable(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_disk_reservation_denied(tmp_path))


def test_storage_gate_reopens_only_after_budget_and_sqlite_checks_pass(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_storage_gate_recovery_checks(tmp_path))


async def _exercise_disk_reservation_lifecycle(data_root: Path) -> None:
    from types import SimpleNamespace

    from app.services.download_dispatcher import DownloadDispatcher

    database = open_test_database(data_root)
    MigrationRunner(database, data_root / "backups").upgrade_to_head()
    connector = MockConnector()
    DownloadJobService(database).create_selected_jobs(
        connector=connector.metadata,
        remote_series_id="mock-series-1",
        series_metadata=connector.normalize_metadata(await connector.get_series("mock-series-1")),
        chapters=await connector.get_chapters("mock-series-1"),
        selected_remote_ids=("mock-chapter-1",),
    )[0]
    recovery_demand = DiskBudgetManager(database).snapshot().recovery_demand_bytes
    assert recovery_demand is not None
    free_bytes = [
        recovery_demand
        + MINIMUM_RESERVED_BYTES
        + PAGE_TEMP_RESERVATION_BYTES
        + CBZ_RESERVATION_BYTES
    ]
    total_bytes = 1 << 50
    budget = DiskBudgetManager(
        database,
        disk_usage_provider=lambda _path: SimpleNamespace(
            total=total_bytes,
            used=total_bytes - free_bytes[0],
            free=free_bytes[0],
        ),
    )

    class HoldingRunner:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        def existing_artifact_bytes_for_budget(self, _job_id: UUID) -> tuple[int, int]:
            return 0, 0

        async def run_job(self, job_id: UUID) -> object:
            del job_id
            assert budget.snapshot().reserved_bytes == (
                PAGE_TEMP_RESERVATION_BYTES + CBZ_RESERVATION_BYTES
            )
            self.started.set()
            await self.release.wait()
            return object()

    runner = HoldingRunner()
    dispatcher = DownloadDispatcher(
        database=database,
        worker=runner,
        recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
        disk_budget_manager=budget,
        poll_interval_seconds=0.05,
    )
    try:
        await dispatcher.start()
        await asyncio.wait_for(runner.started.wait(), timeout=2)
        assert budget.snapshot().reserved_bytes == (
            PAGE_TEMP_RESERVATION_BYTES + CBZ_RESERVATION_BYTES
        )
        dispatcher.request_stop()
        runner.release.set()
        await asyncio.wait_for(dispatcher.stop(), timeout=2)
        assert budget.snapshot().reserved_bytes == 0
    finally:
        runner.release.set()
        await dispatcher.stop()
        database.shutdown()


async def _exercise_disk_reservation_denied(data_root: Path) -> None:
    from types import SimpleNamespace

    from app.services.download_dispatcher import DownloadDispatcher

    database = open_test_database(data_root)
    MigrationRunner(database, data_root / "backups").upgrade_to_head()
    connector = MockConnector()
    summary = DownloadJobService(database).create_selected_jobs(
        connector=connector.metadata,
        remote_series_id="mock-series-1",
        series_metadata=connector.normalize_metadata(await connector.get_series("mock-series-1")),
        chapters=await connector.get_chapters("mock-series-1"),
        selected_remote_ids=("mock-chapter-1",),
    )[0]
    initial = DiskBudgetManager(database).snapshot()
    assert initial.recovery_demand_bytes is not None
    required_for_one_job = (
        initial.recovery_demand_bytes
        + MINIMUM_RESERVED_BYTES
        + PAGE_TEMP_RESERVATION_BYTES
        + CBZ_RESERVATION_BYTES
    )
    total_bytes = 1 << 50
    budget = DiskBudgetManager(
        database,
        disk_usage_provider=lambda _path: SimpleNamespace(
            total=total_bytes,
            used=total_bytes - required_for_one_job + 1,
            free=required_for_one_job - 1,
        ),
    )

    class Runner:
        calls = 0

        def existing_artifact_bytes_for_budget(self, _job_id: UUID) -> tuple[int, int]:
            return 0, 0

        async def run_job(self, job_id: UUID) -> object:
            del job_id
            self.calls += 1
            return object()

    runner = Runner()
    dispatcher = DownloadDispatcher(
        database=database,
        worker=runner,
        recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
        disk_budget_manager=budget,
        poll_interval_seconds=0.05,
    )
    try:
        await dispatcher.start()
        await asyncio.sleep(0.15)
        await dispatcher.stop()
        assert runner.calls == 0
        assert budget.snapshot().reserved_bytes == 0
        with database.read_session() as session:
            queued = DownloadJobRepository().get(session, summary.job.job_id)
        assert queued is not None
        assert queued.status is DownloadJobStatus.QUEUED
    finally:
        await dispatcher.stop()
        database.shutdown()


async def _exercise_storage_gate_recovery_checks(data_root: Path) -> None:
    from types import SimpleNamespace

    from app.services.download_dispatcher import DownloadDispatcher

    database = open_test_database(data_root)
    baseline = DiskBudgetManager(database).snapshot()
    assert baseline.recovery_demand_bytes is not None
    minimum_safe_free = baseline.recovery_demand_bytes + MINIMUM_RESERVED_BYTES
    free_bytes = [minimum_safe_free - 1]
    total_bytes = 1 << 50
    budget = DiskBudgetManager(
        database,
        disk_usage_provider=lambda _path: SimpleNamespace(
            total=total_bytes,
            used=total_bytes - free_bytes[0],
            free=free_bytes[0],
        ),
    )

    class Runner:
        async def run_job(self, job_id: UUID) -> object:
            del job_id
            return object()

        def existing_artifact_bytes_for_budget(self, _job_id: UUID) -> tuple[int, int]:
            return 0, 0

    dispatcher = DownloadDispatcher(
        database=database,
        worker=Runner(),
        recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
        disk_budget_manager=budget,
    )
    database.storage_gate.close("SQLITE_FULL")
    try:
        assert await dispatcher._try_recover_storage_gate() is False  # pyright: ignore[reportPrivateUsage]
        assert database.storage_gate.closed is True
        free_bytes[0] = minimum_safe_free
        assert await dispatcher._try_recover_storage_gate() is True  # pyright: ignore[reportPrivateUsage]
        assert database.storage_gate.closed is False
    finally:
        database.close()


async def _exercise_dispatcher_drain(data_root: Path) -> None:
    from app.services.download_dispatcher import DownloadDispatcher

    database = open_test_database(data_root)
    MigrationRunner(database, data_root / "backups").upgrade_to_head()
    connector = MockConnector()
    chapters = await connector.get_chapters("mock-series-1")
    second_chapter = replace(
        chapters[0],
        remote_id="mock-chapter-2",
        remote_label="2",
        chapter_raw_label="2",
        chapter_major=2,
        chapter_sort_key=(2, 0),
        display_label="Capítulo 2",
    )
    summaries = DownloadJobService(database).create_selected_jobs(
        connector=connector.metadata,
        remote_series_id="mock-series-1",
        series_metadata=connector.normalize_metadata(await connector.get_series("mock-series-1")),
        chapters=chapters + (second_chapter,),
        selected_remote_ids=("mock-chapter-1", "mock-chapter-2"),
    )
    summary, queued_summary = summaries
    other_summary = DownloadJobService(database).create_selected_jobs(
        connector=replace(connector.metadata, connector_id="yomira.second-source"),
        remote_series_id="mock-series-1",
        series_metadata=connector.normalize_metadata(await connector.get_series("mock-series-1")),
        chapters=(second_chapter,),
        selected_remote_ids=("mock-chapter-2",),
    )[0]
    selected_job_ids = {summary.job.job_id, queued_summary.job.job_id}

    class HoldingRunner:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls: list[UUID] = []

        async def run_job(self, job_id: UUID) -> object:
            self.calls.append(job_id)
            repository = DownloadJobRepository()
            with database.write_session() as session:
                current = repository.get(session, job_id)
                assert current is not None
                active = repository.transition_status(
                    session,
                    current_job=current,
                    target_status=DownloadJobStatus.FETCHING_METADATA,
                    changed_at=current.updated_at,
                )
                assert active is not None
            self.started.set()
            await self.release.wait()
            with database.write_session() as session:
                current = repository.get(session, job_id)
                assert current is not None
                failed = repository.transition_status(
                    session,
                    current_job=current,
                    target_status=DownloadJobStatus.FAILED,
                    changed_at=current.updated_at,
                )
                assert failed is not None
            return failed

    runner = HoldingRunner()
    dispatcher = DownloadDispatcher(
        database=database,
        worker=runner,
        recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
    )
    try:
        await dispatcher.start()
        await asyncio.wait_for(runner.started.wait(), timeout=2)
        snapshots = dispatcher.active_job_snapshots
        assert len(snapshots) == 1
        active_job_id = snapshots[0].job_id
        assert active_job_id in selected_job_ids
        assert snapshots[0].stage is DownloadJobStatus.FETCHING_METADATA
        # A different source must not use a second slot while this chapter is active.
        await dispatcher._dispatch_available()
        assert len(dispatcher.active_job_ids) == 1
        with database.read_session() as session:
            assert DownloadJobRepository().get(session, other_summary.job.job_id).status is (
                DownloadJobStatus.QUEUED
            )

        dispatcher.request_stop()
        stop_task = asyncio.create_task(dispatcher.stop())
        await asyncio.sleep(0)
        assert not stop_task.done()
        runner.release.set()
        await asyncio.wait_for(stop_task, timeout=2)
        assert runner.calls == [active_job_id]
        queued_job_id = next(job_id for job_id in selected_job_ids if job_id != active_job_id)
        with database.read_session() as session:
            final = DownloadJobRepository().get(session, active_job_id)
            queued = DownloadJobRepository().get(session, queued_job_id)
        assert final is not None
        assert final.status is DownloadJobStatus.FAILED
        assert queued is not None
        assert queued.status is DownloadJobStatus.QUEUED
    finally:
        runner.release.set()
        await dispatcher.stop()
        database.shutdown()


async def _exercise_unhandled_failure(data_root: Path) -> None:
    from app.services.download_dispatcher import DownloadDispatcher

    database = open_test_database(data_root)
    MigrationRunner(database, data_root / "backups").upgrade_to_head()
    connector = MockConnector()
    summary = DownloadJobService(database).create_selected_jobs(
        connector=connector.metadata,
        remote_series_id="mock-series-1",
        series_metadata=connector.normalize_metadata(await connector.get_series("mock-series-1")),
        chapters=await connector.get_chapters("mock-series-1"),
        selected_remote_ids=("mock-chapter-1",),
    )[0]

    class FailingRunner:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.calls: list[UUID] = []

        async def run_job(self, job_id: UUID) -> object:
            self.calls.append(job_id)
            repository = DownloadJobRepository()
            with database.write_session() as session:
                current = repository.get(session, job_id)
                assert current is not None
                active = repository.transition_status(
                    session,
                    current_job=current,
                    target_status=DownloadJobStatus.FETCHING_METADATA,
                    changed_at=current.updated_at,
                )
                assert active is not None
            self.started.set()
            raise RuntimeError("synthetic hidden detail")

    runner = FailingRunner()
    dispatcher = DownloadDispatcher(
        database=database,
        worker=runner,
        recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
    )
    try:
        await dispatcher.start()
        await asyncio.wait_for(runner.started.wait(), timeout=2)
        await asyncio.wait_for(dispatcher.stop(), timeout=2)
        assert runner.calls == [summary.job.job_id]
        with database.read_session() as session:
            final = DownloadJobRepository().get(session, summary.job.job_id)
        assert final is not None
        assert final.status is DownloadJobStatus.INTERRUPTED
        assert final.recovery_blocked is True
        assert final.interrupted_from_status is DownloadJobStatus.FETCHING_METADATA
    finally:
        await dispatcher.stop()
        database.shutdown()


async def _exercise_backpressure_admission(data_root: Path) -> None:
    from app.services.download_dispatcher import DownloadDispatcher

    database = open_test_database(data_root)
    MigrationRunner(database, data_root / "backups").upgrade_to_head()
    connector = MockConnector()
    summary = DownloadJobService(database).create_selected_jobs(
        connector=connector.metadata,
        remote_series_id="mock-series-1",
        series_metadata=connector.normalize_metadata(await connector.get_series("mock-series-1")),
        chapters=await connector.get_chapters("mock-series-1"),
        selected_remote_ids=("mock-chapter-1",),
    )[0]
    controller = JobPauseController()
    admission = {"allowed": False}

    class HoldingRunner:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls: list[UUID] = []

        async def run_job(self, job_id: UUID) -> object:
            self.calls.append(job_id)
            assert controller.state(job_id).value == "RUNNING"
            repository = DownloadJobRepository()
            with database.write_session() as session:
                current = repository.get(session, job_id)
                assert current is not None
                active = repository.transition_status(
                    session,
                    current_job=current,
                    target_status=DownloadJobStatus.FETCHING_METADATA,
                    changed_at=current.updated_at,
                )
                assert active is not None
            self.started.set()
            await self.release.wait()
            with database.write_session() as session:
                current = repository.get(session, job_id)
                assert current is not None
                failed = repository.transition_status(
                    session,
                    current_job=current,
                    target_status=DownloadJobStatus.FAILED,
                    changed_at=current.updated_at,
                )
                assert failed is not None
            return failed

    runner = HoldingRunner()
    dispatcher = DownloadDispatcher(
        database=database,
        worker=runner,
        recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
        poll_interval_seconds=0.05,
        admission_allowed=lambda: admission["allowed"],
        pause_controller=controller,
    )
    try:
        await dispatcher.start()
        await asyncio.sleep(0.12)
        assert runner.calls == []
        with database.read_session() as session:
            queued = DownloadJobRepository().get(session, summary.job.job_id)
        assert queued is not None
        assert queued.status is DownloadJobStatus.QUEUED
        assert controller.active_job_count == 0

        admission["allowed"] = True
        dispatcher.notify_new_jobs()
        await asyncio.wait_for(runner.started.wait(), timeout=2)
        assert runner.calls == [summary.job.job_id]
        assert controller.active_job_count == 1
        runner.release.set()
        await asyncio.wait_for(dispatcher.stop(), timeout=2)
        assert controller.active_job_count == 0
    finally:
        runner.release.set()
        await dispatcher.stop()
        database.shutdown()


async def _exercise_storage_gate_admission(data_root: Path) -> None:
    from app.services.download_dispatcher import DownloadDispatcher

    database = open_test_database(data_root)
    MigrationRunner(database, data_root / "backups").upgrade_to_head()
    connector = MockConnector()
    summary = DownloadJobService(database).create_selected_jobs(
        connector=connector.metadata,
        remote_series_id="mock-series-1",
        series_metadata=connector.normalize_metadata(await connector.get_series("mock-series-1")),
        chapters=await connector.get_chapters("mock-series-1"),
        selected_remote_ids=("mock-chapter-1",),
    )[0]

    class Runner:
        def __init__(self) -> None:
            self.calls: list[UUID] = []

        async def run_job(self, job_id: UUID) -> object:
            self.calls.append(job_id)
            return object()

    runner = Runner()
    dispatcher = DownloadDispatcher(
        database=database,
        worker=runner,
        recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
        poll_interval_seconds=0.05,
    )
    database.storage_gate.close("SQLITE_FULL")
    try:
        await dispatcher.start()
        await asyncio.sleep(0.12)
        assert runner.calls == []
        with database.read_session() as session:
            queued = DownloadJobRepository().get(session, summary.job.job_id)
        assert queued is not None
        assert queued.status is DownloadJobStatus.QUEUED
    finally:
        await dispatcher.stop()
        database.shutdown()


async def _exercise_storage_gate_quiescence(data_root: Path) -> None:
    from app.services.download_dispatcher import DownloadDispatcher

    database = open_test_database(data_root)
    MigrationRunner(database, data_root / "backups").upgrade_to_head()
    connector = MockConnector()
    chapters = await connector.get_chapters("mock-series-1")
    second_chapter = replace(
        chapters[0],
        remote_id="mock-chapter-2",
        remote_label="2",
        chapter_raw_label="2",
        chapter_major=2,
        chapter_sort_key=(2, 0),
        display_label="Capítulo 2",
    )
    summaries = DownloadJobService(database).create_selected_jobs(
        connector=connector.metadata,
        remote_series_id="mock-series-1",
        series_metadata=connector.normalize_metadata(await connector.get_series("mock-series-1")),
        chapters=chapters + (second_chapter,),
        selected_remote_ids=("mock-chapter-1", "mock-chapter-2"),
    )
    job_ids = {summary.job.job_id for summary in summaries}
    started_job_id: UUID | None = None
    queued_job_id: UUID | None = None

    class HoldingRunner:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls: list[UUID] = []

        async def run_job(self, job_id: UUID) -> object:
            self.calls.append(job_id)
            repository = DownloadJobRepository()
            with database.write_session() as session:
                current = repository.get(session, job_id)
                assert current is not None
                active = repository.transition_status(
                    session,
                    current_job=current,
                    target_status=DownloadJobStatus.FETCHING_METADATA,
                    changed_at=current.updated_at,
                )
                assert active is not None
            self.started.set()
            await self.release.wait()
            return object()

    runner = HoldingRunner()
    dispatcher = DownloadDispatcher(
        database=database,
        worker=runner,
        recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
        poll_interval_seconds=0.05,
    )
    try:
        await dispatcher.start()
        await asyncio.wait_for(runner.started.wait(), timeout=2)
        assert len(runner.calls) == 1
        started_job_id = runner.calls[0]
        queued_job_id = next(job_id for job_id in job_ids if job_id != started_job_id)
        database.storage_gate.close("FILESYSTEM_DISK_FULL")
        runner.release.set()
        await asyncio.sleep(0.12)
        await dispatcher.stop()

        with database.read_session() as session:
            interrupted = DownloadJobRepository().get(session, started_job_id)
            queued = DownloadJobRepository().get(session, queued_job_id)
        assert interrupted is not None
        assert interrupted.status is DownloadJobStatus.INTERRUPTED
        assert interrupted.interrupted_from_status is DownloadJobStatus.FETCHING_METADATA
        assert interrupted.cancel_requested_at is None
        assert interrupted.recovery_blocked is True
        assert queued is not None
        assert queued.status is DownloadJobStatus.QUEUED
        assert runner.calls == [started_job_id]
    finally:
        runner.release.set()
        await dispatcher.stop()
        database.shutdown()


async def _exercise_global_storage_gate_quiescence(
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exercise the safety guard for an already accepted multi-worker window.
    monkeypatch.setattr("app.services.download_dispatcher.MAX_ACTIVE_JOBS", 2)
    from app.services.download_dispatcher import DownloadDispatcher

    monkeypatch.setattr(
        "app.services.download_dispatcher.MAX_ACTIVE_JOBS_PER_SOURCE",
        2,
    )
    database = open_test_database(data_root)
    MigrationRunner(database, data_root / "backups").upgrade_to_head()
    connector = MockConnector()
    chapters = await connector.get_chapters("mock-series-1")
    extra_chapters = tuple(
        replace(
            chapters[0],
            remote_id=f"mock-chapter-{ordinal}",
            remote_label=str(ordinal),
            chapter_raw_label=str(ordinal),
            chapter_major=ordinal,
            chapter_sort_key=(ordinal, 0),
            display_label=f"Capítulo {ordinal}",
        )
        for ordinal in (2, 3)
    )
    summaries = DownloadJobService(database).create_selected_jobs(
        connector=connector.metadata,
        remote_series_id="mock-series-1",
        series_metadata=connector.normalize_metadata(await connector.get_series("mock-series-1")),
        chapters=chapters + extra_chapters,
        selected_remote_ids=("mock-chapter-1", "mock-chapter-2", "mock-chapter-3"),
    )
    job_ids = [summary.job.job_id for summary in summaries]
    queued_job_id: UUID | None = None

    class TwoHoldingRunner:
        def __init__(self) -> None:
            self.started: asyncio.Queue[UUID] = asyncio.Queue()
            self.release: dict[UUID, asyncio.Event] = {
                job_id: asyncio.Event() for job_id in job_ids
            }
            self.returned: dict[UUID, asyncio.Event] = {
                job_id: asyncio.Event() for job_id in job_ids
            }
            self.calls: list[UUID] = []

        async def run_job(self, job_id: UUID) -> object:
            self.calls.append(job_id)
            repository = DownloadJobRepository()
            with database.write_session() as session:
                current = repository.get(session, job_id)
                assert current is not None
                active = repository.transition_status(
                    session,
                    current_job=current,
                    target_status=DownloadJobStatus.FETCHING_METADATA,
                    changed_at=current.updated_at,
                )
                assert active is not None
            self.started.put_nowait(job_id)
            await self.release[job_id].wait()
            self.returned[job_id].set()
            return object()

    runner = TwoHoldingRunner()
    dispatcher = DownloadDispatcher(
        database=database,
        worker=runner,
        recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
        poll_interval_seconds=0.05,
    )
    try:
        await dispatcher.start()
        started_ids = {
            await asyncio.wait_for(runner.started.get(), timeout=2),
            await asyncio.wait_for(runner.started.get(), timeout=2),
        }
        assert len(started_ids) == 2
        queued_job_id = next(job_id for job_id in job_ids if job_id not in started_ids)
        database.storage_gate.close("FILESYSTEM_DISK_FULL")

        first_id = next(iter(started_ids))
        runner.release[first_id].set()
        await asyncio.wait_for(runner.returned[first_id].wait(), timeout=2)
        await asyncio.sleep(0.12)
        with database.read_session() as session:
            first_before_global_ack = DownloadJobRepository().get(session, first_id)
            still_queued = DownloadJobRepository().get(session, queued_job_id)
        assert first_before_global_ack is not None
        assert first_before_global_ack.status is DownloadJobStatus.FETCHING_METADATA
        assert still_queued is not None
        assert still_queued.status is DownloadJobStatus.QUEUED

        second_id = next(job_id for job_id in started_ids if job_id != first_id)
        runner.release[second_id].set()
        await asyncio.wait_for(dispatcher.stop(), timeout=2)
        with database.read_session() as session:
            interrupted_jobs = [
                DownloadJobRepository().get(session, job_id) for job_id in started_ids
            ]
            still_queued = DownloadJobRepository().get(session, queued_job_id)
        assert all(job is not None for job in interrupted_jobs)
        assert all(
            job.status is DownloadJobStatus.INTERRUPTED
            and job.interrupted_from_status is DownloadJobStatus.FETCHING_METADATA
            and job.cancel_requested_at is None
            and job.recovery_blocked is True
            for job in interrupted_jobs
            if job is not None
        )
        assert still_queued is not None
        assert still_queued.status is DownloadJobStatus.QUEUED
        assert set(runner.calls) == started_ids
    finally:
        for event in runner.release.values():
            event.set()
        await dispatcher.stop()
        database.shutdown()


async def _exercise_quiescence_timeout_retains_pending_job(
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from app.persistence.database import SQLiteOptions
    from app.services.download_dispatcher import DownloadDispatcher

    database = open_test_database(
        data_root,
        options=SQLiteOptions(shutdown_timeout_seconds=0.08),
    )
    MigrationRunner(database, data_root / "backups").upgrade_to_head()
    connector = MockConnector()
    summary = DownloadJobService(database).create_selected_jobs(
        connector=connector.metadata,
        remote_series_id="mock-series-1",
        series_metadata=connector.normalize_metadata(await connector.get_series("mock-series-1")),
        chapters=await connector.get_chapters("mock-series-1"),
        selected_remote_ids=("mock-chapter-1",),
    )[0]

    class HoldingRunner:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.returned = asyncio.Event()

        async def run_job(self, job_id: UUID) -> object:
            repository = DownloadJobRepository()
            with database.write_session() as session:
                current = repository.get(session, job_id)
                assert current is not None
                active = repository.transition_status(
                    session,
                    current_job=current,
                    target_status=DownloadJobStatus.FETCHING_METADATA,
                    changed_at=current.updated_at,
                )
                assert active is not None
                downloading = repository.transition_status(
                    session,
                    current_job=active,
                    target_status=DownloadJobStatus.DOWNLOADING,
                    changed_at=active.updated_at,
                )
                assert downloading is not None
            self.started.set()
            await self.release.wait()
            self.returned.set()
            return object()

    runner = HoldingRunner()
    recovery = RecoveryCoordinator(database, database.data_root_identity)
    settlement_calls: list[UUID] = []
    settlement_observed = asyncio.Event()
    original_interrupt = recovery.interrupt_quiesced_job

    def record_interrupt(
        job_id: UUID,
        expected_snapshot: DownloadJob,
        *,
        quiescence_ack: WorkerQuiescenceAck,
        recovery_blocked: bool | None = None,
        storage_gate_quiesced: bool = False,
    ) -> None:
        settlement_calls.append(job_id)
        settlement_observed.set()
        original_interrupt(
            job_id,
            expected_snapshot,
            quiescence_ack=quiescence_ack,
            recovery_blocked=recovery_blocked,
            storage_gate_quiesced=storage_gate_quiesced,
        )

    monkeypatch.setattr(recovery, "interrupt_quiesced_job", record_interrupt)
    wait_timeout_observed = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_wait = database.storage_gate.wait_for_quiescence

    def observe_wait_timeout(timeout_seconds: float) -> bool:
        quiesced = original_wait(timeout_seconds)
        if not quiesced:
            loop.call_soon_threadsafe(wait_timeout_observed.set)
        return quiesced

    monkeypatch.setattr(
        database.storage_gate,
        "wait_for_quiescence",
        observe_wait_timeout,
    )
    dispatcher = DownloadDispatcher(
        database=database,
        worker=runner,
        recovery_coordinator=recovery,
        poll_interval_seconds=0.05,
    )
    job_id = summary.job.job_id
    try:
        await dispatcher.start()
        await asyncio.wait_for(runner.started.wait(), timeout=2)
        cancelled = DownloadJobService(database).cancel_job(
            job_id,
            now=datetime.now(UTC),
        )
        assert cancelled is not None
        cancellation_requested_at = cancelled.job.cancel_requested_at
        assert cancellation_requested_at is not None

        with database.storage_gate.mutation():
            database.storage_gate.close("FILESYSTEM_DISK_FULL")
            runner.release.set()
            await asyncio.wait_for(runner.returned.wait(), timeout=2)
            await asyncio.wait_for(wait_timeout_observed.wait(), timeout=2)
            assert settlement_calls == []
            with database.read_session() as session:
                before_ack = DownloadJobRepository().get(session, job_id)
            assert before_ack is not None
            assert before_ack.status is DownloadJobStatus.DOWNLOADING
            assert before_ack.cancel_requested_at == cancellation_requested_at

        await asyncio.wait_for(settlement_observed.wait(), timeout=2)
        await asyncio.sleep(0.12)
        with database.read_session() as session:
            settled = DownloadJobRepository().get(session, job_id)
        assert settled is not None
        assert settled.status is DownloadJobStatus.INTERRUPTED
        assert settled.interrupted_from_status is DownloadJobStatus.DOWNLOADING
        assert settled.cancel_requested_at == cancellation_requested_at
        assert settled.recovery_blocked is True
        assert settlement_calls == [job_id]
    finally:
        runner.release.set()
        await dispatcher.stop()
        database.shutdown()
