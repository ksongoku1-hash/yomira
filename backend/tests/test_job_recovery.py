import hashlib
import os
import sqlite3
import subprocess
import sys
import textwrap
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from support.sqlite import open_test_database, reopen_test_database

from app.domain import (
    Chapter,
    ChapterSource,
    DownloadJob,
    DownloadJobStatus,
    DownloadPage,
    Series,
    Source,
    SourceHealthStatus,
)
from app.domain.download_manifest_state import DownloadManifestState
from app.domain.job_manifest import DownloadJobManifest, manifest_to_bytes
from app.errors import ErrorCode
from app.persistence.database import SQLiteDatabase
from app.persistence.manifest_store import ManifestStoreError
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.download_page import download_page_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.download_manifest_state import DownloadManifestStateRow
from app.persistence.models.download_page import DownloadPageRow
from app.persistence.repositories.download_job import DownloadJobRepository
from app.persistence.repositories.download_page import DownloadPageRepository
from app.services.job_recovery import RecoveryCoordinator, WorkerQuiescenceAck
from app.services.page_manifest_writer import PageManifestWriter

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _RecoveryFixture:
    database: SQLiteDatabase
    repository: DownloadJobRepository
    coordinator: RecoveryCoordinator
    interrupted_job: DownloadJob
    manifest: DownloadJobManifest
    target: Path
    recovery_directory: Path

    def unclassified_staging_path(self) -> Path:
        # Deliberately omits the required generation and canonical UUID grammar.
        return self.recovery_directory / f".stg-{uuid4().hex}"

    def unclassified_backup_path(self) -> Path:
        # Deliberately omits the required generation and canonical UUID grammar.
        return self.recovery_directory / f".bak-{uuid4().hex}"


def _recovery_fixture(
    tmp_path: Path,
    *,
    interrupted_from: DownloadJobStatus = DownloadJobStatus.DOWNLOADING,
    cancel_requested: bool = False,
    recovery_blocked: bool = False,
) -> _RecoveryFixture:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()

    series = Series.new_local("Recovery series", "Recovery series", now=NOW)
    chapter = Chapter.new_local(
        series.series_id,
        "1",
        "Chapter 1",
        chapter_sort_key=(1, 0, 0),
    )
    source = Source.new_local(
        "recovery-test",
        "v1",
        "Recovery test source",
        True,
        SourceHealthStatus.HEALTHY,
        now=NOW,
    )
    chapter_source = ChapterSource.new_local(
        chapter.chapter_id,
        source.source_id,
        remote_id="recovery-chapter-1",
        available=True,
        last_seen_at=NOW,
    )
    job = DownloadJob.new_local(chapter_source.chapter_source_id, now=NOW)
    repository = DownloadJobRepository()
    with database.write_session() as session:
        session.add_all([series_to_row(series), source_to_row(source)])
        session.flush()
        session.add(chapter_to_row(chapter))
        session.flush()
        session.add(chapter_source_to_row(chapter_source))
        session.flush()
        repository.add(session, job)

    route = (
        DownloadJobStatus.QUEUED,
        DownloadJobStatus.FETCHING_METADATA,
        DownloadJobStatus.DOWNLOADING,
        DownloadJobStatus.VALIDATING,
        DownloadJobStatus.PROCESSING,
        DownloadJobStatus.PACKAGING,
    )
    current = job
    for status in route[1 : route.index(interrupted_from) + 1]:
        with database.write_session() as session:
            transitioned = repository.transition_status(
                session,
                current_job=current,
                target_status=status,
                changed_at=current.updated_at + timedelta(seconds=1),
            )
        assert transitioned is not None
        current = transitioned

    page = DownloadPage.new_local(
        job.job_id,
        1,
        "connector-page-1",
        temporary_path=f"jobs/{job.job_id.hex}/001.part",
    )
    with database.write_session() as session:
        session.add(download_page_to_row(page))

    if cancel_requested:
        with database.write_session() as session:
            requested = repository.request_cancellation(
                session,
                current_job=current,
                requested_at=current.updated_at + timedelta(seconds=1),
            )
        assert requested is not None
        current = requested

    coordinator = RecoveryCoordinator(database, database.data_root_identity)
    # The typed token represents K096's explicit confirmation that this worker
    # can no longer issue filesystem or database mutations.
    ack = WorkerQuiescenceAck(job_id=job.job_id)
    coordinator.interrupt_quiesced_job(
        job.job_id,
        current,
        quiescence_ack=ack,
        recovery_blocked=recovery_blocked,
    )
    with database.read_session() as session:
        persisted_interrupted_job = repository.get(session, job.job_id)
    assert persisted_interrupted_job is not None
    assert persisted_interrupted_job.status is DownloadJobStatus.INTERRUPTED
    assert persisted_interrupted_job.interrupted_from_status is interrupted_from
    assert persisted_interrupted_job.recovery_blocked is recovery_blocked

    manifest = DownloadJobManifest(
        job_id=job.job_id,
        source_id=source.source_id,
        chapter_id=chapter.chapter_id,
        expected_pages=1,
        pages=(page,),
    )
    root = database.data_root_identity.canonical_path
    manifest_directory = root / "database" / "manifests"
    return _RecoveryFixture(
        database=database,
        repository=repository,
        coordinator=coordinator,
        interrupted_job=persisted_interrupted_job,
        manifest=manifest,
        target=manifest_directory / f"{job.job_id}.json",
        recovery_directory=(manifest_directory / ".recovery" / str(job.job_id)),
    )


def test_quiesced_worker_can_preserve_a_job_in_explicit_recovery_blocked_state(
    tmp_path: Path,
) -> None:
    fixture = _recovery_fixture(tmp_path, recovery_blocked=True)
    try:
        assert fixture.interrupted_job.recovery_blocked is True
        fixture.coordinator.recover_job(fixture.interrupted_job.job_id)
        with fixture.database.read_session() as session:
            still_blocked = fixture.repository.get(
                session,
                fixture.interrupted_job.job_id,
            )
        assert still_blocked is not None
        assert still_blocked.status is DownloadJobStatus.INTERRUPTED
        assert still_blocked.recovery_blocked is True
    finally:
        fixture.database.shutdown()


def test_sqlite_full_during_quiesced_recovery_cas_rolls_back_and_keeps_gate_closed(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    series = Series.new_local("CAS full series", "CAS full series", now=NOW)
    chapter = Chapter.new_local(
        series.series_id,
        "1",
        "Chapter 1",
        chapter_sort_key=(1, 0, 0),
    )
    source = Source.new_local(
        "cas-full-test",
        "v1",
        "CAS full test source",
        True,
        SourceHealthStatus.HEALTHY,
        now=NOW,
    )
    chapter_source = ChapterSource.new_local(
        chapter.chapter_id,
        source.source_id,
        remote_id="cas-full-chapter-1",
        available=True,
        last_seen_at=NOW,
    )
    job = DownloadJob.new_local(chapter_source.chapter_source_id, now=NOW)
    repository = DownloadJobRepository()
    with database.write_session() as session:
        session.add_all([series_to_row(series), source_to_row(source)])
        session.flush()
        session.add(chapter_to_row(chapter))
        session.flush()
        session.add(chapter_source_to_row(chapter_source))
        session.flush()
        repository.add(session, job)
    with database.write_session() as session:
        active = repository.transition_status(
            session,
            current_job=job,
            target_status=DownloadJobStatus.FETCHING_METADATA,
            changed_at=NOW + timedelta(seconds=1),
        )
    assert active is not None

    with database.write_session() as session:
        session.execute(text("CREATE TABLE full_probe (payload BLOB NOT NULL)"))
        session.execute(
            text(
                "CREATE TRIGGER force_full_on_interrupt "
                "AFTER UPDATE OF status ON download_jobs "
                "WHEN NEW.status = 'INTERRUPTED' BEGIN "
                "INSERT INTO full_probe(payload) VALUES (zeroblob(1048576)); END"
            )
        )
    with database.exclusive_connection() as connection:
        current_pages = connection.exec_driver_sql("PRAGMA page_count").scalar_one()
        connection.exec_driver_sql(f"PRAGMA max_page_count={current_pages}")

    database.storage_gate.close("FILESYSTEM_DISK_FULL")
    coordinator = RecoveryCoordinator(database, database.data_root_identity)
    with pytest.raises(OperationalError) as raised:
        coordinator.interrupt_quiesced_job(
            job.job_id,
            active,
            quiescence_ack=WorkerQuiescenceAck(job.job_id),
            recovery_blocked=True,
            storage_gate_quiesced=True,
        )

    assert getattr(raised.value.orig, "sqlite_errorcode", 0) & 0xFF == sqlite3.SQLITE_FULL
    assert database.storage_gate.closed is True
    with database.read_session() as session:
        unchanged = repository.get(session, job.job_id)
        probe_rows = session.execute(text("SELECT count(*) FROM full_probe")).scalar_one()
    assert unchanged == active
    assert unchanged is not None
    assert unchanged.status is DownloadJobStatus.FETCHING_METADATA
    assert unchanged.interrupted_from_status is None
    assert unchanged.cancel_requested_at is None
    assert probe_rows == 0
    database.shutdown()


def _assert_recovery_blocked(fixture: _RecoveryFixture) -> None:
    with fixture.database.read_session() as session:
        blocked = session.execute(
            text("SELECT recovery_blocked FROM download_jobs WHERE job_id = :job_id"),
            {"job_id": str(fixture.interrupted_job.job_id)},
        ).scalar_one()
    assert blocked == 1


def _different_valid_manifest_bytes(fixture: _RecoveryFixture) -> bytes:
    page = replace(
        fixture.manifest.pages[0],
        remote_reference="older-connector-page-reference",
    )
    previous_manifest = replace(fixture.manifest, pages=(page,))
    return manifest_to_bytes(previous_manifest)


def test_recovery_blocks_and_preserves_unlinked_sidecars_even_when_target_matches(
    tmp_path: Path,
) -> None:
    fixture = _recovery_fixture(tmp_path)
    expected_bytes = manifest_to_bytes(fixture.manifest)
    backup_bytes = _different_valid_manifest_bytes(fixture)
    fixture.target.parent.mkdir(parents=True, exist_ok=True)
    fixture.target.write_bytes(expected_bytes)
    fixture.recovery_directory.mkdir(parents=True)
    staging = fixture.unclassified_staging_path()
    backup = fixture.unclassified_backup_path()
    staging.write_bytes(expected_bytes)
    backup.write_bytes(backup_bytes)

    try:
        fixture.coordinator.recover_job(fixture.interrupted_job.job_id)

        with fixture.database.read_session() as session:
            recovered = fixture.repository.get(session, fixture.interrupted_job.job_id)
        assert recovered is not None
        assert recovered.status is DownloadJobStatus.INTERRUPTED
        _assert_recovery_blocked(fixture)
        assert fixture.target.read_bytes() == expected_bytes
        assert staging.read_bytes() == expected_bytes
        assert backup.read_bytes() == backup_bytes
    finally:
        fixture.database.close()


def test_recovery_does_not_promote_unlinked_staging_without_durable_intent(
    tmp_path: Path,
) -> None:
    fixture = _recovery_fixture(tmp_path)
    expected_bytes = manifest_to_bytes(fixture.manifest)
    backup_bytes = _different_valid_manifest_bytes(fixture)
    fixture.recovery_directory.mkdir(parents=True)
    staging = fixture.unclassified_staging_path()
    backup = fixture.unclassified_backup_path()
    staging.write_bytes(expected_bytes)
    backup.write_bytes(backup_bytes)

    try:
        fixture.coordinator.recover_job(fixture.interrupted_job.job_id)

        with fixture.database.read_session() as session:
            recovered = fixture.repository.get(session, fixture.interrupted_job.job_id)
        assert recovered is not None
        assert recovered.status is DownloadJobStatus.INTERRUPTED
        _assert_recovery_blocked(fixture)
        assert not fixture.target.exists()
        assert staging.read_bytes() == expected_bytes
        assert backup.read_bytes() == backup_bytes
    finally:
        fixture.database.close()


def test_recovery_blocks_unlinked_backup_without_target_or_staging(
    tmp_path: Path,
) -> None:
    fixture = _recovery_fixture(tmp_path)
    expected_bytes = manifest_to_bytes(fixture.manifest)
    fixture.recovery_directory.mkdir(parents=True)
    backup = fixture.unclassified_backup_path()
    backup.write_bytes(expected_bytes)

    try:
        fixture.coordinator.recover_job(fixture.interrupted_job.job_id)

        with fixture.database.read_session() as session:
            recovered = fixture.repository.get(session, fixture.interrupted_job.job_id)
        assert recovered is not None
        assert recovered.status is DownloadJobStatus.INTERRUPTED
        _assert_recovery_blocked(fixture)
        assert not fixture.target.exists()
        assert backup.read_bytes() == expected_bytes
    finally:
        fixture.database.close()


def test_recovery_keeps_packaging_cancellation_interrupted_without_cleanup_evidence(
    tmp_path: Path,
) -> None:
    fixture = _recovery_fixture(
        tmp_path,
        interrupted_from=DownloadJobStatus.PACKAGING,
        cancel_requested=True,
    )
    expected_bytes = manifest_to_bytes(fixture.manifest)
    fixture.target.parent.mkdir(parents=True, exist_ok=True)
    fixture.target.write_bytes(expected_bytes)
    requested_at = fixture.interrupted_job.cancel_requested_at

    try:
        fixture.coordinator.recover_job(fixture.interrupted_job.job_id)

        with fixture.database.read_session() as session:
            recovered = fixture.repository.get(session, fixture.interrupted_job.job_id)
        assert recovered is not None
        assert recovered.status is DownloadJobStatus.INTERRUPTED
        assert recovered.cancel_requested_at == requested_at
        _assert_recovery_blocked(fixture)
        assert fixture.target.read_bytes() == expected_bytes
    finally:
        fixture.database.close()


def test_startup_retries_partial_page_attempt_without_overwriting_previous_part(
    tmp_path: Path,
) -> None:
    """K114 slice: restart and attempt reservation, not worker-to-CBZ recovery."""
    data_root = tmp_path / "data"
    database = open_test_database(data_root)
    try:
        MigrationRunner(database, data_root / "backups").upgrade_to_head()
        series = Series.new_local("Partial recovery series", "Partial recovery series", now=NOW)
        chapter = Chapter.new_local(
            series.series_id,
            "1",
            "Chapter 1",
            chapter_sort_key=(1, 0, 0),
        )
        source = Source.new_local(
            "partial-recovery-test",
            "v1",
            "Partial recovery test source",
            True,
            SourceHealthStatus.HEALTHY,
            now=NOW,
        )
        chapter_source = ChapterSource.new_local(
            chapter.chapter_id,
            source.source_id,
            remote_id="partial-recovery-chapter-1",
            available=True,
            last_seen_at=NOW,
        )
        job = DownloadJob.new_local(chapter_source.chapter_source_id, now=NOW)
        job_repository = DownloadJobRepository()
        with database.write_session() as session:
            session.add_all([series_to_row(series), source_to_row(source)])
            session.flush()
            session.add(chapter_to_row(chapter))
            session.flush()
            session.add(chapter_source_to_row(chapter_source))
            session.flush()
            job_repository.add(session, job)

        recovery = RecoveryCoordinator(database, database.data_root_identity)
        page_writer = PageManifestWriter(
            database,
            manifest_store=recovery.manifest_store,
        )
        current_job = job
        with database.write_session() as session:
            current_job = job_repository.transition_status(
                session,
                current_job=current_job,
                target_status=DownloadJobStatus.FETCHING_METADATA,
                changed_at=current_job.updated_at + timedelta(seconds=1),
            )
        assert current_job is not None

        page = DownloadPage.new_local(job.job_id, 1, "connector-page-1")
        page_writer.replace_pending_pages(job.job_id, (page,))
        with database.write_session() as session:
            current_job = job_repository.transition_status(
                session,
                current_job=current_job,
                target_status=DownloadJobStatus.DOWNLOADING,
                changed_at=current_job.updated_at + timedelta(seconds=1),
            )
        assert current_job is not None

        first_attempt_path = DownloadPageRepository.attempt_path(job.job_id, 1, 1)
        first_attempt = replace(
            page,
            status="DOWNLOADING",
            temporary_path=first_attempt_path,
            attempts=1,
        )
        first_snapshot = page_writer.publish_snapshot(job.job_id, (first_attempt,))
        assert first_snapshot.publication_pending is False

        partial_bytes = b"partial-page-bytes-before-process-exit"
        first_part = (
            database.data_root_identity.canonical_path / "temp" / first_attempt_path
        )
        with recovery.temporary_page_store.create(first_attempt_path) as stream:
            stream.write(partial_bytes)
        manifest_path = (
            database.data_root_identity.canonical_path
            / "database"
            / "manifests"
            / f"{job.job_id}.json"
        )
        assert manifest_path.read_bytes() == first_snapshot.manifest_bytes

        # Close the test DB pool without worker cleanup, then reopen the same
        # persisted root as the next backend process would.
        database.close()
        database = reopen_test_database(database)
        restarted_recovery = RecoveryCoordinator(
            database,
            database.data_root_identity,
        )
        assert restarted_recovery.reconcile_startup() == 1

        with database.read_session() as session:
            recovered_job = job_repository.get(session, job.job_id)
            assert recovered_job is not None
            recovered_manifest = DownloadPageRepository().project_manifest(
                session,
                recovered_job,
            )

        assert recovered_job.status is DownloadJobStatus.QUEUED
        assert recovered_job.recovery_blocked is False
        assert recovered_manifest.pages == (first_attempt,)
        assert first_part.read_bytes() == partial_bytes

        # Model the next worker's durable reservation before opening attempt 2.
        for target_status in (
            DownloadJobStatus.FETCHING_METADATA,
            DownloadJobStatus.DOWNLOADING,
        ):
            with database.write_session() as session:
                transitioned = job_repository.transition_status(
                    session,
                    current_job=recovered_job,
                    target_status=target_status,
                    changed_at=recovered_job.updated_at + timedelta(seconds=1),
                )
            assert transitioned is not None
            recovered_job = transitioned

        second_attempt_path = DownloadPageRepository.attempt_path(job.job_id, 1, 2)
        second_attempt = replace(
            first_attempt,
            temporary_path=second_attempt_path,
            attempts=2,
        )
        second_snapshot = PageManifestWriter(
            database,
            manifest_store=restarted_recovery.manifest_store,
        ).publish_snapshot(job.job_id, (second_attempt,))
        assert second_snapshot.publication_pending is False
        assert second_snapshot.manifest.pages == (second_attempt,)
        assert first_part.read_bytes() == partial_bytes

        second_part = (
            database.data_root_identity.canonical_path / "temp" / second_attempt_path
        )
        assert second_part != first_part
        assert not second_part.exists()
        with restarted_recovery.temporary_page_store.create(second_attempt_path) as stream:
            stream.write(b"new-attempt-bytes")
        assert second_part.read_bytes() == b"new-attempt-bytes"
        assert first_part.read_bytes() == partial_bytes

        with pytest.raises(ManifestStoreError) as raised:
            with restarted_recovery.temporary_page_store.create(second_attempt_path):
                pass
        assert raised.value.code is ErrorCode.DL_TEMPORARY_PATH_CONFLICT
        assert second_part.read_bytes() == b"new-attempt-bytes"
    finally:
        database.shutdown()


@pytest.mark.parametrize(
    (
        "retain_previous_backup",
        "crash_after_publication",
        "namespace_poison",
        "crash_during_cleanup",
    ),
    (
        (False, False, None, False),
        (True, False, None, False),
        (True, True, None, False),
        (True, False, "extra_generation", False),
        (True, False, "duplicate_generation", False),
        (True, False, "malformed_retained", False),
        (True, False, "foreign_retained", False),
        (True, False, "reparse_retained", False),
        (True, True, None, True),
    ),
    ids=(
        "generation-2-before-publication",
        "generation-3-before-publication-retained-backup",
        "generation-3-after-publication-retained-backup",
        "generation-3-extra-backup-blocked",
        "generation-3-duplicate-backup-blocked",
        "generation-3-malformed-backup-blocked",
        "generation-3-foreign-backup-blocked",
        "generation-3-reparse-backup-blocked",
        "generation-3-recovery-crash-during-cleanup",
    ),
)
def test_abrupt_child_exit_after_page_state_commit_replays_manifest_on_restart(
    tmp_path: Path,
    retain_previous_backup: bool,
    crash_after_publication: bool,
    namespace_poison: str | None,
    crash_during_cleanup: bool,
) -> None:
    """A hard process exit between SQLite commit and manifest save is replayable."""
    data_root = tmp_path / "data"
    database = open_test_database(data_root)
    try:
        MigrationRunner(database, data_root / "backups").upgrade_to_head()
        series = Series.new_local("Crash recovery series", "Crash recovery series", now=NOW)
        chapter = Chapter.new_local(
            series.series_id,
            "1",
            "Chapter 1",
            chapter_sort_key=(1, 0, 0),
        )
        source = Source.new_local(
            "crash-recovery-test",
            "v1",
            "Crash recovery test source",
            True,
            SourceHealthStatus.HEALTHY,
            now=NOW,
        )
        chapter_source = ChapterSource.new_local(
            chapter.chapter_id,
            source.source_id,
            remote_id="crash-recovery-chapter-1",
            available=True,
            last_seen_at=NOW,
        )
        job = DownloadJob.new_local(chapter_source.chapter_source_id, now=NOW)
        repository = DownloadJobRepository()
        with database.write_session() as session:
            session.add_all([series_to_row(series), source_to_row(source)])
            session.flush()
            session.add(chapter_to_row(chapter))
            session.flush()
            session.add(chapter_source_to_row(chapter_source))
            session.flush()
            repository.add(session, job)

        recovery = RecoveryCoordinator(database, database.data_root_identity)
        writer = PageManifestWriter(database, manifest_store=recovery.manifest_store)
        with database.write_session() as session:
            fetching = repository.transition_status(
                session,
                current_job=job,
                target_status=DownloadJobStatus.FETCHING_METADATA,
                changed_at=NOW + timedelta(seconds=1),
            )
        assert fetching is not None
        pending_page = DownloadPage.new_local(job.job_id, 1, "crash-page-1")
        writer.replace_pending_pages(job.job_id, (pending_page,))
        with database.write_session() as session:
            downloading = repository.transition_status(
                session,
                current_job=fetching,
                target_status=DownloadJobStatus.DOWNLOADING,
                changed_at=NOW + timedelta(seconds=2),
            )
        assert downloading is not None

        root = database.data_root_identity.canonical_path
        manifest_path = root / "database" / "manifests" / f"{job.job_id}.json"
        initial_manifest = manifest_path.read_bytes()
        attempt_number = 1
        previous_part: Path | None = None
        previous_attempt_bytes = b"preserved-attempt-one-bytes"
        if retain_previous_backup:
            first_attempt_path = DownloadPageRepository.attempt_path(job.job_id, 1, 1)
            first_attempt = DownloadPage.new_local(
                job.job_id,
                1,
                "crash-page-1",
                status="DOWNLOADING",
                temporary_path=first_attempt_path,
                attempts=1,
            )
            writer.publish_snapshot(job.job_id, (first_attempt,))
            previous_part = root / "temp" / first_attempt_path
            with recovery.temporary_page_store.create(first_attempt_path) as stream:
                stream.write(previous_attempt_bytes)
            attempt_number = 2

        pre_crash_manifest = manifest_path.read_bytes()
        retained_backup_path: Path | None = None
        with database.read_session() as session:
            pre_crash_state = session.get(DownloadManifestStateRow, str(job.job_id))
        assert pre_crash_state is not None
        pre_crash_generation = pre_crash_state.generation
        assert pre_crash_generation == (2 if retain_previous_backup else 1)
        if retain_previous_backup:
            retained_backup_path = next(
                (manifest_path.parent / ".recovery" / str(job.job_id)).glob(
                    f".bak-{pre_crash_generation:020d}-*"
                )
            )
            assert retained_backup_path.read_bytes() == initial_manifest

        backend_root = Path(__file__).resolve().parents[1]
        child_script = textwrap.dedent(
            """
            import os
            import sys
            import traceback
            from pathlib import Path
            from uuid import UUID

            backend_root = Path(sys.argv[3])
            sys.path.insert(0, str(backend_root / "tests"))
            sys.path.insert(0, str(backend_root))

            from support.sqlite import open_test_database
            from app.domain.download_page import DownloadPage
            from app.persistence.repositories.download_page import DownloadPageRepository
            from app.services.job_recovery import RecoveryCoordinator
            from app.services.page_manifest_writer import PageManifestWriter

            data_root = Path(sys.argv[1])
            job_id = UUID(sys.argv[2])
            attempt_number = int(sys.argv[4])
            crash_after_publication = sys.argv[5] == "1"
            database = open_test_database(data_root)
            recovery = RecoveryCoordinator(database, database.data_root_identity)
            writer = PageManifestWriter(database, manifest_store=recovery.manifest_store)
            page = DownloadPage.new_local(
                job_id,
                1,
                "crash-page-1",
                status="DOWNLOADING",
                temporary_path=DownloadPageRepository.attempt_path(
                    job_id,
                    1,
                    attempt_number,
                ),
                attempts=attempt_number,
            )

            def exit_after_database_commit(*args, **kwargs):
                os._exit(86)

            if crash_after_publication:
                original_verify_published = recovery.manifest_store.verify_published

                def exit_after_manifest_publication(reservation):
                    original_verify_published(reservation)
                    os._exit(86)

                recovery.manifest_store.verify_published = exit_after_manifest_publication
            else:
                recovery.manifest_store.save = exit_after_database_commit
            try:
                writer.publish_snapshot(job_id, (page,))
            except BaseException:
                traceback.print_exc()
                os._exit(87)
            os._exit(88)
            """
        )
        child_env = os.environ.copy()
        inherited_pythonpath = child_env.get("PYTHONPATH")
        pythonpath_parts = [str(backend_root), str(backend_root / "tests")]
        if inherited_pythonpath:
            pythonpath_parts.append(inherited_pythonpath)
        child_env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

        # Do not keep a SQLite pool open across the process boundary.
        database.close()
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                child_script,
                str(data_root),
                str(job.job_id),
                str(backend_root),
                str(attempt_number),
                "1" if crash_after_publication else "0",
            ],
            cwd=backend_root,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        (tmp_path / "crash-child.stdout.log").write_text(
            child.stdout,
            encoding="utf-8",
        )
        (tmp_path / "crash-child.stderr.log").write_text(
            child.stderr,
            encoding="utf-8",
        )
        (tmp_path / "crash-child.exit-code.txt").write_text(
            f"{child.returncode}\n",
            encoding="ascii",
        )
        assert child.returncode == 86, child.stderr

        database = reopen_test_database(database)
        attempt_path = DownloadPageRepository.attempt_path(
            job.job_id,
            1,
            attempt_number,
        )
        part_path = root / "temp" / attempt_path
        with database.read_session() as session:
            crashed_job = repository.get(session, job.job_id)
            assert crashed_job is not None
            crashed_manifest = DownloadPageRepository().project_manifest(
                session,
                crashed_job,
            )
            crashed_state = session.get(DownloadManifestStateRow, str(job.job_id))
            crashed_page_row = session.get(DownloadPageRow, (str(job.job_id), 1))
        assert crashed_job.status is DownloadJobStatus.DOWNLOADING
        assert crashed_page_row is not None
        assert crashed_page_row.status == "DOWNLOADING"
        assert crashed_page_row.temporary_path == attempt_path
        assert crashed_page_row.attempts == attempt_number
        assert crashed_state is not None
        assert crashed_state.generation == pre_crash_generation + 1
        assert crashed_state.publication_pending is True
        crash_projection_bytes = manifest_to_bytes(crashed_manifest)
        assert crashed_state.manifest_sha256 == hashlib.sha256(
            crash_projection_bytes
        ).hexdigest()
        assert crashed_state.manifest_size_bytes == len(crash_projection_bytes)
        assert manifest_path.read_bytes() == (
            crash_projection_bytes if crash_after_publication else pre_crash_manifest
        )
        assert not part_path.exists()
        recovery_directory = manifest_path.parent / ".recovery" / str(job.job_id)
        if retain_previous_backup:
            assert retained_backup_path is not None
            assert retained_backup_path.read_bytes() == initial_manifest
        if crash_after_publication:
            pending_generation_backups = tuple(
                recovery_directory.glob(f".bak-{crashed_state.generation:020d}-*")
            )
            assert len(pending_generation_backups) == 1
            assert pending_generation_backups[0].read_bytes() == pre_crash_manifest
        else:
            assert not tuple(
                recovery_directory.glob(f".bak-{crashed_state.generation:020d}-*")
            )

        namespace_poison_path: Path | None = None
        namespace_poison_bytes: bytes | None = None
        outside_reparse_target: Path | None = None
        if namespace_poison is not None:
            assert retain_previous_backup
            assert retained_backup_path is not None
            if namespace_poison == "extra_generation":
                namespace_poison_path = recovery_directory / (
                    f".bak-{pre_crash_generation - 1:020d}-{uuid4()}"
                )
                namespace_poison_bytes = initial_manifest
                namespace_poison_path.write_bytes(namespace_poison_bytes)
            elif namespace_poison == "duplicate_generation":
                namespace_poison_path = recovery_directory / (
                    f".bak-{pre_crash_generation:020d}-{uuid4()}"
                )
                namespace_poison_bytes = initial_manifest
                namespace_poison_path.write_bytes(namespace_poison_bytes)
            elif namespace_poison == "malformed_retained":
                namespace_poison_path = retained_backup_path
                namespace_poison_bytes = b"malformed-retained-backup"
                namespace_poison_path.write_bytes(namespace_poison_bytes)
            elif namespace_poison == "foreign_retained":
                namespace_poison_path = retained_backup_path
                foreign_job_id = uuid4()
                foreign_page = DownloadPage.new_local(
                    foreign_job_id,
                    1,
                    "crash-page-1",
                )
                foreign_manifest = DownloadJobManifest(
                    job_id=foreign_job_id,
                    source_id=source.source_id,
                    chapter_id=chapter.chapter_id,
                    expected_pages=1,
                    pages=(foreign_page,),
                )
                namespace_poison_bytes = manifest_to_bytes(foreign_manifest)
                namespace_poison_path.write_bytes(namespace_poison_bytes)
            elif namespace_poison == "reparse_retained":
                namespace_poison_path = retained_backup_path
                outside_reparse_target = tmp_path / "outside-retained-backup.json"
                outside_reparse_target.write_bytes(initial_manifest)
                namespace_poison_path.unlink()
                try:
                    namespace_poison_path.symlink_to(outside_reparse_target)
                except OSError as error:
                    pytest.skip(f"file symlinks are unavailable: {type(error).__name__}")
            else:
                raise AssertionError(f"unknown namespace poison: {namespace_poison}")

        if crash_during_cleanup:
            assert retain_previous_backup and crash_after_publication
            database.close()
            cleanup_crash_script = textwrap.dedent(
                """
                import os
                import sys
                from pathlib import Path

                backend_root = Path(sys.argv[2])
                sys.path.insert(0, str(backend_root / "tests"))
                sys.path.insert(0, str(backend_root))

                from support.sqlite import open_test_database
                from app.services.job_recovery import RecoveryCoordinator

                database = open_test_database(Path(sys.argv[1]))
                recovery = RecoveryCoordinator(database, database.data_root_identity)

                def exit_after_marker_close_before_cleanup(*args, **kwargs):
                    os._exit(86)

                recovery.manifest_store.prune_obsolete_backups = (
                    exit_after_marker_close_before_cleanup
                )
                recovery.reconcile_startup()
                os._exit(88)
                """
            )
            cleanup_child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    cleanup_crash_script,
                    str(data_root),
                    str(backend_root),
                ],
                cwd=backend_root,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            (tmp_path / "cleanup-child.stdout.log").write_text(
                cleanup_child.stdout,
                encoding="utf-8",
            )
            (tmp_path / "cleanup-child.stderr.log").write_text(
                cleanup_child.stderr,
                encoding="utf-8",
            )
            (tmp_path / "cleanup-child.exit-code.txt").write_text(
                f"{cleanup_child.returncode}\n",
                encoding="ascii",
            )
            assert cleanup_child.returncode == 86, cleanup_child.stderr
            database = reopen_test_database(database)
            with database.read_session() as session:
                interrupted_after_cleanup_crash = repository.get(session, job.job_id)
                confirmed_after_cleanup_crash = session.get(
                    DownloadManifestStateRow,
                    str(job.job_id),
                )
            assert interrupted_after_cleanup_crash is not None
            assert interrupted_after_cleanup_crash.status is DownloadJobStatus.INTERRUPTED
            assert interrupted_after_cleanup_crash.recovery_blocked is False
            assert confirmed_after_cleanup_crash is not None
            assert confirmed_after_cleanup_crash.publication_pending is False
            assert retained_backup_path is not None
            assert retained_backup_path.read_bytes() == initial_manifest
            assert manifest_path.read_bytes() == crash_projection_bytes
            restarted_recovery = RecoveryCoordinator(
                database,
                database.data_root_identity,
            )
            restarted_recovery.recover_job(job.job_id)
        else:
            restarted_recovery = RecoveryCoordinator(
                database,
                database.data_root_identity,
            )
            if retain_previous_backup:
                assert retained_backup_path is not None
                original_close_publication = (
                    restarted_recovery.manifest_state_repository.close_publication
                )

                def assert_old_backup_survives_until_marker_cas(
                    session: Session,
                    *,
                    expected_pending_state: DownloadManifestState,
                ) -> bool:
                    assert retained_backup_path.read_bytes() == initial_manifest
                    return original_close_publication(
                        session,
                        expected_pending_state=expected_pending_state,
                    )

                restarted_recovery.manifest_state_repository.close_publication = (
                    assert_old_backup_survives_until_marker_cas
                )
            assert restarted_recovery.reconcile_startup() == 1

        if namespace_poison is not None:
            with database.read_session() as session:
                blocked_job = repository.get(session, job.job_id)
                blocked_state = session.get(DownloadManifestStateRow, str(job.job_id))
            assert blocked_job is not None
            assert blocked_job.status is DownloadJobStatus.INTERRUPTED
            assert blocked_job.recovery_blocked is True
            assert blocked_state is not None
            assert blocked_state.publication_pending is True
            assert blocked_state.generation == pre_crash_generation + 1
            assert retained_backup_path is not None
            if namespace_poison_path is not None:
                if namespace_poison == "reparse_retained":
                    assert namespace_poison_path.is_symlink()
                    assert outside_reparse_target is not None
                    assert outside_reparse_target.read_bytes() == initial_manifest
                else:
                    assert namespace_poison_bytes is not None
                    assert namespace_poison_path.read_bytes() == namespace_poison_bytes
            if namespace_poison in {"extra_generation", "duplicate_generation"}:
                assert retained_backup_path.read_bytes() == initial_manifest
            assert manifest_path.read_bytes() == pre_crash_manifest
            assert blocked_state.manifest_sha256 == crashed_state.manifest_sha256
            assert blocked_state.manifest_size_bytes == crashed_state.manifest_size_bytes
            assert blocked_state.previous_sha256 == crashed_state.previous_sha256
            assert previous_part is not None
            assert previous_part.read_bytes() == previous_attempt_bytes
            assert not part_path.exists()
            assert not tuple(
                recovery_directory.glob(f".bak-{crashed_state.generation:020d}-*")
            )
            return

        with database.read_session() as session:
            recovered_job = repository.get(session, job.job_id)
            assert recovered_job is not None
            recovered_manifest = DownloadPageRepository().project_manifest(
                session,
                recovered_job,
            )
            recovered_state = session.get(DownloadManifestStateRow, str(job.job_id))
        assert recovered_job.status is DownloadJobStatus.QUEUED
        assert recovered_job.recovery_blocked is False
        assert recovered_state is not None
        assert recovered_state.publication_pending is False
        assert recovered_state.generation == pre_crash_generation + 1
        recovered_bytes = manifest_to_bytes(recovered_manifest)
        assert recovered_manifest.pages == (
            DownloadPage.new_local(
                job.job_id,
                1,
                "crash-page-1",
                status="DOWNLOADING",
                temporary_path=attempt_path,
                attempts=attempt_number,
            ),
        )
        assert manifest_path.read_bytes() == recovered_bytes
        assert recovered_state.manifest_sha256 == hashlib.sha256(
            recovered_bytes
        ).hexdigest()
        assert recovered_state.manifest_size_bytes == len(recovered_bytes)
        assert not part_path.exists()

        # A second startup pass must be idempotent and preserve all candidate bytes.
        assert restarted_recovery.reconcile_startup() == 0
        assert manifest_path.read_bytes() == recovered_bytes
        with database.read_session() as session:
            final_job = repository.get(session, job.job_id)
            final_state = session.get(DownloadManifestStateRow, str(job.job_id))
        assert final_job == recovered_job
        assert final_state is not None
        assert recovered_state is not None
        assert (
            final_state.job_id,
            final_state.generation,
            final_state.manifest_sha256,
            final_state.manifest_size_bytes,
            final_state.previous_sha256,
            final_state.publication_pending,
        ) == (
            recovered_state.job_id,
            recovered_state.generation,
            recovered_state.manifest_sha256,
            recovered_state.manifest_size_bytes,
            recovered_state.previous_sha256,
            recovered_state.publication_pending,
        )
        retained_backups = sorted(
            recovery_directory.glob(f".bak-{crashed_state.generation:020d}-*")
        )
        assert len(retained_backups) == 1
        assert retained_backups[0].read_bytes() == pre_crash_manifest
        if retain_previous_backup:
            earlier_backups = sorted(
                recovery_directory.glob(f".bak-{pre_crash_generation:020d}-*")
            )
            assert earlier_backups == []
            assert previous_part is not None
            assert previous_part.read_bytes() == previous_attempt_bytes
        assert not tuple(recovery_directory.glob(".stg-*-*"))
        assert not part_path.exists()
    finally:
        database.shutdown()
