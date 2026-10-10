"""Normal, metadata-only SQLite to manifest publication for one download job."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.domain.download_job import DownloadJob, DownloadJobStatus
from app.domain.download_manifest_state import DownloadManifestState
from app.domain.download_page import DownloadPage
from app.domain.job_manifest import (
    MAX_MANIFEST_PAGE_COUNT,
    DownloadJobManifest,
    InvalidManifestError,
    manifest_to_bytes,
)
from app.errors import ErrorCode
from app.persistence.database import SQLiteDatabase
from app.persistence.manifest_store import JobManifestStore, ManifestWriteReservation
from app.persistence.repositories.download_job import DownloadJobRepository
from app.persistence.repositories.download_manifest_state import (
    DownloadManifestStateRepository,
)
from app.persistence.repositories.download_page import DownloadPageRepository


def _is_sqlite_database_instance(value: object) -> bool:
    return isinstance(value, SQLiteDatabase)


class PageManifestWriteBlocked(RuntimeError):
    """The existing durable state is not safe for a new metadata publication."""

    code = ErrorCode.DL_JOB_RECOVERY_BLOCKED
    retryable = False

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("Page metadata publication is blocked by durable state.")


class StalePageManifestSnapshot(RuntimeError):
    """A database or manifest snapshot changed before the writer's CAS."""

    code = ErrorCode.DL_MANIFEST_STALE_SNAPSHOT
    retryable = False

    def __init__(self) -> None:
        super().__init__("The page manifest snapshot changed during publication.")


@dataclass(frozen=True, slots=True)
class PageManifestWriteResult:
    job_id: UUID
    generation: int
    manifest: DownloadJobManifest
    manifest_bytes: bytes
    manifest_sha256: str
    manifest_size_bytes: int
    publication_pending: bool


@dataclass(frozen=True, slots=True)
class _BeforeSnapshot:
    job: DownloadJob
    manifest: DownloadJobManifest
    manifest_bytes: bytes
    manifest_sha256: str
    state: DownloadManifestState | None
    current_generation: int


class PageManifestWriter:
    """Atomically persist pending page metadata and its canonical manifest."""

    _MUTABLE_JOB_STATUSES = frozenset({DownloadJobStatus.FETCHING_METADATA})
    _PAGE_PUBLICATION_JOB_STATUSES = frozenset(
        {
            DownloadJobStatus.DOWNLOADING,
            DownloadJobStatus.VALIDATING,
            DownloadJobStatus.FAILED,
            DownloadJobStatus.INTERRUPTED,
        }
    )

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        manifest_store: JobManifestStore,
        job_repository: DownloadJobRepository | None = None,
        page_repository: DownloadPageRepository | None = None,
        state_repository: DownloadManifestStateRepository | None = None,
    ) -> None:
        if not _is_sqlite_database_instance(database):
            raise TypeError("database must be a SQLiteDatabase")
        if type(manifest_store) is not JobManifestStore:
            raise TypeError("manifest_store must be a JobManifestStore")
        if manifest_store.data_root_identity is not database.data_root_identity:
            raise ValueError("database and manifest store must share one root identity")
        self.database = database
        self.manifest_store = manifest_store
        self.job_repository = (
            job_repository if job_repository is not None else DownloadJobRepository()
        )
        self.page_repository = (
            page_repository if page_repository is not None else DownloadPageRepository()
        )
        self.page_repository.bind_reservation_validator(
            manifest_store.assert_write_reservation_issued
        )
        self.state_repository = (
            state_repository
            if state_repository is not None
            else DownloadManifestStateRepository()
        )

    def replace_pending_pages(
        self,
        job_id: UUID,
        pages: tuple[DownloadPage, ...],
    ) -> PageManifestWriteResult:
        """Publish a complete metadata-only PENDING snapshot for one job."""
        self._validate_job_id(job_id)
        if type(pages) is not tuple:
            raise TypeError("pages must be a tuple of DownloadPage values")
        if len(pages) > MAX_MANIFEST_PAGE_COUNT:
            raise PageManifestWriteBlocked("page_count_limit")
        if any(type(page) is not DownloadPage for page in pages):
            raise TypeError("pages must be a tuple of DownloadPage values")

        with self.manifest_store.lock_job(job_id):
            before = self._read_before_snapshot(job_id)
            self._assert_metadata_only(before.manifest.pages)
            self._assert_metadata_only(pages)
            self._assert_mutable_job(before.job)
            try:
                proposed = DownloadJobManifest(
                    job_id=job_id,
                    source_id=before.manifest.source_id,
                    chapter_id=before.manifest.chapter_id,
                    expected_pages=len(pages),
                    pages=pages,
                )
                proposed_bytes = manifest_to_bytes(proposed)
            except (InvalidManifestError, TypeError, ValueError) as error:
                raise PageManifestWriteBlocked("proposed_manifest_invalid") from error
            return self._publish_proposed_snapshot(
                job_id,
                before=before,
                proposed=proposed,
                proposed_bytes=proposed_bytes,
                allowed_job_statuses=self._MUTABLE_JOB_STATUSES,
            )

    def publish_snapshot(
        self,
        job_id: UUID,
        pages: tuple[DownloadPage, ...],
    ) -> PageManifestWriteResult:
        """Publish one monotonic page-state snapshot for an active download job."""
        self._validate_job_id(job_id)
        if type(pages) is not tuple:
            raise TypeError("pages must be a tuple of DownloadPage values")
        if len(pages) > MAX_MANIFEST_PAGE_COUNT:
            raise PageManifestWriteBlocked("page_count_limit")
        if any(type(page) is not DownloadPage for page in pages):
            raise TypeError("pages must be a tuple of DownloadPage values")

        with self.manifest_store.lock_job(job_id):
            before = self._read_before_snapshot(job_id)
            allowed_job_statuses = self._PAGE_PUBLICATION_JOB_STATUSES
            self._assert_mutable_job(before.job, allowed_job_statuses)
            try:
                self.page_repository.assert_snapshot_transition(
                    before.job,
                    before.manifest.pages,
                    pages,
                )
                proposed = DownloadJobManifest(
                    job_id=job_id,
                    source_id=before.manifest.source_id,
                    chapter_id=before.manifest.chapter_id,
                    expected_pages=len(before.manifest.pages),
                    pages=pages,
                )
                proposed_bytes = manifest_to_bytes(proposed)
            except (InvalidManifestError, TypeError, ValueError) as error:
                reason = (
                    str(error)
                    if isinstance(error, ValueError)
                    else "proposed_manifest_invalid"
                )
                raise PageManifestWriteBlocked(reason) from error
            return self._publish_proposed_snapshot(
                job_id,
                before=before,
                proposed=proposed,
                proposed_bytes=proposed_bytes,
                allowed_job_statuses=frozenset({before.job.status}),
            )

    def _publish_proposed_snapshot(
        self,
        job_id: UUID,
        *,
        before: _BeforeSnapshot,
        proposed: DownloadJobManifest,
        proposed_bytes: bytes,
        allowed_job_statuses: frozenset[DownloadJobStatus],
    ) -> PageManifestWriteResult:
        proposed_sha256 = hashlib.sha256(proposed_bytes).hexdigest()
        proposed_size = len(proposed_bytes)
        self._assert_state_matches_projection(before)
        if before.state is not None:
            if before.state.generation > 1 and before.state.previous_sha256 is None:
                raise PageManifestWriteBlocked("confirmed_state_backup_digest_missing")
            self.manifest_store.prune_obsolete_backups(
                job_id,
                current_generation=before.state.generation,
                current_sha256=before.state.manifest_sha256,
                current_size_bytes=before.state.manifest_size_bytes,
                previous_sha256=before.state.previous_sha256,
            )
        retained_backup_sha256 = (
            before.state.previous_sha256 if before.state is not None else None
        )
        reservation = self.manifest_store.reserve_write(
            job_id=job_id,
            expected_current_generation=before.current_generation,
            current_sha256=before.manifest_sha256,
            current_size_bytes=len(before.manifest_bytes),
            new_sha256=proposed_sha256,
            new_size_bytes=proposed_size,
            retained_backup_sha256=retained_backup_sha256,
        )
        self._assert_reservation_matches(
            reservation,
            before=before,
            proposed_sha256=proposed_sha256,
            proposed_size=proposed_size,
        )
        pending_state = DownloadManifestState(
            job_id=job_id,
            generation=reservation.new_generation,
            manifest_sha256=proposed_sha256,
            manifest_size_bytes=proposed_size,
            previous_sha256=reservation.target_sha256,
            publication_pending=True,
        )

        self._commit_pending_snapshot(
            job_id,
            before=before,
            proposed=proposed,
            proposed_bytes=proposed_bytes,
            pending_state=pending_state,
            reservation=reservation,
            allowed_job_statuses=allowed_job_statuses,
        )

        published_sha256 = self.manifest_store.save(
            proposed,
            reservation=reservation,
        )
        if published_sha256 != proposed_sha256:
            raise StalePageManifestSnapshot()
        self.manifest_store.verify_published(reservation)
        self._close_pending_snapshot(
            job_id,
            expected_job=before.job,
            expected_manifest=proposed,
            expected_bytes=proposed_bytes,
            expected_state=pending_state,
        )
        self.manifest_store.prune_retained_backup(reservation)

        return PageManifestWriteResult(
            job_id=job_id,
            generation=reservation.new_generation,
            manifest=proposed,
            manifest_bytes=proposed_bytes,
            manifest_sha256=proposed_sha256,
            manifest_size_bytes=proposed_size,
            publication_pending=False,
        )

    def _read_before_snapshot(self, job_id: UUID) -> _BeforeSnapshot:
        try:
            with self.database.read_session() as session:
                job = self.job_repository.get(session, job_id)
                if job is None:
                    raise PageManifestWriteBlocked("job_missing")
                manifest = self.page_repository.project_manifest(session, job)
                state = self.state_repository.get(session, job_id)
        except PageManifestWriteBlocked:
            raise
        except (InvalidManifestError, TypeError, ValueError) as error:
            raise PageManifestWriteBlocked("current_projection_invalid") from error

        try:
            serialized = manifest_to_bytes(manifest)
        except (InvalidManifestError, TypeError, ValueError) as error:
            raise PageManifestWriteBlocked("current_projection_invalid") from error
        if state is not None and state.publication_pending:
            raise PageManifestWriteBlocked("publication_pending")
        if state is not None and (
            state.generation < 1
            or state.manifest_sha256 != hashlib.sha256(serialized).hexdigest()
            or state.manifest_size_bytes != len(serialized)
        ):
            raise PageManifestWriteBlocked("confirmed_state_diverges_from_database")
        return _BeforeSnapshot(
            job=job,
            manifest=manifest,
            manifest_bytes=serialized,
            manifest_sha256=hashlib.sha256(serialized).hexdigest(),
            state=state,
            current_generation=state.generation if state is not None else 0,
        )

    def _assert_state_matches_projection(self, before: _BeforeSnapshot) -> None:
        if before.state is not None and before.state.publication_pending:
            raise PageManifestWriteBlocked("publication_pending")
        if before.state is not None and (
            before.state.manifest_sha256 != before.manifest_sha256
            or before.state.manifest_size_bytes != len(before.manifest_bytes)
        ):
            raise PageManifestWriteBlocked("confirmed_state_diverges_from_database")

    def _commit_pending_snapshot(
        self,
        job_id: UUID,
        *,
        before: _BeforeSnapshot,
        proposed: DownloadJobManifest,
        proposed_bytes: bytes,
        pending_state: DownloadManifestState,
        reservation: ManifestWriteReservation,
        allowed_job_statuses: frozenset[DownloadJobStatus],
    ) -> None:
        with self.database.write_session() as session:
            self._assert_database_snapshot_current(
                session,
                job_id,
                before,
                allowed_job_statuses=allowed_job_statuses,
            )
            if pending_state.generation != before.current_generation + 1:
                raise StalePageManifestSnapshot()
            if (
                reservation.new_generation != before.current_generation + 1
                or pending_state.generation != reservation.new_generation
                or pending_state.previous_sha256 != reservation.target_sha256
            ):
                raise StalePageManifestSnapshot()
            if not self.state_repository.mark_publication_pending(
                session,
                expected_state=before.state,
                new_state=pending_state,
            ):
                raise StalePageManifestSnapshot()
            if allowed_job_statuses == self._MUTABLE_JOB_STATUSES:
                self.page_repository.replace_pages_for_manifest_publication(
                    session,
                    job_id=job_id,
                    pages=proposed.pages,
                    reservation=reservation,
                    pending_state=pending_state,
                )
            else:
                self.page_repository.publish_pages_for_manifest_publication(
                    session,
                    job_id=job_id,
                    expected_job=before.job,
                    expected_pages=before.manifest.pages,
                    pages=proposed.pages,
                    reservation=reservation,
                    pending_state=pending_state,
                )
            job = self.job_repository.get(session, job_id)
            if job is None:
                raise StalePageManifestSnapshot()
            projected = self.page_repository.project_manifest(session, job)
            if manifest_to_bytes(projected) != proposed_bytes:
                raise StalePageManifestSnapshot()

    def _assert_database_snapshot_current(
        self,
        session: Session,
        job_id: UUID,
        before: _BeforeSnapshot,
        *,
        allowed_job_statuses: frozenset[DownloadJobStatus],
    ) -> None:
        job = self.job_repository.get(session, job_id)
        if job is None or job != before.job:
            raise StalePageManifestSnapshot()
        current_manifest = self.page_repository.project_manifest(session, job)
        try:
            current_bytes = manifest_to_bytes(current_manifest)
        except (InvalidManifestError, TypeError, ValueError) as error:
            raise PageManifestWriteBlocked("current_projection_invalid") from error
        if current_bytes != before.manifest_bytes:
            raise StalePageManifestSnapshot()
        current_state = self.state_repository.get(session, job_id)
        if current_state != before.state:
            raise StalePageManifestSnapshot()
        self._assert_mutable_job(job, allowed_job_statuses)

    def _close_pending_snapshot(
        self,
        job_id: UUID,
        *,
        expected_job: DownloadJob,
        expected_manifest: DownloadJobManifest,
        expected_bytes: bytes,
        expected_state: DownloadManifestState,
    ) -> None:
        with self.database.write_session() as session:
            job = self.job_repository.get(session, job_id)
            if job is None or job != expected_job:
                raise StalePageManifestSnapshot()
            projected = self.page_repository.project_manifest(session, job)
            try:
                projected_bytes = manifest_to_bytes(projected)
            except (InvalidManifestError, TypeError, ValueError) as error:
                raise PageManifestWriteBlocked("postcommit_projection_invalid") from error
            if projected != expected_manifest or projected_bytes != expected_bytes:
                raise StalePageManifestSnapshot()
            current_state = self.state_repository.get(session, job_id)
            if current_state != expected_state:
                raise StalePageManifestSnapshot()
            if not self.state_repository.close_publication(
                session,
                expected_pending_state=expected_state,
            ):
                raise StalePageManifestSnapshot()

    @staticmethod
    def _assert_reservation_matches(
        reservation: ManifestWriteReservation,
        *,
        before: _BeforeSnapshot,
        proposed_sha256: str,
        proposed_size: int,
    ) -> None:
        if (
            reservation.job_id != before.job.job_id
            or reservation.expected_current_generation != before.current_generation
            or reservation.new_generation != before.current_generation + 1
            or reservation.current_sha256 != before.manifest_sha256
            or reservation.current_size_bytes != len(before.manifest_bytes)
            or reservation.new_sha256 != proposed_sha256
            or reservation.new_size_bytes != proposed_size
            or reservation.retained_backup_sha256
            != (before.state.previous_sha256 if before.state is not None else None)
            or (before.state is not None and reservation.target_sha256 is None)
        ):
            raise StalePageManifestSnapshot()

    @staticmethod
    def _assert_metadata_only(pages: tuple[DownloadPage, ...]) -> None:
        for page in pages:
            if (
                page.status != "PENDING"
                or page.temporary_path is not None
                or page.size_bytes is not None
                or page.sha256 is not None
                or page.width is not None
                or page.height is not None
                or page.mime is not None
                or page.attempts != 0
            ):
                raise PageManifestWriteBlocked("metadata_only_pages_required")

    @classmethod
    def _assert_mutable_job(
        cls,
        job: DownloadJob,
        allowed_statuses: frozenset[DownloadJobStatus] | None = None,
    ) -> None:
        mutable_statuses = (
            allowed_statuses
            if allowed_statuses is not None
            else cls._MUTABLE_JOB_STATUSES
        )
        if (
            job.status not in mutable_statuses
            or job.recovery_blocked
            or (
                job.status
                in {
                    DownloadJobStatus.FETCHING_METADATA,
                    DownloadJobStatus.DOWNLOADING,
                    DownloadJobStatus.VALIDATING,
                }
                and job.cancel_requested_at is not None
            )
        ):
            raise PageManifestWriteBlocked("job_not_mutable")

    @staticmethod
    def _validate_job_id(job_id: UUID) -> None:
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
