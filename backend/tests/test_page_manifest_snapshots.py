"""Monotonic SQLite/manifest page-state publication contract tests."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import event
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
from app.domain.download_manifest_state import DownloadManifestState
from app.domain.job_manifest import MAX_SQLITE_INTEGER
from app.persistence.database import SQLiteDatabase
from app.persistence.manifest_store import ManifestWriteReservation
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.download_job import download_job_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.download_manifest_state import DownloadManifestStateRow
from app.persistence.repositories.download_job import DownloadJobRepository
from app.persistence.repositories.download_page import DownloadPageRepository
from app.services.page_manifest_writer import (
    PageManifestWriteBlocked,
    PageManifestWriter,
)


def _database_with_job(tmp_path: Path) -> tuple[SQLiteDatabase, DownloadJob]:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    series = Series.new_local("Page snapshot fixture", "Page snapshot fixture")
    chapter = Chapter.new_local(
        series.series_id,
        "1",
        "Chapter 1",
        chapter_sort_key=(1, 0, 0),
    )
    source = Source.new_local(
        "page-snapshot-fixture",
        "1",
        "Page snapshot fixture",
        True,
        SourceHealthStatus.HEALTHY,
    )
    chapter_source = ChapterSource.new_local(
        chapter.chapter_id,
        source.source_id,
        remote_id="page-snapshot-chapter",
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


def _writer(database: SQLiteDatabase) -> PageManifestWriter:
    from app.persistence.manifest_store import JobManifestStore

    def recovery_guard(_job_id: UUID) -> None:
        return None

    store = JobManifestStore(
        database.data_root_identity.canonical_path,
        data_root_identity=database.data_root_identity,
        recovery_guard=recovery_guard,
    )
    return PageManifestWriter(database, manifest_store=store)


def _page(
    job_id: UUID,
    ordinal: int,
    reference: str,
    *,
    status: str = "PENDING",
    path: str | None = None,
    attempts: int = 0,
    size: int | None = None,
    sha256: str | None = None,
    width: int | None = None,
    height: int | None = None,
    mime: str | None = None,
) -> DownloadPage:
    return DownloadPage.new_local(
        job_id,
        ordinal,
        reference,
        temporary_path=path,
        status=status,
        attempts=attempts,
        size_bytes=size,
        sha256=sha256,
        width=width,
        height=height,
        mime=mime,
    )


def _transition_job(
    database: SQLiteDatabase,
    job_id: UUID,
    target: DownloadJobStatus,
) -> None:
    repository = DownloadJobRepository()
    with database.read_session() as session:
        current = repository.get(session, job_id)
    assert current is not None
    with database.write_session() as session:
        result = repository.transition_status(
            session,
            current_job=current,
            target_status=target,
            changed_at=current.updated_at + timedelta(seconds=1),
        )
        assert result is not None


def _interrupt_job(database: SQLiteDatabase, job_id: UUID) -> None:
    repository = DownloadJobRepository()
    with database.read_session() as session:
        current = repository.get(session, job_id)
    assert current is not None
    with database.write_session() as session:
        result = repository.interrupt_active_job(
            session,
            current_job=current,
            interrupted_at=current.updated_at + timedelta(seconds=1),
        )
        assert result is not None


def _manifest_path(database: SQLiteDatabase, job_id: UUID) -> Path:
    return (
        database.data_root_identity.canonical_path
        / "database"
        / "manifests"
        / f"{job_id}.json"
    )


def _current_pages(database: SQLiteDatabase, job: DownloadJob) -> tuple[DownloadPage, ...]:
    repository = DownloadPageRepository()
    with database.read_session() as session:
        current_job = DownloadJobRepository().get(session, job.job_id)
        assert current_job is not None
        return repository.project_manifest(session, current_job).pages


def _part_path(database: SQLiteDatabase, relative_path: str) -> Path:
    return database.data_root_identity.canonical_path / "temp" / relative_path


def test_attempt_path_exposes_canonical_attempt_artifact_path() -> None:
    job_id = UUID("f7a2cfe3-62c5-4a80-b1b3-30364b62d012")

    assert DownloadPageRepository.attempt_path(job_id, 3, 2) == (
        f"jobs/{job_id}/pages/3/attempt-2.part"
    )


@pytest.mark.parametrize(
    ("ordinal", "attempt"),
    [
        (0, 1),
        (MAX_SQLITE_INTEGER + 1, 1),
        (1, 0),
        (1, MAX_SQLITE_INTEGER + 1),
    ],
    ids=("ordinal-zero", "ordinal-too-large", "attempt-zero", "attempt-too-large"),
)
def test_attempt_path_rejects_out_of_range_components(
    ordinal: int,
    attempt: int,
) -> None:
    job_id = UUID("f7a2cfe3-62c5-4a80-b1b3-30364b62d012")

    with pytest.raises(ValueError):
        DownloadPageRepository.attempt_path(job_id, ordinal, attempt)


def test_publish_snapshot_records_download_attempt_without_promoting_files(
    tmp_path: Path,
) -> None:
    database, job = _database_with_job(tmp_path)
    writer = _writer(database)
    original = _page(job.job_id, 1, "opaque-page-1")
    first = writer.replace_pending_pages(job.job_id, (original,))
    _transition_job(database, job.job_id, DownloadJobStatus.DOWNLOADING)

    first_part = f"jobs/{job.job_id}/pages/1/attempt-1.part"
    attempt1 = replace(
        original,
        status="DOWNLOADING",
        temporary_path=first_part,
        attempts=1,
    )
    started = writer.publish_snapshot(job.job_id, (attempt1,))

    assert started.generation == first.generation + 1
    assert started.manifest.pages == (attempt1,)
    assert _current_pages(database, job) == (attempt1,)
    assert _part_path(database, first_part).exists() is False


@pytest.mark.parametrize("changed_count", [1, 2])
def test_publish_snapshot_updates_only_changed_rows_and_publishes_manifest(
    tmp_path: Path,
    changed_count: int,
) -> None:
    database, job = _database_with_job(tmp_path)
    writer = _writer(database)
    original_pages = tuple(
        _page(job.job_id, ordinal, f"opaque-page-{ordinal}")
        for ordinal in range(1, 4)
    )
    writer.replace_pending_pages(job.job_id, original_pages)
    _transition_job(database, job.job_id, DownloadJobStatus.DOWNLOADING)
    changed_page = replace(
        original_pages[1],
        status="DOWNLOADING",
        temporary_path=f"jobs/{job.job_id}/pages/2/attempt-1.part",
        attempts=1,
    )
    proposed_pages = (
        replace(
            original_pages[0], status="DOWNLOADING", attempts=1,
            temporary_path=f"jobs/{job.job_id}/pages/1/attempt-1.part",
        ) if changed_count == 2 else original_pages[0],
        changed_page,
        original_pages[2],
    )
    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", record_statement)
    try:
        result = writer.publish_snapshot(
            job.job_id,
            proposed_pages,
        )
    finally:
        event.remove(database.engine, "before_cursor_execute", record_statement)

    page_dml = [
        statement.strip().lower()
        for statement in statements
        if "download_pages" in statement.lower()
        and statement.lstrip().split(maxsplit=1)[0].lower()
        in {"delete", "insert", "update"}
    ]
    assert len(page_dml) == changed_count
    assert all(statement.startswith("update") for statement in page_dml)
    assert _current_pages(database, job) == proposed_pages
    assert result.manifest_bytes == _manifest_path(database, job.job_id).read_bytes()
    assert hashlib.sha256(result.manifest_bytes).hexdigest() == result.manifest_sha256
    with database.read_session() as session:
        state = session.get(DownloadManifestStateRow, str(job.job_id))
        assert state is not None
        assert state.publication_pending is False
        assert state.manifest_sha256 == result.manifest_sha256


def test_publish_snapshot_reserves_next_attempt_and_preserves_previous_part(
    tmp_path: Path,
) -> None:
    database, job = _database_with_job(tmp_path)
    writer = _writer(database)
    pending = _page(job.job_id, 1, "opaque-page-1")
    writer.replace_pending_pages(job.job_id, (pending,))
    _transition_job(database, job.job_id, DownloadJobStatus.DOWNLOADING)

    first_relative = f"jobs/{job.job_id}/pages/1/attempt-1.part"
    attempt1 = replace(
        pending,
        status="DOWNLOADING",
        temporary_path=first_relative,
        attempts=1,
    )
    writer.publish_snapshot(job.job_id, (attempt1,))
    previous_part = _part_path(database, first_relative)
    previous_part.parent.mkdir(parents=True, exist_ok=True)
    previous_part.write_bytes(b"ambiguous-first-attempt")

    second_relative = f"jobs/{job.job_id}/pages/1/attempt-2.part"
    attempt2 = replace(attempt1, temporary_path=second_relative, attempts=2)
    result = writer.publish_snapshot(job.job_id, (attempt2,))

    assert result.manifest.pages == (attempt2,)
    assert previous_part.read_bytes() == b"ambiguous-first-attempt"
    assert _part_path(database, second_relative).exists() is False


def test_publish_snapshot_records_downloaded_then_validated_metadata(
    tmp_path: Path,
) -> None:
    database, job = _database_with_job(tmp_path)
    writer = _writer(database)
    pending = _page(job.job_id, 1, "opaque-page-1")
    writer.replace_pending_pages(job.job_id, (pending,))
    _transition_job(database, job.job_id, DownloadJobStatus.DOWNLOADING)

    part_relative = f"jobs/{job.job_id}/pages/1/attempt-1.part"
    downloading = replace(
        pending,
        status="DOWNLOADING",
        temporary_path=part_relative,
        attempts=1,
    )
    writer.publish_snapshot(job.job_id, (downloading,))
    body = b"verified-page-bytes"
    part_path = _part_path(database, part_relative)
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.write_bytes(body)

    downloaded = replace(
        downloading,
        status="DOWNLOADED",
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )
    downloaded_result = writer.publish_snapshot(job.job_id, (downloaded,))
    assert downloaded_result.manifest.pages == (downloaded,)
    assert part_path.read_bytes() == body

    _transition_job(database, job.job_id, DownloadJobStatus.VALIDATING)
    final_relative = f"jobs/{job.job_id}/pages/1/page.webp"
    final_path = _part_path(database, final_relative)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(body)
    validated = replace(
        downloaded,
        status="VALIDATED",
        temporary_path=final_relative,
        width=11,
        height=13,
        mime="image/webp",
    )

    validated_result = writer.publish_snapshot(job.job_id, (validated,))

    assert validated_result.manifest.pages == (validated,)
    assert validated_result.generation == downloaded_result.generation + 1
    assert part_path.read_bytes() == body
    assert final_path.read_bytes() == body


@pytest.mark.parametrize(
    ("attempts", "path"),
    [
        (0, None),
        (3, "attempt-3.part"),
        (2, "attempt-1.part"),
    ],
    ids=("attempt-decrease", "attempt-skip", "path-reuse"),
)
def test_retry_snapshot_rejects_decreasing_skipped_or_reused_attempts(
    tmp_path: Path,
    attempts: int,
    path: str | None,
) -> None:
    database, job = _database_with_job(tmp_path)
    writer = _writer(database)
    pending = _page(job.job_id, 1, "opaque-page-1")
    writer.replace_pending_pages(job.job_id, (pending,))
    _transition_job(database, job.job_id, DownloadJobStatus.DOWNLOADING)
    current_path = f"jobs/{job.job_id}/pages/1/attempt-1.part"
    current = replace(
        pending,
        status="DOWNLOADING",
        temporary_path=current_path,
        attempts=1,
    )
    writer.publish_snapshot(job.job_id, (current,))
    target = _manifest_path(database, job.job_id).read_bytes()
    before_state: tuple[object, ...]
    with database.read_session() as session:
        state = session.get(DownloadManifestStateRow, str(job.job_id))
        assert state is not None
        before_state = (
            state.generation,
            state.manifest_sha256,
            state.manifest_size_bytes,
            state.previous_sha256,
            state.publication_pending,
        )
    relative_path = (
        f"jobs/{job.job_id}/pages/1/{path}" if path and "/" not in path else path
    )
    invalid = replace(current, attempts=attempts, temporary_path=relative_path)

    with pytest.raises(PageManifestWriteBlocked):
        writer.publish_snapshot(job.job_id, (invalid,))

    assert _current_pages(database, job) == (current,)
    assert _manifest_path(database, job.job_id).read_bytes() == target
    with database.read_session() as session:
        state = session.get(DownloadManifestStateRow, str(job.job_id))
        assert state is not None
        assert (
            state.generation,
            state.manifest_sha256,
            state.manifest_size_bytes,
            state.previous_sha256,
            state.publication_pending,
        ) == before_state


def test_snapshot_rejects_reordered_ordinals_and_remote_references_without_mutation(
    tmp_path: Path,
) -> None:
    database, job = _database_with_job(tmp_path)
    writer = _writer(database)
    pages = (
        _page(job.job_id, 1, "remote-a"),
        _page(job.job_id, 2, "remote-b"),
    )
    writer.replace_pending_pages(job.job_id, pages)
    _transition_job(database, job.job_id, DownloadJobStatus.DOWNLOADING)
    target_before = _manifest_path(database, job.job_id).read_bytes()
    saved_pages = _current_pages(database, job)

    with pytest.raises(PageManifestWriteBlocked):
        writer.publish_snapshot(job.job_id, (saved_pages[1], saved_pages[0]))
    with pytest.raises(PageManifestWriteBlocked):
        writer.publish_snapshot(
            job.job_id,
            (
                replace(saved_pages[0], remote_reference="remote-b"),
                replace(saved_pages[1], remote_reference="remote-a"),
            ),
        )

    assert _current_pages(database, job) == saved_pages
    assert _manifest_path(database, job.job_id).read_bytes() == target_before


def test_failed_and_interrupted_page_states_must_match_terminal_job_state(
    tmp_path: Path,
) -> None:
    database, job = _database_with_job(tmp_path)
    writer = _writer(database)
    pending = _page(job.job_id, 1, "opaque-page-1")
    writer.replace_pending_pages(job.job_id, (pending,))
    _transition_job(database, job.job_id, DownloadJobStatus.DOWNLOADING)
    downloading_path = f"jobs/{job.job_id}/pages/1/attempt-1.part"
    downloading = replace(
        pending,
        status="DOWNLOADING",
        temporary_path=downloading_path,
        attempts=1,
    )
    writer.publish_snapshot(job.job_id, (downloading,))

    failed_page = replace(downloading, status="FAILED")
    with pytest.raises(PageManifestWriteBlocked):
        writer.publish_snapshot(job.job_id, (failed_page,))
    assert _current_pages(database, job) == (downloading,)

    _transition_job(database, job.job_id, DownloadJobStatus.FAILED)
    failed = writer.publish_snapshot(job.job_id, (failed_page,))
    assert failed.manifest.pages == (failed_page,)


def test_interrupted_page_state_requires_job_interrupted_from_page_pipeline(
    tmp_path: Path,
) -> None:
    database, job = _database_with_job(tmp_path)
    writer = _writer(database)
    pending = _page(job.job_id, 1, "opaque-page-1")
    writer.replace_pending_pages(job.job_id, (pending,))
    _transition_job(database, job.job_id, DownloadJobStatus.DOWNLOADING)
    downloading = replace(
        pending,
        status="DOWNLOADING",
        temporary_path=f"jobs/{job.job_id}/pages/1/attempt-1.part",
        attempts=1,
    )
    writer.publish_snapshot(job.job_id, (downloading,))
    _interrupt_job(database, job.job_id)

    interrupted = replace(downloading, status="INTERRUPTED")
    result = writer.publish_snapshot(job.job_id, (interrupted,))

    assert result.manifest.pages == (interrupted,)


def test_publish_snapshot_precommit_fault_rolls_back_rows_and_pending_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, job = _database_with_job(tmp_path)
    writer = _writer(database)
    pending = _page(job.job_id, 1, "opaque-page-1")
    writer.replace_pending_pages(job.job_id, (pending,))
    _transition_job(database, job.job_id, DownloadJobStatus.DOWNLOADING)
    before_pages = _current_pages(database, job)
    before_manifest = _manifest_path(database, job.job_id).read_bytes()
    original = DownloadPageRepository.publish_pages_for_manifest_publication

    def mutate_then_fail(
        repository: DownloadPageRepository,
        session: Session,
        *,
        job_id: UUID,
        expected_job: DownloadJob,
        expected_pages: tuple[DownloadPage, ...],
        pages: tuple[DownloadPage, ...],
        reservation: ManifestWriteReservation,
        pending_state: DownloadManifestState,
    ) -> None:
        original(
            repository,
            session,
            job_id=job_id,
            expected_job=expected_job,
            expected_pages=expected_pages,
            pages=pages,
            reservation=reservation,
            pending_state=pending_state,
        )
        raise RuntimeError("injected precommit failure")

    monkeypatch.setattr(
        DownloadPageRepository,
        "publish_pages_for_manifest_publication",
        mutate_then_fail,
    )
    started = replace(
        pending,
        status="DOWNLOADING",
        temporary_path=f"jobs/{job.job_id}/pages/1/attempt-1.part",
        attempts=1,
    )

    with pytest.raises(RuntimeError, match="injected precommit failure"):
        writer.publish_snapshot(job.job_id, (started,))

    assert _current_pages(database, job) == before_pages
    assert _manifest_path(database, job.job_id).read_bytes() == before_manifest
    with database.read_session() as session:
        state = session.get(DownloadManifestStateRow, str(job.job_id))
    assert state is not None and state.publication_pending is False


def test_publish_snapshot_manifest_fault_keeps_reconcilable_pending_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, job = _database_with_job(tmp_path)
    writer = _writer(database)
    pending = _page(job.job_id, 1, "opaque-page-1")
    writer.replace_pending_pages(job.job_id, (pending,))
    _transition_job(database, job.job_id, DownloadJobStatus.DOWNLOADING)
    before_manifest = _manifest_path(database, job.job_id).read_bytes()
    started = replace(
        pending,
        status="DOWNLOADING",
        temporary_path=f"jobs/{job.job_id}/pages/1/attempt-1.part",
        attempts=1,
    )

    def fail_manifest_save(*_args: object, **_kwargs: object) -> str:
        raise OSError("injected manifest publication failure")

    monkeypatch.setattr(writer.manifest_store, "save", fail_manifest_save)
    with pytest.raises(OSError, match="injected manifest publication failure"):
        writer.publish_snapshot(job.job_id, (started,))

    assert _current_pages(database, job) == (started,)
    assert _manifest_path(database, job.job_id).read_bytes() == before_manifest
    with database.read_session() as session:
        state = session.get(DownloadManifestStateRow, str(job.job_id))
    assert state is not None and state.publication_pending is True

    with pytest.raises(PageManifestWriteBlocked):
        writer.publish_snapshot(job.job_id, (started,))
