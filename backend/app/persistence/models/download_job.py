from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase

DOWNLOAD_JOB_ID_UUID4_CHECK = (
    "typeof(job_id) = 'text' "
    "AND length(job_id) = 36 "
    "AND length(replace(job_id, '-', '')) = 32 "
    "AND substr(job_id, 9, 1) = '-' "
    "AND substr(job_id, 14, 1) = '-' "
    "AND substr(job_id, 19, 1) = '-' "
    "AND substr(job_id, 24, 1) = '-' "
    "AND substr(job_id, 15, 1) = '4' "
    "AND substr(job_id, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND job_id = lower(job_id) "
    "AND job_id NOT GLOB '*[^0-9a-f-]*'"
)
DOWNLOAD_JOB_STATUSES = (
    "'QUEUED', 'FETCHING_METADATA', 'DOWNLOADING', 'VALIDATING', 'PROCESSING', "
    "'PACKAGING', 'VERIFYING', 'INDEXING', 'COMPLETED', 'FAILED', 'CANCELLED', 'INTERRUPTED'"
)


class DownloadJobRow(PersistenceBase):
    __tablename__ = "download_jobs"
    __table_args__ = (
        CheckConstraint(DOWNLOAD_JOB_ID_UUID4_CHECK, name="job_id_uuid4"),
        CheckConstraint(
            f"status IN ({DOWNLOAD_JOB_STATUSES})",
            name="status_supported",
        ),
        CheckConstraint(
            "priority IN ('HIGH', 'NORMAL', 'LOW')",
            name="priority_supported",
        ),
        CheckConstraint(
            "typeof(created_at_us) = 'integer' AND typeof(updated_at_us) = 'integer'",
            name="timestamps_integer",
        ),
        CheckConstraint(
            "cancel_requested_at_us IS NULL OR ("
            "typeof(cancel_requested_at_us) = 'integer' "
            "AND cancel_requested_at_us >= created_at_us "
            "AND cancel_requested_at_us <= updated_at_us)",
            name="cancel_requested_at_ordered_integer",
        ),
        CheckConstraint(
            "interrupted_from_status IS NULL OR ("
            "typeof(interrupted_from_status) = 'text' AND interrupted_from_status IN ("
            "'FETCHING_METADATA', 'DOWNLOADING', 'VALIDATING', 'PROCESSING', "
            "'PACKAGING', 'VERIFYING', 'INDEXING'))",
            name="interrupted_from_status_supported",
        ),
        CheckConstraint(
            "typeof(recovery_blocked) = 'integer' AND recovery_blocked IN (0, 1)",
            name="recovery_blocked_boolean",
        ),
        Index("ix_download_jobs_chapter_source_id", "chapter_source_id"),
        Index(
            "ix_download_jobs_dispatch_source",
            "chapter_source_id",
            "status",
            "priority",
            "created_at_us",
            "job_id",
        ),
        Index("ix_download_jobs_status", "status"),
    )

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chapter_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "chapter_sources.chapter_source_id",
            ondelete="RESTRICT",
            name="fk_download_jobs_chapter_source_id",
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text(), nullable=False)
    priority: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    updated_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    cancel_requested_at_us: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    interrupted_from_status: Mapped[str | None] = mapped_column(Text(), nullable=True)
    recovery_blocked: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=text("0")
    )
