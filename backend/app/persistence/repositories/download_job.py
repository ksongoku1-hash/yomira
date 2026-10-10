from collections.abc import Collection
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.domain.download_job import DownloadJob, DownloadJobStatus, DownloadPriority
from app.domain.job_cancellation import (
    CancellationFinalizationRequired,
    CancellationRequestPending,
    CancellationSafetyEvidence,
    JobCancellationNotAllowed,
    JobCancellationNotRequested,
    assert_cancellation_safe,
)
from app.domain.job_fairness import (
    MAX_DISPATCH_WINDOW_SIZE,
    PRIORITY_WHEEL,
    DispatchCursor,
    DispatchWindow,
    QueuedJobCandidate,
)
from app.domain.job_state_machine import IllegalJobTransition, assert_transition_allowed
from app.domain.timestamps import normalise_utc
from app.persistence.mappers.download_job import download_job_from_row, download_job_to_row
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.download_manifest_state import DownloadManifestStateRow

MAX_DISPATCH_BATCH_SIZE = 8
MAX_RECOVERY_BATCH_SIZE = 64
_INTERRUPTIBLE_ACTIVE_STATUSES = (
    DownloadJobStatus.FETCHING_METADATA,
    DownloadJobStatus.DOWNLOADING,
    DownloadJobStatus.VALIDATING,
    DownloadJobStatus.PROCESSING,
    DownloadJobStatus.PACKAGING,
    DownloadJobStatus.VERIFYING,
    DownloadJobStatus.INDEXING,
)


class StaleJobStateError(RuntimeError):
    def __init__(self, expected: DownloadJob, actual: DownloadJob | None) -> None:
        self.job_id = expected.job_id
        self.expected_status = expected.status
        self.expected_updated_at = expected.updated_at
        self.expected_cancel_requested_at = expected.cancel_requested_at
        self.expected_interrupted_from_status = expected.interrupted_from_status
        self.expected_recovery_blocked = expected.recovery_blocked
        self.actual_status = actual.status if actual is not None else None
        self.actual_updated_at = actual.updated_at if actual is not None else None
        self.actual_cancel_requested_at = actual.cancel_requested_at if actual is not None else None
        self.actual_interrupted_from_status = (
            actual.interrupted_from_status if actual is not None else None
        )
        self.actual_recovery_blocked = actual.recovery_blocked if actual is not None else None
        super().__init__("DownloadJob state changed since it was read.")


class DownloadJobRepository:
    def add(self, session: Session, job: DownloadJob) -> None:
        session.add(download_job_to_row(job))

    def get(self, session: Session, job_id: UUID) -> DownloadJob | None:
        row = session.get(DownloadJobRow, str(job_id))
        return download_job_from_row(row) if row is not None else None

    def list_active_jobs(
        self,
        session: Session,
        *,
        limit: int,
        after_job_id: UUID | None = None,
        include_unblocked_interrupted: bool = False,
    ) -> tuple[DownloadJob, ...]:
        """Read active jobs, optionally including unfinished startup recovery."""
        if type(limit) is not int or not 1 <= limit <= MAX_RECOVERY_BATCH_SIZE:
            raise ValueError("limit must be between 1 and 64")
        if after_job_id is not None and (
            type(after_job_id) is not UUID or after_job_id.version != 4
        ):
            raise ValueError("after_job_id must be a UUIDv4 or None")
        if type(include_unblocked_interrupted) is not bool:
            raise TypeError("include_unblocked_interrupted must be a bool")

        recovery_candidates = DownloadJobRow.status.in_(
            tuple(status.value for status in _INTERRUPTIBLE_ACTIVE_STATUSES)
        )
        if include_unblocked_interrupted:
            recovery_candidates = or_(
                recovery_candidates,
                (DownloadJobRow.status == DownloadJobStatus.INTERRUPTED.value)
                & DownloadJobRow.recovery_blocked.is_(False),
            )
        statement = select(DownloadJobRow).where(recovery_candidates)
        if after_job_id is not None:
            statement = statement.where(DownloadJobRow.job_id > str(after_job_id))
        rows = session.execute(
            statement.order_by(DownloadJobRow.job_id.asc()).limit(limit)
        ).scalars()
        return tuple(download_job_from_row(row) for row in rows)

    def list_queued_for_dispatch(
        self,
        session: Session,
        *,
        limit: int,
        cursor: DispatchCursor | None = None,
        excluded_job_ids: Collection[UUID] = (),
        saturated_source_ids: Collection[UUID] = (),
        sequential: bool = False,
        preferred_series_id: UUID | None = None,
    ) -> DispatchWindow:
        if type(limit) is not int or not 1 <= limit <= MAX_DISPATCH_BATCH_SIZE:
            raise ValueError("limit must be between 1 and 8")
        if cursor is None:
            current_cursor = DispatchCursor()
        elif type(cursor) is DispatchCursor:
            current_cursor = cursor
        else:
            raise TypeError("cursor must be DispatchCursor")
        excluded_ids = self._bounded_uuid4_collection(
            excluded_job_ids,
            field_name="excluded_job_ids",
        )
        saturated_ids = self._bounded_uuid4_collection(
            saturated_source_ids,
            field_name="saturated_source_ids",
        )
        if type(sequential) is not bool:
            raise TypeError("sequential must be a bool")
        if preferred_series_id is not None and (
            type(preferred_series_id) is not UUID or preferred_series_id.version != 4
        ):
            raise ValueError("preferred_series_id must be a UUIDv4 or None")
        if sequential:
            candidate = self._next_queued_candidate(
                session,
                priority=None,
                source_after=None,
                excluded_job_ids=excluded_ids,
                saturated_source_ids=saturated_ids,
                preferred_series_id=preferred_series_id,
            )
            return DispatchWindow((candidate,) if candidate is not None else (), current_cursor)

        selected: list[QueuedJobCandidate] = []
        selected_ids = list(excluded_ids)
        empty_priorities: set[DownloadPriority] = set()

        while len(selected) < limit:
            next_candidate: QueuedJobCandidate | None = None
            selected_slot: int | None = None
            for offset in range(len(PRIORITY_WHEEL)):
                slot = (current_cursor.next_priority_slot + offset) % len(PRIORITY_WHEEL)
                priority = PRIORITY_WHEEL[slot]
                if priority in empty_priorities:
                    continue

                source_after = current_cursor.source_after(priority)
                next_candidate = self._next_queued_candidate(
                    session,
                    priority=priority,
                    source_after=source_after,
                    excluded_job_ids=selected_ids,
                    saturated_source_ids=saturated_ids,
                )
                if next_candidate is None and source_after is not None:
                    next_candidate = self._next_queued_candidate(
                        session,
                        priority=priority,
                        source_after=None,
                        excluded_job_ids=selected_ids,
                        saturated_source_ids=saturated_ids,
                    )
                if next_candidate is None:
                    empty_priorities.add(priority)
                    continue

                selected_slot = slot
                break

            if next_candidate is None or selected_slot is None:
                break

            selected.append(next_candidate)
            selected_ids.append(next_candidate.job.job_id)
            current_cursor = current_cursor.advance(
                selected_slot=selected_slot,
                priority=next_candidate.job.priority,
                source_id=next_candidate.source_id,
            )

        return DispatchWindow(tuple(selected), current_cursor)

    @staticmethod
    def _next_queued_candidate(
        session: Session,
        *,
        priority: DownloadPriority | None,
        source_after: UUID | None,
        excluded_job_ids: Collection[UUID],
        saturated_source_ids: Collection[UUID],
        preferred_series_id: UUID | None = None,
    ) -> QueuedJobCandidate | None:
        statement = (
            select(DownloadJobRow, ChapterSourceRow.source_id, ChapterRow.series_id)
            .join(
                ChapterSourceRow,
                ChapterSourceRow.chapter_source_id == DownloadJobRow.chapter_source_id,
            )
            .join(ChapterRow, ChapterRow.chapter_id == ChapterSourceRow.chapter_id)
            .outerjoin(
                DownloadManifestStateRow,
                DownloadManifestStateRow.job_id == DownloadJobRow.job_id,
            )
            .where(
                DownloadJobRow.status == DownloadJobStatus.QUEUED.value,
                DownloadJobRow.recovery_blocked.is_(False),
                or_(
                    DownloadManifestStateRow.job_id.is_(None),
                    DownloadManifestStateRow.publication_pending.is_(False),
                ),
            )
        )
        if priority is not None:
            statement = statement.where(DownloadJobRow.priority == priority.value)
        if source_after is not None:
            statement = statement.where(ChapterSourceRow.source_id > str(source_after))
        if excluded_job_ids:
            statement = statement.where(
                DownloadJobRow.job_id.not_in(tuple(str(job_id) for job_id in excluded_job_ids))
            )
        if saturated_source_ids:
            statement = statement.where(
                ChapterSourceRow.source_id.not_in(
                    tuple(str(source_id) for source_id in saturated_source_ids)
                )
            )

        order = (
            DownloadJobRepository.queue_order(preferred_series_id)
            if priority is None
            else (
                ChapterSourceRow.source_id.asc(),
                DownloadJobRow.created_at_us.asc(),
                ChapterRow.series_id.asc(),
                func.json_extract(ChapterRow.chapter_sort_key, "$[0]").asc(),
                func.json_extract(ChapterRow.chapter_sort_key, "$[1]").asc(),
                func.json_extract(ChapterRow.chapter_sort_key, "$[2]").asc(),
                DownloadJobRow.job_id.asc(),
            )
        )
        row = session.execute(statement.order_by(*order).limit(1)).one_or_none()
        if row is None:
            return None

        job_row, source_id, series_id = row
        return QueuedJobCandidate(
            job=download_job_from_row(job_row),
            source_id=UUID(source_id),
            series_id=UUID(series_id),
        )

    @staticmethod
    def queue_order(preferred_series_id: UUID | None = None) -> tuple[ColumnElement[Any], ...]:
        """Finish the current manga, then choose a queue and its lowest chapter."""
        queued = DownloadJobRow.status == DownloadJobStatus.QUEUED.value
        priority_rank = case(
            (DownloadJobRow.priority == DownloadPriority.HIGH.value, 0),
            (DownloadJobRow.priority == DownloadPriority.NORMAL.value, 1),
            else_=2,
        )
        return (
            case((ChapterRow.series_id == str(preferred_series_id), 0), else_=1),
            func.min(case((queued, priority_rank))).over(partition_by=ChapterRow.series_id),
            func.min(case((queued, DownloadJobRow.created_at_us))).over(
                partition_by=ChapterRow.series_id
            ),
            ChapterRow.series_id.asc(),
            *(
                func.json_extract(ChapterRow.chapter_sort_key, f"$[{index}]").asc()
                for index in range(3)
            ),
            priority_rank,
            DownloadJobRow.created_at_us.asc(),
            DownloadJobRow.job_id.asc(),
        )

    @staticmethod
    def _bounded_uuid4_collection(
        values: Collection[UUID],
        *,
        field_name: str,
    ) -> tuple[UUID, ...]:
        if len(values) > MAX_DISPATCH_WINDOW_SIZE:
            raise ValueError(f"{field_name} must not exceed 8 IDs")
        unique_ids = set(values)
        if any(type(value) is not UUID or value.version != 4 for value in unique_ids):
            raise ValueError(f"{field_name} must contain only UUIDv4 values")
        return tuple(sorted(unique_ids, key=str))

    def transition_status(
        self,
        session: Session,
        *,
        current_job: DownloadJob,
        target_status: DownloadJobStatus,
        changed_at: datetime,
    ) -> DownloadJob | None:
        session.flush()
        row = session.get(
            DownloadJobRow,
            str(current_job.job_id),
            populate_existing=True,
        )
        if row is None:
            return None

        actual_job = download_job_from_row(row)
        self._assert_current_snapshot(current_job, actual_job)

        assert_transition_allowed(actual_job.status, target_status)
        if target_status is DownloadJobStatus.CANCELLED:
            raise CancellationFinalizationRequired()
        if actual_job.cancel_requested_at is not None and target_status not in {
            DownloadJobStatus.FAILED,
            DownloadJobStatus.INTERRUPTED,
        }:
            raise CancellationRequestPending()
        updated_at = normalise_utc(changed_at, "changed_at")
        if updated_at < actual_job.updated_at:
            raise ValueError("changed_at must not precede current updated_at")

        changed_job = DownloadJob(
            job_id=actual_job.job_id,
            chapter_source_id=actual_job.chapter_source_id,
            status=target_status,
            priority=actual_job.priority,
            created_at=actual_job.created_at,
            updated_at=updated_at,
            cancel_requested_at=actual_job.cancel_requested_at,
            interrupted_from_status=(
                actual_job.status
                if target_status is DownloadJobStatus.INTERRUPTED
                else actual_job.interrupted_from_status
            ),
            recovery_blocked=actual_job.recovery_blocked,
        )
        result = session.connection().execute(
            update(DownloadJobRow)
            .where(
                DownloadJobRow.job_id == str(actual_job.job_id),
                *self._current_snapshot_predicates(actual_job),
            )
            .values(
                status=target_status.value,
                updated_at_us=datetime_to_epoch_us(updated_at),
                interrupted_from_status=(
                    actual_job.status.value
                    if target_status is DownloadJobStatus.INTERRUPTED
                    else (
                        actual_job.interrupted_from_status.value
                        if actual_job.interrupted_from_status is not None
                        else None
                    )
                ),
                recovery_blocked=actual_job.recovery_blocked,
            )
        )
        if result.rowcount != 1:
            raise StaleJobStateError(current_job, None)
        session.expire(
            row,
            attribute_names=[
                "status",
                "updated_at_us",
                "interrupted_from_status",
                "recovery_blocked",
            ],
        )
        return changed_job

    def interrupt_active_job(
        self,
        session: Session,
        *,
        current_job: DownloadJob,
        interrupted_at: datetime,
        recovery_blocked: bool | None = None,
    ) -> DownloadJob | None:
        """Interrupt one active job with a full-snapshot CAS and preserve its stage."""
        session.flush()
        row = session.get(
            DownloadJobRow,
            str(current_job.job_id),
            populate_existing=True,
        )
        if row is None:
            return None

        actual_job = download_job_from_row(row)
        self._assert_current_snapshot(current_job, actual_job)
        if actual_job.status not in _INTERRUPTIBLE_ACTIVE_STATUSES:
            raise IllegalJobTransition(
                actual_job.status,
                DownloadJobStatus.INTERRUPTED,
            )

        interruption_time = normalise_utc(interrupted_at, "interrupted_at")
        # A delayed startup signal must not move the durable job timestamp backwards.
        if interruption_time < actual_job.updated_at:
            interruption_time = actual_job.updated_at
        blocked = actual_job.recovery_blocked if recovery_blocked is None else recovery_blocked
        if type(blocked) is not bool:
            raise TypeError("recovery_blocked must be a boolean or None")

        changed_job = DownloadJob(
            job_id=actual_job.job_id,
            chapter_source_id=actual_job.chapter_source_id,
            status=DownloadJobStatus.INTERRUPTED,
            priority=actual_job.priority,
            created_at=actual_job.created_at,
            updated_at=interruption_time,
            cancel_requested_at=actual_job.cancel_requested_at,
            interrupted_from_status=actual_job.status,
            recovery_blocked=blocked,
        )
        result = session.connection().execute(
            update(DownloadJobRow)
            .where(
                DownloadJobRow.job_id == str(actual_job.job_id),
                *self._current_snapshot_predicates(actual_job),
            )
            .values(
                status=DownloadJobStatus.INTERRUPTED.value,
                updated_at_us=datetime_to_epoch_us(interruption_time),
                interrupted_from_status=actual_job.status.value,
                recovery_blocked=blocked,
            )
        )
        if result.rowcount != 1:
            raise StaleJobStateError(current_job, None)
        session.expire(
            row,
            attribute_names=[
                "status",
                "updated_at_us",
                "interrupted_from_status",
                "recovery_blocked",
            ],
        )
        return changed_job

    def block_interrupted_job(
        self,
        session: Session,
        *,
        current_job: DownloadJob,
        changed_at: datetime,
    ) -> DownloadJob | None:
        """Set the recovery quarantine bit with a full-snapshot CAS."""
        if current_job.status is not DownloadJobStatus.INTERRUPTED:
            raise ValueError("only interrupted jobs can be recovery-blocked")
        if current_job.recovery_blocked:
            return current_job

        session.flush()
        row = session.get(
            DownloadJobRow,
            str(current_job.job_id),
            populate_existing=True,
        )
        if row is None:
            return None
        actual_job = download_job_from_row(row)
        self._assert_current_snapshot(current_job, actual_job)
        if actual_job.status is not DownloadJobStatus.INTERRUPTED:
            raise IllegalJobTransition(
                actual_job.status,
                DownloadJobStatus.INTERRUPTED,
            )
        changed_time = normalise_utc(changed_at, "changed_at")
        if changed_time < actual_job.updated_at:
            changed_time = actual_job.updated_at
        result = session.connection().execute(
            update(DownloadJobRow)
            .where(
                DownloadJobRow.job_id == str(actual_job.job_id),
                *self._current_snapshot_predicates(actual_job),
            )
            .values(
                updated_at_us=datetime_to_epoch_us(changed_time),
                recovery_blocked=True,
            )
        )
        if result.rowcount != 1:
            raise StaleJobStateError(current_job, None)
        session.expire(row, attribute_names=["updated_at_us", "recovery_blocked"])
        return DownloadJob(
            job_id=actual_job.job_id,
            chapter_source_id=actual_job.chapter_source_id,
            status=actual_job.status,
            priority=actual_job.priority,
            created_at=actual_job.created_at,
            updated_at=changed_time,
            cancel_requested_at=actual_job.cancel_requested_at,
            interrupted_from_status=actual_job.interrupted_from_status,
            recovery_blocked=True,
        )

    def resume_interrupted_job_for_safe_retry(
        self,
        session: Session,
        *,
        current_job: DownloadJob,
        resumed_at: datetime,
    ) -> DownloadJob | None:
        """Requeue only an early-stage interrupted job after external verification."""
        if current_job.status is not DownloadJobStatus.INTERRUPTED:
            raise ValueError("current_job must be INTERRUPTED")
        if current_job.cancel_requested_at is not None:
            raise ValueError("a job with pending cancellation cannot be resumed")
        if current_job.interrupted_from_status not in {
            DownloadJobStatus.FETCHING_METADATA,
            DownloadJobStatus.DOWNLOADING,
        }:
            raise ValueError("the interrupted stage is not safe for a full retry")

        session.flush()
        row = session.get(
            DownloadJobRow,
            str(current_job.job_id),
            populate_existing=True,
        )
        if row is None:
            return None
        actual_job = download_job_from_row(row)
        self._assert_current_snapshot(current_job, actual_job)
        if actual_job.status is not DownloadJobStatus.INTERRUPTED:
            raise IllegalJobTransition(actual_job.status, DownloadJobStatus.QUEUED)
        if actual_job.cancel_requested_at is not None:
            raise CancellationRequestPending()

        changed_time = normalise_utc(resumed_at, "resumed_at")
        if changed_time < actual_job.updated_at:
            changed_time = actual_job.updated_at
        result = session.connection().execute(
            update(DownloadJobRow)
            .where(
                DownloadJobRow.job_id == str(actual_job.job_id),
                *self._current_snapshot_predicates(actual_job),
                DownloadJobRow.status == DownloadJobStatus.INTERRUPTED.value,
                DownloadJobRow.cancel_requested_at_us.is_(None),
            )
            .values(
                status=DownloadJobStatus.QUEUED.value,
                updated_at_us=datetime_to_epoch_us(changed_time),
                recovery_blocked=False,
            )
        )
        if result.rowcount != 1:
            raise StaleJobStateError(current_job, None)
        session.expire(
            row,
            attribute_names=["status", "updated_at_us", "recovery_blocked"],
        )
        return DownloadJob(
            job_id=actual_job.job_id,
            chapter_source_id=actual_job.chapter_source_id,
            status=DownloadJobStatus.QUEUED,
            priority=actual_job.priority,
            created_at=actual_job.created_at,
            updated_at=changed_time,
            cancel_requested_at=None,
            interrupted_from_status=actual_job.interrupted_from_status,
            recovery_blocked=False,
        )

    def request_cancellation(
        self,
        session: Session,
        *,
        current_job: DownloadJob,
        requested_at: datetime,
    ) -> DownloadJob | None:
        session.flush()
        row = session.get(
            DownloadJobRow,
            str(current_job.job_id),
            populate_existing=True,
        )
        if row is None:
            return None

        actual_job = download_job_from_row(row)
        self._assert_current_snapshot(current_job, actual_job)
        request_time = normalise_utc(requested_at, "requested_at")

        if actual_job.status is DownloadJobStatus.CANCELLED:
            return actual_job
        if actual_job.status in {
            DownloadJobStatus.COMPLETED,
            DownloadJobStatus.FAILED,
            DownloadJobStatus.INTERRUPTED,
        }:
            raise JobCancellationNotAllowed(actual_job.status)
        if actual_job.cancel_requested_at is not None:
            return actual_job
        if request_time < actual_job.updated_at:
            raise ValueError("requested_at must not precede current updated_at")

        target_status = actual_job.status
        if actual_job.status in {
            DownloadJobStatus.QUEUED,
            DownloadJobStatus.FETCHING_METADATA,
        }:
            target_status = DownloadJobStatus.CANCELLED
            assert_transition_allowed(actual_job.status, target_status)

        request_us = datetime_to_epoch_us(request_time)
        result = session.connection().execute(
            update(DownloadJobRow)
            .where(
                DownloadJobRow.job_id == str(actual_job.job_id),
                *self._current_snapshot_predicates(actual_job),
            )
            .values(
                status=target_status.value,
                updated_at_us=request_us,
                cancel_requested_at_us=request_us,
            )
        )
        if result.rowcount != 1:
            raise StaleJobStateError(current_job, None)
        session.expire(
            row,
            attribute_names=["status", "updated_at_us", "cancel_requested_at_us"],
        )
        return DownloadJob(
            job_id=actual_job.job_id,
            chapter_source_id=actual_job.chapter_source_id,
            status=target_status,
            priority=actual_job.priority,
            created_at=actual_job.created_at,
            updated_at=request_time,
            cancel_requested_at=request_time,
            interrupted_from_status=actual_job.interrupted_from_status,
            recovery_blocked=actual_job.recovery_blocked,
        )

    def complete_cancellation(
        self,
        session: Session,
        *,
        current_job: DownloadJob,
        completed_at: datetime,
        evidence: CancellationSafetyEvidence,
    ) -> DownloadJob | None:
        session.flush()
        row = session.get(
            DownloadJobRow,
            str(current_job.job_id),
            populate_existing=True,
        )
        if row is None:
            return None

        actual_job = download_job_from_row(row)
        self._assert_current_snapshot(current_job, actual_job)
        if actual_job.status is DownloadJobStatus.CANCELLED:
            return actual_job
        if actual_job.cancel_requested_at is None:
            raise JobCancellationNotRequested()
        assert_cancellation_safe(actual_job.status, evidence)
        assert_transition_allowed(actual_job.status, DownloadJobStatus.CANCELLED)

        completed_time = normalise_utc(completed_at, "completed_at")
        if completed_time < actual_job.updated_at:
            raise ValueError("completed_at must not precede current updated_at")
        result = session.connection().execute(
            update(DownloadJobRow)
            .where(
                DownloadJobRow.job_id == str(actual_job.job_id),
                *self._current_snapshot_predicates(actual_job),
            )
            .values(
                status=DownloadJobStatus.CANCELLED.value,
                updated_at_us=datetime_to_epoch_us(completed_time),
            )
        )
        if result.rowcount != 1:
            raise StaleJobStateError(current_job, None)
        session.expire(row, attribute_names=["status", "updated_at_us"])
        return DownloadJob(
            job_id=actual_job.job_id,
            chapter_source_id=actual_job.chapter_source_id,
            status=DownloadJobStatus.CANCELLED,
            priority=actual_job.priority,
            created_at=actual_job.created_at,
            updated_at=completed_time,
            cancel_requested_at=actual_job.cancel_requested_at,
            interrupted_from_status=actual_job.interrupted_from_status,
            recovery_blocked=actual_job.recovery_blocked,
        )

    @staticmethod
    def _assert_current_snapshot(expected: DownloadJob, actual: DownloadJob) -> None:
        if (
            expected.status is not actual.status
            or expected.updated_at != actual.updated_at
            or expected.cancel_requested_at != actual.cancel_requested_at
            or expected.interrupted_from_status is not actual.interrupted_from_status
            or expected.recovery_blocked is not actual.recovery_blocked
        ):
            raise StaleJobStateError(expected, actual)

    @staticmethod
    def _current_snapshot_predicates(
        current_job: DownloadJob,
    ) -> tuple[ColumnElement[bool], ...]:
        return (
            DownloadJobRow.status == current_job.status.value,
            DownloadJobRow.updated_at_us == datetime_to_epoch_us(current_job.updated_at),
            (
                DownloadJobRow.cancel_requested_at_us.is_(None)
                if current_job.cancel_requested_at is None
                else DownloadJobRow.cancel_requested_at_us
                == datetime_to_epoch_us(current_job.cancel_requested_at)
            ),
            (
                DownloadJobRow.interrupted_from_status.is_(None)
                if current_job.interrupted_from_status is None
                else DownloadJobRow.interrupted_from_status
                == current_job.interrupted_from_status.value
            ),
            DownloadJobRow.recovery_blocked == current_job.recovery_blocked,
        )
