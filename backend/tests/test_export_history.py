from __future__ import annotations

import asyncio
import hashlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from support.sqlite import open_test_database

from app.domain.asset import Asset, AssetType
from app.domain.export_job import ExportJobStatus
from app.errors import ErrorCode
from app.factory import create_app
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.asset import asset_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.asset import AssetRow
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_cbz_asset import ChapterCbzAssetRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.export_job import ExportJobRow
from app.persistence.models.series import SeriesRow
from app.persistence.models.source import SourceRow
from app.persistence.repositories.export_job import (
    ExportIdempotencyConflict,
    ExportJobRepository,
)
from app.runtime import RuntimeState
from app.services.export_jobs import (
    MAX_EXPORT_HISTORY_PAGE_SIZE,
    ExportAssetUnavailable,
    UsbExportJobService,
)
from app.services.usb_export import ExportFailure, ExportResult, ExportTarget, UsbExportService
from app.services.wpd_worker import IsolatedWpdClient
from app.tracing import current_trace_id

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
TEST_BYTES = b"verified CBZ fixture"
TEST_HASH = hashlib.sha256(TEST_BYTES).hexdigest()


def test_history_exposes_live_progress_only_for_active_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, asset, _exporter, service = _make_service(tmp_path)
    job = service.create_queued_job(
        asset.asset_id, target_id="test-usb-01", idempotency_key="live-progress"
    )
    service.mark_exporting(job.export_id)
    def live_progress(trace: str) -> dict[str, str | int]:
        return {
            "stage": "copy",
            "processed_bytes": 4,
            "total_bytes": asset.size_bytes,
        }

    monkeypatch.setattr(service, "progress_for_trace", live_progress)
    app = create_app()
    app.state.runtime.database = database
    app.state.runtime.usb_export_job_service = service

    async def read_progress() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            for path in ["/api/v1/usb-exports", "/api/v1/usb-exports/pages"]:
                response = await client.get(path)
                assert response.status_code == 200
                assert response.json()["data"][0]["progress"] == {
                    "stage": "copy",
                    "processed_bytes": 4,
                    "total_bytes": asset.size_bytes,
                }
            service.mark_failed(
                job.export_id,
                error_code="EXP_COPY_FAILED",
                failure_stage="copy",
                retryable=False,
                cause_type="Test",
            )
            response = await client.get("/api/v1/usb-exports/pages")
            assert response.json()["data"][0]["progress"] is None

    try:
        asyncio.run(read_progress())
    finally:
        database.shutdown()


class SimulatedUsbExporter:
    """Test double: observes durable state and never accesses a real device."""

    def __init__(self, database: SQLiteDatabase, mount_path: Path) -> None:
        self.database = database
        self.mount_path = mount_path
        self.targets = (
            ExportTarget(
                target_id="test-usb-01",
                mount_path=mount_path,
                display_name="Simulated removable drive",
                file_system="exFAT",
                total_bytes=10_000_000,
                free_bytes=9_000_000,
            ),
        )
        self.calls: list[tuple[UUID, str]] = []
        self.row_before_external_write: tuple[str, str] | None = None
        self.result_status = "EXPORTED"
        self.failure: Exception | None = None

    def list_targets(self) -> tuple[ExportTarget, ...]:
        return self.targets

    def export_asset(self, asset: Asset, *, target_id: str) -> ExportResult:
        with self.database.read_session() as session:
            row = session.scalar(
                select(ExportJobRow).where(ExportJobRow.asset_id == str(asset.asset_id))
            )
            assert row is not None
            self.row_before_external_write = (row.export_id, row.status)
        self.calls.append((asset.asset_id, target_id))
        if self.failure is not None:
            raise self.failure
        return ExportResult(
            export_id=uuid4(),
            asset_id=asset.asset_id,
            target_id=target_id,
            destination_name=Path(asset.relative_path).name,
            status=self.result_status,  # type: ignore[arg-type]
            sha256=asset.sha256,
            size_bytes=asset.size_bytes,
            trace_id=current_trace_id() or "00000000-0000-4000-8000-000000000001",
        )


def _make_asset() -> Asset:
    return Asset.new_local(
        AssetType.CBZ,
        "library/chapters/series-a/chapter-1.cbz",
        TEST_HASH,
        len(TEST_BYTES),
        created_at=NOW - timedelta(minutes=1),
        verified_at=NOW - timedelta(minutes=1),
    )


def _make_database(tmp_path: Path) -> SQLiteDatabase:
    database = open_test_database(tmp_path)
    report = MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    assert report.current_revision == "0024_reader_sessions"
    return database


def _make_service(
    tmp_path: Path,
    *,
    now: datetime = NOW,
) -> tuple[SQLiteDatabase, Asset, SimulatedUsbExporter, UsbExportJobService]:
    database = _make_database(tmp_path)
    asset = _make_asset()
    with database.write_session() as session:
        session.add(asset_to_row(asset))
    simulated = SimulatedUsbExporter(database, tmp_path / "simulated-usb")
    service = UsbExportJobService(
        database=database,
        exporter=simulated,
        clock=lambda: now,
    )
    return database, asset, simulated, service


def test_whole_manga_export_over_2_gib_has_durable_bounded_history(tmp_path: Path) -> None:
    database, asset, exporter, service = _make_service(tmp_path)
    size = 6 * 1024 * 1024 * 1024
    try:
        with database.write_session() as session:
            row = session.get(AssetRow, str(asset.asset_id))
            assert row is not None
            row.asset_type = "CBR"
            row.relative_path = "exports/cbr/Vinland Saga.cbr"
            row.size_bytes = size
        job = service.export_asset(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="whole-manga",
        )
        assert job.status == "EXPORTED"
        assert job.expected_size_bytes == size
        assert service.list_jobs(limit=100, offset=0).items[0] == job
        assert exporter.calls == [(asset.asset_id, "test-usb-01")]
        with pytest.raises(IntegrityError):
            with database.write_session() as session:
                row = session.get(ExportJobRow, str(job.export_id))
                assert row is not None
                row.expected_size_bytes = 8 * 1024 * 1024 * 1024 + 1
    finally:
        database.shutdown()


def _insert_ready_chapter(
    database: SQLiteDatabase,
    *,
    asset: Asset,
) -> tuple[UUID, UUID]:
    series_id = uuid4()
    chapter_id = uuid4()
    source_id = uuid4()
    chapter_source_id = uuid4()
    download_job_id = uuid4()
    now_us = int(NOW.timestamp() * 1_000_000)
    with database.write_session() as session:
        session.add(
            SeriesRow(
                series_id=str(series_id),
                canonical_title="Export fixture series",
                display_title="Export fixture series",
                description=None,
                status=None,
                language="es",
                author=None,
                genres_json=None,
                cover_asset_id=None,
                created_at_us=now_us,
                updated_at_us=now_us,
            )
        )
        session.add(
            SourceRow(
                source_id=str(source_id),
                connector_id="test.connector",
                connector_version="1.0.0",
                display_name="Test connector",
                enabled=True,
                health_status="HEALTHY",
                created_at_us=now_us,
                updated_at_us=now_us,
            )
        )
        session.flush()
        session.add(
            ChapterRow(
                chapter_id=str(chapter_id),
                series_id=str(series_id),
                chapter_raw_label="1",
                chapter_major=1,
                chapter_minor=None,
                chapter_variant=None,
                chapter_type=None,
                chapter_sort_key="[1]",
                volume_number=None,
                language="es",
                display_label="Capítulo 1",
            )
        )
        session.add(
            ChapterSourceRow(
                chapter_source_id=str(chapter_source_id),
                chapter_id=str(chapter_id),
                source_id=str(source_id),
                remote_id="chapter-1",
                remote_url=None,
                remote_label="Capítulo 1",
                available=True,
                last_seen_at_us=now_us,
            )
        )
        session.add(
            DownloadJobRow(
                job_id=str(download_job_id),
                chapter_source_id=str(chapter_source_id),
                status="COMPLETED",
                priority="NORMAL",
                created_at_us=now_us,
                updated_at_us=now_us,
                cancel_requested_at_us=None,
                interrupted_from_status=None,
                recovery_blocked=False,
            )
        )
        session.flush()
        session.add(
            ChapterCbzAssetRow(
                chapter_id=str(chapter_id),
                asset_id=str(asset.asset_id),
                download_job_id=str(download_job_id),
                page_count=1,
                indexed_at_us=now_us,
            )
        )
    return series_id, chapter_id


def test_migration_creates_bounded_export_job_table_and_verified_backup(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    try:
        report = MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
        assert report.current_revision == "0024_reader_sessions"
        assert report.backup_path is not None and report.backup_path.is_file()

        with database.read_session() as session:
            columns = {
                column["name"]
                for column in inspect(session.connection()).get_columns("usb_export_jobs")
            }
            indexes = {
                index["name"]
                for index in inspect(session.connection()).get_indexes("usb_export_jobs")
            }
            assert {
                "export_id",
                "idempotency_key_hash",
                "request_fingerprint",
                "asset_id",
                "target_id",
                "target_label",
                "file_system",
                "destination_name",
                "expected_size_bytes",
                "expected_sha256",
                "status",
                "created_at_us",
                "updated_at_us",
                "started_at_us",
                "terminal_at_us",
                "trace_id",
                "error_code",
                "failure_stage",
                "retryable",
                "cause_type",
            }.issubset(columns)
            assert not {"mount_path", "absolute_path", "cbz_bytes", "secret"}.intersection(columns)
            assert "uq_usb_export_jobs_idempotency_key_hash" in indexes
    finally:
        database.shutdown()


def test_export_job_is_committed_before_external_write_and_idempotent(
    tmp_path: Path,
) -> None:
    database, asset, exporter, service = _make_service(tmp_path)
    try:
        first = service.export_asset(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="request-123",
        )
        repeated = service.export_asset(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="request-123",
        )

        assert first.export_id == repeated.export_id
        assert first.status == "EXPORTED"
        assert exporter.row_before_external_write == (str(first.export_id), "EXPORTING")
        assert len(exporter.calls) == 1
        assert first.target_label == "Simulated removable drive"
        assert first.file_system == "exFAT"
        assert UUID(first.trace_id).version == 4
        assert str(tmp_path) not in repr(first)
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(ExportJobRow)) == 1
    finally:
        database.shutdown()


def test_verified_cbr_asset_uses_generic_usb_export_flow(tmp_path: Path) -> None:
    database = _make_database(tmp_path)
    asset = Asset.new_local(
        AssetType.CBR,
        "exports/cbr/Series - Capitulos 1-10.cbr",
        TEST_HASH,
        len(TEST_BYTES),
        created_at=NOW,
        verified_at=NOW,
    )
    with database.write_session() as session:
        session.add(asset_to_row(asset))
    exporter = SimulatedUsbExporter(database, tmp_path / "simulated-usb")
    service = UsbExportJobService(
        database=database,
        exporter=exporter,
        clock=lambda: NOW,
    )
    try:
        result = service.export_asset(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="cbr-export-123",
        )

        assert result.status is ExportJobStatus.EXPORTED
        assert result.destination_name == Path(asset.relative_path).name
        assert result.destination_name.endswith(".cbr")
        assert exporter.calls == [(asset.asset_id, "test-usb-01")]
    finally:
        database.shutdown()


def test_export_is_not_started_when_retention_or_insert_transaction_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, asset, exporter, service = _make_service(tmp_path)
    try:

        def fail_pruning(
            _repository: ExportJobRepository,
            _session: object,
            _now_us: int,
        ) -> None:
            raise OSError("simulated SQLite retention failure")

        monkeypatch.setattr(ExportJobRepository, "_prune_terminal_history", fail_pruning)
        with pytest.raises(OSError, match="retention failure"):
            service.export_asset(
                asset.asset_id,
                target_id="test-usb-01",
                idempotency_key="request-fails-closed",
            )

        assert exporter.calls == []
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(ExportJobRow)) == 0
    finally:
        database.shutdown()


def test_insert_failure_after_retention_pruning_rolls_back_and_never_writes_usb(
    tmp_path: Path,
) -> None:
    database, asset, exporter, service = _make_service(tmp_path)
    expired_export_id = uuid4()
    expired_terminal_at_us = int((NOW - timedelta(days=90)).timestamp() * 1_000_000)
    expired_created_at_us = int((NOW - timedelta(days=91)).timestamp() * 1_000_000)
    failed_insert_key_hash = hashlib.sha256(b"insert-fails-after-pruning").hexdigest()
    try:
        with database.write_session() as session:
            session.add(
                ExportJobRow(
                    export_id=str(expired_export_id),
                    idempotency_key_hash=hashlib.sha256(b"expired-terminal").hexdigest(),
                    request_fingerprint=hashlib.sha256(b"expired-payload").hexdigest(),
                    asset_id=str(asset.asset_id),
                    target_id="test-usb-01",
                    target_label="Simulated removable drive",
                    file_system="exFAT",
                    destination_name="expired-chapter.cbz",
                    expected_size_bytes=len(TEST_BYTES),
                    expected_sha256=TEST_HASH,
                    status="EXPORTED",
                    created_at_us=expired_created_at_us,
                    updated_at_us=expired_terminal_at_us,
                    started_at_us=expired_created_at_us,
                    terminal_at_us=expired_terminal_at_us,
                    trace_id="00000000-0000-4000-8000-000000000002",
                )
            )
            session.flush()
            session.connection().exec_driver_sql(
                f"""
                CREATE TRIGGER fail_usb_export_job_insert_after_retention
                BEFORE INSERT ON usb_export_jobs
                WHEN NOT EXISTS (
                    SELECT 1 FROM usb_export_jobs
                    WHERE export_id = '{expired_export_id}'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'insert failed after retention pruning');
                END;
                """
            )

        with pytest.raises(IntegrityError, match="insert failed after retention pruning"):
            service.export_asset(
                asset.asset_id,
                target_id="test-usb-01",
                idempotency_key="insert-fails-after-pruning",
            )

        assert exporter.calls == []
        assert not exporter.mount_path.exists()
        with database.read_session() as session:
            restored = session.get(ExportJobRow, str(expired_export_id))
            assert restored is not None
            assert restored.status == "EXPORTED"
            assert restored.terminal_at_us == expired_terminal_at_us
            assert session.scalar(select(func.count()).select_from(ExportJobRow)) == 1
            assert (
                session.scalar(
                    select(ExportJobRow.export_id).where(
                        ExportJobRow.idempotency_key_hash == failed_insert_key_hash
                    )
                )
                is None
            )
    finally:
        database.shutdown()


def test_copy_failure_is_persisted_with_traceable_sanitized_cause(
    tmp_path: Path,
) -> None:
    database, asset, exporter, service = _make_service(tmp_path)
    exporter.failure = ExportFailure(
        code=ErrorCode.EXP_DEVICE_DISCONNECTED,
        stage="copy",
        trace_id="00000000-0000-4000-8000-000000000004",
        retryable=True,
        cause_type="DeviceDisconnected",
    )
    try:
        job = service.export_asset(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="disconnect-case",
        )

        assert job.status == "FAILED"
        assert job.error_code == "EXP_DEVICE_DISCONNECTED"
        assert job.failure_stage == "copy"
        assert job.retryable is True
        assert job.cause_type == "DeviceDisconnected"
        assert job.terminal_at is not None
        assert exporter.row_before_external_write == (str(job.export_id), "EXPORTING")
    finally:
        database.shutdown()


def test_native_wpd_worker_crash_is_persisted_and_preserves_cbz_source(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database, asset, _simulated, _service = _make_service(tmp_path)
    source_path = tmp_path.joinpath(*asset.relative_path.split("/"))
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(TEST_BYTES)

    private_device_id = "private-wpd-id-must-not-be-logged"
    worker_script = tmp_path / "crash_wpd_worker.py"
    worker_script.write_text(
        "import os, sys\nsys.stdin.buffer.read()\nos._exit(17)\n",
        encoding="utf-8",
    )
    worker = IsolatedWpdClient(worker_command=(sys.executable, str(worker_script)))
    target = ExportTarget(
        target_id="wpd:fixture-reader",
        mount_path=None,
        display_name="Fixture reader",
        file_system="MTP/WPD",
        total_bytes=10_000_000,
        free_bytes=9_000_000,
        transport="wpd",
        portable_device_id=private_device_id,
        storage_object_id="private-storage-object",
        storage_persistent_id="private-persistent-id",
    )

    class PortableTargetProvider:
        def list_targets(self) -> tuple[ExportTarget, ...]:
            return (target,)

    exporter = UsbExportService(
        target_provider=PortableTargetProvider(),
        library_root=tmp_path,
        data_root=tmp_path,
        portable_exporter=worker,
    )
    service = UsbExportJobService(
        database=database,
        exporter=exporter,
        clock=lambda: NOW,
    )

    try:
        job = service.export_asset(
            asset.asset_id,
            target_id=target.target_id,
            idempotency_key="native-worker-crash",
        )

        assert job.status.value == "FAILED"
        assert job.error_code == ErrorCode.EXP_COPY_FAILED.value
        assert job.failure_stage == "worker_crash"
        assert job.retryable is False
        assert job.cause_type == "WorkerExit17"
        history = service.list_jobs(limit=10, offset=0)
        assert len(history.items) == 1
        assert history.items[0].export_id == job.export_id
        assert history.items[0].status == job.status
        assert source_path.read_bytes() == TEST_BYTES
        assert hashlib.sha256(source_path.read_bytes()).hexdigest() == asset.sha256
        assert private_device_id not in caplog.text
    finally:
        database.shutdown()


def test_missing_library_source_is_persisted_as_terminal_export_failure(
    tmp_path: Path,
) -> None:
    database, asset, exporter, service = _make_service(tmp_path)
    exporter.failure = ExportFailure(
        code=ErrorCode.LIB_PATH_UNAVAILABLE,
        stage="source_path",
        trace_id="00000000-0000-4000-8000-000000000005",
        retryable=False,
        cause_type="FileNotFoundError",
    )
    try:
        job = service.export_asset(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="missing-library-source",
        )

        assert job.status == "FAILED"
        assert job.error_code == ErrorCode.EXP_SOURCE_INVALID.value
        assert job.failure_stage == "source_path"
        assert job.retryable is False
        assert job.cause_type == "FileNotFoundError"
        assert job.terminal_at is not None
        with database.read_session() as session:
            row = session.get(ExportJobRow, str(job.export_id))
            assert row is not None
            assert row.status == "FAILED"
    finally:
        database.shutdown()


def test_idempotency_conflict_and_terminal_retention_preserve_active_jobs(
    tmp_path: Path,
) -> None:
    database, asset, _exporter, service = _make_service(tmp_path)
    try:
        created = service.create_queued_job(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="stable-key",
        )
        with pytest.raises(ExportIdempotencyConflict):
            service.create_queued_job(
                asset.asset_id,
                target_id="different-usb",
                idempotency_key="stable-key",
            )

        old_terminal_ids = [uuid4() for _ in range(1000)]
        active_ids = [uuid4(), uuid4()]
        old_terminal_at_us = int((NOW - timedelta(days=1)).timestamp() * 1_000_000)
        old_created_at_us = int((NOW - timedelta(days=2)).timestamp() * 1_000_000)
        with database.write_session() as session:
            for index, export_id in enumerate(old_terminal_ids):
                session.add(
                    ExportJobRow(
                        export_id=str(export_id),
                        idempotency_key_hash=hashlib.sha256(f"old-{index}".encode()).hexdigest(),
                        request_fingerprint=hashlib.sha256(f"payload-{index}".encode()).hexdigest(),
                        asset_id=str(asset.asset_id),
                        target_id="test-usb-01",
                        target_label="Simulated removable drive",
                        file_system="exFAT",
                        destination_name=f"chapter-{index}.cbz",
                        expected_size_bytes=len(TEST_BYTES),
                        expected_sha256=TEST_HASH,
                        status="EXPORTED",
                        created_at_us=old_created_at_us,
                        updated_at_us=old_terminal_at_us,
                        started_at_us=old_created_at_us,
                        terminal_at_us=old_terminal_at_us,
                        trace_id="00000000-0000-4000-8000-000000000002",
                    )
                )
            for index, export_id in enumerate(active_ids):
                session.add(
                    ExportJobRow(
                        export_id=str(export_id),
                        idempotency_key_hash=hashlib.sha256(f"active-{index}".encode()).hexdigest(),
                        request_fingerprint=hashlib.sha256(
                            f"active-payload-{index}".encode()
                        ).hexdigest(),
                        asset_id=str(asset.asset_id),
                        target_id="test-usb-01",
                        target_label="Simulated removable drive",
                        file_system="exFAT",
                        destination_name=f"active-{index}.cbz",
                        expected_size_bytes=len(TEST_BYTES),
                        expected_sha256=TEST_HASH,
                        status="EXPORTING",
                        created_at_us=old_created_at_us,
                        updated_at_us=old_created_at_us,
                        started_at_us=old_created_at_us,
                        terminal_at_us=None,
                        trace_id="00000000-0000-4000-8000-000000000003",
                    )
                )

        new_job = service.create_queued_job(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="new-key",
        )
        with database.read_session() as session:
            terminal_count = session.scalar(
                select(func.count())
                .select_from(ExportJobRow)
                .where(ExportJobRow.status.in_(("EXPORTED", "SKIPPED", "FAILED", "INTERRUPTED")))
            )
            active_count = session.scalar(
                select(func.count())
                .select_from(ExportJobRow)
                .where(ExportJobRow.status.in_(("QUEUED", "EXPORTING")))
            )
            retained_active = set(
                session.scalars(
                    select(ExportJobRow.export_id).where(
                        ExportJobRow.export_id.in_([str(item) for item in active_ids])
                    )
                ).all()
            )
            assert terminal_count == 996
            assert active_count == 4  # first request, two seeded jobs, and the new reservation.
            assert retained_active == {str(item) for item in active_ids}
            assert session.get(ExportJobRow, str(created.export_id)) is not None
            assert session.get(ExportJobRow, str(new_job.export_id)) is not None
        assert terminal_count + active_count == 1000
    finally:
        database.shutdown()


def test_expired_history_is_pruned_and_restart_marks_active_jobs_interrupted(
    tmp_path: Path,
) -> None:
    database, asset, exporter, service = _make_service(tmp_path)
    try:
        queued = service.create_queued_job(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="queued-before-restart",
        )
        exporting = service.create_queued_job(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="exporting-before-restart",
        )
        service.mark_exporting(exporting.export_id)
        old_job = service.create_queued_job(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="old-terminal",
        )
        service.mark_exporting(old_job.export_id)
        service.mark_failed(
            old_job.export_id,
            error_code="EXP_COPY_FAILED",
            failure_stage="copy",
            retryable=False,
            cause_type="OSError",
        )
        with database.write_session() as session:
            row = session.get(ExportJobRow, str(old_job.export_id))
            assert row is not None
            expired_terminal_us = int((NOW - timedelta(days=90)).timestamp() * 1_000_000)
            expired_created_us = int((NOW - timedelta(days=91)).timestamp() * 1_000_000)
            row.created_at_us = expired_created_us
            row.updated_at_us = expired_terminal_us
            row.started_at_us = expired_created_us
            row.terminal_at_us = expired_terminal_us

        interrupted_count = service.recover_after_restart()
        new_job = service.create_queued_job(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="old-terminal",
        )

        assert interrupted_count == 2
        assert new_job.export_id != old_job.export_id
        page = service.list_jobs(limit=MAX_EXPORT_HISTORY_PAGE_SIZE, offset=0)
        assert page.limit == 100
        assert all(item.export_id != old_job.export_id for item in page.items)
        assert any(
            item.export_id == queued.export_id
            and item.status == "INTERRUPTED"
            and item.trace_id == queued.trace_id
            for item in page.items
        )
        assert any(
            item.export_id == exporting.export_id
            and item.status == "INTERRUPTED"
            and item.trace_id == exporting.trace_id
            for item in page.items
        )
        assert any(item.export_id == new_job.export_id for item in page.items)
        assert exporter.calls == []
    finally:
        database.shutdown()


def test_history_api_pages_at_100_and_export_endpoint_requires_idempotency_key(
    tmp_path: Path,
) -> None:
    database, asset, exporter, service = _make_service(tmp_path)
    app = create_app()
    runtime = app.state.runtime
    assert isinstance(runtime, RuntimeState)
    runtime.database = database
    runtime.usb_export_job_service = service

    async def exercise_api() -> tuple[
        Response,
        Response,
        Response,
        tuple[Response, Response],
    ]:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            missing_key = await client.post(
                "/api/v1/usb-exports",
                json={"asset_id": str(asset.asset_id), "target_id": "test-usb-01"},
            )
            first = await client.post(
                "/api/v1/usb-exports",
                headers={"Idempotency-Key": "api-request-1"},
                json={"asset_id": str(asset.asset_id), "target_id": "test-usb-01"},
            )
            repeated = await client.post(
                "/api/v1/usb-exports",
                headers={"Idempotency-Key": "api-request-1"},
                json={"asset_id": str(asset.asset_id), "target_id": "test-usb-01"},
            )
            page = await client.get("/api/v1/usb-exports?limit=100&offset=0")
            too_large = await client.get("/api/v1/usb-exports?limit=101&offset=0")
            return missing_key, first, repeated, (page, too_large)

    try:
        missing_key, first, repeated, pages = asyncio.run(exercise_api())
        page, too_large = pages
        assert missing_key.status_code == 422
        assert first.status_code == 200
        assert repeated.status_code == 200
        assert first.json()["data"]["export_id"] == repeated.json()["data"]["export_id"]
        assert page.status_code == 200
        assert page.json()["meta"] == {"limit": 100, "offset": 0, "total": 1}
        assert too_large.status_code == 422
        assert exporter.calls == [(asset.asset_id, "test-usb-01")]
        encoded = first.text.casefold()
        assert "api-request-1" not in encoded
        assert str(tmp_path).casefold() not in encoded
    finally:
        database.shutdown()


def test_usb_export_api_exposes_safe_targets_cursor_history_and_chapter_export(
    tmp_path: Path,
) -> None:
    database, asset, exporter, service = _make_service(tmp_path)
    _series_id, chapter_id = _insert_ready_chapter(database, asset=asset)
    service.export_asset(
        asset.asset_id,
        target_id="test-usb-01",
        idempotency_key="chapter-export-history-prior",
    )
    app = create_app()
    runtime = app.state.runtime
    assert isinstance(runtime, RuntimeState)
    runtime.database = database
    runtime.usb_export_job_service = service

    async def exercise_api() -> tuple[Response, Response, Response, Response, Response]:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            targets = await client.get("/api/v1/usb-exports/targets")
            exported = await client.post(
                f"/api/v1/usb-exports/chapters/{chapter_id}",
                headers={"Idempotency-Key": "chapter-export-1"},
                json={"target_id": "test-usb-01"},
            )
            history = await client.get("/api/v1/usb-exports/pages?limit=1")
            cursor = history.json()["meta"]["next_cursor"]
            following_page = await client.get(
                "/api/v1/usb-exports/pages",
                params={"limit": 1, "cursor": cursor},
            )
            invalid_cursor = await client.get("/api/v1/usb-exports/pages?cursor=bad!")
            return targets, exported, history, following_page, invalid_cursor

    try:
        targets, exported, history, following_page, invalid_cursor = asyncio.run(exercise_api())
        assert targets.status_code == 200
        assert targets.json() == {
            "data": [
                {
                    "target_id": "test-usb-01",
                    "display_name": "Simulated removable drive",
                    "file_system": "exFAT",
                    "total_bytes": 10_000_000,
                    "free_bytes": 9_000_000,
                }
            ]
        }
        assert str(exporter.mount_path).casefold() not in targets.text.casefold()
        assert exported.status_code == 200
        assert exported.json()["data"]["status"] == "EXPORTED"
        assert exporter.calls == [(asset.asset_id, "test-usb-01")] * 2
        assert history.status_code == 200
        history_data = history.json()
        assert history_data["meta"]["total"] == 2
        assert history_data["meta"]["next_cursor"]
        assert following_page.status_code == 200
        assert following_page.json()["meta"]["total"] == 2
        assert following_page.json()["meta"]["next_cursor"] is None
        assert history_data["data"][0]["export_id"] != following_page.json()["data"][0]["export_id"]
        assert invalid_cursor.status_code == 422
    finally:
        database.shutdown()


def test_usb_export_api_exposes_wpd_targets_without_mount_or_private_ids(
    tmp_path: Path,
) -> None:
    database, _asset, exporter, service = _make_service(tmp_path)
    exporter.targets = (
        ExportTarget(
            target_id="wpd:opaque-target-hash",
            mount_path=None,
            display_name="B6 Color - Almacenamiento interno compartido",
            file_system="MTP/WPD",
            total_bytes=21_549_252_608,
            free_bytes=16_995_741_696,
            transport="wpd",
            portable_device_id="USB\\PRIVATE-DEVICE-ID",
            storage_object_id="private-storage-object-id",
            storage_persistent_id="private-persistent-id",
        ),
    )
    app = create_app()
    runtime = app.state.runtime
    assert isinstance(runtime, RuntimeState)
    runtime.database = database
    runtime.usb_export_job_service = service

    async def read_targets() -> Response:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.get("/api/v1/usb-exports/targets")

    try:
        response = asyncio.run(read_targets())
        assert response.status_code == 200
        assert response.json() == {
            "data": [
                {
                    "target_id": "wpd:opaque-target-hash",
                    "display_name": "B6 Color - Almacenamiento interno compartido",
                    "file_system": "MTP/WPD",
                    "total_bytes": 21_549_252_608,
                    "free_bytes": 16_995_741_696,
                }
            ]
        }
        for secret in ("PRIVATE-DEVICE-ID", "private-storage-object-id", "private-persistent-id"):
            assert secret not in response.text
    finally:
        database.shutdown()


def test_cursor_history_excludes_new_insertions_and_observes_retention_pruning(
    tmp_path: Path,
) -> None:
    database, asset, _exporter, service = _make_service(tmp_path)
    try:
        first_oldest = service.create_queued_job(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="cursor-oldest",
        )
        pruned = service.create_queued_job(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="cursor-pruned",
        )
        service.mark_exporting(pruned.export_id)
        service.mark_failed(
            pruned.export_id,
            error_code="EXP_COPY_FAILED",
            failure_stage="copy",
            retryable=False,
            cause_type="OSError",
        )
        first_page_item = service.create_queued_job(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="cursor-first-page",
        )
        later_insert = service.create_queued_job(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="cursor-later-insert",
        )

        base_us = int(NOW.timestamp() * 1_000_000)
        with database.write_session() as session:
            for job, second in (
                (first_oldest, 1),
                (pruned, 2),
                (first_page_item, 3),
                (later_insert, 3),
            ):
                row = session.get(ExportJobRow, str(job.export_id))
                assert row is not None
                row.created_at_us = base_us + second * 1_000_000
                row.updated_at_us = row.created_at_us
                if job.export_id == pruned.export_id:
                    row.started_at_us = row.created_at_us
                    row.terminal_at_us = row.created_at_us

        first_page = service.list_jobs_after(limit=1, before=None)
        tie_order = sorted(
            (first_page_item.export_id, later_insert.export_id),
            key=str,
            reverse=True,
        )
        assert [item.export_id for item in first_page.items] == [tie_order[0]]
        assert first_page.has_more is True

        # Simulate a terminal record removed by the approved retention policy
        # while the client is between pages. A newer insertion must stay before
        # the cursor, while total reports the current request's observation.
        newer_insert = service.create_queued_job(
            asset.asset_id,
            target_id="test-usb-01",
            idempotency_key="cursor-newer-after-first-page",
        )
        with database.write_session() as session:
            row = session.get(ExportJobRow, str(newer_insert.export_id))
            assert row is not None
            row.created_at_us = base_us + 5_000_000
            row.updated_at_us = row.created_at_us
            session.delete(session.get(ExportJobRow, str(pruned.export_id)))

        next_page = service.list_jobs_after(
            limit=2,
            before=(
                int(first_page.items[-1].created_at.timestamp() * 1_000_000),
                first_page.items[-1].export_id,
            ),
        )
        assert [item.export_id for item in next_page.items] == [
            tie_order[1],
            first_oldest.export_id,
        ]
        assert newer_insert.export_id not in {item.export_id for item in next_page.items}
        assert next_page.total == 4
        assert next_page.has_more is False
    finally:
        database.shutdown()


def test_chapter_export_rejects_a_cbz_whose_download_is_not_ready(
    tmp_path: Path,
) -> None:
    database, asset, exporter, service = _make_service(tmp_path)
    _series_id, chapter_id = _insert_ready_chapter(database, asset=asset)
    with database.write_session() as session:
        job = session.scalar(select(DownloadJobRow))
        assert job is not None
        job.status = "FAILED"

    try:
        with pytest.raises(ExportAssetUnavailable):
            service.export_chapter(
                chapter_id,
                target_id="test-usb-01",
                idempotency_key="not-ready-chapter",
            )
        assert exporter.calls == []
    finally:
        database.shutdown()
