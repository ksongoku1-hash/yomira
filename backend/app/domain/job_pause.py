"""Bounded, cooperative pause control for active download jobs."""

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from app.domain.download_job import DownloadJobStatus
from app.domain.job_fairness import MAX_DISPATCH_WINDOW_SIZE

SERIES_PAUSE_KEY_PREFIX = "downloads.paused_series."


class JobPauseState(StrEnum):
    RUNNING = "RUNNING"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"


class PauseCheckpointOutcome(StrEnum):
    CONTINUE = "CONTINUE"
    RESUMED = "RESUMED"
    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"


class PauseReason(StrEnum):
    USER = "USER"
    BACKPRESSURE = "BACKPRESSURE"
    MONITOR_UNAVAILABLE = "MONITOR_UNAVAILABLE"


class JobPauseError(ValueError):
    """Base class for invalid pause-controller operations."""


class ActiveJobAlreadyRegistered(JobPauseError):
    def __init__(self, job_id: UUID) -> None:
        self.job_id = job_id
        super().__init__("Download job is already registered as active.")


class UnknownActiveJob(JobPauseError):
    def __init__(self, job_id: UUID) -> None:
        self.job_id = job_id
        super().__init__("Download job is not registered as active.")


class PauseCapacityExceeded(JobPauseError):
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        super().__init__("Active job pause registry reached its bounded capacity.")


class JobPauseNotAllowed(JobPauseError):
    def __init__(self, status: DownloadJobStatus | None, reason: str) -> None:
        self.status = status
        self.reason = reason
        if status is None:
            message = f"Job cannot pause: {reason}"
        else:
            message = f"Job cannot pause in {status.value}: {reason}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PauseSafetyEvidence:
    network_operation_inactive: bool = False
    new_requests_stopped: bool = False
    stream_closed: bool = False
    safe_point_reached: bool = False
    between_pages: bool = False
    write_critical_operation_inactive: bool = False
    cleanup_complete: bool = False
    database_transaction_closed: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "network_operation_inactive",
            "new_requests_stopped",
            "stream_closed",
            "safe_point_reached",
            "between_pages",
            "write_critical_operation_inactive",
            "cleanup_complete",
            "database_transaction_closed",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be a bool")


@dataclass(slots=True)
class _PauseRecord:
    state: JobPauseState
    resume_event: asyncio.Event
    cancellation_event: asyncio.Event = field(default_factory=asyncio.Event)
    pause_reasons: set[PauseReason] = field(default_factory=lambda: set[PauseReason]())


_PAUSABLE_STATUSES = frozenset(
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


class JobPauseController:
    """Track pause intent for no more than eight active jobs on one event loop."""

    def __init__(self, *, max_active_jobs: int = MAX_DISPATCH_WINDOW_SIZE) -> None:
        if (
            type(max_active_jobs) is not int
            or not 1 <= max_active_jobs <= MAX_DISPATCH_WINDOW_SIZE
        ):
            raise ValueError("max_active_jobs must be between 1 and 8")
        self._max_active_jobs = max_active_jobs
        self._records: dict[UUID, _PauseRecord] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def active_job_count(self) -> int:
        self._require_event_loop()
        return len(self._records)

    async def register(self, job_id: UUID) -> None:
        self._require_event_loop(bind=True)
        self._validate_job_id(job_id)
        if job_id in self._records:
            raise ActiveJobAlreadyRegistered(job_id)
        if len(self._records) >= self._max_active_jobs:
            raise PauseCapacityExceeded(self._max_active_jobs)
        self._records[job_id] = _PauseRecord(
            state=JobPauseState.RUNNING,
            resume_event=asyncio.Event(),
        )

    async def unregister(self, job_id: UUID) -> None:
        self._require_event_loop()
        record = self._get_record(job_id)
        record.state = JobPauseState.CANCELLATION_REQUESTED
        record.pause_reasons.clear()
        record.resume_event.set()
        record.cancellation_event.set()
        del self._records[job_id]

    def request_pause(
        self,
        job_id: UUID,
        *,
        reason: PauseReason = PauseReason.USER,
    ) -> JobPauseState:
        self._require_event_loop()
        self._validate_reason(reason)
        record = self._get_record(job_id)
        if record.state is JobPauseState.CANCELLATION_REQUESTED:
            raise JobPauseNotAllowed(
                None,
                "cancellation is already pending",
            )
        record.pause_reasons.add(reason)
        if record.state is JobPauseState.RUNNING:
            record.state = JobPauseState.PAUSE_REQUESTED
        return record.state

    def resume(
        self,
        job_id: UUID,
        *,
        reason: PauseReason = PauseReason.USER,
    ) -> JobPauseState:
        self._require_event_loop()
        self._validate_reason(reason)
        record = self._get_record(job_id)
        if record.state is JobPauseState.CANCELLATION_REQUESTED:
            raise JobPauseNotAllowed(
                None,
                "cancellation has precedence over resume",
            )
        record.pause_reasons.discard(reason)
        if not record.pause_reasons and record.state in {
            JobPauseState.PAUSE_REQUESTED,
            JobPauseState.PAUSED,
        }:
            record.state = JobPauseState.RUNNING
            record.resume_event.set()
        return record.state

    def notify_cancellation_requested(self, job_id: UUID) -> JobPauseState:
        """Wake a paused worker after the caller durably records cancellation."""
        self._require_event_loop()
        record = self._get_record(job_id)
        record.state = JobPauseState.CANCELLATION_REQUESTED
        record.pause_reasons.clear()
        record.resume_event.set()
        record.cancellation_event.set()
        return record.state

    def cancellation_event(self, job_id: UUID) -> asyncio.Event:
        """Return the active job's event for interruptible network operations."""
        self._require_event_loop()
        return self._get_record(job_id).cancellation_event

    def state(self, job_id: UUID) -> JobPauseState:
        self._require_event_loop()
        return self._get_record(job_id).state

    def pause_reasons(self, job_id: UUID) -> frozenset[PauseReason]:
        self._require_event_loop()
        return frozenset(self._get_record(job_id).pause_reasons)

    async def pause_if_requested(
        self,
        job_id: UUID,
        *,
        status: DownloadJobStatus,
        evidence: PauseSafetyEvidence,
    ) -> PauseCheckpointOutcome:
        self._require_event_loop()
        if type(status) is not DownloadJobStatus:
            raise TypeError("status must be a supported DownloadJobStatus")
        if type(evidence) is not PauseSafetyEvidence:
            raise TypeError("evidence must be PauseSafetyEvidence")
        if status not in _PAUSABLE_STATUSES:
            raise JobPauseNotAllowed(status, "the job has no active pause checkpoint")

        record = self._get_record(job_id)
        if record.state is JobPauseState.CANCELLATION_REQUESTED:
            return PauseCheckpointOutcome.CANCELLATION_REQUESTED
        if not record.pause_reasons:
            return PauseCheckpointOutcome.CONTINUE
        if record.state not in {JobPauseState.PAUSE_REQUESTED, JobPauseState.PAUSED}:
            return PauseCheckpointOutcome.CONTINUE
        if not self._is_safe_checkpoint(status, evidence):
            return PauseCheckpointOutcome.CONTINUE

        record.state = JobPauseState.PAUSED
        record.resume_event.clear()
        await record.resume_event.wait()
        return self._outcome_after_resume(record)

    @staticmethod
    def _outcome_after_resume(record: _PauseRecord) -> PauseCheckpointOutcome:
        if record.state is JobPauseState.CANCELLATION_REQUESTED:
            return PauseCheckpointOutcome.CANCELLATION_REQUESTED
        if record.state is not JobPauseState.RUNNING:
            raise RuntimeError("Pause checkpoint resumed without a valid signal.")
        return PauseCheckpointOutcome.RESUMED

    def _require_event_loop(self, *, bind: bool = False) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as error:
            raise RuntimeError(
                "JobPauseController must be used from a running asyncio event loop."
            ) from error
        if self._loop is None:
            if bind:
                self._loop = loop
            return
        if self._loop is not loop:
            raise RuntimeError("JobPauseController is confined to its owning event loop.")

    def _get_record(self, job_id: UUID) -> _PauseRecord:
        self._validate_job_id(job_id)
        record = self._records.get(job_id)
        if record is None:
            raise UnknownActiveJob(job_id)
        return record

    @staticmethod
    def _validate_job_id(job_id: UUID) -> None:
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")

    @staticmethod
    def _validate_reason(reason: PauseReason) -> None:
        if type(reason) is not PauseReason:
            raise ValueError("reason must be a supported PauseReason")

    @staticmethod
    def _is_safe_checkpoint(
        status: DownloadJobStatus,
        evidence: PauseSafetyEvidence,
    ) -> bool:
        match status:
            case DownloadJobStatus.FETCHING_METADATA:
                return evidence.network_operation_inactive
            case DownloadJobStatus.DOWNLOADING:
                return evidence.new_requests_stopped and evidence.stream_closed
            case DownloadJobStatus.VALIDATING | DownloadJobStatus.VERIFYING:
                return evidence.safe_point_reached
            case DownloadJobStatus.PROCESSING:
                return evidence.between_pages
            case DownloadJobStatus.PACKAGING:
                return (
                    evidence.write_critical_operation_inactive
                    and evidence.cleanup_complete
                )
            case DownloadJobStatus.INDEXING:
                return evidence.database_transaction_closed
            case _:
                return False
