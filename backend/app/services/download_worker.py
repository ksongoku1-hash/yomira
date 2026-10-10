"""One bounded, durable page-to-CBZ worker for an already queued job."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections.abc import Awaitable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy import select

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.metadata import ConnectorCapability
from app.connectors.pages import ConnectorPageReference
from app.connectors.protocol import AcquisitionProvider
from app.connectors.registry import ConnectorRegistry, RegisteredSourceConnector
from app.domain.download_job import DownloadJob, DownloadJobStatus
from app.domain.download_page import MAX_DOWNLOAD_PAGE_ATTEMPTS, DownloadPage
from app.domain.job_cancellation import (
    CancellationRequestPending,
    CancellationSafetyEvidence,
)
from app.domain.job_manifest import MAX_MANIFEST_PAGE_COUNT, DownloadJobManifest
from app.domain.job_pause import (
    JobPauseController,
    PauseCheckpointOutcome,
    PauseSafetyEvidence,
    UnknownActiveJob,
)
from app.domain.metadata import MetadataField
from app.domain.source import SourceHealthStatus
from app.errors import ErrorCode
from app.fmd2_bridge import Fmd2Connector
from app.persistence.atomic_file_publisher import AtomicFilePublisher
from app.persistence.database import SQLiteDatabase
from app.persistence.filesystem_retry import (
    RetryingFileStream,
    retry_lstat,
)
from app.persistence.managed_directories import ensure_managed_directory
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.series import SeriesRow
from app.persistence.models.series_source import SeriesSourceRow
from app.persistence.models.source import SourceRow
from app.persistence.repositories.download_job import (
    DownloadJobRepository,
    StaleJobStateError,
)
from app.persistence.repositories.download_page import DownloadPageRepository
from app.persistence.storage_gate import StorageGateClosedError
from app.persistence.windows_paths import DataRootIdentity
from app.services.cbz_builder import (
    MAX_CBZ_BYTES_V1,
    CBZBuilder,
    CBZBuildError,
    CBZBuildResult,
    ComicInfoMetadata,
    ValidatedPage,
)
from app.services.download_transport import (
    DownloadCancellationRequested,
    DownloadTransport,
    DownloadTransportError,
    NetworkOrigin,
)
from app.services.image_validation import (
    ImageValidationError,
    ImageValidationErrorCode,
    ImageValidationPolicy,
    validate_image_file,
)
from app.services.job_recovery import (
    RecoveryCoordinator,
    WorkerQuiescenceAck,
)
from app.services.metadata import MetadataService
from app.services.page_manifest_writer import (
    PageManifestWriteBlocked,
    PageManifestWriter,
    StalePageManifestSnapshot,
)
from app.services.reader_asset import ReaderAssetError, ReaderAssetService
from app.tracing import activate_trace_id, create_trace_id, reset_trace_id

logger = logging.getLogger("yomira.download_worker")
_IMAGE_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_MAX_PAGES_IN_FLIGHT_PER_JOB = 2


def _is_sqlite_database_instance(value: object) -> bool:
    return isinstance(value, SQLiteDatabase)


class DownloadWorkerInputError(RuntimeError):
    """A requested job is absent, incomplete, or not currently dispatchable."""


class DownloadWorkerIntegrityError(RuntimeError):
    """Stored source/job lineage cannot be used without risking false association."""


class _JobCancellationRequested(Exception):
    """An interruptible connector operation was stopped after durable intent."""


class _CancellationCompleted(Exception):
    def __init__(self, job: DownloadJob) -> None:
        self.job = job
        super().__init__("Download job cancellation completed at a safe point.")


@dataclass(frozen=True, slots=True)
class DownloadWorkerResult:
    job: DownloadJob
    series_id: UUID
    chapter_id: UUID
    page_count: int
    asset: CBZBuildResult | None
    error_code: str | None
    failure_stage: str | None
    trace_id: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class _JobLineage:
    job: DownloadJob
    connector_id: str
    connector_version: str
    remote_chapter_reference: str
    chapter_title: str
    series_id: UUID
    chapter_id: UUID


@dataclass(frozen=True, slots=True)
class _PreparedPagePlan:
    connector: RegisteredSourceConnector
    references: tuple[ConnectorPageReference, ...]
    allowed_origins: frozenset[NetworkOrigin]


@dataclass(slots=True)
class _JobExecution:
    job_id: UUID
    trace_id: str
    lineage: _JobLineage
    job_state: list[DownloadJob]
    page_state: list[DownloadPage]
    content_types: list[str]
    validated_pages: list[ValidatedPage]
    stage: str = "job_claim"
    staged_cbz_path: Path | None = None
    published_cbz_path: Path | None = None


class DownloadWorker:
    """Run one job with short DB sessions and preserved filesystem evidence."""

    def __init__(
        self,
        *,
        database: SQLiteDatabase,
        data_root_identity: DataRootIdentity,
        library_root_identity: DataRootIdentity | None = None,
        connector_registry: ConnectorRegistry,
        recovery_coordinator: RecoveryCoordinator,
        transport: DownloadTransport,
        image_validation_policy: ImageValidationPolicy,
        cbz_builder: CBZBuilder,
        file_publisher: AtomicFilePublisher,
        library_file_publisher: AtomicFilePublisher | None = None,
        reader_asset_service: ReaderAssetService,
        pause_controller: JobPauseController | None = None,
    ) -> None:
        if not _is_sqlite_database_instance(database):
            raise TypeError("database must be a SQLiteDatabase")
        if type(data_root_identity) is not DataRootIdentity:
            raise TypeError("data_root_identity must be a DataRootIdentity")
        if database.data_root_identity is not data_root_identity:
            raise ValueError("database and worker must share one data root identity")
        if (
            library_root_identity is not None
            and type(library_root_identity) is not DataRootIdentity
        ):
            raise TypeError("library_root_identity must be a DataRootIdentity or None")
        if recovery_coordinator.database is not database:
            raise ValueError("recovery coordinator must use the worker database")
        if type(connector_registry) is not ConnectorRegistry:
            raise TypeError("connector_registry must be a ConnectorRegistry")
        if type(transport) is not DownloadTransport:
            raise TypeError("transport must be a DownloadTransport")
        if type(image_validation_policy) is not ImageValidationPolicy:
            raise TypeError("image_validation_policy must be an ImageValidationPolicy")
        if type(cbz_builder) is not CBZBuilder:
            raise TypeError("cbz_builder must be a CBZBuilder")
        if type(file_publisher) is not AtomicFilePublisher:
            raise TypeError("file_publisher must be an AtomicFilePublisher")
        if (
            library_file_publisher is not None
            and type(library_file_publisher) is not AtomicFilePublisher
        ):
            raise TypeError("library_file_publisher must be an AtomicFilePublisher or None")
        if type(reader_asset_service) is not ReaderAssetService:
            raise TypeError("reader_asset_service must be a ReaderAssetService")
        if pause_controller is not None and type(pause_controller) is not JobPauseController:
            raise TypeError("pause_controller must be a JobPauseController")

        self._database = database
        self._data_root_identity = data_root_identity
        self._library_root_identity = library_root_identity or data_root_identity
        self._connector_registry = connector_registry
        self._recovery = recovery_coordinator
        self._transport = transport
        self._image_validation_policy = image_validation_policy
        self._cbz_builder = cbz_builder
        self._file_publisher = file_publisher
        self._library_file_publisher = library_file_publisher or file_publisher
        self._reader_asset_service = reader_asset_service
        self._pause_controller = pause_controller
        self._job_repository = DownloadJobRepository()
        self._page_writer = PageManifestWriter(
            database,
            manifest_store=recovery_coordinator.manifest_store,
        )

    async def run_job(self, job_id: UUID) -> DownloadWorkerResult:
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        trace_id = create_trace_id()
        trace_token = activate_trace_id(trace_id)
        try:
            lineage = await asyncio.to_thread(self._read_lineage, job_id)
            if lineage.job.status is not DownloadJobStatus.QUEUED:
                raise DownloadWorkerInputError("Only QUEUED jobs can be dispatched.")
            execution = _JobExecution(
                job_id=job_id,
                trace_id=trace_id,
                lineage=lineage,
                job_state=[lineage.job],
                page_state=[],
                content_types=[],
                validated_pages=[],
            )
            try:
                prepared = await self._prepare_page_plan(execution)
                if isinstance(prepared, DownloadWorkerResult):
                    return prepared
                cancelled = await self._download_pages(execution, prepared)
                if cancelled is not None:
                    return cancelled
                cancelled = await self._validate_pages(execution)
                if cancelled is not None:
                    return cancelled
                result = await self._package_and_publish(execution)
                if result.job.status is DownloadJobStatus.COMPLETED and isinstance(
                    prepared.connector, Fmd2Connector
                ):
                    try:
                        await prepared.connector.finalize_download(job_id)
                    except SourceConnectorError, OSError, ValueError:
                        logger.warning(
                            "fmd2.completed_staging_retained",
                            extra={"job_id": str(job_id), "stage": "external_staging_release"},
                        )
                return result
            except Exception as error:
                return await self._handle_pipeline_error(error, execution)
        finally:
            try:
                self._transport.finish_job_temporary_budget(job_id)
            finally:
                reset_trace_id(trace_token)

    async def source_available(self, job_id: UUID) -> bool:
        lineage = await asyncio.to_thread(self._read_lineage, job_id)
        connector = self._connector_registry.resolve(lineage.connector_id)
        if isinstance(connector, Fmd2Connector):
            return await connector.connection_available()
        return await connector.health_check() == SourceHealthStatus.HEALTHY

    def existing_artifact_bytes_for_budget(self, job_id: UUID) -> tuple[int, int]:
        """Inventory bounded page and packaging artifacts before disk admission."""
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        page_bytes = self._transport.existing_page_bytes_for_budget(job_id)
        for connector_id in self._connector_registry.connector_ids:
            if connector_id == "yomira.fmd2.webtoons":
                connector = self._connector_registry.resolve(connector_id)
                if isinstance(connector, Fmd2Connector):
                    page_bytes += connector.existing_stage_bytes(job_id)
        identity = self._library_root_identity
        identity.assert_current()
        trace_id = create_trace_id()
        root = identity.canonical_path
        staging_root = root / ".yomira-staging"
        packaging = staging_root / "jobs" / str(job_id) / "packaging"
        directories = (
            staging_root,
            staging_root / "jobs",
            staging_root / "jobs" / str(job_id),
            packaging,
        )

        def stat_snapshot(path: Path) -> os.stat_result:
            return retry_lstat(
                path,
                component="download_worker",
                stage="disk_budget_packaging_inventory_stat",
                trace_id=trace_id,
                job_id=str(job_id),
                validate_path=identity.assert_current,
            )

        def snapshot(information: os.stat_result) -> tuple[object, ...]:
            file_identity = RetryingFileStream.identity_of(information)
            if file_identity is None:
                raise ValueError("packaging inventory identity is unavailable")
            return (
                file_identity,
                information.st_mode,
                information.st_size,
                information.st_mtime_ns,
                information.st_ctime_ns,
            )

        directory_snapshots: dict[Path, tuple[object, ...]] = {}
        for directory in directories:
            try:
                information = stat_snapshot(directory)
            except FileNotFoundError:
                identity.assert_current()
                return page_bytes, 0
            attributes = getattr(information, "st_file_attributes", 0)
            if (
                stat.S_ISLNK(information.st_mode)
                or attributes & _REPARSE_POINT_ATTRIBUTE
                or not stat.S_ISDIR(information.st_mode)
            ):
                raise ValueError("packaging inventory contains an unsafe directory")
            directory_snapshots[directory] = snapshot(information)

        names: list[str] = []
        file_snapshots: dict[Path, tuple[object, ...]] = {}
        total_bytes = 0
        with os.scandir(packaging) as entries:
            for entry in entries:
                if len(names) >= 1 or type(entry.name) is not str:
                    raise ValueError("packaging inventory exceeds its entry bound")
                name = entry.name
                if (
                    not name
                    or "/" in name
                    or "\\" in name
                    or not (name.endswith(".cbz") or name.endswith(".cbz.tmp"))
                ):
                    raise ValueError("packaging inventory contains an unknown artifact")
                path = packaging / name
                information = stat_snapshot(path)
                attributes = getattr(information, "st_file_attributes", 0)
                if (
                    stat.S_ISLNK(information.st_mode)
                    or attributes & _REPARSE_POINT_ATTRIBUTE
                    or not stat.S_ISREG(information.st_mode)
                    or type(information.st_size) is not int
                    or not 0 <= information.st_size <= MAX_CBZ_BYTES_V1
                ):
                    raise ValueError("packaging inventory contains an unsafe artifact")
                names.append(name)
                total_bytes = information.st_size
                file_snapshots[path] = snapshot(information)

        with os.scandir(packaging) as entries:
            if tuple(sorted(names)) != tuple(sorted(entry.name for entry in entries)):
                raise OSError("packaging inventory namespace changed during scan")
        for path, expected in (*directory_snapshots.items(), *file_snapshots.items()):
            if snapshot(stat_snapshot(path)) != expected:
                raise OSError("packaging inventory changed during scan")
        identity.assert_current()
        return page_bytes, total_bytes

    async def _prepare_page_plan(
        self,
        execution: _JobExecution,
    ) -> _PreparedPagePlan | DownloadWorkerResult:
        lineage = execution.lineage
        job_id = execution.job_id
        execution.job_state[0] = await self._transition(
            execution.job_state[0],
            DownloadJobStatus.FETCHING_METADATA,
        )
        execution.stage = "connector_resolution"
        connector_metadata = self._connector_registry.get_metadata(lineage.connector_id)
        if connector_metadata.connector_version != lineage.connector_version:
            raise SourceConnectorError(
                code=ErrorCode.SRC_SCHEMA_CHANGED,
                operation=SourceConnectorOperation.GET_CHAPTER,
            )
        connector = self._connector_registry.resolve(
            lineage.connector_id,
            required_capability=ConnectorCapability.PAGE_LIST,
        )
        execution.stage = "chapter_metadata"
        remote_chapter = await self._await_interruptible(
            job_id,
            connector.get_chapter(lineage.remote_chapter_reference),
        )
        if remote_chapter.remote_id != lineage.remote_chapter_reference:
            raise DownloadWorkerIntegrityError()
        references = tuple(
            await self._await_interruptible(
                job_id,
                cast(AcquisitionProvider, connector).acquire_pages(
                    job_id,
                    lineage.remote_chapter_reference,
                    lineage.chapter_title,
                ),
            )
        )
        if (
            not references
            or len(references) > MAX_MANIFEST_PAGE_COUNT
            or any(type(reference) is not ConnectorPageReference for reference in references)
        ):
            raise ValueError("Connector page list exceeds or violates the V1 contract.")

        execution.stage = "durable_page_plan_reconciliation"
        durable_manifest = await asyncio.to_thread(
            self._recovery.read_verified_download_plan,
            job_id,
        )
        if durable_manifest is None:
            execution.page_state.extend(
                DownloadPage.new_local(job_id, ordinal, reference.remote_reference)
                for ordinal, reference in enumerate(references, start=1)
            )
        else:
            if not self._manifest_matches_connector_plan(durable_manifest, lineage, references):
                raise DownloadWorkerIntegrityError()
            execution.page_state.extend(durable_manifest.pages)

        cancelled = await self._complete_cancellation_if_pending(
            lineage,
            execution.job_state,
            execution.page_state,
            evidence=CancellationSafetyEvidence(safe_point_reached=True),
            trace_id=execution.trace_id,
        )
        if cancelled is not None:
            return cancelled
        if durable_manifest is None:
            execution.stage = "page_manifest_initialization"
            await asyncio.to_thread(
                self._page_writer.replace_pending_pages,
                job_id,
                tuple(execution.page_state),
            )
        cancelled = await self._cooperative_checkpoint(
            lineage,
            execution.job_state,
            execution.page_state,
            job_id,
            DownloadJobStatus.FETCHING_METADATA,
            pause_evidence=PauseSafetyEvidence(network_operation_inactive=True),
            cancellation_evidence=CancellationSafetyEvidence(safe_point_reached=True),
            trace_id=execution.trace_id,
        )
        if cancelled is not None:
            return cancelled
        execution.job_state[0] = await self._transition(
            execution.job_state[0],
            DownloadJobStatus.DOWNLOADING,
        )
        execution.stage = "page_origin_validation"
        allowed_origins = frozenset(
            NetworkOrigin.from_url(origin)
            for origin in self._connector_registry.get_download_origins(lineage.connector_id)
        )
        if not allowed_origins:
            raise SourceConnectorError(
                code=ErrorCode.SRC_CAPABILITY_UNSUPPORTED,
                operation=SourceConnectorOperation.RESOLVE_PAGE,
            )
        return _PreparedPagePlan(connector, references, allowed_origins)

    async def _download_pages(
        self,
        execution: _JobExecution,
        plan: _PreparedPagePlan,
    ) -> DownloadWorkerResult | None:
        pages_to_download: list[tuple[int, DownloadPage]] = []
        content_types = [""] * len(execution.page_state)
        for index, page in enumerate(tuple(execution.page_state)):
            if page.status == "DOWNLOADED":
                if page.temporary_path is None:
                    raise DownloadWorkerIntegrityError()
                content_types[index] = await asyncio.to_thread(
                    self._infer_content_type,
                    self._temporary_path(page.temporary_path),
                )
                continue
            if page.status not in {"PENDING", "DOWNLOADING"}:
                raise DownloadWorkerIntegrityError()
            pages_to_download.append((index, page))

        page_state_lock = asyncio.Lock()
        page_request_lock = asyncio.Lock()
        page_request_failed = [False]
        for start in range(0, len(pages_to_download), _MAX_PAGES_IN_FLIGHT_PER_JOB):
            cancelled = await self._complete_cancellation_if_pending(
                execution.lineage,
                execution.job_state,
                execution.page_state,
                evidence=CancellationSafetyEvidence(
                    new_requests_stopped=True,
                    stream_closed=True,
                ),
                trace_id=execution.trace_id,
            )
            if cancelled is not None:
                return cancelled

            batch = pages_to_download[start : start + _MAX_PAGES_IN_FLIGHT_PER_JOB]
            execution.stage = "page_attempt_reservation"
            reserved_batch: list[tuple[int, DownloadPage]] = []
            for index, page in batch:
                attempt = 1 if page.status == "PENDING" else page.attempts + 1
                if attempt > MAX_DOWNLOAD_PAGE_ATTEMPTS:
                    raise DownloadWorkerIntegrityError()
                reserved = replace(
                    page,
                    status="DOWNLOADING",
                    temporary_path=DownloadPageRepository.attempt_path(
                        execution.job_id, index + 1, attempt
                    ),
                    attempts=attempt,
                )
                execution.page_state[index] = reserved
                reserved_batch.append((index, reserved))
            # Reserve both independent paths durably before either request starts.
            publication = asyncio.create_task(
                asyncio.to_thread(
                    self._page_writer.publish_snapshot,
                    execution.job_id,
                    tuple(execution.page_state),
                )
            )
            try:
                await asyncio.shield(publication)
            except asyncio.CancelledError:
                await publication
                raise
            batch = reserved_batch
            task_executions = [replace(execution) for _ in batch]
            tasks = [
                asyncio.create_task(
                    self._download_page(
                        task_execution,
                        plan,
                        index,
                        index + 1,
                        page,
                        page_state_lock,
                        page_request_lock,
                        page_request_failed,
                    )
                )
                for task_execution, (index, page) in zip(
                    task_executions,
                    batch,
                    strict=True,
                )
            ]
            try:
                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                failed = next(
                    (
                        task
                        for task in tasks
                        if task in done and not task.cancelled() and task.exception() is not None
                    ),
                    None,
                )
                if failed is not None:
                    execution.stage = task_executions[tasks.index(failed)].stage
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    error = failed.exception()
                    if error is None:
                        raise DownloadWorkerIntegrityError()
                    raise error
                results = [task.result() for task in tasks]
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

            for (index, _page), content_type in zip(batch, results, strict=True):
                content_types[index] = content_type
            cancelled = await self._cooperative_checkpoint(
                execution.lineage,
                execution.job_state,
                execution.page_state,
                execution.job_id,
                DownloadJobStatus.DOWNLOADING,
                pause_evidence=PauseSafetyEvidence(
                    new_requests_stopped=True,
                    stream_closed=True,
                ),
                cancellation_evidence=CancellationSafetyEvidence(
                    new_requests_stopped=True,
                    stream_closed=True,
                ),
                trace_id=execution.trace_id,
            )
            if cancelled is not None:
                return cancelled

        if any(not content_type for content_type in content_types):
            raise DownloadWorkerIntegrityError()
        execution.content_types[:] = content_types
        return None

    async def _download_page(
        self,
        execution: _JobExecution,
        plan: _PreparedPagePlan,
        index: int,
        ordinal: int,
        page: DownloadPage,
        page_state_lock: asyncio.Lock,
        page_request_lock: asyncio.Lock,
        page_request_failed: list[bool],
    ) -> str:
        job_id = execution.job_id
        next_attempt_number = page.attempts

        async def publish_page_state() -> None:
            publication = asyncio.create_task(
                asyncio.to_thread(
                    self._page_writer.publish_snapshot,
                    job_id,
                    tuple(execution.page_state),
                )
            )
            try:
                await asyncio.shield(publication)
            except asyncio.CancelledError:
                await publication
                raise

        async def reserve_next_attempt(
            target_job_id: UUID,
            target_ordinal: int,
            previous_attempt: int,
        ) -> int:
            if target_job_id != job_id or target_ordinal != ordinal:
                raise DownloadWorkerIntegrityError()
            async with page_state_lock:
                current = execution.page_state[index]
                next_attempt = previous_attempt + 1
                if current.status != "DOWNLOADING" or current.attempts != previous_attempt:
                    raise DownloadWorkerIntegrityError()
                if next_attempt > MAX_DOWNLOAD_PAGE_ATTEMPTS:
                    raise DownloadWorkerIntegrityError()
                execution.page_state[index] = replace(
                    current,
                    temporary_path=DownloadPageRepository.attempt_path(
                        job_id,
                        ordinal,
                        next_attempt,
                    ),
                    attempts=next_attempt,
                )
                await publish_page_state()
                return next_attempt

        cancellation_event = self._cancellation_event(job_id)
        async with page_request_lock:
            if cancellation_event is not None and cancellation_event.is_set():
                raise DownloadCancellationRequested()
            if page_request_failed[0]:
                raise asyncio.CancelledError()
            try:
                execution.stage = "page_transport"
                try:
                    request = plan.connector.resolve_page(plan.references[index])
                except SourceConnectorError as error:
                    if (
                        error.code is not ErrorCode.SRC_CONNECTOR_FAILED
                        or error.operation is not SourceConnectorOperation.RESOLVE_PAGE
                        or not error.retryable
                    ):
                        raise
                    execution.stage = "page_request_refresh"
                    references = tuple(
                        await self._await_interruptible(
                            job_id,
                            cast(AcquisitionProvider, plan.connector).acquire_pages(
                                job_id,
                                execution.lineage.remote_chapter_reference,
                                execution.lineage.chapter_title,
                            ),
                        )
                    )
                    if references != plan.references:
                        raise DownloadWorkerIntegrityError() from None
                    request = plan.connector.resolve_page(plan.references[index])
                    execution.stage = "page_transport"
            except Exception:
                page_request_failed[0] = True
                raise
        result = await self._transport.download(
            request,
            job_id=job_id,
            ordinal=ordinal,
            attempt=next_attempt_number,
            reserve_next_attempt=reserve_next_attempt,
            allowed_origins=plan.allowed_origins,
            cancellation_event=cancellation_event,
        )
        async with page_state_lock:
            current = execution.page_state[index]
            if (
                result.job_id != job_id
                or result.ordinal != ordinal
                or result.attempt != current.attempts
                or result.temporary_path != current.temporary_path
            ):
                raise DownloadWorkerIntegrityError()
            execution.page_state[index] = replace(
                current,
                status="DOWNLOADED",
                size_bytes=result.size_bytes,
                sha256=result.sha256,
            )
            await publish_page_state()
        return result.content_type

    async def _validate_pages(
        self,
        execution: _JobExecution,
    ) -> DownloadWorkerResult | None:
        job_id = execution.job_id
        execution.job_state[0] = await self._transition(
            execution.job_state[0],
            DownloadJobStatus.VALIDATING,
        )
        for index, page in enumerate(tuple(execution.page_state)):
            ordinal = index + 1
            execution.stage = "image_validation"
            if page.temporary_path is None:
                raise DownloadWorkerIntegrityError()
            source_path = self._temporary_path(page.temporary_path)
            validated = await asyncio.to_thread(
                validate_image_file,
                source_path,
                execution.content_types[index],
                policy=self._image_validation_policy,
                job_id=str(job_id),
            )
            if page.status == "DOWNLOADED" and (
                validated.size_bytes != page.size_bytes or validated.sha256 != page.sha256
            ):
                raise DownloadWorkerIntegrityError()
            extension = _IMAGE_EXTENSIONS.get(validated.mime)
            if extension is None:
                raise ImageValidationError(ImageValidationErrorCode.MIME_UNSUPPORTED)
            final_relative = f"jobs/{job_id}/pages/{ordinal}/page-{page.attempts}.{extension}"
            final_path = self._temporary_path(final_relative)
            execution.stage = "page_atomic_promotion"
            await asyncio.to_thread(
                self._file_publisher.publish_no_replace,
                source_path,
                final_path,
                job_id=job_id,
            )
            execution.page_state[index] = replace(
                page,
                status="VALIDATED",
                temporary_path=final_relative,
                width=validated.width,
                height=validated.height,
                mime=validated.mime,
                size_bytes=validated.size_bytes,
                sha256=validated.sha256,
            )
            await asyncio.to_thread(
                self._page_writer.publish_snapshot,
                job_id,
                tuple(execution.page_state),
            )
            cancelled = await self._cooperative_checkpoint(
                execution.lineage,
                execution.job_state,
                execution.page_state,
                job_id,
                DownloadJobStatus.VALIDATING,
                pause_evidence=PauseSafetyEvidence(safe_point_reached=True),
                cancellation_evidence=CancellationSafetyEvidence(safe_point_reached=True),
                trace_id=execution.trace_id,
            )
            if cancelled is not None:
                return cancelled
            execution.validated_pages.append(
                ValidatedPage(
                    ordinal,
                    final_path,
                    validated.size_bytes,
                    validated.sha256,
                )
            )
        return None

    async def _package_and_publish(
        self,
        execution: _JobExecution,
    ) -> DownloadWorkerResult:
        lineage = execution.lineage
        job_id = execution.job_id
        job_state = execution.job_state
        page_state = execution.page_state
        trace_id = execution.trace_id
        job_state[0] = await self._transition(job_state[0], DownloadJobStatus.PROCESSING)
        job_state[0] = await self._transition(job_state[0], DownloadJobStatus.PACKAGING)
        cancelled = await self._cooperative_checkpoint(
            lineage,
            job_state,
            page_state,
            job_id,
            DownloadJobStatus.PACKAGING,
            pause_evidence=PauseSafetyEvidence(
                write_critical_operation_inactive=True,
                cleanup_complete=True,
            ),
            cancellation_evidence=CancellationSafetyEvidence(
                write_critical_operation_inactive=True,
                cleanup_complete=True,
            ),
            trace_id=trace_id,
        )
        if cancelled is not None:
            return cancelled
        execution.stage = "cbz_metadata_snapshot"
        metadata = await asyncio.to_thread(self._read_comic_info_metadata, lineage.series_id)
        execution.stage = "cbz_directory_preflight"
        relative_directory = f"chapters/{lineage.series_id}"
        staging_directory = f".yomira-staging/jobs/{job_id}/packaging"
        await asyncio.to_thread(
            self._ensure_library_directory,
            staging_directory,
            job_id=job_id,
            trace_id=trace_id,
        )
        execution.staged_cbz_path = self._library_path(
            f".yomira-staging/jobs/{job_id}/packaging/{lineage.chapter_id}.cbz"
        )
        cbz_relative_path = f"{relative_directory}/{lineage.chapter_id}-{job_id}.cbz"
        execution.published_cbz_path = self._library_path(cbz_relative_path)
        execution.stage = "cbz_build_verify_publish"
        asset = await asyncio.to_thread(
            self._cbz_builder.build,
            destination=execution.staged_cbz_path,
            metadata=metadata,
            pages=tuple(execution.validated_pages),
            job_id=job_id,
        )
        job_state[0] = await self._transition(job_state[0], DownloadJobStatus.VERIFYING)
        job_state[0] = await self._transition(job_state[0], DownloadJobStatus.INDEXING)
        cancelled = await self._complete_cancellation_if_pending(
            lineage,
            job_state,
            page_state,
            evidence=CancellationSafetyEvidence(database_transaction_closed=True),
            trace_id=trace_id,
        )
        if cancelled is not None:
            return cancelled
        await asyncio.to_thread(
            self._ensure_library_directory,
            relative_directory,
            job_id=job_id,
            trace_id=trace_id,
        )
        execution.stage = "cbz_publish_for_index"
        await asyncio.to_thread(
            self._library_file_publisher.publish_no_replace,
            execution.staged_cbz_path,
            execution.published_cbz_path,
            job_id=job_id,
        )
        asset = replace(asset, path=execution.published_cbz_path)
        cancelled = await self._complete_cancelled_cbz_if_pending(
            lineage,
            job_state,
            page_state,
            staged_path=execution.staged_cbz_path,
            published_path=execution.published_cbz_path,
            trace_id=trace_id,
        )
        if cancelled is not None:
            return cancelled
        execution.stage = "reader_index_verification"
        indexed = await asyncio.to_thread(
            self._reader_asset_service.index_verified_cbz,
            job_id=job_id,
            chapter_id=lineage.chapter_id,
            result=asset,
        )
        if indexed.page_count != len(page_state):
            raise DownloadWorkerIntegrityError()
        with self._database.read_session() as session:
            completed = self._job_repository.get(session, job_id)
        if completed is None or completed.status is not DownloadJobStatus.COMPLETED:
            raise DownloadWorkerIntegrityError()
        return DownloadWorkerResult(
            completed,
            lineage.series_id,
            lineage.chapter_id,
            indexed.page_count,
            asset,
            None,
            None,
            trace_id,
        )

    def _read_comic_info_metadata(self, series_id: UUID) -> ComicInfoMetadata:
        resolutions = MetadataService(self._database).resolve_series(series_id)
        if resolutions is None:
            raise DownloadWorkerIntegrityError()
        values = {item.field: item.value for item in resolutions}
        title = cast(str | None, values[MetadataField.TITLE])
        return ComicInfoMetadata(
            title=title,
            series=title,
            summary=cast(str | None, values[MetadataField.DESCRIPTION]),
            writer=cast(str | None, values[MetadataField.AUTHOR]),
            genres=cast(tuple[str, ...] | None, values[MetadataField.GENRES]),
            language_iso=cast(str | None, values[MetadataField.LANGUAGE]),
        )

    async def _handle_pipeline_error(
        self,
        error: Exception,
        execution: _JobExecution,
    ) -> DownloadWorkerResult:
        lineage = execution.lineage
        job_state = execution.job_state
        page_state = execution.page_state
        trace_id = execution.trace_id
        if isinstance(error, _CancellationCompleted):
            job_state[0] = error.job
            return self._cancelled_result(lineage, error.job, page_state, trace_id)
        if isinstance(error, _JobCancellationRequested):
            cancelled = await self._complete_cancellation_if_pending(
                lineage,
                job_state,
                page_state,
                evidence=CancellationSafetyEvidence(safe_point_reached=True),
                trace_id=trace_id,
            )
            if cancelled is not None:
                return cancelled
            return await self._interrupt_blocked(
                lineage,
                job_state,
                page_state,
                trace_id=trace_id,
                stage="job_cancellation",
                error_code=ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
                cause=DownloadWorkerIntegrityError(),
            )
        if isinstance(error, DownloadCancellationRequested):
            cancelled = await self._complete_cancellation_if_pending(
                lineage,
                job_state,
                page_state,
                evidence=CancellationSafetyEvidence(
                    new_requests_stopped=True,
                    stream_closed=True,
                ),
                trace_id=trace_id,
            )
            if cancelled is not None:
                return cancelled
            return await self._interrupt_blocked(
                lineage,
                job_state,
                page_state,
                trace_id=trace_id,
                stage="job_cancellation",
                error_code=ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
                cause=DownloadWorkerIntegrityError(),
            )
        if isinstance(error, DownloadTransportError):
            if self._database.storage_gate.closed:
                return self._storage_gate_closed_result(
                    lineage,
                    job_state,
                    page_state,
                    trace_id=trace_id,
                    cause=StorageGateClosedError(
                        self._database.storage_gate.closed_reason or "storage_closed"
                    ),
                )
            if error.code is ErrorCode.DL_JOB_RECOVERY_BLOCKED:
                return await self._interrupt_blocked(
                    lineage,
                    job_state,
                    page_state,
                    trace_id=trace_id,
                    stage=error.stage,
                    error_code=error.code.value,
                    cause=error,
                )
            return await self._fail_job(
                lineage,
                job_state,
                page_state,
                trace_id=trace_id,
                stage=error.stage,
                error_code=error.code.value,
                cause=error,
            )
        if isinstance(error, ImageValidationError):
            return await self._fail_job(
                lineage,
                job_state,
                page_state,
                trace_id=trace_id,
                stage=execution.stage,
                error_code=str(error.code.value),
                cause=error,
            )
        if isinstance(error, SourceConnectorError):
            result = await self._fail_job(
                lineage,
                job_state,
                page_state,
                trace_id=trace_id,
                stage=execution.stage,
                error_code=error.code.value,
                cause=error,
            )
            return replace(result, retryable=error.retryable)
        if isinstance(error, (PageManifestWriteBlocked, StalePageManifestSnapshot)):
            return await self._interrupt_blocked(
                lineage,
                job_state,
                page_state,
                trace_id=trace_id,
                stage=execution.stage,
                error_code=ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
                cause=error,
            )
        if isinstance(error, CBZBuildError):
            if self._database.storage_gate.closed:
                return self._storage_gate_closed_result(
                    lineage,
                    job_state,
                    page_state,
                    trace_id=trace_id,
                    cause=StorageGateClosedError(
                        self._database.storage_gate.closed_reason or "storage_closed"
                    ),
                )
            if error.stage == "archive_size_limit_preflight":
                return await self._fail_job(
                    lineage,
                    job_state,
                    page_state,
                    trace_id=trace_id,
                    stage=error.stage,
                    error_code="CBZ_VERIFY_FAILED",
                    cause=error,
                )
            return await self._interrupt_blocked(
                lineage,
                job_state,
                page_state,
                trace_id=trace_id,
                stage=error.stage,
                error_code="CBZ_VERIFY_FAILED",
                cause=error,
            )
        if isinstance(error, ReaderAssetError):
            cancelled = None
            if execution.staged_cbz_path is not None and execution.published_cbz_path is not None:
                cancelled = await self._complete_cancelled_cbz_if_pending(
                    lineage,
                    job_state,
                    page_state,
                    staged_path=execution.staged_cbz_path,
                    published_path=execution.published_cbz_path,
                    trace_id=trace_id,
                )
            if cancelled is not None:
                return cancelled
            return await self._interrupt_blocked(
                lineage,
                job_state,
                page_state,
                trace_id=trace_id,
                stage=execution.stage,
                error_code=ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
                cause=error,
            )
        if isinstance(error, DownloadWorkerIntegrityError):
            return await self._interrupt_blocked(
                lineage,
                job_state,
                page_state,
                trace_id=trace_id,
                stage=execution.stage,
                error_code=ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
                cause=error,
            )
        if isinstance(error, (TypeError, ValueError)):
            return await self._fail_job(
                lineage,
                job_state,
                page_state,
                trace_id=trace_id,
                stage=execution.stage,
                error_code=ErrorCode.SRC_SCHEMA_CHANGED.value,
                cause=error,
            )
        if isinstance(error, StorageGateClosedError):
            return self._storage_gate_closed_result(
                lineage,
                job_state,
                page_state,
                trace_id=trace_id,
                cause=error,
            )
        if self._database.storage_gate.closed:
            return self._storage_gate_closed_result(
                lineage,
                job_state,
                page_state,
                trace_id=trace_id,
                cause=StorageGateClosedError(
                    self._database.storage_gate.closed_reason or "storage_closed"
                ),
            )
        return await self._interrupt_blocked(
            lineage,
            job_state,
            page_state,
            trace_id=trace_id,
            stage=execution.stage,
            error_code=ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
            cause=error,
        )

    async def _pause_at_safe_checkpoint(
        self,
        job_id: UUID,
        status: DownloadJobStatus,
        evidence: PauseSafetyEvidence,
    ) -> PauseCheckpointOutcome:
        controller = self._pause_controller
        if controller is None:
            return PauseCheckpointOutcome.CONTINUE
        return await controller.pause_if_requested(
            job_id,
            status=status,
            evidence=evidence,
        )

    async def _cooperative_checkpoint(
        self,
        lineage: _JobLineage,
        job_state: list[DownloadJob],
        pages: list[DownloadPage],
        job_id: UUID,
        status: DownloadJobStatus,
        *,
        pause_evidence: PauseSafetyEvidence,
        cancellation_evidence: CancellationSafetyEvidence,
        trace_id: str,
    ) -> DownloadWorkerResult | None:
        if self._database.storage_gate.closed:
            return self._storage_gate_closed_result(
                lineage,
                job_state,
                pages,
                trace_id=trace_id,
                cause=StorageGateClosedError(
                    self._database.storage_gate.closed_reason or "storage_closed"
                ),
            )
        await self._pause_at_safe_checkpoint(job_id, status, pause_evidence)
        if self._database.storage_gate.closed:
            return self._storage_gate_closed_result(
                lineage,
                job_state,
                pages,
                trace_id=trace_id,
                cause=StorageGateClosedError(
                    self._database.storage_gate.closed_reason or "storage_closed"
                ),
            )
        return await self._complete_cancellation_if_pending(
            lineage,
            job_state,
            pages,
            evidence=cancellation_evidence,
            trace_id=trace_id,
        )

    @staticmethod
    def _storage_gate_closed_result(
        lineage: _JobLineage,
        job_state: list[DownloadJob],
        pages: list[DownloadPage],
        *,
        trace_id: str,
        cause: StorageGateClosedError,
    ) -> DownloadWorkerResult:
        DownloadWorker._log_failure(
            lineage.job.job_id,
            trace_id=trace_id,
            stage="shared_storage_gate_closed",
            error_code=ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
            cause=cause,
        )
        return DownloadWorkerResult(
            job_state[0],
            lineage.series_id,
            lineage.chapter_id,
            len(pages),
            None,
            ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
            "shared_storage_gate_closed",
            trace_id,
        )

    async def _complete_cancellation_if_pending(
        self,
        lineage: _JobLineage,
        job_state: list[DownloadJob],
        pages: list[DownloadPage],
        *,
        evidence: CancellationSafetyEvidence,
        trace_id: str,
    ) -> DownloadWorkerResult | None:
        for _attempt in range(3):
            with self._database.read_session() as session:
                current = self._job_repository.get(session, lineage.job.job_id)
            if current is None:
                return None
            if current.status is DownloadJobStatus.CANCELLED:
                job_state[0] = current
                return self._cancelled_result(lineage, current, pages, trace_id)
            if current.cancel_requested_at is None:
                return None
            try:
                with self._database.write_session() as session:
                    latest = self._job_repository.get(session, lineage.job.job_id)
                    if latest is None:
                        return None
                    if latest.status is DownloadJobStatus.CANCELLED:
                        completed = latest
                    elif latest.cancel_requested_at is None:
                        return None
                    else:
                        completed = self._job_repository.complete_cancellation(
                            session,
                            current_job=latest,
                            completed_at=max(datetime.now(UTC), latest.updated_at),
                            evidence=evidence,
                        )
                        if completed is None:
                            return None
            except StaleJobStateError:
                continue
            job_state[0] = completed
            return self._cancelled_result(lineage, completed, pages, trace_id)
        return None

    async def _complete_cancelled_cbz_if_pending(
        self,
        lineage: _JobLineage,
        job_state: list[DownloadJob],
        pages: list[DownloadPage],
        *,
        staged_path: Path,
        published_path: Path,
        trace_id: str,
    ) -> DownloadWorkerResult | None:
        """Return a promoted, unindexed CBZ to temp before finalizing cancellation."""
        with self._database.read_session() as session:
            current = self._job_repository.get(session, lineage.job.job_id)
        if current is None or current.cancel_requested_at is None:
            return None
        if current.status is DownloadJobStatus.CANCELLED:
            job_state[0] = current
            return self._cancelled_result(lineage, current, pages, trace_id)

        # The publisher verifies the source identity and bytes during this move.
        # A failure is intentionally allowed to interrupt the job with its durable
        # cancellation intent intact; ambiguous artifacts are never deleted.
        await asyncio.to_thread(
            self._library_file_publisher.publish_no_replace,
            published_path,
            staged_path,
            job_id=lineage.job.job_id,
        )
        return await self._complete_cancellation_if_pending(
            lineage,
            job_state,
            pages,
            evidence=CancellationSafetyEvidence(database_transaction_closed=True),
            trace_id=trace_id,
        )

    @staticmethod
    def _cancelled_result(
        lineage: _JobLineage,
        job: DownloadJob,
        pages: list[DownloadPage],
        trace_id: str,
    ) -> DownloadWorkerResult:
        return DownloadWorkerResult(
            job,
            lineage.series_id,
            lineage.chapter_id,
            len(pages),
            None,
            None,
            None,
            trace_id,
        )

    def _cancellation_event(self, job_id: UUID) -> asyncio.Event | None:
        if self._pause_controller is None:
            return None
        try:
            return self._pause_controller.cancellation_event(job_id)
        except UnknownActiveJob:
            return None

    async def _await_interruptible[T](
        self,
        job_id: UUID,
        awaitable: Awaitable[T],
    ) -> T:
        """Race connector metadata I/O with the registered job's cancel event."""
        event = self._cancellation_event(job_id)
        if event is None:
            return await awaitable
        if event.is_set():
            raise _JobCancellationRequested()

        operation = asyncio.ensure_future(awaitable)
        cancelled = asyncio.create_task(event.wait())
        try:
            done, _pending = await asyncio.wait(
                {operation, cancelled},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                return operation.result()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise _JobCancellationRequested()
        finally:
            if not cancelled.done():
                cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)

    async def _transition(
        self,
        current_job: DownloadJob,
        target_status: DownloadJobStatus,
    ) -> DownloadJob:
        def transition() -> tuple[DownloadJob, bool]:
            with self._database.write_session() as session:
                try:
                    changed = self._job_repository.transition_status(
                        session,
                        current_job=current_job,
                        target_status=target_status,
                        changed_at=max(datetime.now(UTC), current_job.updated_at),
                    )
                except (CancellationRequestPending, StaleJobStateError) as error:
                    latest = self._job_repository.get(session, current_job.job_id)
                    if latest is None:
                        raise
                    if latest.status is DownloadJobStatus.CANCELLED:
                        return latest, True
                    if latest.cancel_requested_at is None:
                        raise
                    evidence = self._transition_cancellation_evidence(current_job.status)
                    if evidence is None:
                        raise
                    cancelled = self._job_repository.complete_cancellation(
                        session,
                        current_job=latest,
                        completed_at=max(datetime.now(UTC), latest.updated_at),
                        evidence=evidence,
                    )
                    if cancelled is None:
                        raise DownloadWorkerIntegrityError() from error
                    return cancelled, True
                if changed is None:
                    raise DownloadWorkerIntegrityError()
                return changed, False

        changed, cancellation_completed = await asyncio.to_thread(transition)
        if cancellation_completed:
            raise _CancellationCompleted(changed) from None
        return changed

    @staticmethod
    def _transition_cancellation_evidence(
        status: DownloadJobStatus,
    ) -> CancellationSafetyEvidence | None:
        match status:
            case DownloadJobStatus.DOWNLOADING:
                return CancellationSafetyEvidence(
                    new_requests_stopped=True,
                    stream_closed=True,
                )
            case DownloadJobStatus.VALIDATING | DownloadJobStatus.VERIFYING:
                return CancellationSafetyEvidence(safe_point_reached=True)
            case DownloadJobStatus.PROCESSING:
                return CancellationSafetyEvidence(between_pages=True)
            case DownloadJobStatus.PACKAGING:
                return CancellationSafetyEvidence(
                    write_critical_operation_inactive=True,
                    cleanup_complete=True,
                )
            case DownloadJobStatus.INDEXING:
                return CancellationSafetyEvidence(database_transaction_closed=True)
            case _:
                return None

    async def _fail_job(
        self,
        lineage: _JobLineage,
        job_state: list[DownloadJob],
        pages: list[DownloadPage],
        *,
        trace_id: str,
        stage: str,
        error_code: str,
        cause: BaseException,
    ) -> DownloadWorkerResult:
        current = job_state[0]
        if current.status is not DownloadJobStatus.FAILED:
            try:
                current = await self._transition(current, DownloadJobStatus.FAILED)
                job_state[0] = current
            except Exception as transition_error:
                return await self._interrupt_blocked(
                    lineage,
                    job_state,
                    pages,
                    trace_id=trace_id,
                    stage="failure_status_commit",
                    error_code=ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
                    cause=transition_error,
                )
        self._log_failure(
            lineage.job.job_id,
            trace_id=trace_id,
            stage=stage,
            error_code=error_code,
            cause=cause,
        )
        if pages:
            failed_pages = tuple(
                replace(page, status="FAILED")
                if page.status in {"PENDING", "DOWNLOADING", "DOWNLOADED"}
                else page
                for page in pages
            )
            if failed_pages != tuple(pages):
                try:
                    await asyncio.to_thread(
                        self._page_writer.publish_snapshot,
                        lineage.job.job_id,
                        failed_pages,
                    )
                    pages[:] = failed_pages
                except Exception as manifest_error:
                    self._log_failure(
                        lineage.job.job_id,
                        trace_id=trace_id,
                        stage="failure_page_manifest_commit",
                        error_code=ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
                        cause=manifest_error,
                    )
        return DownloadWorkerResult(
            current,
            lineage.series_id,
            lineage.chapter_id,
            len(pages),
            None,
            error_code,
            stage,
            trace_id,
        )

    async def _interrupt_blocked(
        self,
        lineage: _JobLineage,
        job_state: list[DownloadJob],
        pages: list[DownloadPage],
        *,
        trace_id: str,
        stage: str,
        error_code: str,
        cause: BaseException,
    ) -> DownloadWorkerResult:
        current = job_state[0]
        if current.status not in {
            DownloadJobStatus.INTERRUPTED,
            DownloadJobStatus.COMPLETED,
            DownloadJobStatus.FAILED,
            DownloadJobStatus.CANCELLED,
        }:
            try:
                await asyncio.to_thread(
                    self._recovery.interrupt_quiesced_job,
                    current.job_id,
                    current,
                    quiescence_ack=WorkerQuiescenceAck(current.job_id),
                    recovery_blocked=True,
                )
                with self._database.read_session() as session:
                    updated = self._job_repository.get(session, current.job_id)
                if updated is not None:
                    current = updated
                    job_state[0] = updated
            except Exception as interrupt_error:
                self._log_failure(
                    current.job_id,
                    trace_id=trace_id,
                    stage="blocked_interruption_commit",
                    error_code=ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
                    cause=interrupt_error,
                )
        self._log_failure(
            current.job_id,
            trace_id=trace_id,
            stage=stage,
            error_code=error_code,
            cause=cause,
        )
        return DownloadWorkerResult(
            current,
            lineage.series_id,
            lineage.chapter_id,
            len(pages),
            None,
            error_code,
            stage,
            trace_id,
        )

    def _read_lineage(self, job_id: UUID) -> _JobLineage:
        with self._database.read_session() as session:
            job = self._job_repository.get(session, job_id)
            if job is None:
                raise DownloadWorkerInputError("Download job does not exist.")
            chapter_source = session.get(
                ChapterSourceRow,
                str(job.chapter_source_id),
            )
            if chapter_source is None:
                raise DownloadWorkerIntegrityError()
            source = session.get(SourceRow, chapter_source.source_id)
            chapter = session.get(ChapterRow, chapter_source.chapter_id)
            if source is None or chapter is None:
                raise DownloadWorkerIntegrityError()
            series = session.get(SeriesRow, chapter.series_id)
            if series is None:
                raise DownloadWorkerIntegrityError()
            series_source = session.scalar(
                select(SeriesSourceRow).where(
                    SeriesSourceRow.source_id == source.source_id,
                    SeriesSourceRow.series_id == series.series_id,
                )
            )
            if series_source is None:
                raise DownloadWorkerIntegrityError()
            if not source.enabled or not chapter_source.available:
                raise DownloadWorkerInputError("Source or chapter is unavailable.")
            remote_chapter_reference = chapter_source.remote_id or chapter_source.remote_url
            remote_series_reference = series_source.remote_id or series_source.remote_url
            if not remote_series_reference or not remote_chapter_reference:
                raise DownloadWorkerIntegrityError()
            try:
                series_id = UUID(series.series_id)
                chapter_id = UUID(chapter.chapter_id)
            except ValueError:
                raise DownloadWorkerIntegrityError() from None
            if series_id.version != 4 or chapter_id.version != 4:
                raise DownloadWorkerIntegrityError()
            return _JobLineage(
                job=job,
                connector_id=source.connector_id,
                connector_version=source.connector_version,
                remote_chapter_reference=remote_chapter_reference,
                chapter_title=chapter.display_label,
                series_id=series_id,
                chapter_id=chapter_id,
            )

    @staticmethod
    def _manifest_matches_connector_plan(
        manifest: DownloadJobManifest,
        lineage: _JobLineage,
        references: tuple[ConnectorPageReference, ...],
    ) -> bool:
        return (
            manifest.job_id == lineage.job.job_id
            and manifest.chapter_id == lineage.chapter_id
            and manifest.expected_pages == len(references)
            and len(manifest.pages) == len(references)
            and tuple(page.ordinal for page in manifest.pages)
            == tuple(range(1, len(references) + 1))
            and tuple(page.remote_reference for page in manifest.pages)
            == tuple(reference.remote_reference for reference in references)
        )

    @staticmethod
    def _infer_content_type(path: Path) -> str:
        """Infer only the supported media signature for a verified DOWNLOADED page."""
        try:
            with path.open("rb") as stream:
                header = stream.read(12)
        except OSError:
            raise DownloadWorkerIntegrityError() from None
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if header.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
            return "image/webp"
        raise DownloadWorkerIntegrityError()

    def _temporary_path(self, relative_path: str) -> Path:
        root = self._data_root_identity.canonical_path
        target = root / "temp"
        for component in relative_path.split("/"):
            target = target / component
        if not target.is_relative_to(root):
            raise DownloadWorkerIntegrityError()
        self._data_root_identity.assert_current()
        return target

    def _library_path(self, relative_path: str) -> Path:
        root = self._library_root_identity.canonical_path
        target = root.joinpath(*relative_path.split("/"))
        if not target.is_relative_to(root):
            raise DownloadWorkerIntegrityError()
        self._library_root_identity.assert_current()
        return target

    def _ensure_library_directory(
        self,
        relative_path: str,
        *,
        job_id: UUID,
        trace_id: str,
    ) -> Path:
        return ensure_managed_directory(
            relative_path,
            data_root_identity=self._library_root_identity,
            storage_gate=self._database.storage_gate,
            component_name="download_worker",
            job_id=job_id,
            trace_id=trace_id,
        )

    def _ensure_data_directory(self, relative_path: str, *, job_id: UUID, trace_id: str) -> Path:
        return ensure_managed_directory(
            relative_path,
            data_root_identity=self._data_root_identity,
            storage_gate=self._database.storage_gate,
            component_name="download_worker",
            job_id=job_id,
            trace_id=trace_id,
        )

    @staticmethod
    def _log_failure(
        job_id: UUID,
        *,
        trace_id: str,
        stage: str,
        error_code: str,
        cause: BaseException,
    ) -> None:
        logger.error(
            "download_job.worker_failed",
            extra={
                "component": "download_worker",
                "error_code": error_code,
                "retryable": False,
                "trace_id": trace_id,
                "job_id": str(job_id),
                "stage": stage,
                "cause_type": type(cause).__name__,
            },
        )
