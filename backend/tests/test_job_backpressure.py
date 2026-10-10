import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.download_job import DownloadJobStatus
from app.domain.job_backpressure import (
    ActiveJobSnapshot,
    BackpressureAction,
    BackpressureManager,
    BackpressureState,
    PressureCause,
    ResourceMetric,
    ResourceSample,
)
from app.domain.job_pause import (
    JobPauseController,
    JobPauseState,
    PauseCheckpointOutcome,
    PauseReason,
    PauseSafetyEvidence,
)

MIB = 1024 * 1024
GIB = 1024 * MIB


def _sample(
    *,
    available: int = 4 * GIB,
    busy: float | None = 20.0,
    latency_ms: float | None = 5.0,
    sampled_at: float = 100.0,
) -> ResourceSample:
    return ResourceSample(
        available_memory_bytes=available,
        total_memory_bytes=8 * GIB,
        disk_busy_percent=busy,
        disk_latency_ms=latency_ms,
        sampled_at_monotonic=sampled_at,
    )


def test_ram_pressure_requests_pause_with_metric_threshold_job_and_stage() -> None:
    async def scenario() -> None:
        pause_controller = JobPauseController()
        job_id = uuid4()
        await pause_controller.register(job_id)
        manager = BackpressureManager(pause_controller)
        active_job = ActiveJobSnapshot(job_id, DownloadJobStatus.PROCESSING)

        update = manager.observe(
            _sample(available=800 * MIB),
            (active_job,),
            now_monotonic=100.0,
        )

        assert update.state is BackpressureState.PRESSURE
        assert update.admission_allowed is False
        assert update.active_causes == frozenset({PressureCause.LOW_AVAILABLE_RAM})
        assert pause_controller.pause_reasons(job_id) == frozenset(
            {PauseReason.BACKPRESSURE}
        )
        event = next(
            item
            for item in update.events
            if item.action is BackpressureAction.PAUSE_REQUESTED
        )
        assert event.job_id == job_id
        assert event.stage is DownloadJobStatus.PROCESSING
        assert event.metric is ResourceMetric.AVAILABLE_RAM_BYTES
        assert event.observed_value == 800 * MIB
        assert event.threshold_value == int(8 * GIB * 0.10)
        assert event.reason is PressureCause.LOW_AVAILABLE_RAM
        await pause_controller.unregister(job_id)

    asyncio.run(scenario())


def test_ram_recovery_uses_hysteresis_and_preserves_user_pause() -> None:
    async def scenario() -> None:
        pause_controller = JobPauseController()
        job_id = uuid4()
        await pause_controller.register(job_id)
        pause_controller.request_pause(job_id, reason=PauseReason.USER)
        manager = BackpressureManager(pause_controller)
        job = ActiveJobSnapshot(job_id, DownloadJobStatus.VALIDATING)

        pressure = manager.observe(
            _sample(available=800 * MIB),
            (job,),
            now_monotonic=100.0,
        )
        assert pressure.state is BackpressureState.PRESSURE

        for sample_time in (101.0, 102.0):
            update = manager.observe(
                _sample(available=2 * GIB, sampled_at=sample_time),
                (job,),
                now_monotonic=sample_time,
            )
            assert update.state is BackpressureState.PRESSURE
            assert pause_controller.pause_reasons(job_id) == frozenset(
                {PauseReason.USER, PauseReason.BACKPRESSURE}
            )

        recovered = manager.observe(
            _sample(available=2 * GIB, sampled_at=103.0),
            (job,),
            now_monotonic=103.0,
        )
        assert recovered.state is BackpressureState.NORMAL
        assert recovered.admission_allowed is True
        assert pause_controller.pause_reasons(job_id) == frozenset({PauseReason.USER})
        assert pause_controller.state(job_id) is JobPauseState.PAUSE_REQUESTED
        await pause_controller.unregister(job_id)

    asyncio.run(scenario())


def test_disk_pressure_requires_sustained_saturation_and_full_recovery() -> None:
    async def scenario() -> None:
        pause_controller = JobPauseController()
        job_id = uuid4()
        await pause_controller.register(job_id)
        manager = BackpressureManager(pause_controller)
        job = ActiveJobSnapshot(job_id, DownloadJobStatus.DOWNLOADING)

        for sample_time in (100.0, 101.0):
            update = manager.observe(
                _sample(busy=95.0, latency_ms=20.0, sampled_at=sample_time),
                (job,),
                now_monotonic=sample_time,
            )
            assert update.state is BackpressureState.NORMAL

        pressured = manager.observe(
            _sample(busy=95.0, latency_ms=20.0, sampled_at=102.0),
            (job,),
            now_monotonic=102.0,
        )
        assert pressured.state is BackpressureState.PRESSURE
        event = next(
            item
            for item in pressured.events
            if item.action is BackpressureAction.PAUSE_REQUESTED
        )
        assert event.metric is ResourceMetric.DISK_BUSY_PERCENT
        assert event.threshold_value == 90.0

        for sample_time in (103.0, 104.0, 105.0, 106.0):
            update = manager.observe(
                _sample(busy=65.0, latency_ms=50.0, sampled_at=sample_time),
                (job,),
                now_monotonic=sample_time,
            )
            assert update.state is BackpressureState.PRESSURE

        recovered = manager.observe(
            _sample(busy=65.0, latency_ms=50.0, sampled_at=107.0),
            (job,),
            now_monotonic=107.0,
        )
        assert recovered.state is BackpressureState.NORMAL
        assert recovered.admission_allowed is True
        assert PauseReason.BACKPRESSURE not in pause_controller.pause_reasons(job_id)
        await pause_controller.unregister(job_id)

    asyncio.run(scenario())


def test_missing_or_stale_samples_pause_admission_until_three_valid_samples() -> None:
    async def scenario() -> None:
        pause_controller = JobPauseController()
        job_id = uuid4()
        await pause_controller.register(job_id)
        manager = BackpressureManager(pause_controller)
        job = ActiveJobSnapshot(job_id, DownloadJobStatus.PACKAGING)

        normal = manager.observe(
            _sample(),
            (job,),
            now_monotonic=100.0,
        )
        assert normal.state is BackpressureState.NORMAL
        assert normal.admission_allowed is True

        unavailable = manager.observe(
            None,
            (job,),
            now_monotonic=101.0,
        )
        assert unavailable.state is BackpressureState.MONITOR_UNAVAILABLE
        assert unavailable.admission_allowed is False
        assert PressureCause.MONITOR_UNAVAILABLE in unavailable.active_causes
        assert PauseReason.MONITOR_UNAVAILABLE in pause_controller.pause_reasons(job_id)

        for sample_time in (102.0, 103.0):
            update = manager.observe(
                _sample(sampled_at=sample_time),
                (job,),
                now_monotonic=sample_time,
            )
            assert update.state is BackpressureState.MONITOR_UNAVAILABLE

        recovered = manager.observe(
            _sample(sampled_at=104.0),
            (job,),
            now_monotonic=104.0,
        )
        assert recovered.state is BackpressureState.NORMAL
        assert recovered.admission_allowed is True
        assert PauseReason.MONITOR_UNAVAILABLE not in pause_controller.pause_reasons(
            job_id
        )

        stale = manager.observe(
            _sample(sampled_at=100.0),
            (job,),
            now_monotonic=104.0,
        )
        assert stale.state is BackpressureState.MONITOR_UNAVAILABLE
        await pause_controller.unregister(job_id)

    asyncio.run(scenario())


def test_unsafe_pause_preserves_partial_page_file_until_resources_recover(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        pause_controller = JobPauseController()
        job_id = uuid4()
        await pause_controller.register(job_id)
        manager = BackpressureManager(pause_controller)
        job = ActiveJobSnapshot(job_id, DownloadJobStatus.PROCESSING)
        part_path = tmp_path / "chapter.part"
        final_path = tmp_path / "chapter.cbz"
        part_path.write_bytes(b"page-1-valid")

        manager.observe(
            _sample(available=800 * MIB),
            (job,),
            now_monotonic=100.0,
        )
        unsafe = await pause_controller.pause_if_requested(
            job_id,
            status=DownloadJobStatus.PROCESSING,
            evidence=PauseSafetyEvidence(between_pages=False),
        )
        assert unsafe is PauseCheckpointOutcome.CONTINUE
        assert part_path.read_bytes() == b"page-1-valid"

        checkpoint = asyncio.create_task(
            pause_controller.pause_if_requested(
                job_id,
                status=DownloadJobStatus.PROCESSING,
                evidence=PauseSafetyEvidence(between_pages=True),
            )
        )
        await asyncio.sleep(0)
        assert pause_controller.state(job_id) is JobPauseState.PAUSED
        assert part_path.read_bytes() == b"page-1-valid"
        assert not final_path.exists()

        for sample_time in (101.0, 102.0, 103.0):
            manager.observe(
                _sample(available=2 * GIB, sampled_at=sample_time),
                (job,),
                now_monotonic=sample_time,
            )
        assert await checkpoint is PauseCheckpointOutcome.RESUMED
        part_path.write_bytes(part_path.read_bytes() + b"-page-2-valid")
        assert part_path.read_bytes() == b"page-1-valid-page-2-valid"
        assert not final_path.exists()
        await pause_controller.unregister(job_id)

    asyncio.run(scenario())


def test_resource_samples_reject_invalid_or_unbounded_values() -> None:
    with pytest.raises(ValueError):
        _sample(available=-1)
    with pytest.raises(ValueError):
        _sample(available=9 * GIB)
    with pytest.raises(ValueError):
        ResourceSample(
            available_memory_bytes=1,
            total_memory_bytes=GIB,
            disk_busy_percent=50.0,
            disk_latency_ms=None,
            sampled_at_monotonic=100.0,
        )
    with pytest.raises(ValueError):
        _sample(busy=float("inf"))
