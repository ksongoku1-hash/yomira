from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.domain.export_job import ExportJob, ExportJobStatus
from app.persistence.mappers.export_job import export_job_from_row, export_job_to_row
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.models.export_job import ExportJobRow

MAX_TERMINAL_EXPORT_JOBS = 1_000
EXPORT_HISTORY_RETENTION_DAYS = 90
_TERMINAL_STATUS_VALUES = tuple(
    status.value
    for status in (
        ExportJobStatus.EXPORTED,
        ExportJobStatus.SKIPPED,
        ExportJobStatus.FAILED,
        ExportJobStatus.INTERRUPTED,
    )
)
_ACTIVE_STATUS_VALUES = (ExportJobStatus.QUEUED.value, ExportJobStatus.EXPORTING.value)
_RETENTION_US = EXPORT_HISTORY_RETENTION_DAYS * 24 * 60 * 60 * 1_000_000


class ExportIdempotencyConflict(RuntimeError):
    """An idempotency key was reused with a different request payload."""


class ExportHistoryCapacityError(RuntimeError):
    """The bounded history cannot safely reserve another export job."""


class ExportJobTransitionConflict(RuntimeError):
    """An export job changed state before the requested transition committed."""


@dataclass(frozen=True, slots=True)
class ExportJobPage:
    items: tuple[ExportJob, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ExportJobCursorPage:
    items: tuple[ExportJob, ...]
    total: int
    limit: int
    has_more: bool


class ExportJobRepository:
    """SQLite operations for durable, bounded USB export history."""

    def get(self, session: Session, export_id: UUID) -> ExportJob | None:
        row = session.get(ExportJobRow, str(export_id))
        return export_job_from_row(row) if row is not None else None

    def get_by_idempotency_key(
        self,
        session: Session,
        idempotency_key_hash: str,
    ) -> ExportJob | None:
        row = session.scalar(
            select(ExportJobRow).where(ExportJobRow.idempotency_key_hash == idempotency_key_hash)
        )
        return export_job_from_row(row) if row is not None else None

    def create_with_retention(
        self,
        session: Session,
        job: ExportJob,
        *,
        now: datetime,
    ) -> tuple[ExportJob, bool]:
        now_us = datetime_to_epoch_us(now)
        self._prune_terminal_history(session, now_us)
        existing = self.get_by_idempotency_key(session, job.idempotency_key_hash)
        if existing is not None:
            if existing.request_fingerprint != job.request_fingerprint:
                raise ExportIdempotencyConflict()
            return existing, False

        active_count = int(
            session.scalar(
                select(func.count())
                .select_from(ExportJobRow)
                .where(ExportJobRow.status.in_(_ACTIVE_STATUS_VALUES))
            )
            or 0
        )
        if active_count >= MAX_TERMINAL_EXPORT_JOBS:
            raise ExportHistoryCapacityError()

        terminal_count = int(
            session.scalar(
                select(func.count())
                .select_from(ExportJobRow)
                .where(ExportJobRow.status.in_(_TERMINAL_STATUS_VALUES))
            )
            or 0
        )
        # Active records reserve their future terminal slots. This keeps the
        # final history at or below 1,000 even when requests overlap.
        terminal_capacity = MAX_TERMINAL_EXPORT_JOBS - active_count - 1
        excess = terminal_count - terminal_capacity
        if excess > 0:
            oldest_ids = session.scalars(
                select(ExportJobRow.export_id)
                .where(ExportJobRow.status.in_(_TERMINAL_STATUS_VALUES))
                .order_by(
                    ExportJobRow.terminal_at_us.asc(),
                    ExportJobRow.created_at_us.asc(),
                    ExportJobRow.export_id.asc(),
                )
                .limit(excess)
            ).all()
            if len(oldest_ids) != excess:
                raise RuntimeError("Export history retention snapshot changed unexpectedly.")
            session.execute(delete(ExportJobRow).where(ExportJobRow.export_id.in_(oldest_ids)))

        session.add(export_job_to_row(job))
        session.flush()
        return job, True

    def _prune_terminal_history(self, session: Session, now_us: int) -> None:
        cutoff_us = now_us - _RETENTION_US
        session.execute(
            delete(ExportJobRow).where(
                ExportJobRow.status.in_(_TERMINAL_STATUS_VALUES),
                ExportJobRow.terminal_at_us <= cutoff_us,
            )
        )

    def save_transition(
        self,
        session: Session,
        job: ExportJob,
        *,
        expected_status: ExportJobStatus,
    ) -> ExportJob:
        row = session.get(ExportJobRow, str(job.export_id))
        if row is None or row.status != expected_status.value:
            raise ExportJobTransitionConflict()

        mapped = export_job_to_row(job)
        row.status = mapped.status
        row.updated_at_us = mapped.updated_at_us
        row.started_at_us = mapped.started_at_us
        row.terminal_at_us = mapped.terminal_at_us
        row.error_code = mapped.error_code
        row.failure_stage = mapped.failure_stage
        row.retryable = mapped.retryable
        row.cause_type = mapped.cause_type
        session.flush()
        return job

    def interrupt_active(
        self,
        session: Session,
        *,
        now: datetime,
    ) -> int:
        active_rows = session.scalars(
            select(ExportJobRow)
            .where(ExportJobRow.status.in_(_ACTIVE_STATUS_VALUES))
            .order_by(ExportJobRow.created_at_us.asc(), ExportJobRow.export_id.asc())
        ).all()
        interrupted_at = datetime_to_epoch_us(now)
        for row in active_rows:
            row.status = ExportJobStatus.INTERRUPTED.value
            row.updated_at_us = max(row.updated_at_us, interrupted_at)
            row.terminal_at_us = row.updated_at_us
            row.error_code = "SYS_STARTUP_FAILED"
            row.failure_stage = "startup_recovery"
            row.retryable = True
            row.cause_type = "ProcessRestart"
        session.flush()
        return len(active_rows)

    def list_page(
        self,
        session: Session,
        *,
        limit: int,
        offset: int,
    ) -> ExportJobPage:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be non-negative")
        total = int(session.scalar(select(func.count()).select_from(ExportJobRow)) or 0)
        rows = session.scalars(
            select(ExportJobRow)
            .order_by(ExportJobRow.created_at_us.desc(), ExportJobRow.export_id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return ExportJobPage(
            items=tuple(export_job_from_row(row) for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )

    def list_page_after(
        self,
        session: Session,
        *,
        limit: int,
        before: tuple[int, UUID] | None,
    ) -> ExportJobCursorPage:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if before is not None:
            timestamp_us, export_id = before
            if (
                type(timestamp_us) is not int
                or not 0 < timestamp_us <= 9_223_372_036_854_775_807
                or type(export_id) is not UUID
                or export_id.version != 4
            ):
                raise ValueError("before must contain a bounded timestamp and UUIDv4")

        total = int(session.scalar(select(func.count()).select_from(ExportJobRow)) or 0)
        statement = select(ExportJobRow)
        if before is not None:
            timestamp_us, export_id = before
            statement = statement.where(
                or_(
                    ExportJobRow.created_at_us < timestamp_us,
                    and_(
                        ExportJobRow.created_at_us == timestamp_us,
                        ExportJobRow.export_id < str(export_id),
                    ),
                )
            )
        rows = session.scalars(
            statement.order_by(
                ExportJobRow.created_at_us.desc(), ExportJobRow.export_id.desc()
            ).limit(limit + 1)
        ).all()
        has_more = len(rows) > limit
        return ExportJobCursorPage(
            items=tuple(export_job_from_row(row) for row in rows[:limit]),
            total=total,
            limit=limit,
            has_more=has_more,
        )
