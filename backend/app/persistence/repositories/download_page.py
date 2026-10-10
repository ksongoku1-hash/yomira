"""Bounded persistence projections for download page manifests."""

import hashlib
from collections.abc import Callable
from dataclasses import replace
from typing import cast
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.domain.download_job import DownloadJob, DownloadJobStatus
from app.domain.download_manifest_state import DownloadManifestState
from app.domain.download_page import DownloadPage
from app.domain.job_manifest import (
    MAX_MANIFEST_PAGE_COUNT,
    MAX_SQLITE_INTEGER,
    DownloadJobManifest,
    InvalidManifestError,
    manifest_to_bytes,
)
from app.persistence.manifest_store import ManifestWriteReservation
from app.persistence.mappers.download_job import download_job_from_row
from app.persistence.mappers.download_manifest_state import (
    download_manifest_state_from_row,
)
from app.persistence.mappers.download_page import download_page_from_row, download_page_to_row
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.download_manifest_state import DownloadManifestStateRow
from app.persistence.models.download_page import DownloadPageRow
from app.persistence.models.source import SourceRow


class DownloadPageRepository:
    """Read bounded manifests and gate page writes on a pending publication marker."""

    def __init__(
        self,
        *,
        reservation_validator: Callable[[ManifestWriteReservation], None] | None = None,
    ) -> None:
        self._reservation_validator = reservation_validator

    def bind_reservation_validator(
        self,
        validator: Callable[[ManifestWriteReservation], None],
    ) -> None:
        """Bind the owning manifest store's live-reservation validation gate."""
        if not callable(validator):
            raise TypeError("reservation validator must be callable")
        if (
            self._reservation_validator is not None
            and self._reservation_validator != validator
        ):
            raise ValueError("repository is already bound to another manifest store")
        self._reservation_validator = validator

    def replace_pages_for_manifest_publication(
        self,
        session: Session,
        *,
        job_id: UUID,
        pages: tuple[DownloadPage, ...],
        reservation: ManifestWriteReservation,
        pending_state: DownloadManifestState,
    ) -> None:
        """Replace rows only inside the transaction carrying the exact pending marker.

        The manifest reservation binds the proposed generation, digest, size, and
        previous target; the pending state must match it and already be visible
        in this same SQLite transaction. Without both values and that row-level
        match this repository exposes no page-row mutation path.
        """
        if not session.in_transaction():
            raise ValueError("page mutation requires an active write transaction")
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        if type(pages) is not tuple:
            raise TypeError("pages must be a tuple of DownloadPage values")
        if len(pages) > MAX_MANIFEST_PAGE_COUNT:
            raise ValueError("page count exceeds the v1 manifest limit")
        if type(reservation) is not ManifestWriteReservation:
            raise ValueError("a manifest write reservation is required")
        if type(pending_state) is not DownloadManifestState:
            raise ValueError("a pending manifest state is required")
        if (
            reservation.job_id != job_id
            or reservation.new_generation
            != reservation.expected_current_generation + 1
            or pending_state.job_id != job_id
            or pending_state.generation != reservation.new_generation
            or pending_state.manifest_sha256 != reservation.new_sha256
            or pending_state.manifest_size_bytes != reservation.new_size_bytes
            or pending_state.previous_sha256 != reservation.target_sha256
            or not pending_state.publication_pending
            or (
                reservation.expected_current_generation > 0
                and reservation.target_sha256 is None
            )
        ):
            raise ValueError("reservation does not authorize this pending snapshot")
        if self._reservation_validator is None:
            raise ValueError("manifest-store reservation validation is required")
        self._reservation_validator(reservation)

        for page in pages:
            if type(page) is not DownloadPage or page.job_id != job_id:
                raise ValueError("page rows must belong to the authorized job")
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
                raise ValueError("only metadata-only PENDING pages are authorized")

        marker = session.execute(
            select(DownloadManifestStateRow)
            .where(DownloadManifestStateRow.job_id == str(job_id))
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if (
            marker is None
            or download_manifest_state_from_row(marker) != pending_state
        ):
            raise ValueError("the matching publication pending marker is required")

        session.execute(
            delete(DownloadPageRow).where(DownloadPageRow.job_id == str(job_id))
        )
        session.add_all([download_page_to_row(page) for page in pages])
        session.flush()
        job_row = session.get(DownloadJobRow, str(job_id))
        if job_row is None:
            raise ValueError("authorized download job disappeared during mutation")
        projected = self.project_manifest(session, download_job_from_row(job_row))
        try:
            projected_bytes = manifest_to_bytes(projected)
        except (InvalidManifestError, TypeError, ValueError) as error:
            raise ValueError(
                "page rows do not match the reserved manifest digest and size"
            ) from error
        if (
            len(projected_bytes) != reservation.new_size_bytes
            or hashlib.sha256(projected_bytes).hexdigest() != reservation.new_sha256
        ):
            raise ValueError(
                "page rows do not match the reserved manifest digest and size"
            )

    def publish_pages_for_manifest_publication(
        self,
        session: Session,
        *,
        job_id: UUID,
        expected_job: DownloadJob,
        expected_pages: tuple[DownloadPage, ...],
        pages: tuple[DownloadPage, ...],
        reservation: ManifestWriteReservation,
        pending_state: DownloadManifestState,
    ) -> None:
        """CAS a monotonic page snapshot under the matching manifest intent."""
        if not session.in_transaction():
            raise ValueError("page mutation requires an active write transaction")
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        if type(expected_job) is not DownloadJob or expected_job.job_id != job_id:
            raise ValueError("expected_job must be the authorized job snapshot")
        if type(expected_pages) is not tuple or type(pages) is not tuple:
            raise ValueError("page snapshots must be tuples")
        if len(pages) > MAX_MANIFEST_PAGE_COUNT:
            raise ValueError("page count exceeds the v1 manifest limit")
        if type(reservation) is not ManifestWriteReservation:
            raise ValueError("a manifest write reservation is required")
        if type(pending_state) is not DownloadManifestState:
            raise ValueError("a pending manifest state is required")
        if (
            reservation.job_id != job_id
            or reservation.new_generation != reservation.expected_current_generation + 1
            or pending_state.job_id != job_id
            or pending_state.generation != reservation.new_generation
            or pending_state.manifest_sha256 != reservation.new_sha256
            or pending_state.manifest_size_bytes != reservation.new_size_bytes
            or pending_state.previous_sha256 != reservation.target_sha256
            or not pending_state.publication_pending
            or (
                reservation.expected_current_generation > 0
                and reservation.target_sha256 is None
            )
        ):
            raise ValueError("reservation does not authorize this pending snapshot")
        if self._reservation_validator is None:
            raise ValueError("manifest-store reservation validation is required")
        self._reservation_validator(reservation)

        marker = session.execute(
            select(DownloadManifestStateRow)
            .where(DownloadManifestStateRow.job_id == str(job_id))
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if marker is None or download_manifest_state_from_row(marker) != pending_state:
            raise ValueError("the matching publication pending marker is required")

        job_row = session.get(DownloadJobRow, str(job_id), populate_existing=True)
        if job_row is None:
            raise ValueError("authorized download job disappeared during mutation")
        actual_job = download_job_from_row(job_row)
        if actual_job != expected_job:
            raise ValueError("download job changed before page snapshot mutation")
        current_manifest = self.project_manifest(session, actual_job)
        if current_manifest.pages != expected_pages:
            raise ValueError("current page rows changed before snapshot mutation")
        self.assert_snapshot_transition(actual_job, expected_pages, pages)

        changed_pages = tuple(
            proposed
            for current, proposed in zip(expected_pages, pages, strict=True)
            if current != proposed
        )
        for changed_page in changed_pages:
            changed_row = download_page_to_row(changed_page)
            result = cast(
                CursorResult[object],
                session.execute(
                    update(DownloadPageRow)
                    .where(
                        DownloadPageRow.job_id == str(job_id),
                        DownloadPageRow.ordinal == changed_row.ordinal,
                    )
                    .values(
                        temporary_path=changed_row.temporary_path,
                        temporary_path_key=changed_row.temporary_path_key,
                        status=changed_row.status,
                        size_bytes=changed_row.size_bytes,
                        sha256=changed_row.sha256,
                        width=changed_row.width,
                        height=changed_row.height,
                        mime=changed_row.mime,
                        attempts=changed_row.attempts,
                    ),
                    execution_options={"synchronize_session": "fetch"},
                ),
            )
            if result.rowcount != 1:
                raise ValueError("changed page disappeared during snapshot mutation")
        session.flush()
        refreshed_job_row = session.get(DownloadJobRow, str(job_id))
        if refreshed_job_row is None:
            raise ValueError("authorized download job disappeared during mutation")
        projected = self.project_manifest(
            session,
            download_job_from_row(refreshed_job_row),
        )
        projected_bytes = manifest_to_bytes(projected)
        if (
            len(projected_bytes) != reservation.new_size_bytes
            or hashlib.sha256(projected_bytes).hexdigest() != reservation.new_sha256
        ):
            raise ValueError(
                "page rows do not match the reserved manifest digest and size"
            )

    @classmethod
    def assert_snapshot_transition(
        cls,
        job: DownloadJob,
        current_pages: tuple[DownloadPage, ...],
        proposed_pages: tuple[DownloadPage, ...],
    ) -> None:
        """Reject non-monotonic, reordered, or job-incongruent page snapshots."""
        if type(job) is not DownloadJob:
            raise ValueError("job must be a DownloadJob")
        if type(current_pages) is not tuple or type(proposed_pages) is not tuple:
            raise ValueError("page snapshots must be tuples")
        if len(current_pages) != len(proposed_pages):
            raise ValueError("page snapshot cannot add or remove ordinals")
        if any(type(page) is not DownloadPage for page in (*current_pages, *proposed_pages)):
            raise ValueError("page snapshots must contain DownloadPage values")

        changed = False
        for current, proposed in zip(current_pages, proposed_pages, strict=True):
            if (
                current.job_id != job.job_id
                or proposed.job_id != job.job_id
                or current.ordinal != proposed.ordinal
                or current.remote_reference != proposed.remote_reference
            ):
                raise ValueError("page ordinal and remote reference identity must be stable")
            if current == proposed:
                continue
            changed = True
            cls._assert_page_state_shape(current)
            cls._assert_page_state_shape(proposed)
            cls._assert_page_transition(job, current, proposed)

        if not changed:
            raise ValueError("page snapshot must advance at least one page state")

    @classmethod
    def _assert_page_transition(
        cls,
        job: DownloadJob,
        current: DownloadPage,
        proposed: DownloadPage,
    ) -> None:
        if current.status == "PENDING" and proposed.status == "DOWNLOADING":
            expected_path = cls.attempt_path(job.job_id, current.ordinal, 1)
            if (
                job.status is not DownloadJobStatus.DOWNLOADING
                or current.attempts != 0
                or current.temporary_path is not None
                or proposed.attempts != 1
                or proposed.temporary_path != expected_path
                or not cls._has_no_download_metadata(proposed)
            ):
                raise ValueError("PENDING to DOWNLOADING requires the first reserved attempt")
            return

        if current.status == "DOWNLOADING" and proposed.status == "DOWNLOADING":
            next_attempt = current.attempts + 1
            expected_path = cls.attempt_path(job.job_id, current.ordinal, next_attempt)
            if (
                job.status is not DownloadJobStatus.DOWNLOADING
                or proposed.attempts != next_attempt
                or proposed.temporary_path != expected_path
                or current.temporary_path == proposed.temporary_path
                or not cls._has_no_download_metadata(proposed)
            ):
                raise ValueError("retry must reserve the next unique attempt path")
            return

        if current.status == "DOWNLOADING" and proposed.status == "DOWNLOADED":
            if (
                job.status is not DownloadJobStatus.DOWNLOADING
                or proposed.attempts != current.attempts
                or proposed.temporary_path != current.temporary_path
                or proposed.size_bytes is None
                or proposed.size_bytes <= 0
                or proposed.sha256 is None
                or proposed.width is not None
                or proposed.height is not None
                or proposed.mime is not None
            ):
                raise ValueError("DOWNLOADING to DOWNLOADED must verify the same attempt")
            return

        if current.status == "DOWNLOADED" and proposed.status == "VALIDATED":
            if (
                job.status is not DownloadJobStatus.VALIDATING
                or proposed.attempts != current.attempts
                or proposed.temporary_path is None
                or proposed.temporary_path.casefold().endswith(".part")
                or proposed.temporary_path == current.temporary_path
                or proposed.size_bytes != current.size_bytes
                or proposed.sha256 != current.sha256
                or proposed.size_bytes is None
                or proposed.size_bytes <= 0
                or proposed.sha256 is None
                or proposed.width is None
                or proposed.height is None
                or proposed.mime is None
            ):
                raise ValueError("DOWNLOADED to VALIDATED requires the verified final path")
            return

        if proposed.status == "FAILED" and current.status in {
            "PENDING",
            "DOWNLOADING",
            "DOWNLOADED",
        }:
            if (
                job.status is not DownloadJobStatus.FAILED
                or replace(current, status="FAILED") != proposed
            ):
                raise ValueError("page FAILED state requires a failed job and preserved evidence")
            return

        if proposed.status == "INTERRUPTED" and current.status in {
            "PENDING",
            "DOWNLOADING",
            "DOWNLOADED",
        }:
            if (
                job.status is not DownloadJobStatus.INTERRUPTED
                or not cls._interruption_matches_page(job, current)
                or replace(current, status="INTERRUPTED") != proposed
            ):
                raise ValueError(
                    "page INTERRUPTED state must match the job's interrupted page stage"
                )
            return

        raise ValueError("page state transition is not monotonic")

    @classmethod
    def _assert_page_state_shape(cls, page: DownloadPage) -> None:
        if page.status == "PENDING":
            if (
                page.attempts != 0
                or page.temporary_path is not None
                or not cls._has_no_download_metadata(page)
            ):
                raise ValueError("PENDING page must not contain download artifacts")
            return
        if page.status in {"DOWNLOADING", "DOWNLOADED"}:
            if page.attempts < 1 or page.temporary_path != cls.attempt_path(
                page.job_id,
                page.ordinal,
                page.attempts,
            ):
                raise ValueError("active page must use its unique attempt .part path")
            if page.status == "DOWNLOADING" and not cls._has_no_download_metadata(page):
                raise ValueError("DOWNLOADING page must not claim completed bytes")
            if page.status == "DOWNLOADED" and (
                page.size_bytes is None
                or page.size_bytes <= 0
                or page.sha256 is None
                or page.width is not None
                or page.height is not None
                or page.mime is not None
            ):
                raise ValueError("DOWNLOADED page requires only size and hash metadata")
            return
        if page.status == "VALIDATED":
            if (
                page.attempts < 1
                or page.temporary_path is None
                or page.temporary_path.casefold().endswith(".part")
                or page.size_bytes is None
                or page.size_bytes <= 0
                or page.sha256 is None
                or page.width is None
                or page.height is None
                or page.mime is None
            ):
                raise ValueError("VALIDATED page requires complete final image metadata")
            return
        if page.status in {"FAILED", "INTERRUPTED"}:
            if page.temporary_path is None:
                if page.attempts != 0 or not cls._has_no_download_metadata(page):
                    raise ValueError("terminal metadata-only page has inconsistent artifacts")
            elif page.temporary_path.casefold().endswith(".part"):
                if page.attempts < 1:
                    raise ValueError("terminal part path requires an attempt number")
                if (page.size_bytes is None) != (page.sha256 is None):
                    raise ValueError("terminal partial path has inconsistent hash metadata")
                if page.width is not None or page.height is not None or page.mime is not None:
                    raise ValueError("terminal part path cannot contain validated image metadata")
            elif (
                page.attempts < 1
                or page.size_bytes is None
                or page.size_bytes <= 0
                or page.sha256 is None
                or page.width is None
                or page.height is None
                or page.mime is None
            ):
                raise ValueError("terminal final path requires validated image metadata")
            return
        raise ValueError("page state is unsupported")

    @staticmethod
    def _has_no_download_metadata(page: DownloadPage) -> bool:
        return (
            page.size_bytes is None
            and page.sha256 is None
            and page.width is None
            and page.height is None
            and page.mime is None
        )

    @staticmethod
    def attempt_path(job_id: UUID, ordinal: int, attempt: int) -> str:
        """Return the canonical relative temporary path for one reserved attempt."""
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        if type(ordinal) is not int or not 1 <= ordinal <= MAX_SQLITE_INTEGER:
            raise ValueError("ordinal must fit the supported positive SQLite integer range")
        if type(attempt) is not int or not 1 <= attempt <= MAX_SQLITE_INTEGER:
            raise ValueError("attempt must fit the supported positive SQLite integer range")
        return f"jobs/{job_id}/pages/{ordinal}/attempt-{attempt}.part"

    @staticmethod
    def _interruption_matches_page(job: DownloadJob, page: DownloadPage) -> bool:
        interrupted_from = job.interrupted_from_status
        if interrupted_from is DownloadJobStatus.FETCHING_METADATA:
            return page.status == "PENDING"
        if interrupted_from is DownloadJobStatus.DOWNLOADING:
            return page.status in {"PENDING", "DOWNLOADING"}
        if interrupted_from is DownloadJobStatus.VALIDATING:
            return page.status == "DOWNLOADED"
        return False

    def project_manifest(
        self,
        session: Session,
        job: DownloadJob,
    ) -> DownloadJobManifest:
        if type(job) is not DownloadJob:
            raise TypeError("job must be a DownloadJob.")

        snapshot = session.execute(
            select(
                DownloadJobRow.job_id,
                DownloadJobRow.chapter_source_id,
                ChapterSourceRow.source_id,
                ChapterSourceRow.chapter_id,
                DownloadPageRow,
            )
            .select_from(DownloadJobRow)
            .join(
                ChapterSourceRow,
                ChapterSourceRow.chapter_source_id == DownloadJobRow.chapter_source_id,
            )
            .join(
                SourceRow,
                SourceRow.source_id == ChapterSourceRow.source_id,
            )
            .join(
                ChapterRow,
                ChapterRow.chapter_id == ChapterSourceRow.chapter_id,
            )
            .outerjoin(
                DownloadPageRow,
                DownloadPageRow.job_id == DownloadJobRow.job_id,
            )
            .where(
                DownloadJobRow.job_id == str(job.job_id),
                DownloadJobRow.chapter_source_id == str(job.chapter_source_id),
            )
            .order_by(DownloadPageRow.ordinal.asc())
            .limit(MAX_MANIFEST_PAGE_COUNT + 1)
        ).all()
        if not snapshot:
            raise InvalidManifestError(
                "Download job chapter-source lineage is missing or incomplete."
            )

        _, _, first_source_id, first_chapter_id, _ = snapshot[0]
        source_id = self._canonical_uuid4(first_source_id, "source_id")
        chapter_id = self._canonical_uuid4(first_chapter_id, "chapter_id")

        page_rows: list[DownloadPageRow] = []
        for _, _, _, _, page_row in snapshot:
            if page_row is not None:
                page_rows.append(page_row)
        if len(page_rows) > MAX_MANIFEST_PAGE_COUNT:
            raise InvalidManifestError(
                "Download job exceeds the v1 manifest page limit."
            )

        try:
            pages = tuple(download_page_from_row(row) for row in page_rows)
            return DownloadJobManifest(
                job_id=job.job_id,
                source_id=source_id,
                chapter_id=chapter_id,
                expected_pages=len(pages),
                pages=pages,
            )
        except InvalidManifestError:
            raise
        except (AttributeError, OverflowError, TypeError, ValueError) as exc:
            raise InvalidManifestError(
                "Download page rows diverge from the v1 manifest contract."
            ) from exc

    @staticmethod
    def _canonical_uuid4(value: object, field_name: str) -> UUID:
        if type(value) is not str:
            raise InvalidManifestError(f"{field_name} must be a canonical UUIDv4.")
        try:
            parsed = UUID(value)
        except (AttributeError, TypeError, ValueError):
            raise InvalidManifestError(
                f"{field_name} must be a canonical UUIDv4."
            ) from None
        if parsed.version != 4 or str(parsed) != value:
            raise InvalidManifestError(f"{field_name} must be a canonical UUIDv4.")
        return parsed
