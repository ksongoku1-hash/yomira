from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import logging
import os
import threading
import time
import zipfile
from collections.abc import AsyncIterator, Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from mock_manga_server import MockMangaServer, MockScenario
from PIL import Image
from sqlalchemy.orm import Session
from support.sqlite import open_test_database, reopen_test_database

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.mock import MockConnector
from app.connectors.pages import ConnectorPageReference, PageFetchRequest
from app.connectors.registry import ConnectorRegistration, ConnectorRegistry
from app.connectors.sandbox import ConnectorHostContext
from app.domain.chapter_cbz_asset import ChapterCbzAsset
from app.domain.download_job import DownloadJobStatus
from app.domain.download_page import MAX_DOWNLOAD_PAGE_ATTEMPTS, DownloadPage
from app.domain.job_cancellation import JobCancellationNotAllowed
from app.domain.job_pause import (
    JobPauseController,
    JobPauseState,
    PauseCheckpointOutcome,
    PauseReason,
    PauseSafetyEvidence,
)
from app.errors import ErrorCode
from app.persistence import filesystem_retry
from app.persistence.atomic_file_publisher import ADR_0087_RETRY_POLICY, AtomicFilePublisher
from app.persistence.disk_budget import (
    CBZ_RESERVATION_BYTES,
    MINIMUM_RESERVED_BYTES,
    PAGE_TEMP_RESERVATION_BYTES,
    DiskBudgetManager,
)
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.download_manifest_state import DownloadManifestStateRow
from app.persistence.models.download_page import DownloadPageRow
from app.persistence.repositories.download_job import DownloadJobRepository
from app.persistence.repositories.download_page import DownloadPageRepository
from app.persistence.storage_gate import SharedStorageGate
from app.persistence.windows_paths import DataRootIdentity
from app.secrets.vault import WindowsDpapiSecretVault
from app.services.cbz_builder import CBZBuilder
from app.services.download_transport import (
    DownloadCancellationRequested,
    DownloadTransport,
    DownloadTransportPolicy,
    NetworkOrigin,
)
from app.services.download_worker import DownloadWorker, DownloadWorkerResult
from app.services.image_validation import ImageValidationPolicy
from app.services.job_recovery import RecoveryCoordinator
from app.services.jobs import DownloadJobService
from app.services.page_manifest_writer import PageManifestWriter
from app.services.reader_asset import (
    V1_MAX_CBZ_BYTES,
    V1_MAX_PAGE_BYTES,
    V1_STREAM_CHUNK_BYTES,
    ReaderAssetLimits,
    ReaderAssetService,
)

# The same fixture reached PACKAGING in 24.36s and completed in 27.64s on this
# Windows host; stage waits need a bounded window beyond that measured path.
_INTEGRATION_STAGE_TIMEOUT_SECONDS = 40


def _ignore_sync_retry_delay(_delay_seconds: float) -> None:
    return


def test_queued_job_downloads_packages_indexes_and_survives_database_restart(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_download(tmp_path, enable_disk_budget=True))


def test_worker_reserves_two_paths_in_one_durable_snapshot_before_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots: list[tuple[DownloadPage, ...]] = []
    original_publish = PageManifestWriter.publish_snapshot
    original_resolve = MockConnector.resolve_page

    def publish(
        writer: PageManifestWriter, job_id: UUID, pages: tuple[DownloadPage, ...]
    ) -> Any:
        result = original_publish(writer, job_id, pages)
        snapshots.append(pages)
        return result

    def resolve(connector: MockConnector, reference: ConnectorPageReference) -> PageFetchRequest:
        assert snapshots
        assert [page.status for page in snapshots[0]] == ["DOWNLOADING", "DOWNLOADING", "PENDING"]
        assert snapshots[0][0].temporary_path != snapshots[0][1].temporary_path
        return original_resolve(connector, reference)

    monkeypatch.setattr(PageManifestWriter, "publish_snapshot", publish)
    monkeypatch.setattr(MockConnector, "resolve_page", resolve)
    asyncio.run(_exercise_download(tmp_path))
    # Two reservations, three individual completions and three validations.
    assert len(snapshots) == 8


def test_worker_downloads_two_pages_concurrently_and_keeps_manifest_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_get_pages = MockConnector.get_pages

    async def concurrent_page_order(
        connector: MockConnector,
        remote_chapter_reference: str,
    ) -> tuple[ConnectorPageReference, ...]:
        pages = await original_get_pages(connector, remote_chapter_reference)
        return (pages[1], pages[2], pages[0])

    monkeypatch.setattr(MockConnector, "get_pages", concurrent_page_order)
    asyncio.run(
        _exercise_download(
            tmp_path,
            scenario=MockScenario.SLOW,
            concurrency_probe_path="/api/v1/pages/mock-chapter-1/page-1",
            expected_peak_concurrency=2,
            expected_page_references=(
                "mock-chapter-1/page-1",
                "mock-chapter-1/page-1",
                "mock-chapter-1/page-2",
            ),
        )
    )


def test_cbz_is_published_under_configured_documents_mangas_root(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_download(tmp_path, library_root=tmp_path / "Documents" / "Mangas"))


def test_worker_packages_actual_png_when_server_announces_webp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def announced_webp(_response: httpx.Response) -> str:
        return "image/webp"

    monkeypatch.setattr(DownloadTransport, "_safe_content_type", staticmethod(announced_webp))
    # The existing fixture asserts PNG bytes/MIME, CBZ indexing, and reading after restart.
    asyncio.run(_exercise_download(tmp_path))


def test_worker_refreshes_expired_page_requests_without_changing_durable_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_pages = MockConnector.get_pages
    resolve_page = MockConnector.resolve_page
    page_lists = 0

    async def refreshed_pages(
        connector: MockConnector, chapter_reference: str
    ) -> tuple[ConnectorPageReference, ...]:
        nonlocal page_lists
        page_lists += 1
        return await get_pages(connector, chapter_reference)

    def expiring_request(
        connector: MockConnector, page_reference: ConnectorPageReference
    ) -> PageFetchRequest:
        if page_lists == 1:
            raise SourceConnectorError(
                code=ErrorCode.SRC_CONNECTOR_FAILED,
                operation=SourceConnectorOperation.RESOLVE_PAGE,
                retryable=True,
            )
        return resolve_page(connector, page_reference)

    monkeypatch.setattr(MockConnector, "get_pages", refreshed_pages)
    monkeypatch.setattr(MockConnector, "resolve_page", expiring_request)
    asyncio.run(_exercise_download(tmp_path))
    assert page_lists == 2


@pytest.mark.parametrize("failure", ["plan_changed", "still_expired", "not_retryable"])
def test_worker_signature_refresh_fails_closed_without_unbounded_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    get_pages = MockConnector.get_pages
    page_lists = 0

    async def refreshed_pages(
        connector: MockConnector, chapter_reference: str
    ) -> tuple[ConnectorPageReference, ...]:
        nonlocal page_lists
        page_lists += 1
        pages = await get_pages(connector, chapter_reference)
        return pages[::-1] if failure == "plan_changed" and page_lists > 1 else pages

    def expired_request(
        _connector: MockConnector, _page_reference: ConnectorPageReference
    ) -> PageFetchRequest:
        raise SourceConnectorError(
            code=ErrorCode.SRC_CONNECTOR_FAILED,
            operation=SourceConnectorOperation.RESOLVE_PAGE,
            retryable=failure != "not_retryable",
        )

    monkeypatch.setattr(MockConnector, "get_pages", refreshed_pages)
    monkeypatch.setattr(MockConnector, "resolve_page", expired_request)
    stage = "page_transport" if failure == "not_retryable" else "page_request_refresh"
    asyncio.run(
        _exercise_download(
            tmp_path,
            expected_resolution_failure_stage=stage,
            expected_resolution_integrity_block=failure == "plan_changed",
        )
    )
    assert page_lists == (1 if failure == "not_retryable" else 2)


def test_recovered_partial_attempt_runs_worker_from_byte_zero_and_preserves_old_part(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    asyncio.run(_exercise_recovered_partial_attempt(tmp_path, caplog=caplog))


@pytest.mark.parametrize(
    "recovery_fault",
    (
        "db_manifest_divergence",
        "pending_marker_digest_mismatch",
        "unknown_artifact",
        "cancel_requested",
        "attempt_exhausted",
        "downloaded_hash_mismatch",
        "downloaded_size_mismatch",
    ),
)
def test_recovery_fails_closed_for_unreconciled_download_plan(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    recovery_fault: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    asyncio.run(
        _exercise_recovered_partial_attempt(
            tmp_path,
            caplog=caplog,
            recovery_fault=recovery_fault,
        )
    )


def test_recovered_worker_reuses_verified_downloaded_page(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    asyncio.run(
        _exercise_recovered_partial_attempt(
            tmp_path,
            caplog=caplog,
            reuse_completed_page=True,
        )
    )


@pytest.mark.parametrize("locked_operation", ("lstat", "mkdir"))
def test_ensure_data_directory_retries_transient_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    locked_operation: str,
) -> None:
    class _SharingViolation(OSError):
        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.winerror = 32

    worker = object.__new__(DownloadWorker)
    worker._data_root_identity = DataRootIdentity.capture(tmp_path)  # pyright: ignore[reportPrivateUsage]
    worker._database = SimpleNamespace(storage_gate=SharedStorageGate())  # type: ignore[assignment]
    target = tmp_path / "library"
    job_id = UUID("8b25dcca-e824-4aeb-9e0d-06b3d3f94932")
    trace_id = "00000000-0000-4000-8000-000000000099"
    waits: list[float] = []
    caplog.set_level(logging.WARNING, logger="yomira.filesystem_retry")
    monkeypatch.setattr(filesystem_retry.time, "sleep", waits.append)
    original_operation = getattr(Path, locked_operation)
    locked_once = False

    def fail_once(path: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal locked_once
        if path == target and not locked_once:
            locked_once = True
            raise _SharingViolation("injected transient sharing violation")
        return original_operation(path, *args, **kwargs)

    monkeypatch.setattr(Path, locked_operation, fail_once)

    result = worker._ensure_data_directory(  # pyright: ignore[reportPrivateUsage]
        "library/chapters",
        job_id=job_id,
        trace_id=trace_id,
    )

    assert result == target / "chapters"
    assert result.is_dir()
    assert waits == [0.1]
    retry_logs = [
        record
        for record in caplog.records
        if record.name == "yomira.filesystem_retry"
        and record.getMessage() == "filesystem_lock_retry"
    ]
    assert len(retry_logs) == 1
    assert retry_logs[0].__dict__["trace_id"] == trace_id
    assert retry_logs[0].__dict__["job_id"] == str(job_id)


def test_existing_artifact_budget_counts_only_verified_bounded_cbz_staging(
    tmp_path: Path,
) -> None:
    worker = object.__new__(DownloadWorker)
    worker._data_root_identity = DataRootIdentity.capture(tmp_path)  # pyright: ignore[reportPrivateUsage]
    worker._library_root_identity = worker._data_root_identity  # pyright: ignore[reportPrivateUsage]

    worker._connector_registry = cast(  # pyright: ignore[reportPrivateUsage]
        ConnectorRegistry, SimpleNamespace(connector_ids=())
    )

    def page_inventory(_job_id: UUID) -> int:
        return 17

    worker._transport = cast(  # pyright: ignore[reportPrivateUsage]
        DownloadTransport,
        SimpleNamespace(existing_page_bytes_for_budget=page_inventory),
    )
    job_id = UUID("8b25dcca-e824-4aeb-9e0d-06b3d3f94932")
    packaging = tmp_path / ".yomira-staging" / "jobs" / str(job_id) / "packaging"
    packaging.mkdir(parents=True)
    (packaging / "chapter.cbz.tmp").write_bytes(b"abc")

    assert worker.existing_artifact_bytes_for_budget(job_id) == (17, 3)


def test_ensure_data_directory_fails_closed_when_mkdir_error_has_ambiguous_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SharingViolation(OSError):
        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.winerror = 32

    worker = object.__new__(DownloadWorker)
    worker._data_root_identity = DataRootIdentity.capture(tmp_path)  # pyright: ignore[reportPrivateUsage]
    worker._database = SimpleNamespace(storage_gate=SharedStorageGate())  # type: ignore[assignment]
    target = tmp_path / "library"
    original_mkdir = Path.mkdir
    mkdir_calls = 0

    def create_then_fail(path: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal mkdir_calls
        if path == target and mkdir_calls == 0:
            mkdir_calls += 1
            original_mkdir(path, *args, **kwargs)
            raise _SharingViolation("injected ambiguous sharing violation")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", create_then_fail)
    monkeypatch.setattr(filesystem_retry.time, "sleep", _ignore_sync_retry_delay)

    with pytest.raises(_SharingViolation, match="injected ambiguous sharing violation"):
        worker._ensure_data_directory(  # pyright: ignore[reportPrivateUsage]
            "library/chapters",
            job_id=UUID("8b25dcca-e824-4aeb-9e0d-06b3d3f94932"),
            trace_id="00000000-0000-4000-8000-000000000099",
        )

    assert mkdir_calls == 1
    assert target.is_dir()


def test_invalid_image_fails_job_and_preserves_downloaded_evidence(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_download(tmp_path, scenario=MockScenario.HTML_AS_IMAGE))


def test_backpressure_waits_until_the_metadata_manifest_is_durable(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _exercise_download(
            tmp_path,
            pause_at=DownloadJobStatus.FETCHING_METADATA,
        )
    )


def test_backpressure_waits_until_a_page_stream_and_manifest_commit_finish(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _exercise_download(
            tmp_path,
            pause_at=DownloadJobStatus.DOWNLOADING,
        )
    )


def test_backpressure_waits_before_packaging_without_starting_filesystem_work(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _exercise_download(
            tmp_path,
            pause_at=DownloadJobStatus.PACKAGING,
        )
    )


def test_missing_registered_page_origin_reports_specific_failure_stage(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_download(tmp_path, network_origins=frozenset()))


def test_disk_full_during_page_flush_closes_gate_and_preserves_partial_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="app.services.download_worker")
    asyncio.run(
        _exercise_download(
            tmp_path,
            disk_full_during_page_flush=True,
            monkeypatch=monkeypatch,
            caplog=caplog,
        )
    )

    failure_records = [
        record
        for record in caplog.records
        if record.getMessage() == "download_job.worker_failed"
    ]
    assert len(failure_records) == 1
    assert failure_records[0].__dict__["error_code"] == "DL_JOB_RECOVERY_BLOCKED"
    assert failure_records[0].__dict__["stage"] == "shared_storage_gate_closed"
    assert failure_records[0].__dict__["trace_id"]


@pytest.mark.parametrize("failure_stage", ["write", "flush"])
def test_disk_full_during_cbz_staging_closes_gate_and_preserves_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_stage: str,
) -> None:
    caplog.set_level(logging.ERROR, logger="app.services.download_worker")
    asyncio.run(
        _exercise_download(
            tmp_path,
            disk_full_during_cbz_write=failure_stage,
            monkeypatch=monkeypatch,
            caplog=caplog,
        )
    )


def test_disk_full_during_packaging_directory_mkdir_closes_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _exercise_download(
            tmp_path,
            disk_full_during_packaging_mkdir=True,
            monkeypatch=monkeypatch,
        )
    )


def test_cancel_during_page_stream_closes_stream_preserves_part_and_skips_cbz(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_cancel_during_page_stream(tmp_path))


def test_cancel_during_cbz_build_keeps_verified_archive_out_of_library(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_download(tmp_path, cancel_during_cbz_build=True))


def test_cancel_after_cbz_promote_before_index_commit_returns_archive_to_staging(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_download(tmp_path, cancel_before_index_commit=True))


def test_cancel_waits_for_index_transaction_then_conflicts_with_completed_job(
    tmp_path: Path,
) -> None:
    asyncio.run(_exercise_download(tmp_path, cancel_during_index_transaction=True))


class _CancellationByteStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.chunk_written = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield self._body[:8]
        self.chunk_written.set()
        await self.release.wait()
        yield self._body[8:]

    async def aclose(self) -> None:
        self.closed = True


async def _exercise_cancel_during_page_stream(tmp_path: Path) -> None:
    with MockMangaServer() as server:
        data_root = tmp_path / "data"
        database = open_test_database(data_root)
        MigrationRunner(database, data_root / "backups").upgrade_to_head()
        connector = MockConnector(page_server_base_url=server.base_url)
        secret_vault = WindowsDpapiSecretVault(data_root)
        registry = ConnectorRegistry(
            context=ConnectorHostContext(secret_vault=secret_vault),
            registrations=(
                ConnectorRegistration(
                    metadata=connector.metadata,
                    factory=lambda _context: connector,
                    network_origins=frozenset({server.base_url}),
                ),
            ),
        )
        metadata = connector.normalize_metadata(
            await connector.get_series("mock-series-1")
        )
        chapters = await connector.get_chapters("mock-series-1")
        selection = DownloadJobService(database).create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=metadata,
            chapters=chapters,
            selected_remote_ids=("mock-chapter-1",),
        )[0]
        image_buffer = io.BytesIO()
        Image.new("RGB", (2, 2), color=(10, 20, 30)).save(image_buffer, format="PNG")
        streams: list[_CancellationByteStream] = []

        def page_response(_request: httpx.Request) -> httpx.Response:
            stream = _CancellationByteStream(image_buffer.getvalue())
            streams.append(stream)
            return httpx.Response(
                200,
                headers={"Content-Type": "image/png"},
                stream=stream,
            )

        async_transport = httpx.MockTransport(
            page_response
        )
        pause_controller = JobPauseController()
        origin = NetworkOrigin.from_url(server.base_url)
        recovery = RecoveryCoordinator(database, database.data_root_identity)
        reader = ReaderAssetService(
            database,
            data_root_identity=database.data_root_identity,
            limits=ReaderAssetLimits(
                max_cbz_bytes=V1_MAX_CBZ_BYTES,
                max_page_bytes=V1_MAX_PAGE_BYTES,
                chunk_bytes=V1_STREAM_CHUNK_BYTES,
            ),
        )
        publisher = AtomicFilePublisher(
            data_root_identity=database.data_root_identity,
            retry_policy=ADR_0087_RETRY_POLICY,
            sleep=time.sleep,
            clock=time.monotonic,
        )
        builder = CBZBuilder(
            publisher=publisher,
            max_archive_bytes=V1_MAX_CBZ_BYTES,
        )
        try:
            async with httpx.AsyncClient(transport=async_transport) as client:
                transport = DownloadTransport(
                    client,
                    DownloadTransportPolicy(
                        allowed_origins=frozenset({origin}),
                        max_bytes=V1_MAX_PAGE_BYTES,
                        chunk_size=V1_STREAM_CHUNK_BYTES,
                        connect_timeout_seconds=10,
                        read_timeout_seconds=30,
                        write_timeout_seconds=10,
                        pool_timeout_seconds=10,
                        max_redirects=5,
                        max_retry_after_seconds=60,
                        global_concurrency=2,
                        per_host_concurrency=2,
                    ),
                    data_root=data_root,
                    data_root_identity=database.data_root_identity,
                    test_transport=async_transport,
                    sleep=_skip_retry_delay,
                )
                first_chunk_delivered = asyncio.Event()
                in_flight_pages_started = 0

                async def gate_after_first_chunk(
                    response: httpx.Response,
                    *,
                    cancellation_event: asyncio.Event | None,
                ) -> AsyncIterator[bytes]:
                    nonlocal in_flight_pages_started
                    yield b"\x89PNG\r\n\x1a\n"
                    in_flight_pages_started += 1
                    if in_flight_pages_started == 2:
                        first_chunk_delivered.set()
                    if cancellation_event is None:
                        raise AssertionError("worker has no registered cancellation event")
                    await cancellation_event.wait()
                    raise DownloadCancellationRequested()

                transport._iter_response_chunks = gate_after_first_chunk  # pyright: ignore[reportPrivateUsage]
                worker = DownloadWorker(
                    database=database,
                    data_root_identity=database.data_root_identity,
                    connector_registry=registry,
                    recovery_coordinator=recovery,
                    transport=transport,
                    image_validation_policy=ImageValidationPolicy(
                        max_bytes=V1_MAX_PAGE_BYTES,
                        max_width=40_000,
                        max_height=40_000,
                        max_pixels=100_000_000,
                    ),
                    cbz_builder=builder,
                    file_publisher=publisher,
                    reader_asset_service=reader,
                    pause_controller=pause_controller,
                )
                await pause_controller.register(selection.job.job_id)
                worker_task = asyncio.create_task(worker.run_job(selection.job.job_id))
                try:
                    await asyncio.wait_for(
                        first_chunk_delivered.wait(),
                        timeout=_INTEGRATION_STAGE_TIMEOUT_SECONDS,
                    )
                    with database.write_session() as session:
                        repository = DownloadJobRepository()
                        current = repository.get(session, selection.job.job_id)
                        assert current is not None
                        assert current.status is DownloadJobStatus.DOWNLOADING
                        repository.request_cancellation(
                            session,
                            current_job=current,
                            requested_at=max(
                                datetime.now(UTC),
                                current.updated_at + timedelta(microseconds=1),
                            ),
                        )
                    pause_controller.notify_cancellation_requested(
                        selection.job.job_id
                    )
                    done, _pending = await asyncio.wait({worker_task}, timeout=2)
                    assert worker_task in done, "worker did not stop the active page stream"
                    result = await worker_task
                finally:
                    pause_controller.notify_cancellation_requested(
                        selection.job.job_id
                    )
                    for stream in streams:
                        stream.release.set()
                    if not worker_task.done():
                        await asyncio.gather(worker_task, return_exceptions=True)
                    await pause_controller.unregister(selection.job.job_id)

            assert result.job.status is DownloadJobStatus.CANCELLED
            assert len(streams) == 2
            assert all(stream.closed for stream in streams)
            with database.read_session() as session:
                rows = (
                    session.query(DownloadPageRow)
                    .filter_by(job_id=str(selection.job.job_id))
                    .order_by(DownloadPageRow.ordinal)
                    .all()
                )
                final_job = DownloadJobRepository().get(session, selection.job.job_id)
            assert final_job is not None
            assert final_job.status is DownloadJobStatus.CANCELLED
            assert final_job.cancel_requested_at is not None
            attempted_rows = [row for row in rows if row.attempts == 1]
            assert len(attempted_rows) == 2
            for row in attempted_rows:
                assert row.temporary_path is not None
                part = data_root / "temp" / row.temporary_path
                assert part.suffix == ".part"
                assert part.read_bytes() == b"\x89PNG\r\n\x1a\n"
            assert not (data_root / "library" / "chapters").exists()
        finally:
            database.shutdown()


async def _exercise_recovered_partial_attempt(
    tmp_path: Path,
    *,
    caplog: pytest.LogCaptureFixture,
    recovery_fault: str | None = None,
    reuse_completed_page: bool = False,
) -> None:
    data_root = tmp_path / "data"
    database = open_test_database(data_root)
    transport_client: httpx.AsyncClient | None = None
    try:
        MigrationRunner(database, data_root / "backups").upgrade_to_head()
        page_requests: list[httpx.Request] = []
        published_snapshots: list[tuple[DownloadPage, ...]] = []
        image_buffer = io.BytesIO()
        Image.new("RGB", (2, 2), color=(30, 80, 120)).save(
            image_buffer,
            format="PNG",
        )
        image_bytes = image_buffer.getvalue()
        persist_completed_page = reuse_completed_page or recovery_fault in {
            "downloaded_hash_mismatch",
            "downloaded_size_mismatch",
        }

        class ImageResponseStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield image_bytes

            async def aclose(self) -> None:
                return None

        def respond(request: httpx.Request) -> httpx.Response:
            page_requests.append(request)
            return httpx.Response(
                200,
                headers={"Content-Type": "image/png"},
                stream=ImageResponseStream(),
            )

        test_transport = httpx.MockTransport(respond)
        transport_client = httpx.AsyncClient(transport=test_transport)
        page_server_base_url = "http://127.0.0.1:8765"
        connector = MockConnector(page_server_base_url=page_server_base_url)
        registry = ConnectorRegistry(
            context=ConnectorHostContext(
                secret_vault=WindowsDpapiSecretVault(data_root)
            ),
            registrations=(
                ConnectorRegistration(
                    metadata=connector.metadata,
                    factory=lambda _context: connector,
                    network_origins=frozenset({page_server_base_url}),
                ),
            ),
        )
        series_metadata = connector.normalize_metadata(
            await connector.get_series("mock-series-1")
        )
        chapters = await connector.get_chapters("mock-series-1")
        selection = DownloadJobService(database).create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=series_metadata,
            chapters=chapters,
            selected_remote_ids=("mock-chapter-1",),
        )[0]
        job_id = selection.job.job_id
        recovery = RecoveryCoordinator(database, database.data_root_identity)
        writer = PageManifestWriter(database, manifest_store=recovery.manifest_store)
        job_repository = DownloadJobRepository()
        current_job = selection.job
        with database.write_session() as session:
            transitioned = job_repository.transition_status(
                session,
                current_job=current_job,
                target_status=DownloadJobStatus.FETCHING_METADATA,
                changed_at=current_job.updated_at + timedelta(seconds=1),
            )
        assert transitioned is not None
        current_job = transitioned

        references = await connector.get_pages("mock-chapter-1")
        planned_pages = tuple(
            DownloadPage.new_local(job_id, ordinal, reference.remote_reference)
            for ordinal, reference in enumerate(references, start=1)
        )
        writer.replace_pending_pages(job_id, planned_pages)
        with database.write_session() as session:
            transitioned = job_repository.transition_status(
                session,
                current_job=current_job,
                target_status=DownloadJobStatus.DOWNLOADING,
                changed_at=current_job.updated_at + timedelta(seconds=1),
            )
        assert transitioned is not None

        first_attempt_path = DownloadPageRepository.attempt_path(job_id, 1, 1)
        first_attempt = replace(
            planned_pages[0],
            status="DOWNLOADING",
            temporary_path=first_attempt_path,
            attempts=1,
        )
        initial_snapshot = writer.publish_snapshot(
            job_id,
            (first_attempt, *planned_pages[1:]),
        )
        assert initial_snapshot.publication_pending is False
        partial_bytes = (
            image_bytes
            if persist_completed_page
            else b"partial attempt one must survive restart"
        )
        first_part = data_root / "temp" / first_attempt_path
        with recovery.temporary_page_store.create(first_attempt_path) as stream:
            stream.write(partial_bytes)

        if persist_completed_page:
            first_attempt = replace(
                first_attempt,
                status="DOWNLOADED",
                size_bytes=len(image_bytes),
                sha256=hashlib.sha256(image_bytes).hexdigest(),
            )
            initial_snapshot = writer.publish_snapshot(
                job_id,
                (first_attempt, *planned_pages[1:]),
            )
            assert initial_snapshot.publication_pending is False

        if recovery_fault == "downloaded_hash_mismatch":
            partial_bytes = b"x" * len(image_bytes)
            first_part.write_bytes(partial_bytes)
        elif recovery_fault == "downloaded_size_mismatch":
            partial_bytes = image_bytes + b"extra"
            first_part.write_bytes(partial_bytes)

        if recovery_fault == "attempt_exhausted":
            for attempt_number in range(2, MAX_DOWNLOAD_PAGE_ATTEMPTS + 1):
                first_attempt = replace(
                    first_attempt,
                    status="DOWNLOADING",
                    temporary_path=DownloadPageRepository.attempt_path(
                        job_id,
                        1,
                        attempt_number,
                    ),
                    attempts=attempt_number,
                    size_bytes=None,
                    sha256=None,
                )
                initial_snapshot = writer.publish_snapshot(
                    job_id,
                    (first_attempt, *planned_pages[1:]),
                )
                assert initial_snapshot.publication_pending is False
            exhausted_path = first_attempt_path.replace(
                "attempt-1.part",
                f"attempt-{MAX_DOWNLOAD_PAGE_ATTEMPTS}.part",
            )
            with recovery.temporary_page_store.create(exhausted_path) as stream:
                stream.write(b"attempt limit reached")

        unknown_path: Path | None = None
        if recovery_fault == "db_manifest_divergence":
            with database.write_session() as session:
                row = session.get(DownloadPageRow, (str(job_id), 1))
                assert row is not None
                row.remote_reference = "different-reference-than-manifest"
        elif recovery_fault == "pending_marker_digest_mismatch":
            with database.write_session() as session:
                state = session.get(DownloadManifestStateRow, str(job_id))
                assert state is not None
                state.publication_pending = True
                state.manifest_sha256 = "0" * 64
        elif recovery_fault == "unknown_artifact":
            unknown_path = (
                data_root / "temp" / "jobs" / str(job_id) / "unknown.bin"
            )
            unknown_path.parent.mkdir(parents=True, exist_ok=True)
            unknown_path.write_bytes(b"unclassified evidence")
        elif recovery_fault == "cancel_requested":
            with database.write_session() as session:
                cancelled = job_repository.request_cancellation(
                    session,
                    current_job=transitioned,
                    requested_at=transitioned.updated_at + timedelta(seconds=1),
                )
            assert cancelled is not None
            transitioned = cancelled

        # A new backend process sees only durable rows, manifest, and files.
        database.close()
        database = reopen_test_database(database)
        restarted_recovery = RecoveryCoordinator(
            database,
            database.data_root_identity,
        )
        assert restarted_recovery.reconcile_startup() == 1
        with database.read_session() as session:
            recovered_job = job_repository.get(session, job_id)
            assert recovered_job is not None
            recovered_rows = (
                session.query(DownloadPageRow)
                .filter_by(job_id=str(job_id))
                .order_by(DownloadPageRow.ordinal)
                .all()
            )
        if recovery_fault is not None:
            assert recovered_job.status is DownloadJobStatus.INTERRUPTED
            assert recovered_job.recovery_blocked is True
            assert first_part.read_bytes() == partial_bytes
            assert (
                data_root / "database" / "manifests" / f"{job_id}.json"
            ).read_bytes() == initial_snapshot.manifest_bytes
            if recovery_fault == "unknown_artifact":
                assert unknown_path is not None
                assert unknown_path.read_bytes() == b"unclassified evidence"
            if recovery_fault == "cancel_requested":
                assert recovered_job.cancel_requested_at is not None
            if recovery_fault == "db_manifest_divergence":
                assert recovered_rows[0].remote_reference == "different-reference-than-manifest"
            if recovery_fault == "pending_marker_digest_mismatch":
                with database.read_session() as session:
                    state = session.get(DownloadManifestStateRow, str(job_id))
                    assert state is not None
                    assert state.publication_pending is True
                    assert state.manifest_sha256 == "0" * 64
            if recovery_fault == "attempt_exhausted":
                assert recovered_rows[0].attempts == MAX_DOWNLOAD_PAGE_ATTEMPTS
            return
        assert recovered_job.status is DownloadJobStatus.QUEUED
        assert recovered_job.recovery_blocked is False
        assert first_part.read_bytes() == partial_bytes

        origin = NetworkOrigin.from_url(page_server_base_url)
        transport = DownloadTransport(
            transport_client,
            DownloadTransportPolicy(
                allowed_origins=frozenset({origin}),
                max_bytes=V1_MAX_PAGE_BYTES,
                chunk_size=V1_STREAM_CHUNK_BYTES,
                connect_timeout_seconds=10,
                read_timeout_seconds=30,
                write_timeout_seconds=10,
                pool_timeout_seconds=10,
                max_redirects=5,
                max_retry_after_seconds=60,
                global_concurrency=2,
                per_host_concurrency=1,
            ),
            data_root=data_root,
            data_root_identity=database.data_root_identity,
            storage_gate=database.storage_gate,
            test_transport=test_transport,
            sleep=_skip_retry_delay,
        )
        reader = ReaderAssetService(
            database,
            data_root_identity=database.data_root_identity,
            limits=ReaderAssetLimits(
                max_cbz_bytes=V1_MAX_CBZ_BYTES,
                max_page_bytes=V1_MAX_PAGE_BYTES,
                chunk_bytes=V1_STREAM_CHUNK_BYTES,
            ),
        )
        publisher = AtomicFilePublisher(
            data_root_identity=database.data_root_identity,
            retry_policy=ADR_0087_RETRY_POLICY,
            sleep=time.sleep,
            clock=time.monotonic,
            storage_gate=database.storage_gate,
        )
        worker = DownloadWorker(
            database=database,
            data_root_identity=database.data_root_identity,
            connector_registry=registry,
            recovery_coordinator=restarted_recovery,
            transport=transport,
            image_validation_policy=ImageValidationPolicy(
                max_bytes=V1_MAX_PAGE_BYTES,
                max_width=40_000,
                max_height=40_000,
                max_pixels=100_000_000,
            ),
            cbz_builder=CBZBuilder(
                publisher=publisher,
                max_archive_bytes=V1_MAX_CBZ_BYTES,
                storage_gate=database.storage_gate,
            ),
            file_publisher=publisher,
            reader_asset_service=reader,
        )
        original_publish_snapshot = worker._page_writer.publish_snapshot  # pyright: ignore[reportPrivateUsage]

        def record_snapshot(
            target_job_id: UUID,
            pages: tuple[DownloadPage, ...],
        ) -> object:
            result = original_publish_snapshot(target_job_id, pages)
            published_snapshots.append(pages)
            return result

        worker._page_writer.publish_snapshot = record_snapshot  # type: ignore[method-assign]
        second_attempt_path = DownloadPageRepository.attempt_path(job_id, 1, 2)
        reservation_seen_before_create: list[bool] = []
        original_create = transport._page_store.create  # pyright: ignore[reportPrivateUsage]

        @contextmanager
        def check_reservation_before_create(
            relative_path: str,
            **kwargs: Any,
        ) -> Generator[filesystem_retry.RetryingFileStream]:
            if relative_path == second_attempt_path:
                with database.read_session() as session:
                    latest = job_repository.get(session, job_id)
                    assert latest is not None
                    durable = DownloadPageRepository().project_manifest(
                        session,
                        latest,
                    )
                reservation_seen_before_create.append(
                    durable.pages[0].status == "DOWNLOADING"
                    and durable.pages[0].attempts == 2
                    and durable.pages[0].temporary_path == second_attempt_path
                    and not (data_root / "temp" / second_attempt_path).exists()
                )
                assert reservation_seen_before_create[-1]
            with original_create(relative_path, **kwargs) as stream:
                yield stream

        transport._page_store.create = check_reservation_before_create  # type: ignore[method-assign]
        result = await worker.run_job(job_id)

        if result.error_code is not None:
            failures = [
                {
                    "logger": record.name,
                    "message": record.getMessage(),
                    "stage": getattr(record, "stage", None),
                    "cause_type": getattr(record, "cause_type", None),
                    "error_code": getattr(record, "error_code", None),
                }
                for record in caplog.records
            ]
            raise AssertionError(f"recovered worker failed: {failures!r}")
        assert result.job.status is DownloadJobStatus.COMPLETED
        assert result.page_count == len(references)
        expected_request_count = len(references) - (
            1 if reuse_completed_page else 0
        )
        assert len(page_requests) == expected_request_count
        assert all("range" not in request.headers for request in page_requests)
        assert reservation_seen_before_create == ([] if reuse_completed_page else [True])
        if reuse_completed_page:
            assert not any(
                request.url.path.endswith("mock-chapter-1/page-2")
                for request in page_requests
            )
        else:
            assert any(
                snapshot[0].status == "DOWNLOADING"
                and snapshot[0].attempts == 2
                and snapshot[0].temporary_path == second_attempt_path
                for snapshot in published_snapshots
            )
            assert first_part.read_bytes() == partial_bytes
    finally:
        if transport_client is not None:
            await transport_client.aclose()
        database.shutdown()


async def _exercise_download(
    tmp_path: Path,
    *,
    library_root: Path | None = None,
    scenario: MockScenario = MockScenario.NORMAL,
    pause_at: DownloadJobStatus | None = None,
    network_origins: frozenset[str] | None = None,
    disk_full_during_page_flush: bool = False,
    disk_full_during_cbz_write: str | None = None,
    disk_full_during_packaging_mkdir: bool = False,
    cancel_during_cbz_build: bool = False,
    cancel_before_index_commit: bool = False,
    cancel_during_index_transaction: bool = False,
    enable_disk_budget: bool = False,
    expected_resolution_failure_stage: str | None = None,
    expected_resolution_integrity_block: bool = False,
    concurrency_probe_path: str | None = None,
    expected_peak_concurrency: int | None = None,
    expected_page_references: tuple[str, ...] | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
    caplog: pytest.LogCaptureFixture | None = None,
) -> None:
    with MockMangaServer(
        scenario=scenario,
        concurrency_probe_path=concurrency_probe_path,
    ) as server:
        data_root = tmp_path / "data"
        database = open_test_database(data_root)
        manga_root = library_root or data_root
        manga_root.mkdir(parents=True, exist_ok=True)
        library_root_identity = (
            database.data_root_identity
            if manga_root == data_root
            else DataRootIdentity.capture(manga_root)
        )
        MigrationRunner(database, data_root / "backups").upgrade_to_head()
        connector = MockConnector(page_server_base_url=server.base_url)
        secret_vault = WindowsDpapiSecretVault(data_root)
        registry = ConnectorRegistry(
            context=ConnectorHostContext(secret_vault=secret_vault),
            registrations=(
                ConnectorRegistration(
                    metadata=connector.metadata,
                    factory=lambda _context: connector,
                    network_origins=(
                        frozenset({server.base_url})
                        if network_origins is None
                        else network_origins
                    ),
                ),
            ),
        )

        series_metadata = connector.normalize_metadata(
            await connector.get_series("mock-series-1")
        )
        chapters = await connector.get_chapters("mock-series-1")
        selection = DownloadJobService(database).create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=series_metadata,
            chapters=chapters,
            selected_remote_ids=("mock-chapter-1",),
        )[0]
        disk_budget_manager: DiskBudgetManager | None = None
        if enable_disk_budget:
            baseline = DiskBudgetManager(database).snapshot()
            assert baseline.recovery_demand_bytes is not None
            available_for_test = (
                baseline.recovery_demand_bytes
                + MINIMUM_RESERVED_BYTES
                + PAGE_TEMP_RESERVATION_BYTES
                + CBZ_RESERVATION_BYTES
                + 256 * 1024 * 1024
            )
            capacity_for_test = 1 << 50
            disk_budget_manager = DiskBudgetManager(
                database,
                disk_usage_provider=lambda _path: SimpleNamespace(
                    total=capacity_for_test,
                    used=capacity_for_test - available_for_test,
                    free=available_for_test,
                ),
            )
        origin = NetworkOrigin.from_url(server.base_url)
        async_transport = httpx.AsyncHTTPTransport()
        async with httpx.AsyncClient(transport=async_transport) as client:
            transport = DownloadTransport(
                client,
                DownloadTransportPolicy(
                    allowed_origins=frozenset({origin}),
                    max_bytes=V1_MAX_PAGE_BYTES,
                    chunk_size=V1_STREAM_CHUNK_BYTES,
                    connect_timeout_seconds=10,
                    read_timeout_seconds=30,
                    write_timeout_seconds=10,
                    pool_timeout_seconds=10,
                    max_redirects=5,
                    max_retry_after_seconds=60,
                    global_concurrency=2,
                    per_host_concurrency=2,
                ),
                data_root=data_root,
                data_root_identity=database.data_root_identity,
                storage_gate=database.storage_gate,
                disk_budget_manager=disk_budget_manager,
                test_transport=async_transport,
                sleep=_skip_retry_delay,
            )
            page_fsync_failures = 0
            cbz_disk_full_failures: list[str] = []
            result: DownloadWorkerResult | None = None
            run_task: asyncio.Task[DownloadWorkerResult] | None = None
            if disk_full_during_page_flush:
                if monkeypatch is None:
                    raise AssertionError("disk-full scenario requires monkeypatch")
                page_file_descriptor: list[int | None] = [None]
                original_create = transport._page_store.create  # pyright: ignore[reportPrivateUsage]

                @contextmanager
                def track_page_file(
                    relative_path: str,
                    **kwargs: Any,
                ) -> Generator[filesystem_retry.RetryingFileStream]:
                    with original_create(relative_path, **kwargs) as stream:
                        page_file_descriptor[0] = stream.fileno()
                        yield stream

                monkeypatch.setattr(
                    transport._page_store,  # pyright: ignore[reportPrivateUsage]
                    "create",
                    track_page_file,
                )
                original_fsync = os.fsync

                def fail_page_fsync(file_descriptor: int) -> None:
                    nonlocal page_fsync_failures
                    if (
                        file_descriptor == page_file_descriptor[0]
                        and page_fsync_failures == 0
                    ):
                        page_fsync_failures += 1
                        raise OSError(errno.ENOSPC, "disk full")
                    original_fsync(file_descriptor)

                monkeypatch.setattr(
                    "app.services.download_transport.os.fsync",
                    fail_page_fsync,
                )
            recovery = RecoveryCoordinator(database, database.data_root_identity)
            reader = ReaderAssetService(
                database,
                data_root_identity=database.data_root_identity,
                library_root_identity=library_root_identity,
                limits=ReaderAssetLimits(
                    max_cbz_bytes=V1_MAX_CBZ_BYTES,
                    max_page_bytes=V1_MAX_PAGE_BYTES,
                    chunk_bytes=V1_STREAM_CHUNK_BYTES,
                ),
            )
            index_operation_entered = threading.Event()
            release_index_operation = threading.Event()
            if cancel_before_index_commit:
                original_verify_archive = reader._verify_archive_file  # pyright: ignore[reportPrivateUsage]

                def block_archive_verification(
                    *args: object,
                    **kwargs: object,
                ) -> None:
                    index_operation_entered.set()
                    if not release_index_operation.wait(timeout=5):
                        raise TimeoutError("test did not release archive verification")
                    original_verify_archive(*args, **kwargs)  # type: ignore[arg-type]

                reader._verify_archive_file = block_archive_verification  # type: ignore[method-assign]
            elif cancel_during_index_transaction:
                original_add_link = reader._repository.add  # pyright: ignore[reportPrivateUsage]

                def block_index_link_add(
                    session: Session,
                    link: ChapterCbzAsset,
                ) -> None:
                    original_add_link(session, link)
                    index_operation_entered.set()
                    if not release_index_operation.wait(timeout=5):
                        raise TimeoutError("test did not release index transaction")

                reader._repository.add = block_index_link_add  # type: ignore[method-assign]
            publisher = AtomicFilePublisher(
                data_root_identity=database.data_root_identity,
                retry_policy=ADR_0087_RETRY_POLICY,
                sleep=time.sleep,
                clock=time.monotonic,
                storage_gate=database.storage_gate,
            )
            library_publisher = (
                publisher
                if library_root_identity is database.data_root_identity
                else AtomicFilePublisher(
                    data_root_identity=library_root_identity,
                    retry_policy=ADR_0087_RETRY_POLICY,
                    sleep=time.sleep,
                    clock=time.monotonic,
                    storage_gate=database.storage_gate,
                )
            )
            builder = CBZBuilder(
                publisher=library_publisher,
                max_archive_bytes=V1_MAX_CBZ_BYTES,
                storage_gate=database.storage_gate,
                disk_budget_manager=disk_budget_manager,
            )
            if disk_full_during_packaging_mkdir:
                if monkeypatch is None:
                    raise AssertionError("directory disk-full scenario requires monkeypatch")
                real_mkdir: Any = Path.mkdir
                job_id_text = str(selection.job.job_id)

                def fail_packaging_directory(
                    path: Path,
                    *args: Any,
                    **kwargs: Any,
                ) -> None:
                    if path.name == "packaging" and path.parent.name == job_id_text:
                        raise OSError(errno.ENOSPC, "injected packaging directory disk full")
                    real_mkdir(path, *args, **kwargs)

                monkeypatch.setattr(Path, "mkdir", fail_packaging_directory)
            if disk_full_during_cbz_write is not None:
                if monkeypatch is None:
                    raise AssertionError("CBZ disk-full scenario requires monkeypatch")
                real_open: Any = Path.open

                class DiskFullArchiveStream:
                    def __init__(self, stream: Any) -> None:
                        self._stream = stream
                        self._failed = False

                    def __enter__(self) -> DiskFullArchiveStream:
                        return self

                    def __exit__(
                        self,
                        exc_type: object,
                        _exc: object,
                        _traceback: object,
                    ) -> None:
                        self._stream.close()

                    def write(self, value: bytes) -> int:
                        if disk_full_during_cbz_write == "write" and not self._failed:
                            self._failed = True
                            self._stream.write(value[: min(8, len(value))])
                            cbz_disk_full_failures.append("write")
                            raise OSError(
                                errno.ENOSPC,
                                "injected integrated CBZ staging write disk full",
                            )
                        return self._stream.write(value)

                    def flush(self) -> None:
                        self._stream.flush()
                        if disk_full_during_cbz_write == "flush" and not self._failed:
                            self._failed = True
                            cbz_disk_full_failures.append("flush")
                            raise OSError(
                                errno.ENOSPC,
                                "injected integrated CBZ staging flush disk full",
                            )

                    def close(self) -> None:
                        self._stream.close()

                    def __getattr__(self, name: str) -> Any:
                        return getattr(self._stream, name)

                def patch_archive_open(
                    path: Path,
                    mode: str = "r",
                    *args: Any,
                    **kwargs: Any,
                ) -> Any:
                    stream = real_open(path, mode, *args, **kwargs)
                    if path.name.endswith(".cbz.tmp") and mode == "x+b":
                        return DiskFullArchiveStream(stream)
                    return stream

                monkeypatch.setattr(Path, "open", patch_archive_open)
            build_entered = threading.Event()
            release_build = threading.Event()
            if cancel_during_cbz_build:
                original_build = builder.build

                def block_cbz_build(**kwargs: object):
                    build_entered.set()
                    if not release_build.wait(timeout=5):
                        raise TimeoutError("test did not release the CBZ build")
                    return original_build(**kwargs)  # type: ignore[arg-type]

                builder.build = block_cbz_build  # type: ignore[method-assign]
            pause_controller = (
                JobPauseController()
                if (
                    pause_at is not None
                    or cancel_during_cbz_build
                    or cancel_before_index_commit
                    or cancel_during_index_transaction
                )
                else None
            )
            checkpoint_statuses: list[DownloadJobStatus] = []
            worker = DownloadWorker(
                database=database,
                data_root_identity=database.data_root_identity,
                library_root_identity=library_root_identity,
                connector_registry=registry,
                recovery_coordinator=recovery,
                transport=transport,
                image_validation_policy=ImageValidationPolicy(
                    max_bytes=V1_MAX_PAGE_BYTES,
                    max_width=40_000,
                    max_height=40_000,
                    max_pixels=100_000_000,
                ),
                cbz_builder=builder,
                file_publisher=publisher,
                library_file_publisher=library_publisher,
                reader_asset_service=reader,
                pause_controller=pause_controller,
            )
            if pause_controller is not None:
                await pause_controller.register(selection.job.job_id)
                if pause_at is DownloadJobStatus.FETCHING_METADATA:
                    pause_controller.request_pause(
                        selection.job.job_id,
                        reason=PauseReason.BACKPRESSURE,
                    )
                elif pause_at is not None:
                    checkpoint = pause_controller.pause_if_requested
                    requested = False

                    async def request_packaging_pause(
                        job_id: UUID,
                        *,
                        status: DownloadJobStatus,
                        evidence: PauseSafetyEvidence,
                    ) -> PauseCheckpointOutcome:
                        nonlocal requested
                        checkpoint_statuses.append(status)
                        if status is pause_at and not requested:
                            requested = True
                            pause_controller.request_pause(
                                job_id,
                                reason=PauseReason.BACKPRESSURE,
                            )
                        return await checkpoint(
                            job_id,
                            status=status,
                            evidence=evidence,
                        )

                    pause_controller.pause_if_requested = request_packaging_pause

            if (
                disk_full_during_page_flush
                or disk_full_during_cbz_write is not None
                or disk_full_during_packaging_mkdir
            ):
                from app.services.download_dispatcher import DownloadDispatcher

                class ResultCapturingRunner:
                    def __init__(self) -> None:
                        self.results: list[DownloadWorkerResult] = []

                    async def run_job(self, job_id: UUID) -> DownloadWorkerResult:
                        result = await worker.run_job(job_id)
                        self.results.append(result)
                        return result

                capturing_runner = ResultCapturingRunner()
                dispatcher = DownloadDispatcher(
                    database=database,
                    worker=capturing_runner,
                    recovery_coordinator=recovery,
                    poll_interval_seconds=0.05,
                )
                await dispatcher.start()
                try:
                    async with asyncio.timeout(_INTEGRATION_STAGE_TIMEOUT_SECONDS):
                        while not database.storage_gate.closed:
                            await asyncio.sleep(0.01)
                    await dispatcher.stop()
                finally:
                    await dispatcher.stop()
                assert len(capturing_runner.results) == 1
                result = capturing_runner.results[0]
            else:
                if disk_budget_manager is not None:
                    page_bytes, cbz_bytes = worker.existing_artifact_bytes_for_budget(
                        selection.job.job_id
                    )
                    disk_budget_manager.reserve(
                        selection.job.job_id,
                        existing_page_bytes=page_bytes,
                        existing_cbz_bytes=cbz_bytes,
                    )
                run_task = asyncio.create_task(worker.run_job(selection.job.job_id))
            if cancel_during_cbz_build:
                assert pause_controller is not None
                await asyncio.wait_for(
                    asyncio.to_thread(
                        build_entered.wait,
                        _INTEGRATION_STAGE_TIMEOUT_SECONDS,
                    ),
                    timeout=_INTEGRATION_STAGE_TIMEOUT_SECONDS + 1,
                )
                requested = DownloadJobService(database).cancel_job(
                    selection.job.job_id
                )
                assert requested is not None
                assert requested.job.status is DownloadJobStatus.PACKAGING
                assert requested.job.cancel_requested_at is not None
                pause_controller.notify_cancellation_requested(
                    selection.job.job_id
                )
                try:
                    with database.read_session() as session:
                        while_building = DownloadJobRepository().get(
                            session,
                            selection.job.job_id,
                        )
                    assert while_building is not None
                    assert while_building.status is DownloadJobStatus.PACKAGING
                    assert while_building.cancel_requested_at is not None
                finally:
                    release_build.set()
            elif cancel_before_index_commit:
                assert pause_controller is not None
                await asyncio.wait_for(
                    asyncio.to_thread(
                        index_operation_entered.wait,
                        _INTEGRATION_STAGE_TIMEOUT_SECONDS,
                    ),
                    timeout=_INTEGRATION_STAGE_TIMEOUT_SECONDS + 1,
                )
                try:
                    requested = DownloadJobService(database).cancel_job(
                        selection.job.job_id
                    )
                    assert requested is not None
                    assert requested.job.status is DownloadJobStatus.INDEXING
                    assert requested.job.cancel_requested_at is not None
                    pause_controller.notify_cancellation_requested(
                        selection.job.job_id
                    )
                finally:
                    release_index_operation.set()
            elif cancel_during_index_transaction:
                assert pause_controller is not None
                await asyncio.wait_for(
                    asyncio.to_thread(
                        index_operation_entered.wait,
                        _INTEGRATION_STAGE_TIMEOUT_SECONDS,
                    ),
                    timeout=_INTEGRATION_STAGE_TIMEOUT_SECONDS + 1,
                )
                cancel_started = threading.Event()

                def request_cancel_while_indexing() -> object:
                    cancel_started.set()
                    return DownloadJobService(database).cancel_job(
                        selection.job.job_id
                    )

                cancel_task = asyncio.create_task(
                    asyncio.to_thread(request_cancel_while_indexing)
                )
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(cancel_started.wait, 5),
                        timeout=6,
                    )
                    await asyncio.sleep(0.05)
                    request_waited_for_writer = not cancel_task.done()
                    with database.read_session() as session:
                        inside_transaction = DownloadJobRepository().get(
                            session,
                            selection.job.job_id,
                        )
                    assert inside_transaction is not None
                    assert inside_transaction.status is DownloadJobStatus.INDEXING
                    assert inside_transaction.cancel_requested_at is None
                finally:
                    release_index_operation.set()
                assert request_waited_for_writer
                try:
                    await cancel_task
                except JobCancellationNotAllowed as error:
                    assert error.status is DownloadJobStatus.COMPLETED
                else:
                    raise AssertionError("cancellation unexpectedly beat index commit")
            if pause_controller is not None and pause_at is not None:
                await _wait_for_pause(pause_controller, selection.job.job_id)
                with database.read_session() as session:
                    paused_job = DownloadJobRepository().get(
                        session,
                        selection.job.job_id,
                    )
                assert paused_job is not None
                assert paused_job.status is pause_at
                if pause_at is DownloadJobStatus.FETCHING_METADATA:
                    with database.read_session() as session:
                        rows = (
                            session.query(DownloadPageRow)
                            .filter_by(job_id=str(selection.job.job_id))
                            .order_by(DownloadPageRow.ordinal)
                            .all()
                        )
                    assert len(rows) == 3
                    assert [row.status for row in rows] == ["PENDING"] * 3
                elif pause_at is DownloadJobStatus.DOWNLOADING:
                    with database.read_session() as session:
                        rows = (
                            session.query(DownloadPageRow)
                            .filter_by(job_id=str(selection.job.job_id))
                            .order_by(DownloadPageRow.ordinal)
                            .all()
                        )
                        assert [row.status for row in rows] == [
                            "DOWNLOADED",
                            "DOWNLOADED",
                            "PENDING",
                        ]
                elif pause_at is DownloadJobStatus.PACKAGING:
                    assert not (data_root / "library").exists()
                pause_controller.resume(
                    selection.job.job_id,
                    reason=PauseReason.BACKPRESSURE,
                )
            if run_task is not None:
                result = await run_task
                if expected_peak_concurrency is not None:
                    assert server.peak_concurrency == expected_peak_concurrency
                if expected_page_references is not None:
                    with database.read_session() as session:
                        rows = (
                            session.query(DownloadPageRow)
                            .filter_by(job_id=str(selection.job.job_id))
                            .order_by(DownloadPageRow.ordinal)
                            .all()
                        )
                    assert tuple(row.remote_reference for row in rows) == expected_page_references
                if disk_budget_manager is not None:
                    assert disk_budget_manager.snapshot().reserved_bytes < (
                        PAGE_TEMP_RESERVATION_BYTES + CBZ_RESERVATION_BYTES
                    )
                    disk_budget_manager.release(
                        selection.job.job_id,
                        quiescent=True,
                    )
                    assert disk_budget_manager.snapshot().reserved_bytes == 0
            assert result is not None
            budget_states_attribute = "_job_budget_states"
            assert selection.job.job_id not in cast(
                dict[UUID, object],
                getattr(transport, budget_states_attribute),
            )
            if pause_controller is not None and pause_at is DownloadJobStatus.PACKAGING:
                assert DownloadJobStatus.VERIFYING not in checkpoint_statuses
                assert DownloadJobStatus.INDEXING not in checkpoint_statuses
            if pause_controller is not None:
                await pause_controller.unregister(selection.job.job_id)

        with database.read_session() as session:
            job = DownloadJobRepository().get(session, selection.job.job_id)
            assert job is not None
        if disk_full_during_page_flush:
            assert caplog is not None
            assert page_fsync_failures == 1
            assert database.storage_gate.closed is True
            assert result.job.status is DownloadJobStatus.DOWNLOADING
            assert result.error_code == "DL_JOB_RECOVERY_BLOCKED"
            assert result.failure_stage == "shared_storage_gate_closed"
            assert result.trace_id
            assert job.status is DownloadJobStatus.INTERRUPTED
            assert job.interrupted_from_status is DownloadJobStatus.DOWNLOADING
            assert job.cancel_requested_at is None
            assert job.recovery_blocked is True
            assert not (data_root / "library").exists()

            with database.read_session() as session:
                rows = (
                    session.query(DownloadPageRow)
                    .filter_by(job_id=str(selection.job.job_id))
                    .order_by(DownloadPageRow.ordinal)
                    .all()
                )
            assert len(rows) == 3
            assert [row.status for row in rows] == [
                "DOWNLOADING",
                "DOWNLOADING",
                "PENDING",
            ]
            assert [row.attempts for row in rows] == [1, 1, 0]
            assert rows[0].attempts == 1
            preserved_parts: list[Path] = []
            for row in rows[:2]:
                assert row.temporary_path is not None
                preserved_part = data_root / "temp" / row.temporary_path
                assert preserved_part.suffix == ".part"
                if preserved_part.exists():
                    preserved_parts.append(preserved_part)
                    assert preserved_part.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            assert preserved_parts
            assert rows[2].temporary_path is None
        elif disk_full_during_cbz_write:
            assert database.storage_gate.closed is True
            assert cbz_disk_full_failures == [disk_full_during_cbz_write]
            assert result.chapter_id is not None
            assert result.series_id is not None
            assert result.job.status is DownloadJobStatus.PACKAGING
            assert result.error_code == "DL_JOB_RECOVERY_BLOCKED"
            assert result.failure_stage == "shared_storage_gate_closed"
            assert job.status is DownloadJobStatus.INTERRUPTED
            assert job.interrupted_from_status is DownloadJobStatus.PACKAGING
            assert job.recovery_blocked is True
            assert not (manga_root / "chapters").exists()
            staged_cbz_tmp = (
                manga_root
                / ".yomira-staging"
                / "jobs"
                / str(selection.job.job_id)
                / "packaging"
                / f"{result.chapter_id}.cbz.tmp"
            )
            assert staged_cbz_tmp.is_file()
            assert staged_cbz_tmp.stat().st_size > 0
            with database.read_session() as session:
                rows = (
                    session.query(DownloadPageRow)
                    .filter_by(job_id=str(selection.job.job_id))
                    .order_by(DownloadPageRow.ordinal)
                    .all()
                )
            assert len(rows) == 3
            assert [row.status for row in rows] == ["VALIDATED"] * 3
            preserved_pages = [
                data_root / "temp" / row.temporary_path
                for row in rows
                if row.temporary_path is not None
            ]
            assert len(preserved_pages) == 3
            assert all(path.is_file() for path in preserved_pages)
            assert all(
                path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
                for path in preserved_pages
            )
            assert reader.list_chapters_for_series(result.series_id).total == 0
        elif disk_full_during_packaging_mkdir:
            assert database.storage_gate.closed is True
            assert result.job.status is DownloadJobStatus.PACKAGING
            assert result.error_code == "DL_JOB_RECOVERY_BLOCKED"
            assert result.failure_stage == "shared_storage_gate_closed"
            assert job.status is DownloadJobStatus.INTERRUPTED
            assert job.interrupted_from_status is DownloadJobStatus.PACKAGING
            assert job.recovery_blocked is True
            packaging_directory = (
                manga_root
                / ".yomira-staging"
                / "jobs"
                / str(selection.job.job_id)
                / "packaging"
            )
            assert not packaging_directory.exists()
            assert not (manga_root / "chapters").exists()
        elif cancel_during_cbz_build or cancel_before_index_commit:
            assert result.job.status is DownloadJobStatus.CANCELLED
            assert job.status is DownloadJobStatus.CANCELLED
            assert job.cancel_requested_at is not None
            published_cbzs = (
                list((manga_root / "chapters").rglob("*.cbz"))
                if (manga_root / "chapters").exists()
                else []
            )
            assert published_cbzs == []
            assert result.series_id is not None
            assert reader.list_chapters_for_series(result.series_id).total == 0
            staged_cbz = (
                manga_root
                / ".yomira-staging"
                / "jobs"
                / str(selection.job.job_id)
                / "packaging"
                / f"{result.chapter_id}.cbz"
            )
            assert staged_cbz.is_file()
            with zipfile.ZipFile(staged_cbz) as archive:
                assert archive.testzip() is None
        elif network_origins == frozenset():
            assert result.job.status is DownloadJobStatus.FAILED
            assert result.error_code == "SRC_CAPABILITY_UNSUPPORTED"
            assert result.failure_stage == "page_origin_validation"
            assert job.status is DownloadJobStatus.FAILED
            assert not (data_root / "library").exists()
        elif expected_resolution_failure_stage is not None:
            expected_status = (
                DownloadJobStatus.INTERRUPTED
                if expected_resolution_integrity_block
                else DownloadJobStatus.FAILED
            )
            assert result.job.status is expected_status
            assert job.status is expected_status
            assert job.recovery_blocked is expected_resolution_integrity_block
            assert result.failure_stage == expected_resolution_failure_stage
            assert result.trace_id
            assert result.asset is None
            assert not (data_root / "library").exists()
            with database.read_session() as session:
                rows = session.query(DownloadPageRow).filter_by(
                    job_id=str(selection.job.job_id)
                ).order_by(DownloadPageRow.ordinal).all()
            assert [row.remote_reference for row in rows] == [
                "mock-chapter-1/page-2",
                "mock-chapter-1/page-1",
                "mock-chapter-1/page-1",
            ]
            expected_page_statuses = (
                ["DOWNLOADING", "DOWNLOADING", "PENDING"]
                if expected_resolution_integrity_block
                else ["FAILED"] * 3
            )
            assert [row.status for row in rows] == expected_page_statuses
            assert [row.attempts for row in rows] == [1, 1, 0]
            assert list((data_root / "temp").rglob("*.part")) == []
        elif scenario in {MockScenario.NORMAL, MockScenario.SLOW}:
            assert result.job.status is DownloadJobStatus.COMPLETED
            assert result.page_count == 3
            assert result.chapter_id is not None
            assert result.series_id is not None
            assert job.status is DownloadJobStatus.COMPLETED
            assert result.asset is not None
            assert result.asset.path.is_relative_to(manga_root / "chapters")

            chapters_in_reader = reader.list_chapters_for_series(result.series_id)
            assert chapters_in_reader.total == 1
            assert chapters_in_reader.items[0].page_count == 3
            page = reader.open_page(result.chapter_id, 1)
            assert page.media_type == "image/png"
            assert b"".join(page.iter_chunks()).startswith(b"\x89PNG\r\n\x1a\n")

            database.shutdown()
            database = reopen_test_database(database)
            reopened_reader = ReaderAssetService(
                database,
                data_root_identity=database.data_root_identity,
                library_root_identity=library_root_identity,
                limits=ReaderAssetLimits(
                    max_cbz_bytes=V1_MAX_CBZ_BYTES,
                    max_page_bytes=V1_MAX_PAGE_BYTES,
                    chunk_bytes=V1_STREAM_CHUNK_BYTES,
                ),
            )
            reopened_page = reopened_reader.open_page(result.chapter_id, 3)
            assert reopened_page.media_type == "image/png"
            assert b"".join(reopened_page.iter_chunks()).startswith(b"\x89PNG\r\n\x1a\n")
        else:
            assert result.job.status is DownloadJobStatus.FAILED
            assert result.error_code is not None
            assert result.error_code.startswith("IMG_")
            assert result.failure_stage == "image_validation"
            assert job.status is DownloadJobStatus.FAILED
            with database.read_session() as session:
                rows = session.query(DownloadPageRow).filter_by(
                    job_id=str(selection.job.job_id)
                ).order_by(DownloadPageRow.ordinal).all()
            assert len(rows) == 3
            assert [row.status for row in rows] == ["VALIDATED", "FAILED", "FAILED"]
            retained_paths = [
                data_root / "temp" / row.temporary_path
                for row in rows
                if row.temporary_path is not None
            ]
            assert len(retained_paths) == 3
            part_files = [path for path in retained_paths if path.suffix == ".part"]
            validated_files = [path for path in retained_paths if path.suffix == ".png"]
            assert len(part_files) == 2
            assert all(path.read_bytes().startswith(b"<!doctype html>") for path in part_files)
            assert len(validated_files) == 1
            assert validated_files[0].read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            assert not (data_root / "library").exists()
        database.shutdown()


async def _wait_for_pause(controller: JobPauseController, job_id: UUID) -> None:
    async def wait_until_paused() -> None:
        while controller.state(job_id) is not JobPauseState.PAUSED:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(
        wait_until_paused(),
        timeout=_INTEGRATION_STAGE_TIMEOUT_SECONDS,
    )


async def _skip_retry_delay(_delay_seconds: float) -> None:
    return
