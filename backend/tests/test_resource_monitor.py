import asyncio
import json
import logging
import time
from uuid import uuid4

from app.domain.download_job import DownloadJobStatus
from app.domain.job_backpressure import (
    ActiveJobSnapshot,
    BackpressureManager,
    BackpressureState,
    ResourceSample,
)
from app.domain.job_pause import JobPauseController, PauseReason
from app.errors import ErrorCode
from app.logging_config import JsonLogFormatter
from app.platform.windows_resources import ResourceSamplingUnavailable
from app.services.resource_monitor import ResourceMonitorService
from app.tracing import activate_trace_id, current_trace_id, reset_trace_id

MIB = 1024 * 1024
GIB = 1024 * MIB


class FakeSampler:
    def __init__(self, result: ResourceSample | Exception) -> None:
        self.result = result
        self.closed = False
        self.calls = 0

    def sample(self) -> ResourceSample:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        self.closed = True


class CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _attach_capture() -> tuple[logging.Logger, CapturingHandler, int]:
    target_logger = logging.getLogger("yomira.job_manager.backpressure")
    previous_level = target_logger.level
    target_logger.setLevel(logging.INFO)
    handler = CapturingHandler()
    target_logger.addHandler(handler)
    return target_logger, handler, previous_level


def _remove_capture(
    target_logger: logging.Logger,
    handler: CapturingHandler,
    previous_level: int,
) -> None:
    target_logger.removeHandler(handler)
    target_logger.setLevel(previous_level)


def _normal_sample() -> ResourceSample:
    return ResourceSample(
        available_memory_bytes=4 * GIB,
        total_memory_bytes=8 * GIB,
        disk_busy_percent=20.0,
        disk_latency_ms=5.0,
        sampled_at_monotonic=time.monotonic(),
    )


def test_resource_monitor_records_native_sensor_failure_and_blocks_admission() -> None:
    async def scenario() -> BackpressureState:
        pause_controller = JobPauseController()
        manager = BackpressureManager(pause_controller)
        sampler = FakeSampler(ResourceSamplingUnavailable("PDH_COUNTER_MISSING", 55))
        service = ResourceMonitorService(sampler, manager, active_jobs=lambda: ())

        update = await service.sample_once()
        await service.stop()
        assert sampler.calls == 1
        assert sampler.closed is True
        return update.state

    target_logger, capture, previous_level = _attach_capture()
    try:
        state = asyncio.run(scenario())
        assert state is BackpressureState.MONITOR_UNAVAILABLE
        failure = next(
            record
            for record in capture.records
            if record.msg == "job.resource_monitor.unavailable"
        )
        assert (
            failure.__dict__.get("error_code")
            == ErrorCode.SYS_RESOURCE_MONITOR_UNAVAILABLE.value
        )
        assert failure.__dict__.get("native_error_code") == 55
    finally:
        _remove_capture(target_logger, capture, previous_level)


def test_resource_monitor_logs_pressure_with_job_stage_and_threshold() -> None:
    job_id = uuid4()

    async def scenario() -> tuple[BackpressureState, frozenset[PauseReason]]:
        pause_controller = JobPauseController()
        await pause_controller.register(job_id)
        manager = BackpressureManager(pause_controller)
        sample = ResourceSample(
            available_memory_bytes=800 * MIB,
            total_memory_bytes=8 * GIB,
            disk_busy_percent=20.0,
            disk_latency_ms=5.0,
            sampled_at_monotonic=time.monotonic(),
        )
        sampler = FakeSampler(sample)
        service = ResourceMonitorService(
            sampler,
            manager,
            active_jobs=lambda: (
                ActiveJobSnapshot(job_id, DownloadJobStatus.PROCESSING),
            ),
        )

        update = await service.sample_once()
        reasons = pause_controller.pause_reasons(job_id)
        await pause_controller.unregister(job_id)
        await service.stop()
        assert update.admission_allowed is False
        return update.state, reasons

    target_logger, capture, previous_level = _attach_capture()
    try:
        state, reasons = asyncio.run(scenario())
        assert state is BackpressureState.PRESSURE
        assert PauseReason.BACKPRESSURE in reasons
        pause_log = next(
            record
            for record in capture.records
            if getattr(record, "action", None) == "PAUSE_REQUESTED"
        )
        assert pause_log.__dict__.get("job_id") == str(job_id)
        assert pause_log.__dict__.get("stage") == DownloadJobStatus.PROCESSING.value
        assert pause_log.__dict__.get("resource_metric") == "AVAILABLE_RAM_BYTES"
        assert pause_log.__dict__.get("observed_value") == 800 * MIB
        assert pause_log.__dict__.get("threshold_value") == int(8 * GIB * 0.10)
        assert pause_log.__dict__.get("reason") == "LOW_AVAILABLE_RAM"
    finally:
        _remove_capture(target_logger, capture, previous_level)


def test_resource_monitor_background_loop_stops_and_closes_sampler() -> None:
    async def scenario() -> None:
        pause_controller = JobPauseController()
        manager = BackpressureManager(pause_controller)
        sampler = FakeSampler(_normal_sample())
        service = ResourceMonitorService(sampler, manager, active_jobs=lambda: ())

        await service.start()
        for _ in range(100):
            if sampler.calls:
                break
            await asyncio.sleep(0.01)
        assert sampler.calls >= 1
        await service.stop()
        assert sampler.closed is True
        assert service.admission_allowed is True

    asyncio.run(scenario())


def test_background_monitor_uses_a_trace_separate_from_startup() -> None:
    startup_trace_id = str(uuid4())

    class TraceCapturingSampler(FakeSampler):
        def __init__(self) -> None:
            super().__init__(_normal_sample())
            self.trace_ids: list[str | None] = []

        def sample(self) -> ResourceSample:
            self.trace_ids.append(current_trace_id())
            return super().sample()

    async def scenario() -> list[str | None]:
        startup_token = activate_trace_id(startup_trace_id)
        try:
            pause_controller = JobPauseController()
            manager = BackpressureManager(pause_controller)
            sampler = TraceCapturingSampler()
            service = ResourceMonitorService(sampler, manager, active_jobs=lambda: ())

            await service.start()
            for _ in range(100):
                if sampler.calls:
                    break
                await asyncio.sleep(0.01)
            await service.stop()
            return sampler.trace_ids
        finally:
            reset_trace_id(startup_token)

    trace_ids = asyncio.run(scenario())
    assert trace_ids
    assert isinstance(trace_ids[0], str) and trace_ids[0].strip()
    assert trace_ids[0] != startup_trace_id


def test_resource_monitor_logs_unexpected_close_failure_without_raising() -> None:
    class BrokenCloseSampler(FakeSampler):
        def close(self) -> None:
            self.closed = True
            raise RuntimeError("private-native-close-detail")

    async def scenario() -> BrokenCloseSampler:
        pause_controller = JobPauseController()
        manager = BackpressureManager(pause_controller)
        sampler = BrokenCloseSampler(_normal_sample())
        service = ResourceMonitorService(sampler, manager, active_jobs=lambda: ())

        await service.stop()
        assert sampler.closed is True
        return sampler

    target_logger, capture, previous_level = _attach_capture()
    try:
        asyncio.run(scenario())
        close_failure = next(
            record
            for record in capture.records
            if record.msg == "job.resource_monitor.close_unexpected_failure"
        )
        assert (
            close_failure.__dict__.get("error_code")
            == ErrorCode.SYS_RESOURCE_MONITOR_UNAVAILABLE.value
        )
        assert close_failure.__dict__.get("exception_type") == "RuntimeError"
        serialized = JsonLogFormatter().format(close_failure)
        event = json.loads(serialized)
        assert event["exception_type"] == "RuntimeError"
        assert "private-native-close-detail" not in serialized
    finally:
        _remove_capture(target_logger, capture, previous_level)
