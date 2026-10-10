"""Contract and failure tests for the SQLite/manifest page metadata writer."""

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Row
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from support.sqlite import open_test_database

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
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.download_job import download_job_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.download_manifest_state import DownloadManifestStateRow


def _database_with_job(tmp_path: Path) -> tuple[SQLiteDatabase, DownloadJob]:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    runner.upgrade_to_head()

    series = Series.new_local("Writer fixture", "Writer fixture")
    chapter = Chapter.new_local(
        series.series_id,
        "1",
        "Chapter 1",
        chapter_sort_key=(1, 0, 0),
    )
    source = Source.new_local(
        "writer-fixture",
        "1",
        "Writer fixture",
        True,
        SourceHealthStatus.HEALTHY,
    )
    chapter_source = ChapterSource.new_local(
        chapter.chapter_id,
        source.source_id,
        remote_id="writer-fixture-chapter",
        available=True,
    )
    job = replace(
        DownloadJob.new_local(chapter_source.chapter_source_id),
        status=DownloadJobStatus.FETCHING_METADATA,
    )

    with database.write_session() as session:
        session.add(series_to_row(series))
        session.add(source_to_row(source))
        session.flush()
        session.add(chapter_to_row(chapter))
        session.flush()
        session.add(chapter_source_to_row(chapter_source))
        session.flush()
        session.add(download_job_to_row(job))
    return database, job


def _pending_page(job_id: UUID, ordinal: int, reference: str) -> DownloadPage:
    return DownloadPage.new_local(
        job_id,
        ordinal,
        reference,
        status="PENDING",
    )


def _noop_recovery_guard(_job_id: UUID) -> None:
    return None


def _writer(database: SQLiteDatabase):
    from app.persistence.manifest_store import JobManifestStore
    from app.services.page_manifest_writer import PageManifestWriter

    store = JobManifestStore(
        database.data_root_identity.canonical_path,
        data_root_identity=database.data_root_identity,
        recovery_guard=_noop_recovery_guard,
    )
    return PageManifestWriter(database, manifest_store=store), store


def _target_path(database: SQLiteDatabase, job_id: UUID) -> Path:
    return (
        database.data_root_identity.canonical_path
        / "database"
        / "manifests"
        / f"{job_id}.json"
    )


def _page_rows(database: SQLiteDatabase, job_id: UUID) -> list[Row[Any]]:
    with database.read_session() as session:
        return list(
            session.execute(
                text(
                    "SELECT ordinal, remote_reference, status, temporary_path, "
                    "size_bytes, sha256, width, height, mime, attempts "
                    "FROM download_pages WHERE job_id = :job_id ORDER BY ordinal"
                ),
                {"job_id": str(job_id)},
            ).all()
        )


def test_pending_metadata_pages_commit_with_manifest_intent_and_verified_close(
    tmp_path: Path,
) -> None:
    from app.domain.job_manifest import manifest_to_bytes
    database, job = _database_with_job(tmp_path)
    writer, _store = _writer(database)
    proposed = (
        _pending_page(job.job_id, 1, "remote-page-1"),
        _pending_page(job.job_id, 2, "remote-page-2"),
    )

    result = writer.replace_pending_pages(job.job_id, proposed)

    assert result.generation == 1
    assert result.publication_pending is False
    manifest_path = (
        database.data_root_identity.canonical_path
        / "database"
        / "manifests"
        / f"{job.job_id}.json"
    )
    assert manifest_path.read_bytes() == result.manifest_bytes
    assert result.manifest_bytes == manifest_to_bytes(result.manifest)
    with database.read_session() as session:
        state = session.get(DownloadManifestStateRow, str(job.job_id))
        rows = session.execute(
            text(
                "SELECT ordinal, remote_reference, status, temporary_path, size_bytes, "
                "sha256, width, height, mime, attempts FROM download_pages "
                "WHERE job_id = :job_id ORDER BY ordinal"
            ),
            {"job_id": str(job.job_id)},
        ).all()
    assert state is not None
    assert state.generation == 1
    assert state.publication_pending is False
    assert state.previous_sha256 is None
    assert [row[1] for row in rows] == ["remote-page-1", "remote-page-2"]
    assert all(row[2] == "PENDING" for row in rows)
    assert all(row[3:] == (None, None, None, None, None, None, 0) for row in rows)
    assert not any(page.status in {"DOWNLOADED", "VALIDATED"} for page in result.manifest.pages)


def test_prune_retained_backup_reuses_verified_recovery_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, job = _database_with_job(tmp_path)
    writer, store = _writer(database)
    writer.replace_pending_pages(
        job.job_id,
        (_pending_page(job.job_id, 1, "inventory-reference-one"),),
    )
    writer.replace_pending_pages(
        job.job_id,
        (_pending_page(job.job_id, 1, "inventory-reference-two"),),
    )
    prune_retained_backup = store.prune_retained_backup
    assert_recovery_namespace = store._assert_recovery_namespace_expected  # pyright: ignore[reportPrivateUsage]
    namespace_checks = 0
    checks_during_prune: list[int] = []

    def count_namespace_checks(*args: Any, **kwargs: Any) -> Any:
        nonlocal namespace_checks
        namespace_checks += 1
        return assert_recovery_namespace(*args, **kwargs)

    def measure_prune(reservation: Any) -> None:
        nonlocal namespace_checks
        namespace_checks = 0
        prune_retained_backup(reservation)
        checks_during_prune.append(namespace_checks)

    monkeypatch.setattr(
        store,
        "_assert_recovery_namespace_expected",
        count_namespace_checks,
    )
    monkeypatch.setattr(store, "prune_retained_backup", measure_prune)
    writer.replace_pending_pages(
        job.job_id,
        (_pending_page(job.job_id, 1, "inventory-reference-three"),),
    )

    assert checks_during_prune == [2]


def test_page_repository_requires_matching_pending_marker_before_mutation(
    tmp_path: Path,
) -> None:
    import hashlib

    from app.domain.download_manifest_state import DownloadManifestState
    from app.domain.job_manifest import DownloadJobManifest, manifest_to_bytes
    from app.persistence.manifest_store import ManifestStoreError
    from app.persistence.repositories.download_page import DownloadPageRepository

    database, job = _database_with_job(tmp_path)
    writer, store = _writer(database)
    proposed_pages = (_pending_page(job.job_id, 1, "authorization-reference"),)
    with store.lock_job(job.job_id):
        with database.read_session() as session:
            current_job = writer.job_repository.get(session, job.job_id)
            assert current_job is not None
            current_manifest = writer.page_repository.project_manifest(
                session,
                current_job,
            )
            current_state = writer.state_repository.get(session, job.job_id)
        assert current_state is None
        current_bytes = manifest_to_bytes(current_manifest)
        current_digest = hashlib.sha256(current_bytes).hexdigest()
        current_generation = current_state.generation if current_state is not None else 0
        proposed = DownloadJobManifest(
            job_id=job.job_id,
            source_id=current_manifest.source_id,
            chapter_id=current_manifest.chapter_id,
            expected_pages=len(proposed_pages),
            pages=proposed_pages,
        )
        proposed_bytes = manifest_to_bytes(proposed)
        proposed_digest = hashlib.sha256(proposed_bytes).hexdigest()
        reservation = store.reserve_write(
            job_id=job.job_id,
            expected_current_generation=current_generation,
            current_sha256=current_digest,
            current_size_bytes=len(current_bytes),
            new_sha256=proposed_digest,
            new_size_bytes=len(proposed_bytes),
        )
        pending_state = DownloadManifestState(
            job_id=job.job_id,
            generation=reservation.new_generation,
            manifest_sha256=proposed_digest,
            manifest_size_bytes=len(proposed_bytes),
            previous_sha256=reservation.target_sha256,
            publication_pending=True,
        )
        with pytest.raises(ManifestStoreError):
            store.assert_write_reservation_issued(replace(reservation))
        store.assert_write_reservation_issued(reservation)

        repository = DownloadPageRepository(
            reservation_validator=store.assert_write_reservation_issued,
        )
        with database.write_session() as session:
            with pytest.raises(ValueError, match="pending marker"):
                repository.replace_pages_for_manifest_publication(
                    session,
                    job_id=job.job_id,
                    pages=proposed_pages,
                    reservation=reservation,
                    pending_state=pending_state,
                )

    assert _page_rows(database, job.job_id) == []


def test_page_repository_rejects_pages_that_diverge_from_reserved_digest(
    tmp_path: Path,
) -> None:
    import hashlib

    from app.domain.download_manifest_state import DownloadManifestState
    from app.domain.job_manifest import DownloadJobManifest, manifest_to_bytes
    from app.persistence.mappers.download_page import download_page_to_row

    database, job = _database_with_job(tmp_path)
    writer, store = _writer(database)
    original_page = _pending_page(job.job_id, 1, "preexisting-reference")
    with database.write_session() as session:
        session.add(download_page_to_row(original_page))
    original_rows = _page_rows(database, job.job_id)

    intended_pages = (_pending_page(job.job_id, 1, "reserved-reference"),)
    supplied_pages = (_pending_page(job.job_id, 1, "divergent-reference"),)
    with store.lock_job(job.job_id):
        with database.read_session() as session:
            current_job = writer.job_repository.get(session, job.job_id)
            assert current_job is not None
            current_manifest = writer.page_repository.project_manifest(
                session,
                current_job,
            )
            current_state = writer.state_repository.get(session, job.job_id)
        assert current_state is None
        current_bytes = manifest_to_bytes(current_manifest)
        proposed = DownloadJobManifest(
            job_id=job.job_id,
            source_id=current_manifest.source_id,
            chapter_id=current_manifest.chapter_id,
            expected_pages=len(intended_pages),
            pages=intended_pages,
        )
        proposed_bytes = manifest_to_bytes(proposed)
        proposed_digest = hashlib.sha256(proposed_bytes).hexdigest()
        reservation = store.reserve_write(
            job_id=job.job_id,
            expected_current_generation=0,
            current_sha256=hashlib.sha256(current_bytes).hexdigest(),
            current_size_bytes=len(current_bytes),
            new_sha256=proposed_digest,
            new_size_bytes=len(proposed_bytes),
        )
        pending_state = DownloadManifestState(
            job_id=job.job_id,
            generation=reservation.new_generation,
            manifest_sha256=proposed_digest,
            manifest_size_bytes=len(proposed_bytes),
            previous_sha256=reservation.target_sha256,
            publication_pending=True,
        )

        with pytest.raises(ValueError, match="reserved manifest digest and size"):
            with database.write_session() as session:
                assert writer.state_repository.mark_publication_pending(
                    session,
                    expected_state=None,
                    new_state=pending_state,
                )
                writer.page_repository.replace_pages_for_manifest_publication(
                    session,
                    job_id=job.job_id,
                    pages=supplied_pages,
                    reservation=reservation,
                    pending_state=pending_state,
                )

    assert _page_rows(database, job.job_id) == original_rows
    with database.read_session() as session:
        assert writer.state_repository.get(session, job.job_id) is None


def test_repeated_metadata_publications_retain_only_the_verified_previous_backup(
    tmp_path: Path,
) -> None:
    database, job = _database_with_job(tmp_path)
    writer, _store = _writer(database)

    first = writer.replace_pending_pages(
        job.job_id,
        (_pending_page(job.job_id, 1, "reference-one"),),
    )
    second = writer.replace_pending_pages(
        job.job_id,
        (_pending_page(job.job_id, 1, "reference-two"),),
    )
    third = writer.replace_pending_pages(
        job.job_id,
        (_pending_page(job.job_id, 1, "reference-three"),),
    )

    recovery_dir = _target_path(database, job.job_id).parent / ".recovery" / str(job.job_id)
    backups = sorted(recovery_dir.glob(".bak-*"))
    assert first.generation == 1
    assert second.generation == 2
    assert third.generation == 3
    assert len(backups) == 1
    assert backups[0].name.startswith(f".bak-{3:020d}-")
    assert backups[0].read_bytes() == second.manifest_bytes
    with database.read_session() as session:
        state = session.get(DownloadManifestStateRow, str(job.job_id))
    assert state is not None
    assert state.generation == 3
    assert state.publication_pending is False
    assert state.previous_sha256 == second.manifest_sha256


def test_failed_post_cas_backup_prune_is_retried_without_deleting_current_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, job = _database_with_job(tmp_path)
    writer, store = _writer(database)
    first = writer.replace_pending_pages(
        job.job_id,
        (_pending_page(job.job_id, 1, "cleanup-reference-one"),),
    )
    second = writer.replace_pending_pages(
        job.job_id,
        (_pending_page(job.job_id, 1, "cleanup-reference-two"),),
    )
    def fail_prune(_reservation: object) -> None:
        raise OSError("injected cleanup lock")

    with monkeypatch.context() as patcher:
        patcher.setattr(store, "prune_retained_backup", fail_prune)
        with pytest.raises(OSError, match="injected cleanup lock"):
            writer.replace_pending_pages(
                job.job_id,
                (_pending_page(job.job_id, 1, "cleanup-reference-three"),),
            )

    recovery_dir = _target_path(database, job.job_id).parent / ".recovery" / str(job.job_id)
    after_failure = sorted(recovery_dir.glob(".bak-*"))
    assert len(after_failure) == 2
    assert any(path.name.startswith(f".bak-{2:020d}-") for path in after_failure)
    current_backup = next(
        path for path in after_failure if path.name.startswith(f".bak-{3:020d}-")
    )
    assert current_backup.read_bytes() == second.manifest_bytes
    with database.read_session() as session:
        state = session.get(DownloadManifestStateRow, str(job.job_id))
    assert state is not None
    assert state.generation == 3
    assert state.publication_pending is False
    assert state.previous_sha256 == second.manifest_sha256
    third_published_bytes = _target_path(database, job.job_id).read_bytes()

    fourth = writer.replace_pending_pages(
        job.job_id,
        (_pending_page(job.job_id, 1, "cleanup-reference-four"),),
    )

    after_retry = sorted(recovery_dir.glob(".bak-*"))
    assert fourth.generation == 4
    assert len(after_retry) == 1
    assert after_retry[0].name.startswith(f".bak-{4:020d}-")
    assert after_retry[0].read_bytes() != first.manifest_bytes
    assert after_retry[0].read_bytes() == third_published_bytes


def test_precommit_failure_after_page_mutation_rolls_back_rows_and_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.domain.download_manifest_state import DownloadManifestState
    from app.persistence.manifest_store import ManifestWriteReservation
    from app.persistence.repositories.download_page import DownloadPageRepository

    database, job = _database_with_job(tmp_path)
    writer, _store = _writer(database)
    replace_pages = DownloadPageRepository.replace_pages_for_manifest_publication

    def fail_after_page_rows(
        repository: DownloadPageRepository,
        session: Session,
        *,
        job_id: UUID,
        pages: tuple[DownloadPage, ...],
        reservation: ManifestWriteReservation,
        pending_state: DownloadManifestState,
    ) -> None:
        replace_pages(
            repository,
            session,
            job_id=job_id,
            pages=pages,
            reservation=reservation,
            pending_state=pending_state,
        )
        raise RuntimeError("injected precommit failure")

    monkeypatch.setattr(
        DownloadPageRepository,
        "replace_pages_for_manifest_publication",
        fail_after_page_rows,
    )

    with pytest.raises(RuntimeError, match="injected precommit failure"):
        writer.replace_pending_pages(
            job.job_id,
            (_pending_page(job.job_id, 1, "precommit-page"),),
        )

    with database.read_session() as session:
        assert session.get(DownloadManifestStateRow, str(job.job_id)) is None
    assert _page_rows(database, job.job_id) == []
    assert not _target_path(database, job.job_id).exists()


def test_sqlite_full_at_commit_boundary_rolls_back_intent_and_pages(
    tmp_path: Path,
) -> None:
    database, job = _database_with_job(tmp_path)
    writer, _store = _writer(database)

    def fail_before_commit(_session: Session) -> None:
        raise OperationalError(
            "COMMIT",
            {},
            sqlite3.OperationalError("database or disk is full"),
        )

    event.listen(Session, "before_commit", fail_before_commit)
    try:
        with pytest.raises(OperationalError, match="database or disk is full"):
            writer.replace_pending_pages(
                job.job_id,
                (_pending_page(job.job_id, 1, "sqlite-full-page"),),
            )
    finally:
        event.remove(Session, "before_commit", fail_before_commit)

    with database.read_session() as session:
        assert session.get(DownloadManifestStateRow, str(job.job_id)) is None
    assert _page_rows(database, job.job_id) == []
    assert not _target_path(database, job.job_id).exists()


def test_postcommit_publish_failure_preserves_pending_intent_and_blocks_next_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module
    from app.persistence.manifest_store import ManifestStoreError
    from app.services.page_manifest_writer import PageManifestWriteBlocked

    database, job = _database_with_job(tmp_path)
    writer, _store = _writer(database)
    proposal = (_pending_page(job.job_id, 1, "postcommit-page"),)

    def fail_before_publish(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected postcommit publish failure")

    monkeypatch.setattr(store_module, "_move_file_ex_win32", fail_before_publish)
    with pytest.raises(ManifestStoreError):
        writer.replace_pending_pages(job.job_id, proposal)

    with database.read_session() as session:
        state = session.get(DownloadManifestStateRow, str(job.job_id))
        assert state is not None
        assert state.publication_pending is True
        assert state.generation == 1
    committed_rows = _page_rows(database, job.job_id)
    assert [row[1] for row in committed_rows] == ["postcommit-page"]
    assert not _target_path(database, job.job_id).exists()
    recovery_dir = _target_path(database, job.job_id).parent / ".recovery" / str(job.job_id)
    staged = list(recovery_dir.glob(".stg-*"))
    assert len(staged) == 1
    assert staged[0].name.startswith(f".stg-{1:020d}-")

    with pytest.raises(PageManifestWriteBlocked):
        writer.replace_pending_pages(
            job.job_id,
            (_pending_page(job.job_id, 1, "must-not-overwrite"),),
        )
    assert _page_rows(database, job.job_id) == committed_rows
    assert staged[0].exists()


def test_unmatched_preexisting_target_blocks_first_write_without_overwrite(
    tmp_path: Path,
) -> None:
    from uuid import uuid4

    from app.domain.job_manifest import DownloadJobManifest, manifest_to_bytes
    from app.persistence.manifest_store import ManifestStoreError

    database, job = _database_with_job(tmp_path)
    writer, _store = _writer(database)
    target = _target_path(database, job.job_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    unmatched = DownloadJobManifest(
        job_id=job.job_id,
        source_id=uuid4(),
        chapter_id=uuid4(),
        expected_pages=0,
        pages=(),
    )
    unmatched_bytes = manifest_to_bytes(unmatched)
    target.write_bytes(unmatched_bytes)

    with pytest.raises(ManifestStoreError):
        writer.replace_pending_pages(
            job.job_id,
            (_pending_page(job.job_id, 1, "blocked-page"),),
        )

    assert target.read_bytes() == unmatched_bytes
    assert _page_rows(database, job.job_id) == []
    with database.read_session() as session:
        assert session.get(DownloadManifestStateRow, str(job.job_id)) is None


def test_writer_rejects_page_states_that_require_transport_or_validation(
    tmp_path: Path,
) -> None:
    from app.services.page_manifest_writer import PageManifestWriteBlocked

    database, job = _database_with_job(tmp_path)
    writer, _store = _writer(database)
    downloaded_page = DownloadPage.new_local(
        job.job_id,
        1,
        "remote-downloaded-page",
        temporary_path=f"jobs/{job.job_id}/pages/1/attempt-1.part",
        status="DOWNLOADED",
        size_bytes=1,
        sha256="a" * 64,
        attempts=1,
    )

    with pytest.raises(PageManifestWriteBlocked):
        writer.replace_pending_pages(job.job_id, (downloaded_page,))

    assert _page_rows(database, job.job_id) == []
    assert not _target_path(database, job.job_id).exists()
    with database.read_session() as session:
        assert session.get(DownloadManifestStateRow, str(job.job_id)) is None


def test_writer_requires_metadata_fetch_stage_not_queued_dispatch_state(
    tmp_path: Path,
) -> None:
    from app.services.page_manifest_writer import PageManifestWriteBlocked

    database, job = _database_with_job(tmp_path)
    with database.write_session() as session:
        session.execute(
            text("UPDATE download_jobs SET status = 'QUEUED' WHERE job_id = :job_id"),
            {"job_id": str(job.job_id)},
        )
    writer, _store = _writer(database)

    with pytest.raises(PageManifestWriteBlocked):
        writer.replace_pending_pages(
            job.job_id,
            (_pending_page(job.job_id, 1, "queued-must-not-write"),),
        )

    assert _page_rows(database, job.job_id) == []
    assert not _target_path(database, job.job_id).exists()
    with database.read_session() as session:
        assert session.get(DownloadManifestStateRow, str(job.job_id)) is None


def test_stale_marker_cas_rolls_back_page_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.persistence.repositories.download_manifest_state import (
        DownloadManifestStateRepository,
    )
    from app.services.page_manifest_writer import StalePageManifestSnapshot

    database, job = _database_with_job(tmp_path)
    writer, _store = _writer(database)

    def stale_cas(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        DownloadManifestStateRepository,
        "mark_publication_pending",
        stale_cas,
    )
    with pytest.raises(StalePageManifestSnapshot):
        writer.replace_pending_pages(
            job.job_id,
            (_pending_page(job.job_id, 1, "stale-cas-page"),),
        )

    with database.read_session() as session:
        assert session.get(DownloadManifestStateRow, str(job.job_id)) is None
    assert _page_rows(database, job.job_id) == []
    assert not _target_path(database, job.job_id).exists()


def test_stale_close_cas_keeps_committed_pending_state_and_does_not_report_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.persistence.repositories.download_manifest_state import (
        DownloadManifestStateRepository,
    )
    from app.services.page_manifest_writer import StalePageManifestSnapshot

    database, job = _database_with_job(tmp_path)
    writer, _store = _writer(database)

    def stale_close(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        DownloadManifestStateRepository,
        "close_publication",
        stale_close,
    )
    with pytest.raises(StalePageManifestSnapshot):
        writer.replace_pending_pages(
            job.job_id,
            (_pending_page(job.job_id, 1, "close-cas-page"),),
        )

    with database.read_session() as session:
        state = session.get(DownloadManifestStateRow, str(job.job_id))
    assert state is not None
    assert state.publication_pending is True
    assert state.generation == 1
    assert [row[1] for row in _page_rows(database, job.job_id)] == ["close-cas-page"]
    assert _target_path(database, job.job_id).exists()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_bad_retained_backup_blocks_before_mutation_and_preserves_evidence(
    tmp_path: Path,
    damage: str,
) -> None:
    from app.persistence.manifest_store import ManifestStoreError

    database, job = _database_with_job(tmp_path)
    writer, _store = _writer(database)
    writer.replace_pending_pages(
        job.job_id,
        (_pending_page(job.job_id, 1, "backup-reference-one"),),
    )
    writer.replace_pending_pages(
        job.job_id,
        (_pending_page(job.job_id, 1, "backup-reference-two"),),
    )
    target = _target_path(database, job.job_id)
    recovery_dir = target.parent / ".recovery" / str(job.job_id)
    backup = next(recovery_dir.glob(f".bak-{2:020d}-*"))
    if damage == "missing":
        backup.unlink()
        preserved_backup_bytes = None
    else:
        backup.write_bytes(b"untrusted-corrupt-backup")
        preserved_backup_bytes = backup.read_bytes()
    before_target = target.read_bytes()
    before_rows = _page_rows(database, job.job_id)
    with database.read_session() as session:
        before_state = session.get(DownloadManifestStateRow, str(job.job_id))
        assert before_state is not None
        before_state_values = (
            before_state.generation,
            before_state.manifest_sha256,
            before_state.manifest_size_bytes,
            before_state.previous_sha256,
            before_state.publication_pending,
        )

    with pytest.raises(ManifestStoreError):
        writer.replace_pending_pages(
            job.job_id,
            (_pending_page(job.job_id, 1, "must-not-commit"),),
        )

    assert target.read_bytes() == before_target
    assert _page_rows(database, job.job_id) == before_rows
    with database.read_session() as session:
        after_state = session.get(DownloadManifestStateRow, str(job.job_id))
    assert after_state is not None
    assert (
        after_state.generation,
        after_state.manifest_sha256,
        after_state.manifest_size_bytes,
        after_state.previous_sha256,
        after_state.publication_pending,
    ) == before_state_values
    if damage == "missing":
        assert not backup.exists()
    else:
        assert backup.read_bytes() == preserved_backup_bytes


def test_confirmed_state_with_missing_target_blocks_without_reconstructing_manifest(
    tmp_path: Path,
) -> None:
    from app.persistence.manifest_store import ManifestStoreError

    database, job = _database_with_job(tmp_path)
    writer, _store = _writer(database)
    writer.replace_pending_pages(
        job.job_id,
        (_pending_page(job.job_id, 1, "target-missing-first"),),
    )
    target = _target_path(database, job.job_id)
    target.unlink()
    before_rows = _page_rows(database, job.job_id)
    with database.read_session() as session:
        state = session.get(DownloadManifestStateRow, str(job.job_id))
        assert state is not None
        before_generation = state.generation
        before_sha256 = state.manifest_sha256

    with pytest.raises(ManifestStoreError):
        writer.replace_pending_pages(
            job.job_id,
            (_pending_page(job.job_id, 1, "must-not-reconstruct"),),
        )

    assert not target.exists()
    assert _page_rows(database, job.job_id) == before_rows
    with database.read_session() as session:
        state_after = session.get(DownloadManifestStateRow, str(job.job_id))
    assert state_after is not None
    assert state_after.generation == before_generation
    assert state_after.manifest_sha256 == before_sha256
    assert state_after.publication_pending is False


def test_manifest_reservation_rejects_missing_target_for_nonzero_generation(
    tmp_path: Path,
) -> None:
    import hashlib

    from app.domain.job_manifest import DownloadJobManifest, manifest_to_bytes
    from app.persistence.manifest_store import ManifestStoreError

    database, job = _database_with_job(tmp_path)
    _writer_service, store = _writer(database)
    current = DownloadJobManifest(
        job_id=job.job_id,
        source_id=UUID("00000000-0000-4000-8000-000000000002"),
        chapter_id=UUID("00000000-0000-4000-8000-000000000003"),
        expected_pages=0,
        pages=(),
    )
    proposed = DownloadJobManifest(
        job_id=job.job_id,
        source_id=current.source_id,
        chapter_id=current.chapter_id,
        expected_pages=1,
        pages=(_pending_page(job.job_id, 1, "nonzero-generation"),),
    )
    current_bytes = manifest_to_bytes(current)
    proposed_bytes = manifest_to_bytes(proposed)

    with store.lock_job(job.job_id):
        with pytest.raises(ManifestStoreError):
            store.reserve_write(
                job_id=job.job_id,
                expected_current_generation=1,
                current_sha256=hashlib.sha256(current_bytes).hexdigest(),
                current_size_bytes=len(current_bytes),
                new_sha256=hashlib.sha256(proposed_bytes).hexdigest(),
                new_size_bytes=len(proposed_bytes),
            )

    assert not _target_path(database, job.job_id).exists()


def test_page_count_limit_is_checked_before_iterating_or_opening_mutation(
    tmp_path: Path,
) -> None:
    from app.domain.job_manifest import MAX_MANIFEST_PAGE_COUNT
    from app.services.page_manifest_writer import PageManifestWriteBlocked

    database, job = _database_with_job(tmp_path)
    writer, _store = _writer(database)
    over_limit = cast(
        tuple[DownloadPage, ...],
        (object(),) * (MAX_MANIFEST_PAGE_COUNT + 1),
    )

    with pytest.raises(PageManifestWriteBlocked):
        writer.replace_pending_pages(job.job_id, over_limit)

    assert _page_rows(database, job.job_id) == []
    assert not _target_path(database, job.job_id).exists()
    with database.read_session() as session:
        assert session.get(DownloadManifestStateRow, str(job.job_id)) is None

