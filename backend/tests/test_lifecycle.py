import asyncio
import errno
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from support.sqlite import sqlite_test_context

from app import lifecycle
from app.connectors.http_client import ConnectorHttpSession
from app.domain import (
    Chapter,
    ChapterSource,
    DownloadJob,
    DownloadJobStatus,
    Series,
    Source,
    SourceHealthStatus,
)
from app.domain.job_pause import (
    JobPauseController,
    JobPauseState,
    PauseCheckpointOutcome,
    PauseReason,
    PauseSafetyEvidence,
    UnknownActiveJob,
)
from app.factory import create_app
from app.persistence.database import DatabaseMaintenanceReport, SQLiteDatabase
from app.persistence.disk_budget import DiskBudgetUnavailableError
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.download_job import download_job_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.migrations.runner import MigrationReport, MigrationRunner
from app.persistence.repositories.download_job import DownloadJobRepository
from app.persistence.windows_paths import DataRootIdentity
from app.runtime import RuntimeState
from app.secrets.errors import SecretVaultError
from app.services.cbz_builder import MAX_CBZ_BYTES_V1
from app.services.download_dispatcher import DownloadDispatcher
from app.services.download_transport import MAX_JOB_TEMPORARY_BYTES
from app.services.job_recovery import RecoveryCoordinator
from app.services.reader_asset import ReaderAssetLimits, ReaderAssetService
from app.services.resource_monitor import ResourceMonitorService
from app.settings import Settings
from app.single_instance import WindowsMutexGuard
from app.tracing import current_trace_id


class NoopInstanceGuard(WindowsMutexGuard):
    def acquire(self) -> None:
        return

    def release(self) -> None:
        return


def test_missing_fmd2_keeps_library_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target_app = create_app(
        Settings(app_env="test", data_root=tmp_path, fmd2_executable=tmp_path / "missing-fmd.exe")
    )
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)

    async def exercise() -> None:
        async with target_app.router.lifespan_context(target_app):
            async with AsyncClient(
                transport=ASGITransport(app=target_app), base_url="http://testserver"
            ) as client:
                assert (await client.get("/api/v1/library")).status_code == 200
                runtime = cast(RuntimeState, target_app.state.runtime)
                assert runtime.ready

    asyncio.run(exercise())


def test_unsafe_reader_cache_keeps_local_library_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.online_reader import OnlineReaderSessionService
    from app.services.reader_asset import ReaderAssetConflict
    from app.services.reader_session import ReaderSessionService

    target_app = create_app(
        Settings(app_env="test", data_root=tmp_path, fmd2_executable=tmp_path / "missing-fmd.exe")
    )
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)

    def refuse_recovery(_service: ReaderSessionService) -> None:
        raise ReaderAssetConflict("Ambiguous cache ownership")

    monkeypatch.setattr(ReaderSessionService, "recover", refuse_recovery)

    async def exercise() -> None:
        async with target_app.router.lifespan_context(target_app):
            runtime = cast(RuntimeState, target_app.state.runtime)
            assert runtime.ready and runtime.reader_asset_service is not None
            assert runtime.reader_progress_service is not None
            assert isinstance(runtime.reader_session_service, OnlineReaderSessionService)
            async with AsyncClient(
                transport=ASGITransport(app=target_app), base_url="http://testserver"
            ) as client:
                assert (await client.get("/api/v1/library")).status_code == 200
                assert (
                    await client.post(f"/api/v1/reader/{uuid4()}/session", json={})
                ).status_code == 404

    asyncio.run(exercise())


def test_startup_disk_preflight_fails_before_database_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = cast(RuntimeState, target_app.state.runtime)
    migration_calls = 0
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)

    class UnavailableBudget:
        def __init__(self, database: SQLiteDatabase) -> None:
            self.database = database

        @staticmethod
        def snapshot() -> Any:
            return SimpleNamespace(
                safe=False,
                volume_serial=None,
                free_bytes=10,
                required_bytes=100,
                reason="INSUFFICIENT_SPACE",
                recovery_demand_bytes=None,
                reserved_bytes=0,
            )

    def fail_if_migration_runs(_runner: MigrationRunner) -> MigrationReport:
        nonlocal migration_calls
        migration_calls += 1
        raise AssertionError("migration must not run without a safe disk budget")

    monkeypatch.setattr(lifecycle, "DiskBudgetManager", UnavailableBudget)
    monkeypatch.setattr(MigrationRunner, "upgrade_to_head", fail_if_migration_runs)

    async def failed_startup() -> None:
        async with target_app.router.lifespan_context(target_app):
            raise AssertionError("an unsafe startup budget must prevent readiness")

    with pytest.raises(DiskBudgetUnavailableError):
        asyncio.run(failed_startup())

    assert migration_calls == 0
    assert runtime.ready is False
    assert runtime.database is None
    assert runtime.disk_budget_manager is None


def test_current_database_can_start_below_download_space_margin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.persistence.disk_budget import DiskBudgetManager
    from app.services.online_reader import OnlineReaderSessionService
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)

    async def bootstrap() -> None:
        app = create_app(Settings(app_env="test", data_root=tmp_path))
        async with app.router.lifespan_context(app):
            assert cast(RuntimeState, app.state.runtime).ready

    asyncio.run(bootstrap())
    def low_space_budget(database: SQLiteDatabase) -> DiskBudgetManager:
        return DiskBudgetManager(
            database, disk_usage_provider=lambda _path: SimpleNamespace(
                total=1_000_000_000, used=1_000_000_000 - 64 * 1024 * 1024, free=64 * 1024 * 1024
            )
        )

    monkeypatch.setattr(lifecycle, "DiskBudgetManager", low_space_budget)

    async def low_space_startup() -> None:
        app = create_app(Settings(app_env="test", data_root=tmp_path))
        async with app.router.lifespan_context(app):
            runtime = cast(RuntimeState, app.state.runtime)
            assert runtime.ready
            assert isinstance(runtime.reader_session_service, OnlineReaderSessionService)
            assert runtime.disk_budget_manager is not None
            assert not runtime.disk_budget_manager.snapshot().safe
            with pytest.raises(DiskBudgetUnavailableError):
                runtime.disk_budget_manager.reserve(uuid4())

    asyncio.run(low_space_startup())


def test_startup_marks_runtime_ready_only_after_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = cast(RuntimeState, target_app.state.runtime)
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)
    assert runtime.reader_asset_service is None
    opened_http_sessions: list[ConnectorHttpSession] = []

    async def exercise_lifespan() -> None:
        transport = ASGITransport(app=target_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/v1/system/readiness")
            assert response.status_code == 503
            assert response.json() == {"status": "not_ready"}
        async with target_app.router.lifespan_context(target_app):
            assert runtime.ready is True
            assert runtime.data_root_identity is not None
            assert runtime.database is not None
            assert runtime.database.data_root_identity is runtime.data_root_identity
            storage_gate = runtime.database.storage_gate
            assert runtime.recovery_coordinator is not None
            assert runtime.recovery_coordinator.manifest_store.storage_gate is storage_gate
            assert runtime.recovery_coordinator.temporary_page_store.storage_gate is storage_gate
            assert runtime.download_worker is not None
            assert runtime.secret_vault is not None
            assert vars(runtime.secret_vault)["_storage_gate"] is storage_gate
            worker_components: dict[str, Any] = vars(runtime.download_worker)
            assert worker_components["_cbz_builder"].storage_gate is storage_gate
            publisher_components = cast(
                dict[str, Any],
                vars(worker_components["_file_publisher"]),
            )
            assert publisher_components["_storage_gate"] is storage_gate
            assert runtime.connector_http_session is not None
            assert runtime.connector_http_session.is_closed is False
            opened_http_sessions.append(runtime.connector_http_session)
            assert isinstance(runtime.reader_asset_service, ReaderAssetService)
        assert runtime.ready is False
        assert runtime.reader_asset_service is None
        assert runtime.connector_http_session is None

    asyncio.run(exercise_lifespan())

    assert runtime.data_root_identity is not None
    assert len(opened_http_sessions) == 1
    assert opened_http_sessions[0].is_closed is True


def test_vault_disk_full_closes_runtime_gate_and_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = cast(RuntimeState, target_app.state.runtime)
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)

    class TestProtector:
        def protect(self, data: bytes, *, entropy: bytes) -> bytes:
            return b"test-cipher\0" + len(data).to_bytes(4, "big") + entropy[:1]

        def unprotect(self, data: bytes, *, entropy: bytes) -> bytes:
            return data

    async def exercise_lifespan() -> None:
        async with target_app.router.lifespan_context(target_app):
            assert runtime.database is not None
            assert runtime.secret_vault is not None
            vars(runtime.secret_vault)["_protector"] = TestProtector()

            def fail_fsync(_descriptor: int) -> None:
                raise OSError(errno.ENOSPC, "disk full")

            with monkeypatch.context() as disk_full:
                disk_full.setattr("app.secrets.vault.os.fsync", fail_fsync)
                with pytest.raises(SecretVaultError) as error:
                    runtime.secret_vault.store(b"runtime-vault-fixture")

            assert error.value.code.value == "AUTH_SECRET_STORE_FAILED"
            assert runtime.database.storage_gate.closed is True

            attempted_temp_files: list[bool] = []

            def record_temp_file_attempt(*_args: object, **_kwargs: object) -> None:
                attempted_temp_files.append(True)

            with monkeypatch.context() as closed_gate:
                closed_gate.setattr(
                    "app.secrets.vault.tempfile.mkstemp",
                    record_temp_file_attempt,
                )
                with pytest.raises(SecretVaultError):
                    runtime.secret_vault.store(b"must-not-start-another-write")
            assert attempted_temp_files == []

            transport = ASGITransport(app=target_app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/api/v1/system/readiness")
            assert response.status_code == 503
            assert response.json() == {"status": "not_ready"}

    asyncio.run(exercise_lifespan())


def test_download_composition_uses_approved_adr_0086_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = cast(RuntimeState, target_app.state.runtime)
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)

    async def inspect_runtime_policy() -> None:
        async with target_app.router.lifespan_context(target_app):
            assert runtime.download_transport is not None
            policy = runtime.download_transport._policy  # pyright: ignore[reportPrivateUsage]
            assert (
                policy.max_bytes,
                policy.chunk_size,
                policy.connect_timeout_seconds,
                policy.read_timeout_seconds,
                policy.write_timeout_seconds,
                policy.pool_timeout_seconds,
                policy.max_redirects,
                policy.max_retry_after_seconds,
                policy.global_concurrency,
                policy.per_host_concurrency,
            ) == (67_108_864, 65_536, 10, 30, 10, 10, 5, 60, 2, 2)
            timeouts = runtime.download_transport._timeouts  # pyright: ignore[reportPrivateUsage]
            assert (timeouts.connect, timeouts.read, timeouts.write, timeouts.pool) == (
                10,
                30,
                10,
                10,
            )
            assert runtime.download_worker is not None
            image_policy = runtime.download_worker._image_validation_policy  # pyright: ignore[reportPrivateUsage]
            assert (
                image_policy.max_bytes,
                image_policy.max_width,
                image_policy.max_height,
                image_policy.max_pixels,
            ) == (67_108_864, 40_000, 40_000, 100_000_000)

    asyncio.run(inspect_runtime_policy())


def test_download_composition_uses_approved_adr_0088_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = cast(RuntimeState, target_app.state.runtime)
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)

    async def inspect_runtime_limits() -> None:
        async with target_app.router.lifespan_context(target_app):
            assert runtime.download_transport is not None
            assert runtime.download_worker is not None
            components = vars(runtime.download_worker)
            assert components["_transport"] is runtime.download_transport
            builder = components["_cbz_builder"]
            assert builder._max_archive_bytes == MAX_CBZ_BYTES_V1 == 2_147_483_648
            assert MAX_JOB_TEMPORARY_BYTES == 2_147_483_648
            assert builder._max_archive_bytes + MAX_JOB_TEMPORARY_BYTES == 4_294_967_296

    asyncio.run(inspect_runtime_limits())


def test_normal_startup_serves_empty_library_from_runtime_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_app = create_app(
        Settings(
            app_env="test",
            data_root=tmp_path,
            library_root=tmp_path / "Documents" / "Mangas",
        )
    )
    runtime = cast(RuntimeState, target_app.state.runtime)
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)

    async def request_library() -> Response:
        async with target_app.router.lifespan_context(target_app):
            transport = ASGITransport(app=target_app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/api/v1/library")
                assert runtime.ready is True
                assert runtime.database is not None
                assert runtime.library_service is not None
                assert runtime.download_job_service is not None
                assert runtime.metadata_service is not None
                assert runtime.reader_asset_service is not None
                assert runtime.usb_export_job_service is not None
                assert runtime.library_root_identity is not None
                assert runtime.library_root_identity.canonical_path == (
                    tmp_path / "Documents" / "Mangas"
                )
                export_service = vars(runtime.usb_export_job_service)["_exporter"]
                assert vars(export_service)["_library_root"] == (tmp_path / "Documents" / "Mangas")
                assert runtime.secret_vault is not None
                readiness = await client.get("/api/v1/system/readiness")
                assert readiness.status_code == 200
                assert readiness.json() == {"status": "ready"}
                return response

    response = asyncio.run(request_library())

    assert response.status_code == 200
    assert response.json()["data"] == []
    assert response.json()["meta"]["total"] == 0
    assert runtime.ready is False
    assert runtime.database is None
    assert runtime.library_service is None
    assert runtime.download_job_service is None
    assert runtime.metadata_service is None
    assert runtime.reader_asset_service is None
    assert runtime.secret_vault is None
    assert (tmp_path / "database" / "yomira.sqlite3").is_file()
    log_content = (tmp_path / "logs" / "yomira.log").read_text(encoding="utf-8")
    assert '"message":"system.startup_complete"' in log_content


def test_startup_reconciles_stale_jobs_and_preserves_pending_intent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    database_context = sqlite_test_context(tmp_path)
    database = database_context.open()
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()

    series = Series.new_local("Series", "Series", now=now)
    chapter = Chapter.new_local(
        series.series_id,
        "1",
        "Chapter 1",
        chapter_sort_key=(1, 0, 0),
    )
    source = Source.new_local(
        "connector",
        "v1",
        "Connector",
        True,
        SourceHealthStatus.HEALTHY,
        now=now,
    )
    chapter_source = ChapterSource.new_local(
        chapter.chapter_id,
        source.source_id,
        remote_id="remote-chapter-1",
        available=True,
        last_seen_at=now,
    )
    active_job = DownloadJob.new_local(chapter_source.chapter_source_id, now=now)
    requeued_job = DownloadJob.new_local(chapter_source.chapter_source_id, now=now)
    queued_job = DownloadJob.new_local(chapter_source.chapter_source_id, now=now)
    failed_job = DownloadJob.new_local(chapter_source.chapter_source_id, now=now)
    repository = DownloadJobRepository()

    with database.write_session() as session:
        session.add_all([series_to_row(series), source_to_row(source)])
        session.flush()
        session.add(chapter_to_row(chapter))
        session.flush()
        session.add(chapter_source_to_row(chapter_source))
        session.flush()
        session.add_all(
            [
                download_job_to_row(active_job),
                download_job_to_row(requeued_job),
                download_job_to_row(queued_job),
                download_job_to_row(failed_job),
            ]
        )

    with database.write_session() as session:
        fetching = repository.transition_status(
            session,
            current_job=active_job,
            target_status=DownloadJobStatus.FETCHING_METADATA,
            changed_at=now + timedelta(seconds=1),
        )
    assert fetching is not None
    with database.write_session() as session:
        downloading = repository.transition_status(
            session,
            current_job=fetching,
            target_status=DownloadJobStatus.DOWNLOADING,
            changed_at=now + timedelta(seconds=2),
        )
    assert downloading is not None
    with database.write_session() as session:
        requeue_fetching = repository.transition_status(
            session,
            current_job=requeued_job,
            target_status=DownloadJobStatus.FETCHING_METADATA,
            changed_at=now + timedelta(seconds=1),
        )
    assert requeue_fetching is not None
    with database.write_session() as session:
        requeue_downloading = repository.transition_status(
            session,
            current_job=requeue_fetching,
            target_status=DownloadJobStatus.DOWNLOADING,
            changed_at=now + timedelta(seconds=2),
        )
    assert requeue_downloading is not None
    with database.write_session() as session:
        pending = repository.request_cancellation(
            session,
            current_job=downloading,
            requested_at=now + timedelta(seconds=3),
        )
    assert pending is not None
    with database.write_session() as session:
        failed = repository.transition_status(
            session,
            current_job=failed_job,
            target_status=DownloadJobStatus.FAILED,
            changed_at=now + timedelta(seconds=1),
        )
    assert failed is not None
    database.shutdown()

    target_app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = cast(RuntimeState, target_app.state.runtime)
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)

    recovered_snapshot: DownloadJob | None = None
    requeued_snapshot: DownloadJob | None = None

    async def inspect_recovered_database() -> None:
        nonlocal recovered_snapshot, requeued_snapshot
        async with target_app.router.lifespan_context(target_app):
            assert runtime.database is not None
            with runtime.database.read_session() as session:
                recovered_active = repository.get(session, active_job.job_id)
                recovered_requeued = repository.get(session, requeued_job.job_id)
                recovered_queued = repository.get(session, queued_job.job_id)
                recovered_failed = repository.get(session, failed_job.job_id)
            assert recovered_active is not None
            assert recovered_active.status is DownloadJobStatus.INTERRUPTED
            assert recovered_active.cancel_requested_at == pending.cancel_requested_at
            assert recovered_active.interrupted_from_status is DownloadJobStatus.DOWNLOADING
            assert recovered_active.recovery_blocked is True
            assert recovered_requeued is not None
            assert recovered_requeued.status is DownloadJobStatus.QUEUED
            assert recovered_requeued.interrupted_from_status is DownloadJobStatus.DOWNLOADING
            assert recovered_requeued.recovery_blocked is False
            assert recovered_queued == queued_job
            assert recovered_failed == failed
            if recovered_snapshot is None:
                recovered_snapshot = recovered_active
            else:
                assert recovered_active == recovered_snapshot
            if requeued_snapshot is None:
                requeued_snapshot = recovered_requeued
            else:
                assert recovered_requeued == requeued_snapshot

    asyncio.run(inspect_recovered_database())
    asyncio.run(inspect_recovered_database())

    events = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    recovery_event = next(
        event
        for event in events
        if event["message"] == "job.recovery.stale_active_jobs_interrupted"
    )
    assert recovery_event["observed_value"] == 2
    assert recovery_event["reason"] == "PROCESS_RESTART"


def test_reader_service_is_created_after_migrations_and_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = cast(RuntimeState, target_app.state.runtime)
    events: list[str] = []
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)
    original_upgrade = MigrationRunner.upgrade_to_head
    original_reconcile = lifecycle.RecoveryCoordinator.reconcile_startup

    def track_upgrade(runner: MigrationRunner) -> MigrationReport:
        report = original_upgrade(runner)
        events.append("migrated")
        return report

    def track_recovery(coordinator: RecoveryCoordinator) -> int:
        assert events == ["migrated"]
        count = original_reconcile(coordinator)
        events.append("recovered")
        return count

    def create_reader_service(
        database: SQLiteDatabase,
        *,
        data_root_identity: DataRootIdentity,
        library_root_identity: DataRootIdentity,
        limits: ReaderAssetLimits,
    ) -> ReaderAssetService:
        assert events == ["migrated", "recovered"]
        assert limits == ReaderAssetLimits(
            max_cbz_bytes=2_147_483_648,
            max_page_bytes=67_108_864,
            chunk_bytes=65_536,
        )
        events.append("reader_initialized")
        return ReaderAssetService(
            database,
            data_root_identity=data_root_identity,
            library_root_identity=library_root_identity,
            limits=limits,
        )

    monkeypatch.setattr(MigrationRunner, "upgrade_to_head", track_upgrade)
    monkeypatch.setattr(
        lifecycle.RecoveryCoordinator,
        "reconcile_startup",
        track_recovery,
    )
    monkeypatch.setattr(lifecycle, "ReaderAssetService", create_reader_service, raising=False)

    async def exercise_lifespan() -> None:
        assert runtime.reader_asset_service is None
        async with target_app.router.lifespan_context(target_app):
            assert runtime.reader_asset_service is not None
            assert runtime.ready is True
        assert runtime.reader_asset_service is None

    asyncio.run(exercise_lifespan())

    assert events == ["migrated", "recovered", "reader_initialized"]


def test_ten_startup_shutdown_cycles_release_readiness_and_instance_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = cast(RuntimeState, target_app.state.runtime)
    mutex_name = f"Local\\Yomira.V1.Backend.Tests.{uuid4()}"
    monkeypatch.setattr(
        lifecycle,
        "create_instance_guard",
        lambda: WindowsMutexGuard(mutex_name),
    )

    async def exercise_lifecycles() -> None:
        for _ in range(10):
            async with target_app.router.lifespan_context(target_app):
                assert runtime.ready is True
            assert runtime.ready is False

    asyncio.run(exercise_lifecycles())


def test_shutdown_disables_readiness_before_cleaning_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_app = create_app(Settings(app_env="test"))
    runtime = cast(RuntimeState, target_app.state.runtime)
    shutdown_calls: list[bool] = []

    async def observe_shutdown(app: FastAPI) -> None:
        runtime_state = cast(RuntimeState, app.state.runtime)
        shutdown_calls.append(runtime_state.ready)

    async def skip_initialization(_app: FastAPI) -> None:
        return

    monkeypatch.setattr(lifecycle, "initialize_runtime", skip_initialization)
    monkeypatch.setattr(lifecycle, "shutdown_runtime", observe_shutdown)
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)

    async def exercise_lifespan() -> None:
        async with target_app.router.lifespan_context(target_app):
            assert runtime.ready is True
        assert runtime.ready is False

    asyncio.run(exercise_lifespan())

    assert shutdown_calls == [False]


def test_shutdown_blocks_admission_stops_monitor_then_drains_paused_jobs(
    tmp_path: Path,
) -> None:
    target_app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = cast(RuntimeState, target_app.state.runtime)
    events: list[str] = []
    job_id = uuid4()
    controller = JobPauseController()

    class HoldingDispatcher:
        active_job_ids = (job_id,)

        def request_stop(self) -> None:
            events.append("admission_stopped")

        async def stop(self) -> None:
            assert events == ["admission_stopped", "monitor_stopped"]
            assert controller.pause_reasons(job_id) == frozenset({PauseReason.USER})
            events.append("dispatcher_drained")

    class HoldingMonitor:
        async def stop(self) -> None:
            assert events == ["admission_stopped"]
            events.append("monitor_stopped")

    async def exercise_shutdown() -> None:
        await controller.register(job_id)
        controller.request_pause(job_id, reason=PauseReason.USER)
        controller.request_pause(job_id, reason=PauseReason.BACKPRESSURE)
        runtime.download_dispatcher = cast(DownloadDispatcher, HoldingDispatcher())
        runtime.resource_monitor = cast(ResourceMonitorService, HoldingMonitor())
        runtime.job_pause_controller = controller
        await lifecycle.shutdown_runtime(target_app)

    asyncio.run(exercise_shutdown())

    assert events == ["admission_stopped", "monitor_stopped", "dispatcher_drained"]
    assert runtime.download_dispatcher is None
    assert runtime.resource_monitor is None


def test_shutdown_resumes_real_dispatcher_worker_paused_by_backpressure(
    tmp_path: Path,
) -> None:
    target_app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = cast(RuntimeState, target_app.state.runtime)
    database_context = sqlite_test_context(tmp_path)
    database = database_context.open()
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    now = datetime.now(UTC)
    series = Series.new_local("Series", "Series", now=now)
    chapter = Chapter.new_local(
        series.series_id,
        "1",
        "Chapter 1",
        chapter_sort_key=(1, 0, 0),
    )
    source = Source.new_local(
        "connector",
        "v1",
        "Connector",
        True,
        SourceHealthStatus.HEALTHY,
        now=now,
    )
    chapter_source = ChapterSource.new_local(
        chapter.chapter_id,
        source.source_id,
        remote_id="remote-chapter-1",
        available=True,
        last_seen_at=now,
    )
    job = DownloadJob.new_local(chapter_source.chapter_source_id, now=now)
    with database.write_session() as session:
        session.add_all([series_to_row(series), source_to_row(source)])
        session.flush()
        session.add(chapter_to_row(chapter))
        session.flush()
        session.add(chapter_source_to_row(chapter_source))
        session.flush()
        session.add(download_job_to_row(job))

    controller = JobPauseController()
    recovery = RecoveryCoordinator(database, database_context.data_root_identity)

    class BackpressurePausedRunner:
        def __init__(self) -> None:
            self.at_checkpoint = asyncio.Event()
            self.resumed = asyncio.Event()
            self.status_on_resume: DownloadJobStatus | None = None
            self.final_status: DownloadJobStatus | None = None

        async def run_job(self, job_id: UUID) -> object:
            assert controller.state(job_id) is JobPauseState.RUNNING
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
            controller.request_pause(job_id, reason=PauseReason.BACKPRESSURE)
            self.at_checkpoint.set()
            outcome = await controller.pause_if_requested(
                job_id,
                status=DownloadJobStatus.FETCHING_METADATA,
                evidence=PauseSafetyEvidence(network_operation_inactive=True),
            )
            assert outcome is PauseCheckpointOutcome.RESUMED
            self.resumed.set()
            with database.read_session() as session:
                current = repository.get(session, job_id)
            assert current is not None
            self.status_on_resume = current.status
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
                self.final_status = failed.status
            return failed

    class StoppedMonitor:
        async def stop(self) -> None:
            return

    runner = BackpressurePausedRunner()
    dispatcher = DownloadDispatcher(
        database=database,
        worker=runner,
        recovery_coordinator=recovery,
        pause_controller=controller,
        admission_allowed=lambda: True,
        poll_interval_seconds=0.05,
    )
    runtime.database = database
    runtime.download_dispatcher = dispatcher
    runtime.job_pause_controller = controller
    runtime.resource_monitor = cast(ResourceMonitorService, StoppedMonitor())

    async def exercise_shutdown() -> None:
        await dispatcher.start()
        try:
            await asyncio.wait_for(runner.at_checkpoint.wait(), timeout=2)

            async def wait_until_paused() -> None:
                while controller.state(job.job_id) is not JobPauseState.PAUSED:
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_until_paused(), timeout=2)
            with database.read_session() as session:
                paused_job = DownloadJobRepository().get(session, job.job_id)
            assert paused_job is not None
            assert paused_job.status is DownloadJobStatus.FETCHING_METADATA

            await asyncio.wait_for(lifecycle.shutdown_runtime(target_app), timeout=2)
            assert runner.resumed.is_set()
            assert runner.status_on_resume is DownloadJobStatus.FETCHING_METADATA
            assert runner.final_status is DownloadJobStatus.FAILED
            assert runtime.download_dispatcher is None
            assert runtime.database is None
        finally:
            if dispatcher.active_job_ids:
                for active_job_id in dispatcher.active_job_ids:
                    try:
                        reasons = controller.pause_reasons(active_job_id)
                    except UnknownActiveJob:
                        continue
                    for reason in (
                        PauseReason.BACKPRESSURE,
                        PauseReason.MONITOR_UNAVAILABLE,
                    ):
                        if reason in reasons:
                            controller.resume(active_job_id, reason=reason)
            await dispatcher.stop()
            if not database.is_closed:
                database.shutdown()

    asyncio.run(exercise_shutdown())


def test_startup_failure_is_traced_and_never_marks_runtime_ready(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_app = create_app(Settings(app_env="test"))
    runtime = cast(RuntimeState, target_app.state.runtime)
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)

    async def fail_initialization(app: FastAPI) -> None:
        raise RuntimeError("private-startup-detail")

    monkeypatch.setattr(lifecycle, "initialize_runtime", fail_initialization)

    async def run_lifespan() -> None:
        async with target_app.router.lifespan_context(target_app):
            raise AssertionError("Startup failure must prevent entry into the application.")

    with pytest.raises(RuntimeError, match="private-startup-detail"):
        asyncio.run(run_lifespan())

    captured = capsys.readouterr()
    event = json.loads(captured.err.strip())
    assert event["error_code"] == "SYS_STARTUP_FAILED"
    assert event["exception_type"] == "RuntimeError"
    assert "private-startup-detail" not in captured.err
    assert runtime.ready is False


def test_startup_and_cleanup_failures_share_a_trace_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert current_trace_id() is None
    target_app = create_app(Settings(app_env="test"))

    class IsolatedTestGuard(WindowsMutexGuard):
        def acquire(self) -> None:
            return

        def release(self) -> None:
            return

    monkeypatch.setattr(lifecycle, "create_instance_guard", IsolatedTestGuard)

    async def fail_initialization(_app: FastAPI) -> None:
        raise RuntimeError("private-startup-detail")

    async def fail_cleanup(_app: FastAPI) -> None:
        raise RuntimeError("private-cleanup-detail")

    monkeypatch.setattr(lifecycle, "initialize_runtime", fail_initialization)
    monkeypatch.setattr(lifecycle, "shutdown_runtime", fail_cleanup)

    async def run_lifespan() -> None:
        async with target_app.router.lifespan_context(target_app):
            raise AssertionError("Startup failure must prevent entry into the application.")

    with pytest.raises(RuntimeError, match="private-startup-detail"):
        asyncio.run(run_lifespan())

    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.err.strip().splitlines()]
    startup_event = next(event for event in events if event["message"] == "system.startup_failed")
    cleanup_event = next(
        event for event in events if event["message"] == "system.startup_cleanup_failed"
    )
    trace_ids = {startup_event["trace_id"], cleanup_event["trace_id"]}
    assert len(trace_ids) == 1
    assert all(isinstance(trace_id, str) and trace_id.strip() for trace_id in trace_ids)
    assert "private-startup-detail" not in captured.err
    assert "private-cleanup-detail" not in captured.err


def test_migration_failure_closes_runtime_and_allows_startup_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = cast(RuntimeState, target_app.state.runtime)
    upgrade_calls = 0
    original_upgrade = MigrationRunner.upgrade_to_head
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)

    def fail_once(runner: MigrationRunner) -> MigrationReport:
        nonlocal upgrade_calls
        upgrade_calls += 1
        if upgrade_calls == 1:
            raise RuntimeError("private-migration-detail")
        return original_upgrade(runner)

    monkeypatch.setattr(MigrationRunner, "upgrade_to_head", fail_once)

    async def failed_startup() -> None:
        async with target_app.router.lifespan_context(target_app):
            raise AssertionError("A failed migration must prevent readiness.")

    with pytest.raises(RuntimeError, match="private-migration-detail"):
        asyncio.run(failed_startup())

    assert runtime.ready is False
    assert runtime.database is None
    assert runtime.library_service is None
    assert runtime.metadata_service is None
    assert runtime.reader_asset_service is None
    identity_before_retry = runtime.data_root_identity
    assert identity_before_retry is not None
    assert target_app.state.instance_guard is None
    captured = capsys.readouterr()
    assert "private-migration-detail" not in captured.err
    assert str(tmp_path) not in captured.err

    async def retried_startup() -> None:
        async with target_app.router.lifespan_context(target_app):
            assert runtime.ready is True
            assert runtime.database is not None
            assert runtime.data_root_identity is identity_before_retry
            assert runtime.database.data_root_identity is identity_before_retry
            assert runtime.library_service is not None
            assert runtime.download_job_service is not None
            assert runtime.metadata_service is not None
            assert isinstance(runtime.reader_asset_service, ReaderAssetService)

    asyncio.run(retried_startup())

    assert upgrade_calls == 2
    assert runtime.ready is False
    assert runtime.database is None
    assert runtime.reader_asset_service is None


def test_shutdown_failure_is_traced_and_leaves_runtime_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_app = create_app(Settings(app_env="test"))
    runtime = cast(RuntimeState, target_app.state.runtime)
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)

    async def fail_shutdown(app: FastAPI) -> None:
        raise RuntimeError("private-shutdown-detail")

    async def skip_initialization(_app: FastAPI) -> None:
        return

    monkeypatch.setattr(lifecycle, "initialize_runtime", skip_initialization)
    monkeypatch.setattr(lifecycle, "shutdown_runtime", fail_shutdown)

    async def exercise_lifespan() -> None:
        async with target_app.router.lifespan_context(target_app):
            assert runtime.ready is True

    with pytest.raises(RuntimeError, match="private-shutdown-detail"):
        asyncio.run(exercise_lifespan())

    captured = capsys.readouterr()
    event = json.loads(captured.err.strip().splitlines()[-1])
    assert event["error_code"] == "SYS_INTERNAL_ERROR"
    assert event["message"] == "system.shutdown_failed"
    assert "private-shutdown-detail" not in captured.err
    assert runtime.ready is False


def test_database_maintenance_finishes_before_instance_guard_is_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class TrackingGuard(WindowsMutexGuard):
        held = False

        def acquire(self) -> None:
            self.held = True
            events.append("mutex_acquired")

        def release(self) -> None:
            events.append("mutex_released")
            self.held = False

    guard = TrackingGuard()
    monkeypatch.setattr(lifecycle, "create_instance_guard", lambda: guard)
    target_app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = cast(RuntimeState, target_app.state.runtime)

    database_context = sqlite_test_context(tmp_path)
    runtime.data_root_identity = database_context.data_root_identity
    runtime.database = SQLiteDatabase(
        database_context.database_path,
        data_root_identity=database_context.data_root_identity,
    )
    database = runtime.database
    original_shutdown = database.shutdown

    def observe_shutdown() -> DatabaseMaintenanceReport:
        assert runtime.ready is False
        assert guard.held is True
        report = original_shutdown()
        assert database.is_closed is True
        events.append("database_closed")
        return report

    monkeypatch.setattr(database, "shutdown", observe_shutdown)

    async def exercise_lifespan() -> None:
        async with target_app.router.lifespan_context(target_app):
            assert runtime.ready is True
            assert isinstance(runtime.reader_asset_service, ReaderAssetService)
        assert runtime.ready is False
        assert runtime.reader_asset_service is None

    asyncio.run(exercise_lifespan())

    assert events == ["mutex_acquired", "database_closed", "mutex_released"]
