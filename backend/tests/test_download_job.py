from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from support.sqlite import open_test_database, reopen_test_database

from app.domain import (
    Chapter,
    ChapterSource,
    DownloadJob,
    DownloadJobStatus,
    DownloadPriority,
    Series,
    Source,
    SourceHealthStatus,
)
from app.domain.job_cancellation import (
    CancellationFinalizationRequired,
    CancellationRequestPending,
    CancellationSafetyEvidence,
    JobCancellationNotAllowed,
    UnsafeJobCancellation,
)
from app.domain.job_fairness import DispatchCursor
from app.domain.job_state_machine import IllegalJobTransition, normal_successor
from app.persistence.base import PersistenceBase
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.download_job import download_job_from_row, download_job_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models import DownloadJobRow
from app.persistence.repositories.download_job import (
    DownloadJobRepository,
    StaleJobStateError,
)
from app.services.job_recovery import RecoveryCoordinator, WorkerQuiescenceAck

NOW = datetime(2026, 9, 26, 14, 15, 22, 123_456, tzinfo=UTC)
NOW_US = datetime_to_epoch_us(NOW)


def _parent_records() -> tuple[Series, Chapter, Source, ChapterSource]:
    series = Series.new_local("Series", "Series", now=NOW)
    chapter = Chapter.new_local(
        series.series_id,
        "12.10",
        "Chapter 12.10",
        chapter_sort_key=(12, 10, 0),
    )
    source = Source.new_local(
        "connector",
        "v1",
        "Connector",
        True,
        SourceHealthStatus.HEALTHY,
        now=NOW,
    )
    chapter_source = ChapterSource.new_local(
        chapter.chapter_id,
        source.source_id,
        remote_id="remote-chapter-12.10",
        available=True,
        last_seen_at=NOW,
    )
    return series, chapter, source, chapter_source


def _persisted_job(
    tmp_path: Path,
) -> tuple[SQLiteDatabase, DownloadJobRepository, DownloadJob]:
    database, repository, jobs = _persisted_jobs(tmp_path, count=1)
    return database, repository, jobs[0]


def _persisted_jobs(
    tmp_path: Path,
    *,
    count: int,
    priorities: tuple[DownloadPriority, ...] | None = None,
) -> tuple[SQLiteDatabase, DownloadJobRepository, tuple[DownloadJob, ...]]:
    if priorities is not None and len(priorities) != count:
        raise ValueError("priorities length must match count")
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    series, chapter, source, chapter_source = _parent_records()
    jobs = tuple(
        DownloadJob.new_local(
            chapter_source.chapter_source_id,
            priority=(priorities[index] if priorities is not None else DownloadPriority.NORMAL),
            now=NOW + timedelta(microseconds=index),
        )
        for index in range(count)
    )
    repository = DownloadJobRepository()
    with database.write_session() as session:
        session.add_all([series_to_row(series), source_to_row(source)])
        session.flush()
        session.add(chapter_to_row(chapter))
        session.flush()
        session.add(chapter_source_to_row(chapter_source))
        session.flush()
        for job in jobs:
            repository.add(session, job)
    return database, repository, jobs


def _persisted_jobs_by_source(
    tmp_path: Path,
    priorities_by_source: tuple[tuple[DownloadPriority, ...], ...],
) -> tuple[
    SQLiteDatabase,
    DownloadJobRepository,
    tuple[DownloadJob, ...],
    dict[UUID, UUID],
]:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    series = Series.new_local("Series", "Series", now=NOW)
    chapter = Chapter.new_local(
        series.series_id,
        "12.10",
        "Chapter 12.10",
        chapter_sort_key=(12, 10, 0),
    )
    sources = tuple(
        Source.new_local(
            f"connector-{index}",
            "v1",
            f"Connector {index}",
            True,
            SourceHealthStatus.HEALTHY,
            now=NOW,
        )
        for index in range(len(priorities_by_source))
    )
    chapter_sources = tuple(
        ChapterSource.new_local(
            chapter.chapter_id,
            source.source_id,
            remote_id=f"remote-chapter-{index}",
            available=True,
            last_seen_at=NOW,
        )
        for index, source in enumerate(sources)
    )
    source_for_chapter_source = {
        chapter_source.chapter_source_id: source.source_id
        for chapter_source, source in zip(chapter_sources, sources, strict=True)
    }
    jobs: list[DownloadJob] = []
    for chapter_source, priorities in zip(
        chapter_sources,
        priorities_by_source,
        strict=True,
    ):
        for priority in priorities:
            jobs.append(
                DownloadJob.new_local(
                    chapter_source.chapter_source_id,
                    priority=priority,
                    now=NOW + timedelta(microseconds=len(jobs)),
                )
            )

    repository = DownloadJobRepository()
    with database.write_session() as session:
        session.add_all([series_to_row(series), *(source_to_row(s) for s in sources)])
        session.flush()
        session.add(chapter_to_row(chapter))
        session.flush()
        session.add_all(chapter_source_to_row(s) for s in chapter_sources)
        session.flush()
        session.add_all(download_job_to_row(job) for job in jobs)

    return database, repository, tuple(jobs), source_for_chapter_source


def _advance_job_to_status(
    database: SQLiteDatabase,
    repository: DownloadJobRepository,
    job: DownloadJob,
    target_status: DownloadJobStatus,
) -> DownloadJob:
    success_route = (
        DownloadJobStatus.QUEUED,
        DownloadJobStatus.FETCHING_METADATA,
        DownloadJobStatus.DOWNLOADING,
        DownloadJobStatus.VALIDATING,
        DownloadJobStatus.PROCESSING,
        DownloadJobStatus.PACKAGING,
        DownloadJobStatus.VERIFYING,
        DownloadJobStatus.INDEXING,
        DownloadJobStatus.COMPLETED,
    )
    current_index = success_route.index(job.status)
    target_index = success_route.index(target_status)
    if target_index < current_index:
        raise ValueError("target_status precedes job status")

    current = job
    for offset, next_status in enumerate(
        success_route[current_index + 1 : target_index + 1],
        start=1,
    ):
        changed_at = current.updated_at + timedelta(seconds=offset)
        with database.write_session() as session:
            updated = repository.transition_status(
                session,
                current_job=current,
                target_status=next_status,
                changed_at=changed_at,
            )
        if updated is None:
            raise AssertionError("job disappeared while advancing the state")
        current = updated
    return current


def _safe_cancellation_evidence(status: DownloadJobStatus) -> CancellationSafetyEvidence:
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
            raise ValueError("status has no deferred cancellation evidence")


def _insert_job(
    session: Session,
    *,
    job_id: str,
    chapter_source_id: str,
    status: str,
    priority: str,
    created_at_us: int | float,
    updated_at_us: int | float,
) -> None:
    session.execute(
        text(
            "INSERT INTO download_jobs (job_id, chapter_source_id, status, priority, "
            "created_at_us, updated_at_us) VALUES (:job_id, :chapter_source_id, :status, "
            ":priority, :created_at_us, :updated_at_us)"
        ),
        {
            "job_id": job_id,
            "chapter_source_id": chapter_source_id,
            "status": status,
            "priority": priority,
            "created_at_us": created_at_us,
            "updated_at_us": updated_at_us,
        },
    )


def test_download_job_domain_uses_approved_states_priorities_and_utc_times() -> None:
    local_time = datetime(
        2026,
        9,
        26,
        10,
        15,
        22,
        123_456,
        tzinfo=timezone(timedelta(hours=-4)),
    )
    _, _, _, chapter_source = _parent_records()
    job = DownloadJob.new_local(chapter_source.chapter_source_id, now=local_time)

    assert job.job_id.version == 4
    assert job.chapter_source_id == chapter_source.chapter_source_id
    assert job.status is DownloadJobStatus.QUEUED
    assert job.priority is DownloadPriority.NORMAL
    assert job.created_at == local_time.astimezone(UTC)
    assert job.updated_at == local_time.astimezone(UTC)
    assert {status.value for status in DownloadJobStatus} == {
        "QUEUED",
        "FETCHING_METADATA",
        "DOWNLOADING",
        "VALIDATING",
        "PROCESSING",
        "PACKAGING",
        "VERIFYING",
        "INDEXING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "INTERRUPTED",
    }
    assert {priority.value for priority in DownloadPriority} == {"HIGH", "NORMAL", "LOW"}


def test_download_job_domain_rejects_invalid_identity_state_priority_and_times() -> None:
    _, _, _, chapter_source = _parent_records()
    legacy_id = UUID("123e4567-e89b-12d3-a456-426614174000")

    with pytest.raises(ValueError, match="job_id must be a UUIDv4"):
        DownloadJob(
            legacy_id,
            chapter_source.chapter_source_id,
            DownloadJobStatus.QUEUED,
            DownloadPriority.NORMAL,
            NOW,
            NOW,
        )

    with pytest.raises(ValueError, match="status must be a supported DownloadJobStatus"):
        DownloadJob(
            uuid4(),
            chapter_source.chapter_source_id,
            cast(DownloadJobStatus, "UNKNOWN"),
            DownloadPriority.NORMAL,
            NOW,
            NOW,
        )

    with pytest.raises(ValueError, match="priority must be a supported DownloadPriority"):
        DownloadJob.new_local(
            chapter_source.chapter_source_id,
            priority=cast(DownloadPriority, "URGENT"),
            now=NOW,
        )

    with pytest.raises(ValueError, match="timezone information"):
        DownloadJob.new_local(chapter_source.chapter_source_id, now=datetime(2026, 9, 26))

    with pytest.raises(ValueError, match="updated_at must not precede created_at"):
        DownloadJob(
            uuid4(),
            chapter_source.chapter_source_id,
            DownloadJobStatus.QUEUED,
            DownloadPriority.NORMAL,
            NOW,
            NOW - timedelta(microseconds=1),
        )


def test_download_job_mapper_round_trips_all_persisted_fields() -> None:
    _, _, _, chapter_source = _parent_records()
    job = DownloadJob(
        uuid4(),
        chapter_source.chapter_source_id,
        DownloadJobStatus.INTERRUPTED,
        DownloadPriority.HIGH,
        NOW,
        NOW,
        cancel_requested_at=NOW,
        interrupted_from_status=DownloadJobStatus.DOWNLOADING,
    )

    row = download_job_to_row(job)

    assert download_job_from_row(row) == job
    assert row.created_at_us == NOW_US
    assert row.updated_at_us == NOW_US
    assert row.cancel_requested_at_us == NOW_US
    assert row.interrupted_from_status == DownloadJobStatus.DOWNLOADING.value


def test_download_job_schema_and_migration_preserve_exact_source_version(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    try:
        report = runner.upgrade_to_head()
        series, chapter, source, chapter_source = _parent_records()
        job = DownloadJob.new_local(chapter_source.chapter_source_id, now=NOW)
        with database.write_session() as session:
            session.add_all([series_to_row(series), source_to_row(source)])
            session.flush()
            session.add(chapter_to_row(chapter))
            session.flush()
            session.add(chapter_source_to_row(chapter_source))
            session.flush()
            session.add(download_job_to_row(job))

        with database.read_session() as session:
            row = session.get(DownloadJobRow, str(job.job_id))
            parent_ids = session.execute(
                text(
                    "SELECT chapters.chapter_id, chapter_sources.source_id "
                    "FROM download_jobs JOIN chapter_sources USING (chapter_source_id) "
                    "JOIN chapters USING (chapter_id) WHERE download_jobs.job_id = :job_id"
                ),
                {"job_id": str(job.job_id)},
            ).one()
            index_names = {
                index_row[1]
                for index_row in session.connection()
                .exec_driver_sql("PRAGMA index_list('download_jobs')")
                .all()
            }
            foreign_key_errors = (
                session.connection().exec_driver_sql("PRAGMA foreign_key_check").all()
            )
            download_job_columns = {
                column[1]
                for column in session.connection()
                .exec_driver_sql("PRAGMA table_info('download_jobs')")
                .all()
            }
            manifest_state_table_exists = session.execute(
                text(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'download_manifest_state'"
                )
            ).scalar_one_or_none()
            manifest_state_rows = session.execute(
                text("SELECT count(*) FROM download_manifest_state WHERE job_id = :job_id"),
                {"job_id": str(job.job_id)},
            ).scalar_one()
            checkpoint_fields = session.execute(
                text(
                    "SELECT interrupted_from_status, recovery_blocked "
                    "FROM download_jobs WHERE job_id = :job_id"
                ),
                {"job_id": str(job.job_id)},
            ).one()

        assert report.current_revision == "0024_reader_sessions"
        assert row is not None
        assert download_job_from_row(row) == job
        assert checkpoint_fields == (None, 0)
        assert {
            "interrupted_from_status",
            "recovery_blocked",
        } <= download_job_columns
        assert manifest_state_table_exists == 1
        assert manifest_state_rows == 0
        assert tuple(parent_ids) == (str(chapter.chapter_id), str(source.source_id))
        assert {
            "ix_download_jobs_chapter_source_id",
            "ix_download_jobs_dispatch_source",
            "ix_download_jobs_status",
        } <= index_names
        assert foreign_key_errors == []
        assert {table.name for table in PersistenceBase.metadata.sorted_tables} >= {
            "download_jobs",
            "chapter_sources",
        }

        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text(
                        "DELETE FROM chapter_sources WHERE chapter_source_id = :chapter_source_id"
                    ),
                    {"chapter_source_id": str(chapter_source.chapter_source_id)},
                )
    finally:
        database.close()


def test_download_job_schema_rejects_invalid_values_and_orphan_source(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    try:
        runner.upgrade_to_head()
        series, chapter, source, chapter_source = _parent_records()
        with database.write_session() as session:
            session.add_all([series_to_row(series), source_to_row(source)])
            session.flush()
            session.add(chapter_to_row(chapter))
            session.flush()
            session.add(chapter_source_to_row(chapter_source))

        invalid_rows: tuple[tuple[str, str, str, str, int | float, int | float], ...] = (
            (
                "not-a-uuid",
                str(chapter_source.chapter_source_id),
                "QUEUED",
                "NORMAL",
                NOW_US,
                NOW_US,
            ),
            (
                "123e4567-e89b-12d3-a456-426614174000",
                str(chapter_source.chapter_source_id),
                "QUEUED",
                "NORMAL",
                NOW_US,
                NOW_US,
            ),
            (
                str(uuid4()).upper(),
                str(chapter_source.chapter_source_id),
                "QUEUED",
                "NORMAL",
                NOW_US,
                NOW_US,
            ),
            (
                str(uuid4()),
                str(chapter_source.chapter_source_id),
                "UNKNOWN",
                "NORMAL",
                NOW_US,
                NOW_US,
            ),
            (
                str(uuid4()),
                str(chapter_source.chapter_source_id),
                "QUEUED",
                "URGENT",
                NOW_US,
                NOW_US,
            ),
            (
                str(uuid4()),
                str(chapter_source.chapter_source_id),
                "QUEUED",
                "NORMAL",
                1.5,
                NOW_US,
            ),
            (
                str(uuid4()),
                str(chapter_source.chapter_source_id),
                "QUEUED",
                "NORMAL",
                NOW_US,
                1.5,
            ),
            (str(uuid4()), str(uuid4()), "QUEUED", "NORMAL", NOW_US, NOW_US),
        )
        for (
            job_id,
            chapter_source_id,
            status,
            priority,
            created_at_us,
            updated_at_us,
        ) in invalid_rows:
            with pytest.raises(IntegrityError):
                with database.write_session() as session:
                    _insert_job(
                        session,
                        job_id=job_id,
                        chapter_source_id=chapter_source_id,
                        status=status,
                        priority=priority,
                        created_at_us=created_at_us,
                        updated_at_us=updated_at_us,
                    )
    finally:
        database.close()


@pytest.mark.parametrize("cancel_requested_at_us", [1.5, "not-an-integer", NOW_US + 1])
def test_download_job_schema_rejects_invalid_cancellation_timestamp(
    tmp_path: Path,
    cancel_requested_at_us: object,
) -> None:
    database, _, job = _persisted_job(tmp_path)
    try:
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text(
                        "UPDATE download_jobs SET cancel_requested_at_us = :requested_at "
                        "WHERE job_id = :job_id"
                    ),
                    {
                        "requested_at": cancel_requested_at_us,
                        "job_id": str(job.job_id),
                    },
                )
    finally:
        database.close()


def test_download_job_repository_persists_atomic_status_transition(tmp_path: Path) -> None:
    database, repository, original = _persisted_job(tmp_path)
    changed_at = NOW + timedelta(seconds=1)
    try:
        with database.read_session() as session:
            assert repository.get(session, original.job_id) == original

        with database.write_session() as session:
            updated = repository.transition_status(
                session,
                current_job=original,
                target_status=DownloadJobStatus.FETCHING_METADATA,
                changed_at=changed_at,
            )

        expected = DownloadJob(
            job_id=original.job_id,
            chapter_source_id=original.chapter_source_id,
            status=DownloadJobStatus.FETCHING_METADATA,
            priority=original.priority,
            created_at=original.created_at,
            updated_at=changed_at,
        )
        assert updated == expected
        with database.read_session() as session:
            assert repository.get(session, original.job_id) == expected
    finally:
        database.close()


def test_download_job_repository_rejects_illegal_transition_without_mutation(
    tmp_path: Path,
) -> None:
    database, repository, original = _persisted_job(tmp_path)
    try:
        with database.write_session() as session:
            with pytest.raises(IllegalJobTransition):
                repository.transition_status(
                    session,
                    current_job=original,
                    target_status=DownloadJobStatus.COMPLETED,
                    changed_at=NOW + timedelta(seconds=1),
                )

        with database.read_session() as session:
            assert repository.get(session, original.job_id) == original
    finally:
        database.close()


def test_download_job_repository_rejects_stale_snapshot_without_overwrite(
    tmp_path: Path,
) -> None:
    database, repository, original = _persisted_job(tmp_path)
    changed_at = NOW + timedelta(seconds=1)
    try:
        with database.write_session() as session:
            current = repository.transition_status(
                session,
                current_job=original,
                target_status=DownloadJobStatus.FETCHING_METADATA,
                changed_at=changed_at,
            )
        assert current is not None

        with database.write_session() as session:
            with pytest.raises(StaleJobStateError) as error:
                repository.transition_status(
                    session,
                    current_job=original,
                    target_status=DownloadJobStatus.FAILED,
                    changed_at=changed_at + timedelta(seconds=1),
                )
        assert error.value.job_id == original.job_id
        assert error.value.expected_status is DownloadJobStatus.QUEUED
        assert error.value.actual_status is DownloadJobStatus.FETCHING_METADATA

        with database.read_session() as session:
            assert repository.get(session, original.job_id) == current
    finally:
        database.close()


def test_download_job_repository_returns_none_for_missing_job(tmp_path: Path) -> None:
    database, repository, original = _persisted_job(tmp_path)
    missing = DownloadJob.new_local(
        original.chapter_source_id,
        job_id=uuid4(),
        now=NOW,
    )
    try:
        with database.read_session() as session:
            assert repository.get(session, missing.job_id) is None
        with database.write_session() as session:
            assert (
                repository.transition_status(
                    session,
                    current_job=missing,
                    target_status=DownloadJobStatus.FETCHING_METADATA,
                    changed_at=NOW + timedelta(seconds=1),
                )
                is None
            )
    finally:
        database.close()


def test_download_job_repository_respects_transaction_rollback(tmp_path: Path) -> None:
    database, repository, original = _persisted_job(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="rollback marker"):
            with database.write_session() as session:
                repository.transition_status(
                    session,
                    current_job=original,
                    target_status=DownloadJobStatus.FETCHING_METADATA,
                    changed_at=NOW + timedelta(seconds=1),
                )
                raise RuntimeError("rollback marker")

        with database.read_session() as session:
            assert repository.get(session, original.job_id) == original
    finally:
        database.close()


def test_download_job_repository_rejects_timestamp_regression(tmp_path: Path) -> None:
    database, repository, original = _persisted_job(tmp_path)
    try:
        with database.write_session() as session:
            with pytest.raises(ValueError, match="must not precede current updated_at"):
                repository.transition_status(
                    session,
                    current_job=original,
                    target_status=DownloadJobStatus.FETCHING_METADATA,
                    changed_at=NOW - timedelta(microseconds=1),
                )

        with database.read_session() as session:
            assert repository.get(session, original.job_id) == original
    finally:
        database.close()


def test_download_job_repository_returns_bounded_fifo_queued_batch(tmp_path: Path) -> None:
    database, repository, jobs = _persisted_jobs(tmp_path, count=12)
    try:
        with database.write_session() as session:
            updated = repository.transition_status(
                session,
                current_job=jobs[0],
                target_status=DownloadJobStatus.FETCHING_METADATA,
                changed_at=NOW + timedelta(seconds=1),
            )
        assert updated is not None

        expected = tuple(
            sorted(
                jobs[1:],
                key=lambda job: (job.created_at, str(job.job_id)),
            )
        )
        with database.read_session() as session:
            dispatch_window = repository.list_queued_for_dispatch(session, limit=8)
            smaller_window = repository.list_queued_for_dispatch(session, limit=3)

        batch = tuple(candidate.job for candidate in dispatch_window.candidates)
        smaller_batch = tuple(candidate.job for candidate in smaller_window.candidates)

        assert batch == expected[:8]
        assert len(batch) == 8
        assert smaller_batch == expected[:3]
        assert all(job.status is DownloadJobStatus.QUEUED for job in batch)
    finally:
        database.close()


def test_dispatch_skips_recovery_blocked_and_publication_pending_jobs(
    tmp_path: Path,
) -> None:
    from app.domain.download_manifest_state import DownloadManifestState
    from app.persistence.mappers.download_manifest_state import (
        download_manifest_state_to_row,
    )

    database, repository, jobs = _persisted_jobs(tmp_path, count=3)
    healthy, recovery_blocked, publication_pending = jobs
    pending_state = DownloadManifestState(
        job_id=publication_pending.job_id,
        generation=1,
        manifest_sha256="a" * 64,
        manifest_size_bytes=1,
        previous_sha256=None,
        publication_pending=True,
    )
    try:
        with database.write_session() as session:
            session.execute(
                text("UPDATE download_jobs SET recovery_blocked = 1 WHERE job_id = :job_id"),
                {"job_id": str(recovery_blocked.job_id)},
            )
            session.add(download_manifest_state_to_row(pending_state))

        with database.read_session() as session:
            window = repository.list_queued_for_dispatch(session, limit=8)

        assert tuple(candidate.job.job_id for candidate in window.candidates) == (healthy.job_id,)
    finally:
        database.close()


@pytest.mark.parametrize("limit", [0, -1, 9, True, 1.5])
def test_download_job_repository_rejects_dispatch_batch_outside_bounds(
    tmp_path: Path,
    limit: object,
) -> None:
    database, repository, _ = _persisted_job(tmp_path)
    try:
        with database.read_session() as session:
            with pytest.raises(ValueError, match="between 1 and 8"):
                repository.list_queued_for_dispatch(session, limit=cast(int, limit))
    finally:
        database.close()


def test_download_job_repository_orders_priority_then_fifo(tmp_path: Path) -> None:
    priorities = (
        DownloadPriority.LOW,
        DownloadPriority.NORMAL,
        DownloadPriority.HIGH,
        DownloadPriority.NORMAL,
        DownloadPriority.HIGH,
        DownloadPriority.LOW,
    )
    database, repository, jobs = _persisted_jobs(
        tmp_path,
        count=len(priorities),
        priorities=priorities,
    )
    rank = {
        DownloadPriority.HIGH: 0,
        DownloadPriority.NORMAL: 1,
        DownloadPriority.LOW: 2,
    }
    expected = tuple(
        sorted(
            jobs,
            key=lambda job: (rank[job.priority], job.created_at, str(job.job_id)),
        )
    )
    try:
        with database.read_session() as session:
            dispatch_window = repository.list_queued_for_dispatch(session, limit=8)
        ordered = tuple(candidate.job for candidate in dispatch_window.candidates)

        assert ordered == expected
        assert [job.priority for job in ordered] == [
            DownloadPriority.HIGH,
            DownloadPriority.HIGH,
            DownloadPriority.NORMAL,
            DownloadPriority.NORMAL,
            DownloadPriority.LOW,
            DownloadPriority.LOW,
        ]
    finally:
        database.close()


def test_download_job_repository_rotates_sources_within_priority(tmp_path: Path) -> None:
    database, repository, _jobs, source_by_chapter_source = _persisted_jobs_by_source(
        tmp_path,
        (
            (DownloadPriority.NORMAL,) * 500,
            (DownloadPriority.NORMAL,) * 2,
        ),
    )
    source_ids = set(source_by_chapter_source.values())
    assert len(source_ids) == 2
    try:
        with database.read_session() as session:
            batch = repository.list_queued_for_dispatch(session, limit=8)

        source_counts = {source_id: 0 for source_id in source_ids}
        for candidate in batch.candidates:
            assert source_by_chapter_source[candidate.job.chapter_source_id] == candidate.source_id
            source_counts[candidate.source_id] += 1

        assert sorted(source_counts.values()) == [2, 6]
    finally:
        database.close()


def test_dispatch_window_skips_saturated_sources_and_host_blocked_jobs(
    tmp_path: Path,
) -> None:
    database, repository, _, source_by_chapter_source = _persisted_jobs_by_source(
        tmp_path,
        (
            (DownloadPriority.NORMAL,) * 3,
            (DownloadPriority.NORMAL,) * 3,
        ),
    )
    source_ids = sorted(set(source_by_chapter_source.values()), key=str)
    saturated_source_id = source_ids[0]
    try:
        with database.read_session() as session:
            source_filtered = repository.list_queued_for_dispatch(
                session,
                limit=8,
                saturated_source_ids=(saturated_source_id,),
            )
            assert source_filtered.candidates
            host_blocked = repository.list_queued_for_dispatch(
                session,
                limit=1,
                excluded_job_ids=(source_filtered.candidates[0].job.job_id,),
            )

        assert all(
            candidate.source_id != saturated_source_id for candidate in source_filtered.candidates
        )
        assert len(host_blocked.candidates) == 1
        assert host_blocked.candidates[0].job.job_id != source_filtered.candidates[0].job.job_id
    finally:
        database.close()


def test_dispatch_window_rejects_unbounded_exclusion_input(tmp_path: Path) -> None:
    database, repository, _ = _persisted_jobs(tmp_path, count=1)
    try:
        with database.read_session() as session:
            with pytest.raises(ValueError, match="must not exceed 8 IDs"):
                repository.list_queued_for_dispatch(
                    session,
                    limit=8,
                    excluded_job_ids=tuple(uuid4() for _ in range(9)),
                )
    finally:
        database.close()


def test_download_job_repository_reserves_priority_slots_across_bounded_windows(
    tmp_path: Path,
) -> None:
    database, repository, _jobs, _ = _persisted_jobs_by_source(
        tmp_path,
        (
            (DownloadPriority.HIGH,) * 40
            + (DownloadPriority.NORMAL,) * 30
            + (DownloadPriority.LOW,) * 10,
        ),
    )
    dispatched: list[DownloadJob] = []
    cursor = DispatchCursor()
    try:
        for window_number in range(7):
            with database.read_session() as session:
                dispatch_window = repository.list_queued_for_dispatch(
                    session,
                    limit=8,
                    cursor=cursor,
                )
            window = tuple(candidate.job for candidate in dispatch_window.candidates)
            cursor = dispatch_window.cursor
            dispatched.extend(window)
            with database.write_session() as session:
                for offset, job in enumerate(window):
                    assert (
                        repository.transition_status(
                            session,
                            current_job=job,
                            target_status=DownloadJobStatus.FETCHING_METADATA,
                            changed_at=NOW
                            + timedelta(seconds=window_number + 1, microseconds=offset),
                        )
                        is not None
                    )

        counts = {priority: 0 for priority in DownloadPriority}
        for job in dispatched:
            counts[job.priority] += 1

        assert counts == {
            DownloadPriority.HIGH: 28,
            DownloadPriority.NORMAL: 21,
            DownloadPriority.LOW: 7,
        }
    finally:
        database.close()


def test_dispatch_cursor_prevents_starvation_with_one_available_slot(tmp_path: Path) -> None:
    database, repository, _, _ = _persisted_jobs_by_source(
        tmp_path,
        (
            (DownloadPriority.HIGH,) * 30
            + (DownloadPriority.NORMAL,) * 30
            + (DownloadPriority.LOW,) * 30,
        ),
    )
    cursor = DispatchCursor()
    dispatched_priorities: list[DownloadPriority] = []
    try:
        for dispatch_number in range(16):
            with database.read_session() as session:
                dispatch_window = repository.list_queued_for_dispatch(
                    session,
                    limit=1,
                    cursor=cursor,
                )
            assert len(dispatch_window.candidates) == 1
            candidate = dispatch_window.candidates[0]
            cursor = dispatch_window.cursor
            dispatched_priorities.append(candidate.job.priority)
            with database.write_session() as session:
                assert (
                    repository.transition_status(
                        session,
                        current_job=candidate.job,
                        target_status=DownloadJobStatus.FETCHING_METADATA,
                        changed_at=NOW + timedelta(seconds=dispatch_number + 1),
                    )
                    is not None
                )

        assert dispatched_priorities.count(DownloadPriority.HIGH) == 8
        assert dispatched_priorities.count(DownloadPriority.NORMAL) == 6
        assert dispatched_priorities.count(DownloadPriority.LOW) == 2
    finally:
        database.close()


def test_download_job_repository_cancels_queued_job_and_persists_intent(
    tmp_path: Path,
) -> None:
    database, repository, original = _persisted_job(tmp_path)
    requested_at = NOW + timedelta(seconds=1)
    try:
        with database.write_session() as session:
            cancelled = repository.request_cancellation(
                session,
                current_job=original,
                requested_at=requested_at,
            )

        expected = DownloadJob(
            job_id=original.job_id,
            chapter_source_id=original.chapter_source_id,
            status=DownloadJobStatus.CANCELLED,
            priority=original.priority,
            created_at=original.created_at,
            updated_at=requested_at,
            cancel_requested_at=requested_at,
        )
        assert cancelled == expected
        with database.read_session() as session:
            assert repository.get(session, original.job_id) == expected
    finally:
        database.close()


def test_download_job_repository_cancels_metadata_fetch_immediately(
    tmp_path: Path,
) -> None:
    database, repository, original = _persisted_job(tmp_path)
    fetching = _advance_job_to_status(
        database,
        repository,
        original,
        DownloadJobStatus.FETCHING_METADATA,
    )
    requested_at = fetching.updated_at + timedelta(seconds=1)
    try:
        with database.write_session() as session:
            cancelled = repository.request_cancellation(
                session,
                current_job=fetching,
                requested_at=requested_at,
            )

        assert cancelled is not None
        assert cancelled.status is DownloadJobStatus.CANCELLED
        assert cancelled.cancel_requested_at == requested_at
        assert cancelled.updated_at == requested_at
    finally:
        database.close()


@pytest.mark.parametrize(
    "status",
    [
        DownloadJobStatus.DOWNLOADING,
        DownloadJobStatus.VALIDATING,
        DownloadJobStatus.PROCESSING,
        DownloadJobStatus.PACKAGING,
        DownloadJobStatus.VERIFYING,
        DownloadJobStatus.INDEXING,
    ],
)
def test_download_job_repository_requires_stage_specific_safe_point_to_cancel(
    tmp_path: Path,
    status: DownloadJobStatus,
) -> None:
    database, repository, original = _persisted_job(tmp_path)
    staged_job = _advance_job_to_status(database, repository, original, status)
    requested_at = staged_job.updated_at + timedelta(seconds=1)
    try:
        with database.write_session() as session:
            pending = repository.request_cancellation(
                session,
                current_job=staged_job,
                requested_at=requested_at,
            )

        assert pending is not None
        assert pending.status is status
        assert pending.cancel_requested_at == requested_at
        assert pending.updated_at == requested_at
        successor = normal_successor(status)
        assert successor is not None
        with database.write_session() as session:
            with pytest.raises(CancellationRequestPending):
                repository.transition_status(
                    session,
                    current_job=pending,
                    target_status=successor,
                    changed_at=requested_at + timedelta(seconds=1),
                )

        with database.write_session() as session:
            with pytest.raises(UnsafeJobCancellation):
                repository.complete_cancellation(
                    session,
                    current_job=pending,
                    completed_at=requested_at + timedelta(seconds=1),
                    evidence=CancellationSafetyEvidence(),
                )

        with database.write_session() as session:
            cancelled = repository.complete_cancellation(
                session,
                current_job=pending,
                completed_at=requested_at + timedelta(seconds=2),
                evidence=_safe_cancellation_evidence(status),
            )

        expected = DownloadJob(
            job_id=pending.job_id,
            chapter_source_id=pending.chapter_source_id,
            status=DownloadJobStatus.CANCELLED,
            priority=pending.priority,
            created_at=pending.created_at,
            updated_at=requested_at + timedelta(seconds=2),
            cancel_requested_at=requested_at,
        )
        assert cancelled == expected
        with database.read_session() as session:
            assert repository.get(session, pending.job_id) == expected
    finally:
        database.close()


def test_job_cancellation_request_is_idempotent_and_survives_interruption(
    tmp_path: Path,
) -> None:
    database, repository, original = _persisted_job(tmp_path)
    downloading = _advance_job_to_status(
        database,
        repository,
        original,
        DownloadJobStatus.DOWNLOADING,
    )
    requested_at = downloading.updated_at + timedelta(seconds=1)
    try:
        with database.write_session() as session:
            pending = repository.request_cancellation(
                session,
                current_job=downloading,
                requested_at=requested_at,
            )
        assert pending is not None

        with database.write_session() as session:
            repeated = repository.request_cancellation(
                session,
                current_job=pending,
                requested_at=requested_at + timedelta(seconds=5),
            )
        assert repeated == pending

        with database.write_session() as session:
            interrupted = repository.transition_status(
                session,
                current_job=pending,
                target_status=DownloadJobStatus.INTERRUPTED,
                changed_at=requested_at + timedelta(seconds=1),
            )
        assert interrupted is not None
        assert interrupted.status is DownloadJobStatus.INTERRUPTED
        assert interrupted.cancel_requested_at == requested_at
        assert interrupted.interrupted_from_status == DownloadJobStatus.DOWNLOADING
        database.close()

        reopened = reopen_test_database(database)
        try:
            with reopened.read_session() as session:
                recovered_snapshot = repository.get(session, original.job_id)
            assert recovered_snapshot == interrupted
            assert recovered_snapshot is not None
            with reopened.write_session() as session:
                with pytest.raises(JobCancellationNotAllowed):
                    repository.request_cancellation(
                        session,
                        current_job=recovered_snapshot,
                        requested_at=requested_at + timedelta(seconds=2),
                    )
        finally:
            reopened.close()
    finally:
        database.close()


@pytest.mark.parametrize(
    "status",
    [
        DownloadJobStatus.FETCHING_METADATA,
        DownloadJobStatus.DOWNLOADING,
        DownloadJobStatus.VALIDATING,
        DownloadJobStatus.PROCESSING,
        DownloadJobStatus.PACKAGING,
        DownloadJobStatus.VERIFYING,
        DownloadJobStatus.INDEXING,
    ],
)
def test_active_to_interrupted_transition_captures_its_stage_atomically(
    tmp_path: Path,
    status: DownloadJobStatus,
) -> None:
    database, repository, original = _persisted_job(tmp_path)
    active = _advance_job_to_status(database, repository, original, status)
    coordinator = RecoveryCoordinator(database, database.data_root_identity)
    try:
        ack = WorkerQuiescenceAck(job_id=active.job_id)
        coordinator.interrupt_quiesced_job(
            active.job_id,
            active,
            quiescence_ack=ack,
        )
        with database.read_session() as session:
            interrupted = repository.get(session, active.job_id)
        assert interrupted is not None
        assert interrupted.status is DownloadJobStatus.INTERRUPTED
        assert interrupted.interrupted_from_status is status
        assert interrupted.updated_at >= active.updated_at
    finally:
        database.close()


def test_interruption_checkpoint_is_persisted_and_stale_snapshot_cannot_replace_it(
    tmp_path: Path,
) -> None:
    database, repository, original = _persisted_job(tmp_path)
    active = _advance_job_to_status(
        database,
        repository,
        original,
        DownloadJobStatus.DOWNLOADING,
    )
    coordinator = RecoveryCoordinator(database, database.data_root_identity)
    try:
        ack = WorkerQuiescenceAck(job_id=active.job_id)
        coordinator.interrupt_quiesced_job(
            active.job_id,
            active,
            quiescence_ack=ack,
        )
        with database.read_session() as session:
            interrupted = repository.get(session, active.job_id)
        assert interrupted is not None
        assert interrupted.status is DownloadJobStatus.INTERRUPTED
        assert interrupted.interrupted_from_status == DownloadJobStatus.DOWNLOADING

        with database.write_session() as session:
            with pytest.raises(StaleJobStateError):
                repository.transition_status(
                    session,
                    current_job=active,
                    target_status=DownloadJobStatus.INTERRUPTED,
                    changed_at=active.updated_at + timedelta(seconds=2),
                )

        # A concurrent change to only the captured origin must also invalidate
        # the complete snapshot used by the INTERRUPTED -> FAILED CAS.
        with database.write_session() as session:
            session.execute(
                text(
                    "UPDATE download_jobs SET interrupted_from_status = :origin "
                    "WHERE job_id = :job_id"
                ),
                {
                    "origin": DownloadJobStatus.PACKAGING.value,
                    "job_id": str(interrupted.job_id),
                },
            )
        with database.write_session() as session:
            with pytest.raises(StaleJobStateError):
                repository.transition_status(
                    session,
                    current_job=interrupted,
                    target_status=DownloadJobStatus.FAILED,
                    changed_at=interrupted.updated_at + timedelta(seconds=1),
                )
        with database.read_session() as session:
            current_interrupted = repository.get(session, interrupted.job_id)
        assert current_interrupted is not None
        assert current_interrupted.interrupted_from_status is DownloadJobStatus.PACKAGING

        # A subsequent terminal transition preserves the recorded origin.
        with database.write_session() as session:
            failed = repository.transition_status(
                session,
                current_job=current_interrupted,
                target_status=DownloadJobStatus.FAILED,
                changed_at=current_interrupted.updated_at + timedelta(seconds=1),
            )
        assert failed is not None
        assert failed.interrupted_from_status == DownloadJobStatus.PACKAGING
        database.close()

        reopened = reopen_test_database(database)
        try:
            with reopened.read_session() as session:
                persisted = repository.get(session, original.job_id)
            assert persisted == failed
            assert persisted is not None
            assert persisted.interrupted_from_status == DownloadJobStatus.PACKAGING
        finally:
            reopened.close()
    finally:
        database.close()


def test_quiesced_interruption_rejects_missing_or_wrong_ack_without_mutation(
    tmp_path: Path,
) -> None:
    database, repository, original = _persisted_job(tmp_path)
    active = _advance_job_to_status(
        database,
        repository,
        original,
        DownloadJobStatus.DOWNLOADING,
    )
    coordinator = RecoveryCoordinator(database, database.data_root_identity)
    try:
        with pytest.raises(TypeError):
            coordinator.interrupt_quiesced_job(  # pyright: ignore[reportCallIssue]
                active.job_id,
                active,
            )
        with database.read_session() as session:
            assert repository.get(session, active.job_id) == active

        with pytest.raises(TypeError):
            coordinator.interrupt_quiesced_job(
                active.job_id,
                active,
                quiescence_ack=cast(WorkerQuiescenceAck, object()),
            )
        with database.read_session() as session:
            assert repository.get(session, active.job_id) == active

        with pytest.raises(ValueError):
            coordinator.interrupt_quiesced_job(
                active.job_id,
                active,
                quiescence_ack=WorkerQuiescenceAck(job_id=uuid4()),
            )
        with database.read_session() as session:
            assert repository.get(session, active.job_id) == active
    finally:
        database.close()


def test_transition_status_cannot_cancel_without_safe_point_evidence(tmp_path: Path) -> None:
    database, repository, original = _persisted_job(tmp_path)
    try:
        with database.write_session() as session:
            with pytest.raises(CancellationFinalizationRequired):
                repository.transition_status(
                    session,
                    current_job=original,
                    target_status=DownloadJobStatus.CANCELLED,
                    changed_at=NOW + timedelta(seconds=1),
                )
        with database.read_session() as session:
            assert repository.get(session, original.job_id) == original
    finally:
        database.close()
