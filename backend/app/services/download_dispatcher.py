"""Bounded background admission for durable download jobs."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from contextvars import Context
from time import monotonic
from typing import Protocol, cast
from uuid import UUID

from app.domain.download_job import DownloadJobStatus
from app.domain.job_backpressure import ActiveJobSnapshot
from app.domain.job_fairness import (
    MAX_DISPATCH_WINDOW_SIZE,
    DispatchCursor,
    DispatchWindow,
)
from app.domain.job_pause import JobPauseController
from app.errors import ErrorCode
from app.persistence.database import SQLiteDatabase
from app.persistence.disk_budget import DiskBudgetManager, DiskBudgetUnavailableError
from app.persistence.repositories.download_job import DownloadJobRepository
from app.services.chapter_fallback import ChapterFallbackService
from app.services.download_worker import DownloadWorkerResult
from app.services.job_recovery import RecoveryCoordinator, WorkerQuiescenceAck
from app.tracing import create_trace_id

logger = logging.getLogger("yomira.download_dispatcher")

MAX_ACTIVE_JOBS = 1
MAX_ACTIVE_JOBS_PER_SOURCE = 1
DEFAULT_POLL_INTERVAL_SECONDS = 0.25
DISK_BUDGET_RECHECK_SECONDS = 30.0
SOURCE_RECHECK_SECONDS = 30.0
_RECOVERABLE_STORAGE_GATE_REASONS = frozenset(
    {"SQLITE_FULL", "FILESYSTEM_DISK_FULL", "DISK_BUDGET_UNSAFE"}
)
_ACTIVE_STATUSES = frozenset(
    {
        DownloadJobStatus.FETCHING_METADATA,
        DownloadJobStatus.DOWNLOADING,
        DownloadJobStatus.VALIDATING,
        DownloadJobStatus.PROCESSING,
        DownloadJobStatus.PACKAGING,
        DownloadJobStatus.VERIFYING,
        DownloadJobStatus.INDEXING,
    }
)


def _is_sqlite_database_instance(value: object) -> bool:
    return isinstance(value, SQLiteDatabase)


class DownloadJobRunner(Protocol):
    async def run_job(self, job_id: UUID) -> object:
        """Run one queued job until it reaches a durable terminal/checkpoint state."""
        ...


class _DiskBudgetAwareRunner(Protocol):
    def existing_artifact_bytes_for_budget(self, job_id: UUID) -> tuple[int, int]:
        """Return verified bounded page and CBZ artifacts for disk admission."""
        ...


class DownloadDispatcher:
    """Poll the durable queue with a fixed global and per-source admission cap."""

    def __init__(
        self,
        *,
        database: SQLiteDatabase,
        worker: DownloadJobRunner,
        recovery_coordinator: RecoveryCoordinator,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        admission_allowed: Callable[[], bool] | None = None,
        pause_controller: JobPauseController | None = None,
        disk_budget_manager: DiskBudgetManager | None = None,
        chapter_fallback: ChapterFallbackService | None = None,
    ) -> None:
        if not _is_sqlite_database_instance(database):
            raise TypeError("database must be a SQLiteDatabase")
        if not callable(getattr(worker, "run_job", None)):
            raise TypeError("worker must provide an async run_job method")
        if recovery_coordinator.database is not database:
            raise ValueError("recovery coordinator must use the dispatcher database")
        if (
            type(poll_interval_seconds) not in {int, float}
            or not math.isfinite(poll_interval_seconds)
            or not 0.05 <= poll_interval_seconds <= 5.0
        ):
            raise ValueError("poll_interval_seconds must be between 0.05 and 5 seconds")
        if admission_allowed is not None and not callable(admission_allowed):
            raise TypeError("admission_allowed must be a callable returning bool")
        if pause_controller is not None and type(pause_controller) is not JobPauseController:
            raise TypeError("pause_controller must be a JobPauseController")
        if disk_budget_manager is not None:
            if type(disk_budget_manager) is not DiskBudgetManager:
                raise TypeError("disk_budget_manager must be a DiskBudgetManager or None")
            if disk_budget_manager.database is not database:
                raise ValueError("disk budget manager must use the dispatcher database")
            if not callable(getattr(worker, "existing_artifact_bytes_for_budget", None)):
                raise TypeError("worker must inventory existing artifacts for disk admission")

        self._database = database
        self._worker = worker
        self._chapter_fallback = chapter_fallback
        self.recovering_job_id: UUID | None = None
        self._recovery_coordinator = recovery_coordinator
        self._poll_interval_seconds = float(poll_interval_seconds)
        # Unit callers without a resource monitor explicitly retain admission.
        self._admission_allowed = admission_allowed or (lambda: True)
        self._pause_controller = pause_controller
        self._disk_budget_manager = disk_budget_manager
        self._repository = DownloadJobRepository()
        self._cursor = DispatchCursor()
        self._series_id: UUID | None = None
        self._active: dict[UUID, asyncio.Task[None]] = {}
        self._active_sources: dict[UUID, UUID] = {}
        self._waiting_sources: dict[UUID, tuple[UUID, float]] = {}
        self._quiesced_worker_job_ids: set[UUID] = set()
        self._storage_gate_quiesced_job_ids: set[UUID] = set()
        self._reserved_job_ids: set[UUID] = set()
        self._disk_admission_retry_at = 0.0
        self._next_disk_budget_check = 0.0
        self._next_storage_recovery_check = 0.0
        self._wake_event = asyncio.Event()
        self._stop_requested = False
        self._runner_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._remove_storage_gate_listener: Callable[[], None] | None = None

    @property
    def active_job_ids(self) -> tuple[UUID, ...]:
        return tuple(self._active)

    @property
    def source_wait(self) -> tuple[int, int]:
        waiting = tuple(self._waiting_sources.values())
        return len(waiting), max(
            (max(0, math.ceil(deadline - monotonic())) for _, deadline in waiting),
            default=0,
        )

    async def _check_waiting_sources(self) -> None:
        for source_id, (job_id, deadline) in tuple(self._waiting_sources.items()):
            if monotonic() < deadline:
                continue
            try:
                probe = getattr(self._worker, "source_available", None)
                available = callable(probe) and await asyncio.wait_for(probe(job_id), timeout=5.0)
            except Exception:
                available = False
            if available:
                del self._waiting_sources[source_id]
            else:
                self._waiting_sources[source_id] = job_id, monotonic() + SOURCE_RECHECK_SECONDS

    @property
    def active_job_snapshots(self) -> tuple[ActiveJobSnapshot, ...]:
        job_ids = tuple(self._active)
        if not job_ids:
            return ()
        snapshots: list[ActiveJobSnapshot] = []
        with self._database.read_session() as session:
            for job_id in job_ids:
                job = self._repository.get(session, job_id)
                if job is not None and job.status in _ACTIVE_STATUSES:
                    snapshots.append(ActiveJobSnapshot(job_id, job.status))
        return tuple(snapshots)

    async def start(self) -> None:
        if self._runner_task is not None:
            raise RuntimeError("Download dispatcher is already started.")
        self._stop_requested = False
        self._loop = asyncio.get_running_loop()
        self._remove_storage_gate_listener = self._database.storage_gate.subscribe(
            self._storage_gate_closed
        )
        self._wake_event.set()
        self._runner_task = asyncio.create_task(
            self._run(),
            name="yomira-download-dispatcher",
            context=Context(),
        )

    def notify_new_jobs(self) -> None:
        if self._runner_task is not None and not self._stop_requested:
            self._wake_event.set()

    def request_stop(self) -> None:
        """Stop queue admission immediately, then let accepted jobs drain."""
        self._stop_requested = True
        self._wake_event.set()

    async def stop(self) -> None:
        runner_task = self._runner_task
        if runner_task is None:
            return
        self.request_stop()
        try:
            await runner_task
        finally:
            self._runner_task = None
            remove_listener = self._remove_storage_gate_listener
            self._remove_storage_gate_listener = None
            self._loop = None
            if remove_listener is not None:
                remove_listener()

    def _storage_gate_closed(self, _reason: str) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self.notify_new_jobs)

    async def _run(self) -> None:
        while True:
            self._discard_completed_tasks()
            self._release_quiesced_reservations()
            now = monotonic()
            if (
                self._disk_budget_manager is not None
                and not self._database.storage_gate.closed
                and now >= self._next_disk_budget_check
            ):
                self._next_disk_budget_check = now + DISK_BUDGET_RECHECK_SECONDS
                snapshot = await asyncio.to_thread(self._disk_budget_manager.snapshot)
                if not snapshot.safe:
                    self._database.storage_gate.close("DISK_BUDGET_UNSAFE")
            if (
                self._database.storage_gate.closed
                and not self._active
                and now >= self._next_storage_recovery_check
            ):
                if self._disk_budget_manager is None:
                    if await self._settle_quiesced_storage_jobs():
                        self._next_storage_recovery_check = (
                            monotonic() + DISK_BUDGET_RECHECK_SECONDS
                        )
                else:
                    await self._try_recover_storage_gate()
                    self._next_storage_recovery_check = monotonic() + DISK_BUDGET_RECHECK_SECONDS
            if self._stop_requested and not self._active:
                return
            if not self._stop_requested:
                await self._dispatch_available()
            if self._active:
                await asyncio.wait(
                    tuple(self._active.values()),
                    timeout=self._poll_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                continue
            await self._wait_for_wakeup()

    async def _dispatch_available(self) -> None:
        if self._database.storage_gate.closed or not self._is_admission_allowed():
            return
        if monotonic() < self._disk_admission_retry_at:
            return
        await self._check_waiting_sources()
        free_slots = MAX_ACTIVE_JOBS - len(self._active)
        if free_slots <= 0:
            return
        active_by_source: dict[UUID, int] = {}
        for source_id in self._active_sources.values():
            active_by_source[source_id] = active_by_source.get(source_id, 0) + 1
        saturated_sources = frozenset(self._waiting_sources) | frozenset(
            source_id
            for source_id, active_count in active_by_source.items()
            if active_count >= MAX_ACTIVE_JOBS_PER_SOURCE
        )
        try:
            # A single candidate keeps the fairness cursor aligned with an
            # atomic disk reservation; the next poll fills the remaining slot.
            window_limit = 1 if self._disk_budget_manager is not None else free_slots
            window = await asyncio.to_thread(
                self._read_dispatch_window,
                window_limit,
                saturated_sources,
            )
        except Exception as error:
            logger.exception(
                "job.dispatcher.queue_read_failed",
                extra={
                    "error_code": ErrorCode.DB_CONNECTION_FAILED.value,
                    "retryable": True,
                    "stage": "queue_admission",
                    "trace_id": create_trace_id(),
                    "cause_type": type(error).__name__,
                },
            )
            return

        # The monitor can change its decision while the queue query runs in a
        # worker thread. Recheck before advancing the fairness cursor or starting
        # any candidate; a denied window remains eligible on the next poll.
        if self._database.storage_gate.closed or not self._is_admission_allowed():
            return
        previous_cursor = self._cursor
        for candidate in window.candidates:
            if len(self._active) >= MAX_ACTIVE_JOBS:
                break
            active_for_source = active_by_source.get(candidate.source_id, 0)
            if active_for_source >= MAX_ACTIVE_JOBS_PER_SOURCE:
                continue
            job_id = candidate.job.job_id
            if job_id in self._active:
                continue
            if not await self._reserve_disk_for_job(job_id):
                self._cursor = previous_cursor
                return
            if self._database.storage_gate.closed or not self._is_admission_allowed():
                self._quiesced_worker_job_ids.add(job_id)
                self._cursor = previous_cursor
                return
            self._cursor = window.cursor
            self._series_id = candidate.series_id
            self._active_sources[job_id] = candidate.source_id
            active_by_source[candidate.source_id] = active_for_source + 1
            self._active[job_id] = asyncio.create_task(
                self._run_one(job_id),
                name=f"yomira-download-{job_id}",
                context=Context(),
            )

    async def _reserve_disk_for_job(self, job_id: UUID) -> bool:
        manager = self._disk_budget_manager
        if manager is None:
            return True
        try:
            inventory = cast(
                _DiskBudgetAwareRunner,
                self._worker,
            ).existing_artifact_bytes_for_budget
            page_bytes, cbz_bytes = await asyncio.to_thread(
                inventory,
                job_id,
            )
            await asyncio.to_thread(
                manager.reserve,
                job_id,
                existing_page_bytes=page_bytes,
                existing_cbz_bytes=cbz_bytes,
            )
        except DiskBudgetUnavailableError as error:
            snapshot = await asyncio.to_thread(manager.snapshot)
            if not snapshot.safe:
                self._database.storage_gate.close("DISK_BUDGET_UNSAFE")
            self._disk_admission_retry_at = monotonic() + DISK_BUDGET_RECHECK_SECONDS
            logger.warning(
                "job.dispatcher.disk_reservation_denied",
                extra={
                    "error_code": ErrorCode.SYS_DISK_BUDGET_EXCEEDED.value,
                    "retryable": True,
                    "job_id": str(job_id),
                    "stage": "disk_admission",
                    "reason": error.reason,
                    "free_bytes": error.free_bytes,
                    "required_bytes": error.required_bytes,
                    "trace_id": create_trace_id(),
                },
            )
            return False
        except Exception as error:
            self._disk_admission_retry_at = monotonic() + DISK_BUDGET_RECHECK_SECONDS
            logger.exception(
                "job.dispatcher.disk_inventory_failed",
                extra={
                    "error_code": ErrorCode.SYS_DISK_BUDGET_EXCEEDED.value,
                    "retryable": True,
                    "job_id": str(job_id),
                    "stage": "disk_admission_inventory",
                    "cause_type": type(error).__name__,
                    "trace_id": create_trace_id(),
                },
            )
            return False
        self._reserved_job_ids.add(job_id)
        return True

    def _is_admission_allowed(self) -> bool:
        if self._database.storage_gate.closed:
            return False
        try:
            allowed = self._admission_allowed()
            if type(allowed) is not bool:
                raise TypeError("admission callback did not return bool")
            return allowed
        except Exception as error:
            logger.exception(
                "job.dispatcher.admission_monitor_failed",
                extra={
                    "error_code": ErrorCode.SYS_RESOURCE_MONITOR_UNAVAILABLE.value,
                    "retryable": True,
                    "stage": "queue_admission",
                    "trace_id": create_trace_id(),
                    "cause_type": type(error).__name__,
                },
            )
            return False

    def _read_dispatch_window(
        self,
        limit: int,
        saturated_sources: frozenset[UUID],
    ) -> DispatchWindow:
        with self._database.read_session() as session:
            return self._repository.list_queued_for_dispatch(
                session,
                limit=min(limit, MAX_DISPATCH_WINDOW_SIZE),
                cursor=self._cursor,
                excluded_job_ids=tuple(self._active),
                saturated_source_ids=saturated_sources,
                sequential=True,
                preferred_series_id=self._series_id,
            )

    async def _run_one(self, job_id: UUID) -> None:
        registered = False
        quiesced = False
        try:
            if self._pause_controller is not None:
                await self._pause_controller.register(job_id)
                registered = True
            result = await self._worker.run_job(job_id)
            recovered = False
            if (
                isinstance(result, DownloadWorkerResult)
                and self._chapter_fallback is not None
                and self._chapter_fallback.eligible(result)
                and not self._stop_requested
            ):
                self.recovering_job_id = job_id
                try:
                    recovered = await self._chapter_fallback.recover(result)
                except Exception:
                    logger.exception(
                        "chapter.fallback.failed",
                        extra={
                            "job_id": str(job_id),
                            "stage": "alternate_source",
                        },
                    )
                finally:
                    self.recovering_job_id = None
            if (
                not recovered
                and isinstance(result, DownloadWorkerResult)
                and result.retryable
                and (
                    result.error_code
                    in {ErrorCode.SRC_CONNECTOR_FAILED.value, ErrorCode.SRC_CIRCUIT_OPEN.value}
                )
            ):
                source_id = self._active_sources[job_id]
                self._waiting_sources[source_id] = job_id, monotonic() + SOURCE_RECHECK_SECONDS
            quiesced = True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            quiesced = True
            trace_id = create_trace_id()
            logger.exception(
                "job.dispatcher.worker_failed",
                extra={
                    "error_code": ErrorCode.SYS_INTERNAL_ERROR.value,
                    "retryable": False,
                    "job_id": str(job_id),
                    "stage": "worker_entry",
                    "trace_id": trace_id,
                    "cause_type": type(error).__name__,
                },
            )
            try:
                await asyncio.to_thread(self._settle_unhandled_job, job_id)
            except Exception as settlement_error:
                logger.exception(
                    "job.dispatcher.unhandled_job_settlement_failed",
                    extra={
                        "error_code": ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
                        "retryable": False,
                        "job_id": str(job_id),
                        "stage": "worker_entry_settlement",
                        "trace_id": trace_id,
                        "cause_type": type(settlement_error).__name__,
                    },
                )
        finally:
            if registered and self._pause_controller is not None:
                try:
                    await self._pause_controller.unregister(job_id)
                except Exception as error:
                    logger.exception(
                        "job.dispatcher.pause_registration_cleanup_failed",
                        extra={
                            "error_code": ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
                            "retryable": False,
                            "job_id": str(job_id),
                            "stage": "pause_controller_cleanup",
                            "trace_id": create_trace_id(),
                            "cause_type": type(error).__name__,
                        },
                    )
            if quiesced:
                self._quiesced_worker_job_ids.add(job_id)
                if self._database.storage_gate.closed:
                    self._storage_gate_quiesced_job_ids.add(job_id)

    def _settle_unhandled_job(self, job_id: UUID) -> None:
        with self._database.read_session() as session:
            current = self._repository.get(session, job_id)
        if current is None:
            return
        if current.status is DownloadJobStatus.QUEUED:
            if self._database.storage_gate.closed:
                return
            with self._database.write_session() as session:
                latest = self._repository.get(session, job_id)
                if latest is None or latest.status is not DownloadJobStatus.QUEUED:
                    return
                self._repository.transition_status(
                    session,
                    current_job=latest,
                    target_status=DownloadJobStatus.FAILED,
                    changed_at=latest.updated_at,
                )
            return
        if current.status in _ACTIVE_STATUSES:
            if self._database.storage_gate.closed:
                return
            self._recovery_coordinator.interrupt_quiesced_job(
                job_id,
                current,
                quiescence_ack=WorkerQuiescenceAck(job_id),
                recovery_blocked=True,
            )

    async def _try_recover_storage_gate(self) -> bool:
        gate = self._database.storage_gate
        if gate.closed_reason not in _RECOVERABLE_STORAGE_GATE_REASONS:
            return False
        quiesced = await asyncio.to_thread(
            gate.wait_for_quiescence,
            self._database.options.shutdown_timeout_seconds,
        )
        if not quiesced:
            return False
        self._release_quiesced_reservations()
        if self._reserved_job_ids:
            return False
        manager = self._disk_budget_manager
        if manager is None:
            return False
        before = await asyncio.to_thread(manager.snapshot)
        if not before.safe or not await asyncio.to_thread(self._database.verify_recovery_integrity):
            return False
        if not await self._settle_quiesced_storage_jobs():
            return False
        if not await asyncio.to_thread(self._database.verify_recovery_integrity):
            return False
        after = await asyncio.to_thread(manager.snapshot)
        if not after.safe or not gate.reopen_after_recovery():
            return False
        self._disk_admission_retry_at = 0.0
        logger.warning(
            "job.dispatcher.storage_gate_reopened",
            extra={
                "stage": "verified_storage_recovery",
                "volume_serial": after.volume_serial,
                "free_bytes": after.free_bytes,
                "required_bytes": after.required_bytes,
                "trace_id": create_trace_id(),
            },
        )
        return True

    def _release_quiesced_reservations(self) -> None:
        manager = self._disk_budget_manager
        if manager is None or self._database.storage_gate.active_mutations:
            return
        for job_id in tuple(self._reserved_job_ids):
            if job_id in self._active or job_id not in self._quiesced_worker_job_ids:
                continue
            try:
                manager.release(job_id, quiescent=True)
            except RuntimeError:
                continue
            self._reserved_job_ids.discard(job_id)
            self._quiesced_worker_job_ids.discard(job_id)
            self._disk_admission_retry_at = 0.0
            self._wake_event.set()

    async def _settle_quiesced_storage_jobs(self) -> bool:
        if not self._storage_gate_quiesced_job_ids:
            return True
        job_ids = tuple(sorted(self._storage_gate_quiesced_job_ids, key=str))
        quiesced = await asyncio.to_thread(
            self._database.storage_gate.wait_for_quiescence,
            self._database.options.shutdown_timeout_seconds,
        )
        if not quiesced:
            logger.error(
                "job.dispatcher.storage_gate_quiescence_timeout",
                extra={
                    "error_code": ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
                    "retryable": False,
                    "stage": "storage_gate_quiescence",
                    "pending_jobs": len(job_ids),
                    "trace_id": create_trace_id(),
                },
            )
            self._next_storage_recovery_check = monotonic() + self._poll_interval_seconds
            return False

        for job_id in job_ids:
            try:
                self._settle_quiesced_storage_job(job_id)
            except Exception as error:
                logger.exception(
                    "job.dispatcher.storage_gate_settlement_failed",
                    extra={
                        "error_code": ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
                        "retryable": True,
                        "job_id": str(job_id),
                        "stage": "storage_gate_recovery_cas",
                        "trace_id": create_trace_id(),
                        "cause_type": type(error).__name__,
                    },
                )
                # Keep this and every unvisited ID pending for the next
                # bounded recovery attempt; this pass never spins.
                self._next_storage_recovery_check = monotonic() + DISK_BUDGET_RECHECK_SECONDS
                return False
            self._storage_gate_quiesced_job_ids.discard(job_id)
        return True

    def _settle_quiesced_storage_job(self, job_id: UUID) -> None:
        with self._database.read_session() as session:
            current = self._repository.get(session, job_id)
        if current is None:
            return
        ack = WorkerQuiescenceAck(job_id)
        if current.status in _ACTIVE_STATUSES:
            early_stage = self._disk_budget_manager is not None and current.status in {
                DownloadJobStatus.FETCHING_METADATA,
                DownloadJobStatus.DOWNLOADING,
            }
            self._recovery_coordinator.interrupt_quiesced_job(
                job_id,
                current,
                quiescence_ack=ack,
                recovery_blocked=(not early_stage or current.cancel_requested_at is not None),
                storage_gate_quiesced=True,
            )
        elif current.status is not DownloadJobStatus.INTERRUPTED:
            return
        # A failed resume CAS may leave an already interrupted row pending.
        # Reverify its checkpoint on the next bounded recovery attempt.
        if self._disk_budget_manager is not None:
            self._recovery_coordinator.recover_job(job_id, quiescence_ack=ack)

    def _discard_completed_tasks(self) -> None:
        completed = [job_id for job_id, task in self._active.items() if task.done()]
        for job_id in completed:
            task = self._active.pop(job_id)
            self._active_sources.pop(job_id, None)
            if self._database.storage_gate.closed and job_id in self._quiesced_worker_job_ids:
                self._storage_gate_quiesced_job_ids.add(job_id)
            if self._disk_budget_manager is None or job_id not in self._reserved_job_ids:
                self._quiesced_worker_job_ids.discard(job_id)
            try:
                task.result()
            except asyncio.CancelledError:
                logger.error(
                    "job.dispatcher.worker_cancelled",
                    extra={
                        "error_code": ErrorCode.DL_JOB_RECOVERY_BLOCKED.value,
                        "retryable": False,
                        "job_id": str(job_id),
                        "stage": "worker_task",
                        "trace_id": create_trace_id(),
                    },
                )
            except Exception as error:
                logger.exception(
                    "job.dispatcher.task_failed",
                    extra={
                        "error_code": ErrorCode.SYS_INTERNAL_ERROR.value,
                        "retryable": False,
                        "job_id": str(job_id),
                        "stage": "worker_task",
                        "trace_id": create_trace_id(),
                        "cause_type": type(error).__name__,
                    },
                )

    async def _wait_for_wakeup(self) -> None:
        timeout = self._poll_interval_seconds
        if self._database.storage_gate.closed and not self._active:
            timeout = max(timeout, self._next_storage_recovery_check - monotonic())
        try:
            await asyncio.wait_for(
                self._wake_event.wait(),
                timeout=timeout,
            )
        except TimeoutError:
            pass
        finally:
            self._wake_event.clear()
