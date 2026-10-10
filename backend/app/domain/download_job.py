from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from app.domain.timestamps import normalise_utc


class DownloadJobStatus(StrEnum):
    QUEUED = "QUEUED"
    FETCHING_METADATA = "FETCHING_METADATA"
    DOWNLOADING = "DOWNLOADING"
    VALIDATING = "VALIDATING"
    PROCESSING = "PROCESSING"
    PACKAGING = "PACKAGING"
    VERIFYING = "VERIFYING"
    INDEXING = "INDEXING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class DownloadPriority(StrEnum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


_INTERRUPT_SOURCE_STATUSES = frozenset(
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


@dataclass(frozen=True, slots=True)
class DownloadJob:
    job_id: UUID
    chapter_source_id: UUID
    status: DownloadJobStatus
    priority: DownloadPriority
    created_at: datetime
    updated_at: datetime
    cancel_requested_at: datetime | None = None
    interrupted_from_status: DownloadJobStatus | None = None
    recovery_blocked: bool = False

    def __post_init__(self) -> None:
        for field_name in ("job_id", "chapter_source_id"):
            if getattr(self, field_name).version != 4:
                raise ValueError(f"{field_name} must be a UUIDv4")
        if type(self.status) is not DownloadJobStatus:
            raise ValueError("status must be a supported DownloadJobStatus")
        if type(self.priority) is not DownloadPriority:
            raise ValueError("priority must be a supported DownloadPriority")
        interrupted_from_status = self.interrupted_from_status
        if interrupted_from_status is not None and (
            type(interrupted_from_status) is not DownloadJobStatus
            or interrupted_from_status not in _INTERRUPT_SOURCE_STATUSES
        ):
            raise ValueError(
                "interrupted_from_status must be a supported active status or None"
            )
        if type(self.recovery_blocked) is not bool:
            raise ValueError("recovery_blocked must be a boolean")

        created_at = normalise_utc(self.created_at, "created_at")
        updated_at = normalise_utc(self.updated_at, "updated_at")
        if updated_at < created_at:
            raise ValueError("updated_at must not precede created_at")
        cancel_requested_at = self.cancel_requested_at
        if cancel_requested_at is not None:
            cancel_requested_at = normalise_utc(
                cancel_requested_at,
                "cancel_requested_at",
            )
            if cancel_requested_at < created_at:
                raise ValueError("cancel_requested_at must not precede created_at")
            if cancel_requested_at > updated_at:
                raise ValueError("cancel_requested_at must not follow updated_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "cancel_requested_at", cancel_requested_at)

    @classmethod
    def new_local(
        cls,
        chapter_source_id: UUID,
        *,
        priority: DownloadPriority = DownloadPriority.NORMAL,
        job_id: UUID | None = None,
        now: datetime | None = None,
    ) -> DownloadJob:
        created_at = now if now is not None else datetime.now(UTC)
        return cls(
            job_id=job_id if job_id is not None else uuid4(),
            chapter_source_id=chapter_source_id,
            status=DownloadJobStatus.QUEUED,
            priority=priority,
            created_at=created_at,
            updated_at=created_at,
        )
