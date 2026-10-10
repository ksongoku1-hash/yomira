"""Cancellation intent validation and safe-point evidence for download jobs."""

from dataclasses import dataclass

from app.domain.download_job import DownloadJobStatus


@dataclass(frozen=True, slots=True)
class CancellationSafetyEvidence:
    new_requests_stopped: bool = False
    stream_closed: bool = False
    safe_point_reached: bool = False
    between_pages: bool = False
    write_critical_operation_inactive: bool = False
    cleanup_complete: bool = False
    database_transaction_closed: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "new_requests_stopped",
            "stream_closed",
            "safe_point_reached",
            "between_pages",
            "write_critical_operation_inactive",
            "cleanup_complete",
            "database_transaction_closed",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be a bool")


class JobCancellationNotAllowed(ValueError):
    def __init__(self, status: DownloadJobStatus) -> None:
        self.status = status
        super().__init__(f"Cancellation is not allowed while job is {status.value}.")


class JobCancellationNotRequested(ValueError):
    def __init__(self) -> None:
        super().__init__("Job has no pending cancellation request.")


class UnsafeJobCancellation(ValueError):
    def __init__(self, status: DownloadJobStatus, reason: str) -> None:
        self.status = status
        self.reason = reason
        super().__init__(f"Cancellation is not safe in {status.value}: {reason}")


class CancellationRequestPending(ValueError):
    def __init__(self) -> None:
        super().__init__("A pending cancellation request blocks success transitions.")


class CancellationFinalizationRequired(ValueError):
    def __init__(self) -> None:
        super().__init__("Use complete_cancellation with safe-point evidence.")


def assert_cancellation_safe(
    status: DownloadJobStatus,
    evidence: CancellationSafetyEvidence,
) -> None:
    if type(status) is not DownloadJobStatus:
        raise TypeError("status must be a supported DownloadJobStatus")
    if type(evidence) is not CancellationSafetyEvidence:
        raise TypeError("evidence must be CancellationSafetyEvidence")

    match status:
        case DownloadJobStatus.DOWNLOADING:
            if not evidence.new_requests_stopped or not evidence.stream_closed:
                raise UnsafeJobCancellation(
                    status,
                    "stop new requests and close the active stream first",
                )
        case DownloadJobStatus.VALIDATING | DownloadJobStatus.VERIFYING:
            if not evidence.safe_point_reached:
                raise UnsafeJobCancellation(status, "a safe point has not been reached")
        case DownloadJobStatus.PROCESSING:
            if not evidence.between_pages:
                raise UnsafeJobCancellation(status, "finish the current page first")
        case DownloadJobStatus.PACKAGING:
            if (
                not evidence.write_critical_operation_inactive
                or not evidence.cleanup_complete
            ):
                raise UnsafeJobCancellation(
                    status,
                    "finish the critical write and cleanup first",
                )
        case DownloadJobStatus.INDEXING:
            if not evidence.database_transaction_closed:
                raise UnsafeJobCancellation(status, "the database transaction is active")
        case _:
            raise JobCancellationNotAllowed(status)
