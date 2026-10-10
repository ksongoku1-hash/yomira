"""CAS operations for the durable SQLite/manifest publication marker."""

from uuid import UUID

from sqlalchemy import insert, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.domain.download_manifest_state import DownloadManifestState
from app.persistence.mappers.download_manifest_state import (
    download_manifest_state_from_row,
    download_manifest_state_to_row,
)
from app.persistence.models.download_manifest_state import DownloadManifestStateRow


class DownloadManifestStateRepository:
    """Read and compare-and-swap one job's manifest publication state."""

    def get(
        self,
        session: Session,
        job_id: UUID,
    ) -> DownloadManifestState | None:
        row = session.get(DownloadManifestStateRow, str(job_id))
        return download_manifest_state_from_row(row) if row is not None else None

    def mark_publication_pending(
        self,
        session: Session,
        *,
        expected_state: DownloadManifestState | None,
        new_state: DownloadManifestState,
    ) -> bool:
        """CAS the confirmed state (or absence) to one pending generation."""
        if (
            type(new_state) is not DownloadManifestState
            or not new_state.publication_pending
        ):
            raise ValueError("new_state must be a pending DownloadManifestState")
        if expected_state is None:
            result = session.connection().execute(
                insert(DownloadManifestStateRow).values(
                    **self._values(new_state),
                )
            )
            return result.rowcount == 1

        if (
            type(expected_state) is not DownloadManifestState
            or expected_state.job_id != new_state.job_id
            or expected_state.publication_pending
        ):
            raise ValueError("expected_state must be the matching confirmed state")
        result = session.connection().execute(
            update(DownloadManifestStateRow)
            .where(*self._predicates(expected_state))
            .values(**self._values(new_state))
        )
        return result.rowcount == 1

    def close_publication(
        self,
        session: Session,
        *,
        expected_pending_state: DownloadManifestState,
    ) -> bool:
        """CAS the exact pending snapshot to confirmed, retaining its backup hash."""
        if (
            type(expected_pending_state) is not DownloadManifestState
            or not expected_pending_state.publication_pending
        ):
            raise ValueError("expected_pending_state must be pending")
        result = session.connection().execute(
            update(DownloadManifestStateRow)
            .where(*self._predicates(expected_pending_state))
            .values(publication_pending=False)
        )
        return result.rowcount == 1

    @staticmethod
    def _values(state: DownloadManifestState) -> dict[str, object]:
        row = download_manifest_state_to_row(state)
        return {
            "job_id": row.job_id,
            "generation": row.generation,
            "manifest_sha256": row.manifest_sha256,
            "manifest_size_bytes": row.manifest_size_bytes,
            "previous_sha256": row.previous_sha256,
            "publication_pending": row.publication_pending,
        }

    @staticmethod
    def _predicates(state: DownloadManifestState) -> tuple[ColumnElement[bool], ...]:
        return (
            DownloadManifestStateRow.job_id == str(state.job_id),
            DownloadManifestStateRow.generation == state.generation,
            DownloadManifestStateRow.manifest_sha256 == state.manifest_sha256,
            DownloadManifestStateRow.manifest_size_bytes == state.manifest_size_bytes,
            (
                DownloadManifestStateRow.previous_sha256.is_(None)
                if state.previous_sha256 is None
                else DownloadManifestStateRow.previous_sha256 == state.previous_sha256
            ),
            DownloadManifestStateRow.publication_pending
            == state.publication_pending,
        )
