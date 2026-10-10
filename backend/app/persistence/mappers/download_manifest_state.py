from uuid import UUID

from app.domain.download_manifest_state import DownloadManifestState
from app.persistence.models.download_manifest_state import DownloadManifestStateRow


def download_manifest_state_to_row(
    state: DownloadManifestState,
) -> DownloadManifestStateRow:
    return DownloadManifestStateRow(
        job_id=str(state.job_id),
        generation=state.generation,
        manifest_sha256=state.manifest_sha256,
        manifest_size_bytes=state.manifest_size_bytes,
        previous_sha256=state.previous_sha256,
        publication_pending=state.publication_pending,
    )


def download_manifest_state_from_row(
    row: DownloadManifestStateRow,
) -> DownloadManifestState:
    return DownloadManifestState(
        job_id=UUID(row.job_id),
        generation=row.generation,
        manifest_sha256=row.manifest_sha256,
        manifest_size_bytes=row.manifest_size_bytes,
        previous_sha256=row.previous_sha256,
        publication_pending=row.publication_pending,
    )
