import asyncio
from uuid import uuid4

import pytest

from app.domain.download_job import DownloadJobStatus
from app.domain.job_pause import (
    ActiveJobAlreadyRegistered,
    JobPauseController,
    JobPauseNotAllowed,
    JobPauseState,
    PauseCapacityExceeded,
    PauseCheckpointOutcome,
    PauseReason,
    PauseSafetyEvidence,
    UnknownActiveJob,
)


@pytest.mark.parametrize(
    ("status", "evidence"),
    [
        (
            DownloadJobStatus.FETCHING_METADATA,
            PauseSafetyEvidence(network_operation_inactive=True),
        ),
        (
            DownloadJobStatus.DOWNLOADING,
            PauseSafetyEvidence(new_requests_stopped=True, stream_closed=True),
        ),
        (
            DownloadJobStatus.VALIDATING,
            PauseSafetyEvidence(safe_point_reached=True),
        ),
        (
            DownloadJobStatus.PROCESSING,
            PauseSafetyEvidence(between_pages=True),
        ),
        (
            DownloadJobStatus.PACKAGING,
            PauseSafetyEvidence(
                write_critical_operation_inactive=True,
                cleanup_complete=True,
            ),
        ),
        (
            DownloadJobStatus.VERIFYING,
            PauseSafetyEvidence(safe_point_reached=True),
        ),
        (
            DownloadJobStatus.INDEXING,
            PauseSafetyEvidence(database_transaction_closed=True),
        ),
    ],
)
def test_pause_waits_for_resume_only_at_stage_safe_points(
    status: DownloadJobStatus,
    evidence: PauseSafetyEvidence,
) -> None:
    async def scenario() -> None:
        controller = JobPauseController()
        job_id = uuid4()
        await controller.register(job_id)
        assert controller.request_pause(job_id) is JobPauseState.PAUSE_REQUESTED

        checkpoint = asyncio.create_task(
            controller.pause_if_requested(
                job_id,
                status=status,
                evidence=evidence,
            )
        )
        await asyncio.sleep(0)
        assert controller.state(job_id) is JobPauseState.PAUSED
        assert not checkpoint.done()

        assert controller.resume(job_id) is JobPauseState.RUNNING
        assert await checkpoint is PauseCheckpointOutcome.RESUMED
        assert controller.state(job_id) is JobPauseState.RUNNING
        await controller.unregister(job_id)

    asyncio.run(scenario())


def test_pause_request_stays_pending_until_safe_evidence_arrives() -> None:
    async def scenario() -> None:
        controller = JobPauseController()
        job_id = uuid4()
        await controller.register(job_id)
        controller.request_pause(job_id)

        result = await controller.pause_if_requested(
            job_id,
            status=DownloadJobStatus.PACKAGING,
            evidence=PauseSafetyEvidence(
                write_critical_operation_inactive=False,
                cleanup_complete=False,
            ),
        )
        assert result is PauseCheckpointOutcome.CONTINUE
        assert controller.state(job_id) is JobPauseState.PAUSE_REQUESTED

        checkpoint = asyncio.create_task(
            controller.pause_if_requested(
                job_id,
                status=DownloadJobStatus.PACKAGING,
                evidence=PauseSafetyEvidence(
                    write_critical_operation_inactive=True,
                    cleanup_complete=True,
                ),
            )
        )
        await asyncio.sleep(0)
        assert controller.state(job_id) is JobPauseState.PAUSED
        controller.resume(job_id)
        assert await checkpoint is PauseCheckpointOutcome.RESUMED
        await controller.unregister(job_id)

    asyncio.run(scenario())


def test_resume_before_safe_point_cancels_pending_pause_request() -> None:
    async def scenario() -> None:
        controller = JobPauseController()
        job_id = uuid4()
        await controller.register(job_id)
        controller.request_pause(job_id)
        assert controller.resume(job_id) is JobPauseState.RUNNING

        result = await controller.pause_if_requested(
            job_id,
            status=DownloadJobStatus.PROCESSING,
            evidence=PauseSafetyEvidence(between_pages=True),
        )
        assert result is PauseCheckpointOutcome.CONTINUE
        await controller.unregister(job_id)

    asyncio.run(scenario())


def test_cancellation_wakes_a_paused_worker_and_takes_precedence() -> None:
    async def scenario() -> None:
        controller = JobPauseController()
        job_id = uuid4()
        await controller.register(job_id)
        controller.request_pause(job_id)
        checkpoint = asyncio.create_task(
            controller.pause_if_requested(
                job_id,
                status=DownloadJobStatus.PROCESSING,
                evidence=PauseSafetyEvidence(between_pages=True),
            )
        )
        await asyncio.sleep(0)
        assert controller.state(job_id) is JobPauseState.PAUSED

        assert (
            controller.notify_cancellation_requested(job_id)
            is JobPauseState.CANCELLATION_REQUESTED
        )
        assert await checkpoint is PauseCheckpointOutcome.CANCELLATION_REQUESTED
        with pytest.raises(JobPauseNotAllowed) as pause_error:
            controller.request_pause(job_id)
        assert pause_error.value.status is None
        with pytest.raises(JobPauseNotAllowed):
            controller.resume(job_id)
        await controller.unregister(job_id)

    asyncio.run(scenario())


def test_automatic_resume_does_not_clear_a_user_pause() -> None:
    async def scenario() -> None:
        controller = JobPauseController()
        job_id = uuid4()
        await controller.register(job_id)
        controller.request_pause(job_id, reason=PauseReason.USER)
        controller.request_pause(job_id, reason=PauseReason.BACKPRESSURE)
        checkpoint = asyncio.create_task(
            controller.pause_if_requested(
                job_id,
                status=DownloadJobStatus.PROCESSING,
                evidence=PauseSafetyEvidence(between_pages=True),
            )
        )
        await asyncio.sleep(0)
        assert controller.state(job_id) is JobPauseState.PAUSED

        assert (
            controller.resume(job_id, reason=PauseReason.BACKPRESSURE)
            is JobPauseState.PAUSED
        )
        assert controller.pause_reasons(job_id) == frozenset({PauseReason.USER})
        await asyncio.sleep(0)
        assert not checkpoint.done()

        assert controller.resume(job_id, reason=PauseReason.USER) is JobPauseState.RUNNING
        assert await checkpoint is PauseCheckpointOutcome.RESUMED
        await controller.unregister(job_id)

    asyncio.run(scenario())


def test_pause_controller_capacity_is_bounded_and_released_on_unregister() -> None:
    async def scenario() -> None:
        controller = JobPauseController(max_active_jobs=2)
        first_id, second_id, third_id = uuid4(), uuid4(), uuid4()
        await controller.register(first_id)
        await controller.register(second_id)
        assert controller.active_job_count == 2

        with pytest.raises(PauseCapacityExceeded):
            await controller.register(third_id)
        with pytest.raises(ActiveJobAlreadyRegistered):
            await controller.register(first_id)

        await controller.unregister(first_id)
        await controller.register(third_id)
        assert controller.active_job_count == 2
        await controller.unregister(second_id)
        await controller.unregister(third_id)

    asyncio.run(scenario())


def test_pause_controller_rejects_unknown_jobs_and_queued_stage() -> None:
    async def scenario() -> None:
        controller = JobPauseController()
        unknown_id = uuid4()
        with pytest.raises(UnknownActiveJob):
            controller.request_pause(unknown_id)

        job_id = uuid4()
        await controller.register(job_id)
        controller.request_pause(job_id)
        with pytest.raises(JobPauseNotAllowed):
            await controller.pause_if_requested(
                job_id,
                status=DownloadJobStatus.QUEUED,
                evidence=PauseSafetyEvidence(),
            )
        await controller.unregister(job_id)

    asyncio.run(scenario())


def test_unregister_wakes_a_checkpoint_waiting_in_paused_state() -> None:
    async def scenario() -> None:
        controller = JobPauseController()
        job_id = uuid4()
        await controller.register(job_id)
        controller.request_pause(job_id)
        checkpoint = asyncio.create_task(
            controller.pause_if_requested(
                job_id,
                status=DownloadJobStatus.VERIFYING,
                evidence=PauseSafetyEvidence(safe_point_reached=True),
            )
        )
        await asyncio.sleep(0)
        assert controller.state(job_id) is JobPauseState.PAUSED
        await controller.unregister(job_id)
        assert await checkpoint is PauseCheckpointOutcome.CANCELLATION_REQUESTED

    asyncio.run(scenario())


def test_pause_controller_rejects_access_from_a_different_event_loop() -> None:
    controller = JobPauseController()
    job_id = uuid4()
    asyncio.run(controller.register(job_id))

    async def read_state_from_another_loop() -> None:
        controller.state(job_id)

    with pytest.raises(RuntimeError, match="owning event loop"):
        asyncio.run(read_state_from_another_loop())
