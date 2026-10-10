"""Validated status transitions for download jobs.

Recovery exits from INTERRUPTED are checkpoint-gated and belong to the
Recovery Manager, not to the generic transition guard.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from app.domain.download_job import DownloadJobStatus

_NORMAL_SUCCESSORS: Final[Mapping[DownloadJobStatus, DownloadJobStatus]] = (
    MappingProxyType(
        {
            DownloadJobStatus.QUEUED: DownloadJobStatus.FETCHING_METADATA,
            DownloadJobStatus.FETCHING_METADATA: DownloadJobStatus.DOWNLOADING,
            DownloadJobStatus.DOWNLOADING: DownloadJobStatus.VALIDATING,
            DownloadJobStatus.VALIDATING: DownloadJobStatus.PROCESSING,
            DownloadJobStatus.PROCESSING: DownloadJobStatus.PACKAGING,
            DownloadJobStatus.PACKAGING: DownloadJobStatus.VERIFYING,
            DownloadJobStatus.VERIFYING: DownloadJobStatus.INDEXING,
            DownloadJobStatus.INDEXING: DownloadJobStatus.COMPLETED,
        }
    )
)

_ALLOWED_TRANSITIONS: Final[
    Mapping[DownloadJobStatus, frozenset[DownloadJobStatus]]
] = MappingProxyType(
    {
        DownloadJobStatus.QUEUED: frozenset(
            {
                DownloadJobStatus.FETCHING_METADATA,
                DownloadJobStatus.FAILED,
                DownloadJobStatus.CANCELLED,
            }
        ),
        DownloadJobStatus.FETCHING_METADATA: frozenset(
            {
                DownloadJobStatus.DOWNLOADING,
                DownloadJobStatus.FAILED,
                DownloadJobStatus.CANCELLED,
                DownloadJobStatus.INTERRUPTED,
            }
        ),
        DownloadJobStatus.DOWNLOADING: frozenset(
            {
                DownloadJobStatus.VALIDATING,
                DownloadJobStatus.FAILED,
                DownloadJobStatus.CANCELLED,
                DownloadJobStatus.INTERRUPTED,
            }
        ),
        DownloadJobStatus.VALIDATING: frozenset(
            {
                DownloadJobStatus.PROCESSING,
                DownloadJobStatus.FAILED,
                DownloadJobStatus.CANCELLED,
                DownloadJobStatus.INTERRUPTED,
            }
        ),
        DownloadJobStatus.PROCESSING: frozenset(
            {
                DownloadJobStatus.PACKAGING,
                DownloadJobStatus.FAILED,
                DownloadJobStatus.CANCELLED,
                DownloadJobStatus.INTERRUPTED,
            }
        ),
        DownloadJobStatus.PACKAGING: frozenset(
            {
                DownloadJobStatus.VERIFYING,
                DownloadJobStatus.FAILED,
                DownloadJobStatus.CANCELLED,
                DownloadJobStatus.INTERRUPTED,
            }
        ),
        DownloadJobStatus.VERIFYING: frozenset(
            {
                DownloadJobStatus.INDEXING,
                DownloadJobStatus.FAILED,
                DownloadJobStatus.CANCELLED,
                DownloadJobStatus.INTERRUPTED,
            }
        ),
        DownloadJobStatus.INDEXING: frozenset(
            {
                DownloadJobStatus.COMPLETED,
                DownloadJobStatus.FAILED,
                DownloadJobStatus.CANCELLED,
                DownloadJobStatus.INTERRUPTED,
            }
        ),
        DownloadJobStatus.COMPLETED: frozenset(),
        DownloadJobStatus.FAILED: frozenset(),
        DownloadJobStatus.CANCELLED: frozenset(),
        DownloadJobStatus.INTERRUPTED: frozenset({DownloadJobStatus.FAILED}),
    }
)


class IllegalJobTransition(ValueError):
    """Raised when a requested job status transition is not permitted."""

    def __init__(
        self,
        current_status: DownloadJobStatus,
        target_status: DownloadJobStatus,
    ) -> None:
        self.current_status = current_status
        self.target_status = target_status
        super().__init__(
            "Illegal job transition from "
            f"{current_status.value} to {target_status.value}"
        )


def normal_successor(status: DownloadJobStatus) -> DownloadJobStatus | None:
    """Return the next status on the specified success path, if one exists."""
    if type(status) is not DownloadJobStatus:
        raise TypeError("status must be a supported DownloadJobStatus")
    return _NORMAL_SUCCESSORS.get(status)


def assert_normal_transition(
    current_status: DownloadJobStatus,
    target_status: DownloadJobStatus,
) -> None:
    """Reject any requested transition outside the spec-defined success path."""
    if type(current_status) is not DownloadJobStatus:
        raise TypeError("current_status must be a supported DownloadJobStatus")
    if type(target_status) is not DownloadJobStatus:
        raise TypeError("target_status must be a supported DownloadJobStatus")
    if normal_successor(current_status) is not target_status:
        raise IllegalJobTransition(current_status, target_status)


def assert_transition_allowed(
    current_status: DownloadJobStatus,
    target_status: DownloadJobStatus,
) -> None:
    """Validate a generic status edge; recovery resumes need a verified checkpoint."""
    if type(current_status) is not DownloadJobStatus:
        raise TypeError("current_status must be a supported DownloadJobStatus")
    if type(target_status) is not DownloadJobStatus:
        raise TypeError("target_status must be a supported DownloadJobStatus")
    if target_status not in _ALLOWED_TRANSITIONS[current_status]:
        raise IllegalJobTransition(current_status, target_status)
