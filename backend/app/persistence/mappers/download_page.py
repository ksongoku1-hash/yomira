import unicodedata
from uuid import UUID

from app.domain.download_page import DownloadPage
from app.errors import ErrorCode
from app.persistence.models.download_page import DownloadPageRow
from app.tracing import create_trace_id, current_trace_id

REQUIRED_UNICODE_VERSION = "16.0.0"


class TemporaryPathIdentityError(RuntimeError):
    code = ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE
    retryable = False
    stage = "temporary_path_unicode_version"

    def __init__(self) -> None:
        self.trace_id = current_trace_id() or create_trace_id()
        super().__init__("temporary_path identity requires Unicode data version 16.0.0")


def _temporary_path_key(temporary_path: str | None) -> str | None:
    if temporary_path is None:
        return None
    if unicodedata.unidata_version != REQUIRED_UNICODE_VERSION:
        raise TemporaryPathIdentityError()
    return temporary_path.casefold()


def download_page_to_row(page: DownloadPage) -> DownloadPageRow:
    return DownloadPageRow(
        job_id=str(page.job_id),
        ordinal=page.ordinal,
        remote_reference=page.remote_reference,
        temporary_path=page.temporary_path,
        temporary_path_key=_temporary_path_key(page.temporary_path),
        status=page.status,
        size_bytes=page.size_bytes,
        sha256=page.sha256,
        width=page.width,
        height=page.height,
        mime=page.mime,
        attempts=page.attempts,
    )


def download_page_from_row(row: DownloadPageRow) -> DownloadPage:
    return DownloadPage(
        job_id=UUID(row.job_id),
        ordinal=row.ordinal,
        remote_reference=row.remote_reference,
        temporary_path=row.temporary_path,
        status=row.status,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        width=row.width,
        height=row.height,
        mime=row.mime,
        attempts=row.attempts,
    )
