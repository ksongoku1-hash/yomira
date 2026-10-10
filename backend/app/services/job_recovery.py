"""Coordinate per-job interruption and crash recovery."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from app.domain.download_job import DownloadJob, DownloadJobStatus
from app.domain.download_manifest_state import DownloadManifestState
from app.domain.download_page import MAX_DOWNLOAD_PAGE_ATTEMPTS, DownloadPage
from app.domain.job_manifest import (
    DownloadJobManifest,
    InvalidManifestError,
    manifest_to_bytes,
)
from app.persistence.database import SQLiteDatabase
from app.persistence.manifest_store import JobManifestStore, TemporaryPageStore
from app.persistence.mappers.download_manifest_state import download_manifest_state_from_row
from app.persistence.models.download_manifest_state import DownloadManifestStateRow
from app.persistence.repositories.download_job import DownloadJobRepository
from app.persistence.repositories.download_manifest_state import (
    DownloadManifestStateRepository,
)
from app.persistence.repositories.download_page import DownloadPageRepository
from app.persistence.windows_paths import DataRootIdentity

_recovery_guard_suppressed_jobs: ContextVar[frozenset[UUID]] = ContextVar(
    "yomira_recovery_guard_suppressed_jobs",
    default=frozenset(),
)


@dataclass(frozen=True, slots=True)
class WorkerQuiescenceAck:
    """K096 confirmation that a worker can no longer mutate a job."""

    job_id: UUID

    def __post_init__(self) -> None:
        if type(self.job_id) is not UUID:
            raise TypeError("job_id must be a UUID")
        if self.job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")


class RecoveryGuardBlockedError(RuntimeError):
    """A manifest operation was stopped because the job is not safe to mutate."""

    def __init__(self) -> None:
        super().__init__("The interrupted job requires recovery before manifest I/O.")


class DownloadPlanUnavailableError(RuntimeError):
    """The durable page plan could not be reconciled without guessing."""

    def __init__(self) -> None:
        super().__init__("The durable download plan is not safe to resume.")


@dataclass(frozen=True, slots=True)
class _DownloadPlanSnapshot:
    manifest: DownloadJobManifest
    state: DownloadManifestState | None


class RecoveryCoordinator:
    """Run recovery decisions behind the manifest store's per-job mutex."""

    def __init__(
        self,
        database: SQLiteDatabase,
        data_root_identity: DataRootIdentity,
    ) -> None:
        if type(data_root_identity) is not DataRootIdentity:
            raise TypeError("data_root_identity must be a DataRootIdentity")
        if database.data_root_identity is not data_root_identity:
            raise ValueError("database and coordinator must share one root identity")

        self.database = database
        self.data_root_identity = data_root_identity
        self.repository = DownloadJobRepository()
        self.manifest_state_repository = DownloadManifestStateRepository()
        self.page_repository = DownloadPageRepository()
        self.manifest_store = JobManifestStore(
            data_root_identity.canonical_path,
            data_root_identity=data_root_identity,
            recovery_guard=self._recovery_guard,
            storage_gate=database.storage_gate,
        )
        self.temporary_page_store = TemporaryPageStore(
            data_root_identity.canonical_path,
            data_root_identity=data_root_identity,
            storage_gate=database.storage_gate,
        )

    def interrupt_quiesced_job(
        self,
        job_id: UUID,
        expected_snapshot: DownloadJob,
        *,
        quiescence_ack: WorkerQuiescenceAck,
        recovery_blocked: bool | None = None,
        storage_gate_quiesced: bool = False,
    ) -> None:
        """Persist active→interrupted only after K096 confirms quiescence."""
        self._validate_job_id(job_id)
        if type(expected_snapshot) is not DownloadJob:
            raise TypeError("expected_snapshot must be a DownloadJob")
        if type(quiescence_ack) is not WorkerQuiescenceAck:
            raise TypeError("quiescence_ack must be a WorkerQuiescenceAck")
        if expected_snapshot.job_id != job_id:
            raise ValueError("expected_snapshot belongs to a different job")
        if quiescence_ack.job_id != job_id:
            raise ValueError("quiescence_ack belongs to a different job")
        if recovery_blocked is not None and type(recovery_blocked) is not bool:
            raise TypeError("recovery_blocked must be a bool or None")
        if type(storage_gate_quiesced) is not bool:
            raise TypeError("storage_gate_quiesced must be a bool")
        if storage_gate_quiesced and not self.database.storage_gate.closed:
            raise ValueError("storage_gate_quiesced requires a closed shared gate")

        # lock_job invokes the configured guard after acquiring the mutex.
        # This operation already has its own quiescence evidence and CAS, so
        # suppress only that same-job callback while the lock is held.
        with self._suppress_recovery_guard(job_id):
            with self.manifest_store.lock_job(job_id):
                write_session = (
                    self.database.quiesced_recovery_write_session(
                        job_id=job_id,
                        quiescence_ack=quiescence_ack,
                    )
                    if storage_gate_quiesced
                    else self.database.write_session()
                )
                with write_session as session:
                    self.repository.interrupt_active_job(
                        session,
                        current_job=expected_snapshot,
                        interrupted_at=datetime.now(UTC),
                        recovery_blocked=recovery_blocked,
                    )

    def recover_job(
        self,
        job_id: UUID,
        *,
        quiescence_ack: WorkerQuiescenceAck | None = None,
    ) -> None:
        """Attempt a conservative retry only when no durable output exists."""
        self._validate_job_id(job_id)
        if quiescence_ack is not None:
            if type(quiescence_ack) is not WorkerQuiescenceAck:
                raise TypeError("quiescence_ack must be a WorkerQuiescenceAck")
            if quiescence_ack.job_id != job_id:
                raise ValueError("quiescence_ack belongs to a different job")
            if not self.database.storage_gate.closed:
                raise ValueError("quiesced recovery requires a closed shared gate")
        if job_id in _recovery_guard_suppressed_jobs.get():
            return

        # Mark before lock_job: its callback would otherwise recursively enter
        # recovery while the manifest mutex is already held.
        with self._suppress_recovery_guard(job_id):
            with self.manifest_store.lock_job(job_id):
                self._recover_job_locked(
                    job_id, allow_safe_retry=True, quiescence_ack=quiescence_ack
                )

    def read_verified_download_plan(
        self,
        job_id: UUID,
    ) -> DownloadJobManifest | None:
        """Read a queued worker's existing plan without reconstructing evidence."""
        self._validate_job_id(job_id)
        with self._suppress_recovery_guard(job_id):
            with self.manifest_store.lock_job(job_id):
                with self.database.read_session() as session:
                    job = self.repository.get(session, job_id)
                    if (
                        job is None
                        or job.status is not DownloadJobStatus.FETCHING_METADATA
                        or job.cancel_requested_at is not None
                    ):
                        raise DownloadPlanUnavailableError()
                    snapshot = self._read_download_plan_snapshot(session, job)
                try:
                    return self._verified_download_plan(job, snapshot)
                except (
                    OSError,
                    InvalidManifestError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ):
                    raise DownloadPlanUnavailableError() from None

    def reconcile_startup(self, *, batch_size: int = 64) -> int:
        """Interrupt stale workers with per-job CAS, then retry only verified starts."""
        if type(batch_size) is not int or not 1 <= batch_size <= 64:
            raise ValueError("batch_size must be between 1 and 64")

        interrupted_count = 0
        after_job_id: UUID | None = None
        interrupted_at = datetime.now(UTC)
        while True:
            with self.database.read_session() as session:
                batch = self.repository.list_active_jobs(
                    session,
                    limit=batch_size,
                    after_job_id=after_job_id,
                    include_unblocked_interrupted=True,
                )
            if not batch:
                return interrupted_count

            for active_job in batch:
                with self._suppress_recovery_guard(active_job.job_id):
                    with self.manifest_store.lock_job(active_job.job_id):
                        if active_job.status is DownloadJobStatus.INTERRUPTED:
                            # A restart may occur between interruption and resume CAS.
                            self._recover_job_locked(
                                active_job.job_id,
                                allow_safe_retry=True,
                            )
                        else:
                            with self.database.write_session() as session:
                                interrupted = self.repository.interrupt_active_job(
                                    session,
                                    current_job=active_job,
                                    interrupted_at=interrupted_at,
                                    recovery_blocked=(
                                        active_job.cancel_requested_at is not None
                                        or active_job.status
                                        not in {
                                            DownloadJobStatus.FETCHING_METADATA,
                                            DownloadJobStatus.DOWNLOADING,
                                        }
                                    ),
                                )
                            if interrupted is not None:
                                interrupted_count += 1
                                self._recover_job_locked(
                                    active_job.job_id,
                                    allow_safe_retry=True,
                                )
                after_job_id = active_job.job_id

    def _recovery_guard(self, job_id: UUID) -> None:
        """Callback used by JobManifestStore while its job mutex is held."""
        self._validate_job_id(job_id)
        if job_id in _recovery_guard_suppressed_jobs.get():
            return
        with self.database.read_session() as session:
            job = self.repository.get(session, job_id)
        if job is not None and job.status is DownloadJobStatus.INTERRUPTED:
            raise RecoveryGuardBlockedError()

    def _recover_job_locked(
        self,
        job_id: UUID,
        *,
        allow_safe_retry: bool,
        quiescence_ack: WorkerQuiescenceAck | None = None,
    ) -> bool:
        """Requeue only early-stage jobs with an empty, verified restart point."""
        with self.database.read_session() as session:
            job = self.repository.get(session, job_id)
            if job is None or job.status is not DownloadJobStatus.INTERRUPTED:
                return False
            if job.recovery_blocked:
                return False
            safe_stage = job.interrupted_from_status in {
                DownloadJobStatus.FETCHING_METADATA,
                DownloadJobStatus.DOWNLOADING,
            }
            eligible_for_retry = (
                allow_safe_retry
                and job.cancel_requested_at is None
                and safe_stage
            )
            snapshot = (
                self._read_download_plan_snapshot(session, job)
                if eligible_for_retry
                else None
            )

        if (
            eligible_for_retry
            and snapshot is not None
            and snapshot.state is not None
            and snapshot.state.publication_pending
        ):
            # The exceptional recovery permit authorizes only the final DB CAS.
            # Filesystem publication remains blocked until ordinary admission opens.
            snapshot = (
                self._replay_pending_snapshot_locked(job, snapshot)
                if quiescence_ack is None
                else None
            )

        safe_retry = eligible_for_retry and snapshot is not None and self._restart_point_is_clear(
            job,
            snapshot,
        )

        write_session = (
            self.database.quiesced_recovery_write_session(
                job_id=job_id, quiescence_ack=quiescence_ack
            )
            if quiescence_ack is not None
            else self.database.write_session()
        )
        if not safe_retry:
            with write_session as session:
                self.repository.block_interrupted_job(
                    session,
                    current_job=job,
                    changed_at=datetime.now(UTC),
                )
            return False

        with write_session as session:
            resumed = self.repository.resume_interrupted_job_for_safe_retry(
                session,
                current_job=job,
                resumed_at=datetime.now(UTC),
            )
        return resumed is not None

    def _replay_pending_snapshot_locked(
        self,
        expected_job: DownloadJob,
        snapshot: _DownloadPlanSnapshot,
    ) -> _DownloadPlanSnapshot | None:
        """Replay only a marker-authorized manifest while preserving fail-closed checks."""
        state = snapshot.state
        if state is None or not state.publication_pending:
            return None
        try:
            self.data_root_identity.assert_current()
            # Check page ownership before publishing the DB projection. This rejects
            # unknown artifacts and malformed page checkpoints without touching them.
            if not self._page_artifacts_are_classified(snapshot.manifest.pages):
                return None
            self.manifest_store.replay_pending_recovery_snapshot(
                snapshot.manifest,
                generation=state.generation,
                manifest_sha256=state.manifest_sha256,
                manifest_size_bytes=state.manifest_size_bytes,
                previous_sha256=state.previous_sha256,
            )
            self.data_root_identity.assert_current()
            with self.database.write_session() as session:
                current_job = self.repository.get(session, expected_job.job_id)
                current_snapshot = (
                    self._read_download_plan_snapshot(session, current_job)
                    if current_job is not None
                    else None
                )
                if (
                    current_job != expected_job
                    or current_snapshot is None
                    or current_snapshot.manifest != snapshot.manifest
                    or current_snapshot.state != state
                    or not self.manifest_state_repository.close_publication(
                        session,
                        expected_pending_state=state,
                    )
                ):
                    return None

            with self.database.read_session() as session:
                current_job = self.repository.get(session, expected_job.job_id)
                current_snapshot = (
                    self._read_download_plan_snapshot(session, current_job)
                    if current_job is not None
                    else None
                )
            if (
                current_job != expected_job
                or current_snapshot is None
                or current_snapshot.manifest != snapshot.manifest
                or current_snapshot.state is None
                or current_snapshot.state.publication_pending
            ):
                return None
            self.data_root_identity.assert_current()
            return current_snapshot
        except (OSError, InvalidManifestError, RuntimeError, TypeError, ValueError):
            return None

    def _restart_point_is_clear(
        self,
        job: DownloadJob,
        snapshot: _DownloadPlanSnapshot,
    ) -> bool:
        """Accept only an empty start or a fully reconciled page-download checkpoint."""
        try:
            self.data_root_identity.assert_current()
            if job.interrupted_from_status is DownloadJobStatus.FETCHING_METADATA and any(
                page.status != "PENDING" for page in snapshot.manifest.pages
            ):
                return False
            if job.interrupted_from_status not in {
                DownloadJobStatus.FETCHING_METADATA,
                DownloadJobStatus.DOWNLOADING,
            }:
                return False
            manifest = self._verified_download_plan(job, snapshot)
            if manifest is None:
                return True
            return self._page_artifacts_are_classified(manifest.pages)
        except (OSError, InvalidManifestError, RuntimeError, TypeError, ValueError):
            return False

    def _read_download_plan_snapshot(
        self,
        session: Session,
        job: DownloadJob,
    ) -> _DownloadPlanSnapshot:
        """Copy SQLite page and publication state before any filesystem access."""
        manifest = self.page_repository.project_manifest(session, job)
        state_row = session.get(DownloadManifestStateRow, str(job.job_id))
        state = (
            download_manifest_state_from_row(state_row)
            if state_row is not None
            else None
        )
        return _DownloadPlanSnapshot(manifest=manifest, state=state)

    def _verified_download_plan(
        self,
        job: DownloadJob,
        snapshot: _DownloadPlanSnapshot,
    ) -> DownloadJobManifest | None:
        """Reconcile DB projection, confirmed manifest, marker, and temp namespace."""
        manifest = snapshot.manifest
        state = snapshot.state
        if not manifest.pages and state is None:
            self.manifest_store.read_verified_recovery_snapshot(
                manifest,
                generation=None,
                manifest_sha256=None,
                manifest_size_bytes=None,
                previous_sha256=None,
            )
            if self._job_temp_root_is_present(job.job_id):
                raise DownloadPlanUnavailableError()
            return None

        if (
            not manifest.pages
            or state is None
            or state.publication_pending
            or state.generation < 1
            or (state.generation > 1 and state.previous_sha256 is None)
        ):
            raise DownloadPlanUnavailableError()
        if any(
            page.status not in {"PENDING", "DOWNLOADING", "DOWNLOADED"}
            for page in manifest.pages
        ):
            raise DownloadPlanUnavailableError()
        if not self._page_artifacts_are_classified(manifest.pages):
            raise DownloadPlanUnavailableError()
        projected_bytes = manifest_to_bytes(manifest)
        if (
            hashlib.sha256(projected_bytes).hexdigest() != state.manifest_sha256
            or len(projected_bytes) != state.manifest_size_bytes
        ):
            raise DownloadPlanUnavailableError()
        self.data_root_identity.assert_current()
        # A previous recovery may have closed its marker and stopped before
        # pruning the older retained backup. Cleanup is authorized only by this
        # confirmed DB projection and the current target/rollback pair.
        self.manifest_store.prune_obsolete_backups(
            job.job_id,
            current_generation=state.generation,
            current_sha256=state.manifest_sha256,
            current_size_bytes=state.manifest_size_bytes,
            previous_sha256=state.previous_sha256,
        )
        self.data_root_identity.assert_current()
        disk_manifest = self.manifest_store.read_verified_recovery_snapshot(
            manifest,
            generation=state.generation,
            manifest_sha256=state.manifest_sha256,
            manifest_size_bytes=state.manifest_size_bytes,
            previous_sha256=state.previous_sha256,
        )
        if disk_manifest is None:
            raise DownloadPlanUnavailableError()
        self.data_root_identity.assert_current()
        return disk_manifest

    def _page_artifacts_are_classified(
        self,
        pages: tuple[DownloadPage, ...],
    ) -> bool:
        """Reject unknown files while retaining every known attempt artifact."""
        root = self.data_root_identity.canonical_path
        expected_files: dict[str, DownloadPage] = {}
        for page in pages:
            if page.status == "PENDING":
                if page.attempts != 0 or page.temporary_path is not None:
                    return False
                continue
            if (
                page.status not in {"DOWNLOADING", "DOWNLOADED"}
                or not 1 <= page.attempts <= MAX_DOWNLOAD_PAGE_ATTEMPTS
            ):
                return False
            if (
                page.status == "DOWNLOADING"
                and page.attempts >= MAX_DOWNLOAD_PAGE_ATTEMPTS
            ):
                return False
            current_path = self.page_repository.attempt_path(
                page.job_id,
                page.ordinal,
                page.attempts,
            )
            if page.temporary_path != current_path:
                return False
            if page.status == "DOWNLOADING" and (
                page.size_bytes is not None
                or page.sha256 is not None
                or page.width is not None
                or page.height is not None
                or page.mime is not None
            ):
                return False
            if page.status == "DOWNLOADED" and (
                page.size_bytes is None
                or page.size_bytes <= 0
                or page.size_bytes > 67_108_864
                or page.sha256 is None
                or page.width is not None
                or page.height is not None
                or page.mime is not None
            ):
                return False
            for attempt in range(1, page.attempts + 1):
                path = self.page_repository.attempt_path(
                    page.job_id,
                    page.ordinal,
                    attempt,
                )
                prefix = f"jobs/{page.job_id}/"
                if not path.startswith(prefix):
                    return False
                expected_files[path.removeprefix(prefix)] = page

        job_ids = {page.job_id for page in pages}
        if len(job_ids) > 1:
            return False
        if not pages:
            return True
        job_id = next(iter(job_ids))
        job_root = root / "temp" / "jobs" / str(job_id)
        job_info, job_untrusted = self._safe_path_info(job_root, root)
        if job_untrusted:
            return False
        if job_info is None:
            return not any(page.status == "DOWNLOADED" for page in pages)
        if not stat.S_ISDIR(job_info.st_mode):
            return False

        expected_directories: set[str] = set()
        for relative_path in expected_files:
            parent = Path(relative_path).parent
            while parent.as_posix() != ".":
                expected_directories.add(parent.as_posix())
                parent = parent.parent

        stack: list[tuple[Path, str]] = [(job_root, "")]
        visited_entries = 0
        maximum_entries = (len(expected_files) * 2) + (len(pages) * 2) + 8
        seen_files: set[str] = set()
        while stack:
            directory, relative_directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        visited_entries += 1
                        if visited_entries > maximum_entries:
                            return False
                        relative_path = (
                            f"{relative_directory}/{entry.name}"
                            if relative_directory
                            else entry.name
                        )
                        information = entry.stat(follow_symlinks=False)
                        attributes = getattr(information, "st_file_attributes", 0)
                        if stat.S_ISLNK(information.st_mode) or attributes & getattr(
                            stat,
                            "FILE_ATTRIBUTE_REPARSE_POINT",
                            0x0400,
                        ):
                            return False
                        if stat.S_ISDIR(information.st_mode):
                            if relative_path not in expected_directories:
                                return False
                            stack.append((Path(entry.path), relative_path))
                            continue
                        if not stat.S_ISREG(information.st_mode):
                            return False
                        if relative_path not in expected_files:
                            return False
                        page = expected_files[relative_path]
                        if information.st_size < 0 or information.st_size > 67_108_864:
                            return False
                        if (
                            page.status == "DOWNLOADED"
                            and page.temporary_path is not None
                            and relative_path
                            == page.temporary_path.removeprefix(
                                f"jobs/{page.job_id}/"
                            )
                            and not self._downloaded_page_matches(
                                Path(entry.path),
                                page,
                                information,
                                root,
                            )
                        ):
                            return False
                        seen_files.add(relative_path)
            except (OSError, ValueError):
                return False

        return all(
            page.status != "DOWNLOADED"
            or (
                page.temporary_path is not None
                and page.temporary_path.removeprefix(f"jobs/{page.job_id}/")
                in seen_files
            )
            for page in pages
        )

    @staticmethod
    def _downloaded_page_matches(
        path: Path,
        page: DownloadPage,
        expected_info: os.stat_result,
        root: Path,
    ) -> bool:
        if page.size_bytes is None or page.sha256 is None:
            return False
        if (
            not stat.S_ISREG(expected_info.st_mode)
            or expected_info.st_size != page.size_bytes
        ):
            return False
        path_info, path_untrusted = RecoveryCoordinator._safe_path_info(path, root)
        if (
            path_untrusted
            or path_info is None
            or not stat.S_ISREG(path_info.st_mode)
            or path_info.st_size != page.size_bytes
        ):
            return False
        identity = RecoveryCoordinator._managed_identity(path_info)
        enumerated_identity = RecoveryCoordinator._managed_identity(expected_info)
        if identity is None or (
            enumerated_identity is not None and enumerated_identity != identity
        ):
            return False
        parent_info, parent_untrusted = RecoveryCoordinator._safe_path_info(
            path.parent,
            root,
        )
        if parent_untrusted or parent_info is None:
            return False
        parent_identity = RecoveryCoordinator._managed_identity(parent_info)
        if parent_identity is None:
            return False
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or RecoveryCoordinator._managed_identity(opened) != identity
                    or opened.st_size != page.size_bytes
                ):
                    return False
                total = 0
                while chunk := stream.read(65_536):
                    total += len(chunk)
                    if total > 67_108_864:
                        return False
                    digest.update(chunk)
                closed = os.fstat(stream.fileno())
            after_info, after_untrusted = RecoveryCoordinator._safe_path_info(path, root)
            after_parent, after_parent_untrusted = RecoveryCoordinator._safe_path_info(
                path.parent,
                root,
            )
        except OSError:
            return False
        return (
            total == page.size_bytes
            and digest.hexdigest() == page.sha256
            and stat.S_ISREG(closed.st_mode)
            and RecoveryCoordinator._managed_identity(closed) == identity
            and not after_untrusted
            and after_info is not None
            and RecoveryCoordinator._managed_identity(after_info) == identity
            and not after_parent_untrusted
            and after_parent is not None
            and RecoveryCoordinator._managed_identity(after_parent) == parent_identity
        )

    @staticmethod
    def _managed_identity(info: os.stat_result) -> tuple[int, int] | None:
        file_id = getattr(info, "st_ino", 0)
        if type(info.st_dev) is not int or type(file_id) is not int or file_id == 0:
            return None
        return info.st_dev, file_id

    @staticmethod
    def _safe_path_info(path: Path, root: Path) -> tuple[os.stat_result | None, bool]:
        """Return leaf metadata and whether any component is absent or untrusted."""
        try:
            components = path.relative_to(root).parts
        except ValueError:
            return None, True
        cursor = root
        leaf_info: os.stat_result | None = None
        for index, component in enumerate(components):
            cursor = cursor / component
            try:
                information = cursor.lstat()
            except FileNotFoundError:
                return None, False
            except OSError:
                return None, True
            attributes = getattr(information, "st_file_attributes", 0)
            if stat.S_ISLNK(information.st_mode) or attributes & getattr(
                stat,
                "FILE_ATTRIBUTE_REPARSE_POINT",
                0x0400,
            ):
                return None, True
            if index < len(components) - 1 and not stat.S_ISDIR(information.st_mode):
                return None, True
            leaf_info = information
        return leaf_info, False

    def _job_temp_root_is_present(self, job_id: UUID) -> bool:
        root = self.data_root_identity.canonical_path
        information, untrusted = self._safe_path_info(
            root / "temp" / "jobs" / str(job_id),
            root,
        )
        return untrusted or information is not None

    @staticmethod
    def _validate_job_id(job_id: UUID) -> None:
        if type(job_id) is not UUID:
            raise TypeError("job_id must be a UUID")
        if job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")

    @staticmethod
    @contextmanager
    def _suppress_recovery_guard(job_id: UUID) -> Generator[None]:
        suppressed = _recovery_guard_suppressed_jobs.get()
        token = _recovery_guard_suppressed_jobs.set(suppressed | {job_id})
        try:
            yield
        finally:
            _recovery_guard_suppressed_jobs.reset(token)
