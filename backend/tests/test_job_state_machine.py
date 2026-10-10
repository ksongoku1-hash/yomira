from itertools import pairwise
from typing import cast

import pytest

from app.domain.download_job import DownloadJobStatus
from app.domain.job_state_machine import (
    IllegalJobTransition,
    assert_normal_transition,
    assert_transition_allowed,
    normal_successor,
)


def test_job_normal_success_route_matches_specification() -> None:
    route = (
        DownloadJobStatus.QUEUED,
        DownloadJobStatus.FETCHING_METADATA,
        DownloadJobStatus.DOWNLOADING,
        DownloadJobStatus.VALIDATING,
        DownloadJobStatus.PROCESSING,
        DownloadJobStatus.PACKAGING,
        DownloadJobStatus.VERIFYING,
        DownloadJobStatus.INDEXING,
        DownloadJobStatus.COMPLETED,
    )

    assert tuple(normal_successor(status) for status in route[:-1]) == route[1:]


def test_terminal_and_interrupted_states_have_no_normal_successor() -> None:
    for status in (
        DownloadJobStatus.COMPLETED,
        DownloadJobStatus.FAILED,
        DownloadJobStatus.CANCELLED,
        DownloadJobStatus.INTERRUPTED,
    ):
        assert normal_successor(status) is None


def test_normal_successor_rejects_values_outside_the_status_enum() -> None:
    with pytest.raises(TypeError, match="supported DownloadJobStatus"):
        normal_successor(cast(DownloadJobStatus, "QUEUED"))


def test_transition_guard_accepts_each_normal_success_edge() -> None:
    route = (
        DownloadJobStatus.QUEUED,
        DownloadJobStatus.FETCHING_METADATA,
        DownloadJobStatus.DOWNLOADING,
        DownloadJobStatus.VALIDATING,
        DownloadJobStatus.PROCESSING,
        DownloadJobStatus.PACKAGING,
        DownloadJobStatus.VERIFYING,
        DownloadJobStatus.INDEXING,
        DownloadJobStatus.COMPLETED,
    )

    for current, target in pairwise(route):
        assert_normal_transition(current, target)


def test_transition_guard_rejects_skips_and_terminal_mutations() -> None:
    invalid_transitions = (
        (DownloadJobStatus.DOWNLOADING, DownloadJobStatus.COMPLETED),
        (DownloadJobStatus.COMPLETED, DownloadJobStatus.PROCESSING),
        (DownloadJobStatus.FAILED, DownloadJobStatus.QUEUED),
        (DownloadJobStatus.CANCELLED, DownloadJobStatus.QUEUED),
    )

    for current, target in invalid_transitions:
        with pytest.raises(IllegalJobTransition) as error:
            assert_normal_transition(current, target)
        assert error.value.current_status is current
        assert error.value.target_status is target


def test_general_transition_guard_matches_accepted_adr_matrix() -> None:
    expected: dict[DownloadJobStatus, frozenset[DownloadJobStatus]] = {
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

    for current in DownloadJobStatus:
        for target in DownloadJobStatus:
            if target in expected[current]:
                assert_transition_allowed(current, target)
            else:
                with pytest.raises(IllegalJobTransition):
                    assert_transition_allowed(current, target)
