"""Asynchronous lifecycle for resource sampling and backpressure decisions."""

import asyncio
import logging
import time
from collections.abc import Callable
from contextvars import Context
from typing import Protocol

from app.domain.job_backpressure import (
    ActiveJobSnapshot,
    BackpressureAction,
    BackpressureEvent,
    BackpressureManager,
    BackpressureUpdate,
    ResourceSample,
)
from app.errors import ErrorCode
from app.platform.windows_resources import ResourceSamplingUnavailable
from app.tracing import activate_trace_id, create_trace_id, current_trace_id, reset_trace_id

logger = logging.getLogger("yomira.job_manager.backpressure")
SAMPLE_INTERVAL_SECONDS = 1.0


class ResourceSampler(Protocol):
    def sample(self) -> ResourceSample: ...

    def close(self) -> None: ...


class ResourceMonitorService:
    """Take one bounded system sample at a time and reconcile active pause causes."""

    def __init__(
        self,
        sampler: ResourceSampler,
        manager: BackpressureManager,
        active_jobs: Callable[[], tuple[ActiveJobSnapshot, ...]],
    ) -> None:
        self._sampler = sampler
        self._manager = manager
        self._active_jobs = active_jobs
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._reported_sampling_failure = False

    @property
    def admission_allowed(self) -> bool:
        return self._manager.admission_allowed

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("Resource monitor is already running.")
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(),
            name="yomira-resource-monitor",
            context=Context(),
        )

    async def sample_once(self) -> BackpressureUpdate:
        trace_token = None
        if current_trace_id() is None:
            trace_token = activate_trace_id(create_trace_id())
        try:
            return await self._sample_once()
        finally:
            if trace_token is not None:
                reset_trace_id(trace_token)

    async def _sample_once(self) -> BackpressureUpdate:
        try:
            active_jobs = self._active_jobs()
        except Exception:
            update = self._manager.observe(
                None,
                (),
                now_monotonic=time.monotonic(),
            )
            if not self._reported_sampling_failure:
                logger.exception(
                    "job.resource_monitor.active_job_snapshot_failed",
                    extra={
                        "error_code": ErrorCode.SYS_RESOURCE_MONITOR_UNAVAILABLE.value,
                        "retryable": True,
                        "stage": "active_job_snapshot",
                        "resource_metric": "MONITOR_HEALTH",
                        "reason": "ACTIVE_JOB_SNAPSHOT_UNAVAILABLE",
                    },
                )
                self._reported_sampling_failure = True
            self._log_events(update)
            return update
        try:
            sample = await asyncio.to_thread(self._sampler.sample)
        except ResourceSamplingUnavailable as error:
            update = self._manager.observe(
                None,
                active_jobs,
                now_monotonic=time.monotonic(),
            )
            if not self._reported_sampling_failure:
                logger.error(
                    "job.resource_monitor.unavailable",
                    extra={
                        "error_code": ErrorCode.SYS_RESOURCE_MONITOR_UNAVAILABLE.value,
                        "retryable": True,
                        "stage": "resource_sampling",
                        "resource_metric": "MONITOR_HEALTH",
                        "observed_value": error.reason,
                        "threshold_value": None,
                        "reason": "MONITOR_UNAVAILABLE",
                        "native_error_code": error.native_error_code,
                    },
                )
                self._reported_sampling_failure = True
            self._log_events(update)
            return update
        except Exception:
            update = self._manager.observe(
                None,
                active_jobs,
                now_monotonic=time.monotonic(),
            )
            if not self._reported_sampling_failure:
                logger.exception(
                    "job.resource_monitor.unexpected_failure",
                    extra={
                        "error_code": ErrorCode.SYS_RESOURCE_MONITOR_UNAVAILABLE.value,
                        "retryable": True,
                        "stage": "resource_sampling",
                        "resource_metric": "MONITOR_HEALTH",
                        "reason": "UNEXPECTED_SAMPLER_FAILURE",
                    },
                )
                self._reported_sampling_failure = True
            self._log_events(update)
            return update

        self._reported_sampling_failure = False
        update = self._manager.observe(
            sample,
            active_jobs,
            now_monotonic=time.monotonic(),
        )
        self._log_events(update)
        return update

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            self._stop_event.set()
            await task
        try:
            await asyncio.to_thread(self._sampler.close)
        except ResourceSamplingUnavailable as error:
            logger.error(
                "job.resource_monitor.close_failed",
                extra={
                    "error_code": ErrorCode.SYS_RESOURCE_MONITOR_UNAVAILABLE.value,
                    "retryable": True,
                    "stage": "resource_monitor_close",
                    "resource_metric": "MONITOR_HEALTH",
                    "observed_value": error.reason,
                    "reason": "MONITOR_CLOSE_FAILED",
                },
            )
        except Exception as error:
            logger.exception(
                "job.resource_monitor.close_unexpected_failure",
                extra={
                    "error_code": ErrorCode.SYS_RESOURCE_MONITOR_UNAVAILABLE.value,
                    "retryable": False,
                    "stage": "resource_monitor_close",
                    "resource_metric": "MONITOR_HEALTH",
                    "reason": "MONITOR_CLOSE_FAILED",
                    "exception_type": type(error).__name__,
                },
            )

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            sample_started = time.monotonic()
            await self.sample_once()
            remaining = SAMPLE_INTERVAL_SECONDS - (time.monotonic() - sample_started)
            if remaining <= 0:
                continue
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=remaining)
            except TimeoutError:
                continue

    @staticmethod
    def _log_events(update: BackpressureUpdate) -> None:
        for event in update.events:
            ResourceMonitorService._log_event(event, update.admission_allowed)

    @staticmethod
    def _log_event(event: BackpressureEvent, admission_allowed: bool) -> None:
        level = (
            logging.WARNING
            if event.action
            in {
                BackpressureAction.PAUSE_REQUESTED,
                BackpressureAction.PRESSURE_CAUSE_ADDED,
                BackpressureAction.ADMISSION_BLOCKED,
            }
            else logging.INFO
        )
        error_code = (
            ErrorCode.SYS_RESOURCE_MONITOR_UNAVAILABLE.value
            if event.reason is not None and event.reason.value == "MONITOR_UNAVAILABLE"
            else None
        )
        logger.log(
            level,
            f"job.backpressure.{event.action.value.lower()}",
            extra={
                "error_code": error_code,
                "retryable": True if error_code is not None else None,
                "job_id": str(event.job_id) if event.job_id is not None else None,
                "stage": event.stage.value if event.stage is not None else None,
                "resource_metric": event.metric.value,
                "observed_value": event.observed_value,
                "threshold_value": event.threshold_value,
                "reason": event.reason.value if event.reason is not None else None,
                "action": event.action.value,
                "admission_allowed": admission_allowed,
            },
        )
