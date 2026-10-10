"""Resource thresholds and bounded backpressure decisions for active jobs."""

import math
import time
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.domain.download_job import DownloadJobStatus
from app.domain.job_fairness import MAX_DISPATCH_WINDOW_SIZE
from app.domain.job_pause import (
    JobPauseController,
    JobPauseState,
    PauseReason,
)

MEBIBYTE = 1024 * 1024
GIBIBYTE = 1024 * MEBIBYTE
MAX_RESOURCE_SAMPLE_AGE_SECONDS = 2.0
MEMORY_RECOVERY_SAMPLE_COUNT = 3
DISK_PRESSURE_SAMPLE_COUNT = 3
DISK_RECOVERY_SAMPLE_COUNT = 5
MONITOR_RECOVERY_SAMPLE_COUNT = 3
DISK_BUSY_ENTER_PERCENT = 90.0
DISK_BUSY_EXIT_PERCENT = 70.0
DISK_LATENCY_ENTER_MS = 250.0
DISK_LATENCY_EXIT_MS = 100.0


class BackpressureState(StrEnum):
    MONITOR_UNAVAILABLE = "MONITOR_UNAVAILABLE"
    NORMAL = "NORMAL"
    PRESSURE = "PRESSURE"


class PressureCause(StrEnum):
    LOW_AVAILABLE_RAM = "LOW_AVAILABLE_RAM"
    DISK_IO_SATURATION = "DISK_IO_SATURATION"
    MONITOR_UNAVAILABLE = "MONITOR_UNAVAILABLE"


class ResourceMetric(StrEnum):
    AVAILABLE_RAM_BYTES = "AVAILABLE_RAM_BYTES"
    DISK_BUSY_PERCENT = "DISK_BUSY_PERCENT"
    DISK_LATENCY_MS = "DISK_LATENCY_MS"
    MONITOR_HEALTH = "MONITOR_HEALTH"


class BackpressureAction(StrEnum):
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    RESUME_REQUESTED = "RESUME_REQUESTED"
    PRESSURE_CAUSE_ADDED = "PRESSURE_CAUSE_ADDED"
    PRESSURE_CAUSE_CLEARED = "PRESSURE_CAUSE_CLEARED"
    ADMISSION_BLOCKED = "ADMISSION_BLOCKED"
    ADMISSION_ALLOWED = "ADMISSION_ALLOWED"


@dataclass(frozen=True, slots=True)
class ResourceSample:
    available_memory_bytes: int
    total_memory_bytes: int
    disk_busy_percent: float | None
    disk_latency_ms: float | None
    sampled_at_monotonic: float

    def __post_init__(self) -> None:
        if (
            type(self.available_memory_bytes) is not int
            or self.available_memory_bytes < 0
        ):
            raise ValueError("available_memory_bytes must be a non-negative integer")
        if type(self.total_memory_bytes) is not int or self.total_memory_bytes <= 0:
            raise ValueError("total_memory_bytes must be a positive integer")
        if self.available_memory_bytes > self.total_memory_bytes:
            raise ValueError("available_memory_bytes must not exceed total_memory_bytes")
        if (
            type(self.sampled_at_monotonic) not in {int, float}
            or not math.isfinite(self.sampled_at_monotonic)
            or self.sampled_at_monotonic < 0
        ):
            raise ValueError("sampled_at_monotonic must be finite and non-negative")
        for name, value in (
            ("disk_busy_percent", self.disk_busy_percent),
            ("disk_latency_ms", self.disk_latency_ms),
        ):
            if value is not None and (
                type(value) not in {int, float}
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be finite and non-negative when provided")
        if (self.disk_busy_percent is None) is not (self.disk_latency_ms is None):
            raise ValueError("disk counters must either both be available or both be absent")

    @property
    def complete(self) -> bool:
        return self.disk_busy_percent is not None and self.disk_latency_ms is not None


@dataclass(frozen=True, slots=True)
class ActiveJobSnapshot:
    job_id: UUID
    stage: DownloadJobStatus

    def __post_init__(self) -> None:
        if type(self.job_id) is not UUID or self.job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        if type(self.stage) is not DownloadJobStatus:
            raise ValueError("stage must be a supported DownloadJobStatus")
        if self.stage not in {
            DownloadJobStatus.FETCHING_METADATA,
            DownloadJobStatus.DOWNLOADING,
            DownloadJobStatus.VALIDATING,
            DownloadJobStatus.PROCESSING,
            DownloadJobStatus.PACKAGING,
            DownloadJobStatus.VERIFYING,
            DownloadJobStatus.INDEXING,
        }:
            raise ValueError("active job snapshot must identify a running stage")


@dataclass(frozen=True, slots=True)
class BackpressureEvent:
    action: BackpressureAction
    reason: PressureCause | None
    metric: ResourceMetric
    observed_value: int | float | None
    threshold_value: int | float | None
    job_id: UUID | None = None
    stage: DownloadJobStatus | None = None


@dataclass(frozen=True, slots=True)
class BackpressureUpdate:
    state: BackpressureState
    admission_allowed: bool
    active_causes: frozenset[PressureCause]
    events: tuple[BackpressureEvent, ...]


@dataclass(frozen=True, slots=True)
class _Observation:
    metric: ResourceMetric
    observed_value: int | float | None
    threshold_value: int | float | None


class BackpressureManager:
    """Apply fixed hysteresis thresholds and pause causes without persisting job state."""

    def __init__(self, pause_controller: JobPauseController) -> None:
        if type(pause_controller) is not JobPauseController:
            raise TypeError("pause_controller must be a JobPauseController")
        self._pause_controller = pause_controller
        self._monitor_available = False
        self._ever_received_complete_sample = False
        self._monitor_recovery_samples = 0
        self._memory_pressure = False
        self._memory_recovery_samples = 0
        self._disk_pressure = False
        self._disk_pressure_samples = 0
        self._disk_recovery_samples = 0
        self._last_sampled_at: float | None = None
        self._last_complete_sample: ResourceSample | None = None
        self._last_state: BackpressureState | None = None

    @property
    def state(self) -> BackpressureState:
        if not self._monitor_available:
            return BackpressureState.MONITOR_UNAVAILABLE
        if self._memory_pressure or self._disk_pressure:
            return BackpressureState.PRESSURE
        return BackpressureState.NORMAL

    @property
    def admission_allowed(self) -> bool:
        return self.state is BackpressureState.NORMAL

    @property
    def active_causes(self) -> frozenset[PressureCause]:
        causes: set[PressureCause] = set()
        if self._memory_pressure:
            causes.add(PressureCause.LOW_AVAILABLE_RAM)
        if self._disk_pressure:
            causes.add(PressureCause.DISK_IO_SATURATION)
        if not self._monitor_available:
            causes.add(PressureCause.MONITOR_UNAVAILABLE)
        return frozenset(causes)

    def observe(
        self,
        sample: ResourceSample | None,
        active_jobs: tuple[ActiveJobSnapshot, ...],
        *,
        now_monotonic: float | None = None,
    ) -> BackpressureUpdate:
        self._validate_active_jobs(active_jobs)
        now = time.monotonic() if now_monotonic is None else now_monotonic
        if (
            type(now) not in {int, float}
            or not math.isfinite(now)
            or now < 0
        ):
            raise ValueError("now_monotonic must be finite and non-negative")

        reasons_before = {
            job.job_id: self._pause_controller.pause_reasons(job.job_id)
            for job in active_jobs
        }
        states_before = {
            job.job_id: self._pause_controller.state(job.job_id)
            for job in active_jobs
        }
        previous_causes = self.active_causes
        previous_state = self.state

        if not self._sample_is_current(sample, now):
            self._mark_monitor_unavailable()
            current_sample = None
        else:
            assert sample is not None
            self._last_sampled_at = sample.sampled_at_monotonic
            self._last_complete_sample = sample
            self._mark_monitor_available()
            current_sample = sample
            if self._monitor_available:
                self._update_memory_pressure(sample)
                self._update_disk_pressure(sample)

        current_causes = self.active_causes
        current_state = self.state
        desired_reasons = self._desired_pause_reasons(current_causes)
        events: list[BackpressureEvent] = []
        for job in active_jobs:
            if states_before[job.job_id] is JobPauseState.CANCELLATION_REQUESTED:
                continue

            prior_reasons = reasons_before[job.job_id]
            for added_reason in desired_reasons.difference(prior_reasons):
                self._pause_controller.request_pause(job.job_id, reason=added_reason)
            managed_reasons_before = prior_reasons.intersection(
                {
                    PauseReason.BACKPRESSURE,
                    PauseReason.MONITOR_UNAVAILABLE,
                }
            )
            for removed_reason in managed_reasons_before.difference(desired_reasons):
                self._pause_controller.resume(job.job_id, reason=removed_reason)

            handled_reasons: set[PauseReason] = set()
            for cause in sorted(previous_causes ^ current_causes, key=lambda item: item.value):
                pause_reason = self._pause_reason_for(cause)
                action = self._cause_change_action(
                    cause=cause,
                    added=cause in current_causes,
                    pause_reason=pause_reason,
                    prior_reasons=prior_reasons,
                    desired_reasons=desired_reasons,
                )
                if action in {
                    BackpressureAction.PAUSE_REQUESTED,
                    BackpressureAction.RESUME_REQUESTED,
                }:
                    handled_reasons.add(pause_reason)
                for observation in self._observations_for(
                    cause,
                    adding=cause in current_causes,
                    sample=current_sample,
                ):
                    events.append(
                        self._event_for_job(
                            action,
                            cause,
                            observation,
                            job,
                        )
                    )

            for newly_paused_reason in desired_reasons.difference(prior_reasons):
                if newly_paused_reason in handled_reasons:
                    continue
                causes = self._causes_for_pause_reason(newly_paused_reason, current_causes)
                for cause in causes:
                    for observation in self._observations_for(
                        cause,
                        adding=True,
                        sample=current_sample,
                    ):
                        events.append(
                            self._event_for_job(
                                BackpressureAction.PAUSE_REQUESTED,
                                cause,
                                observation,
                                job,
                            )
                        )

        for cause in sorted(previous_causes ^ current_causes, key=lambda item: item.value):
            if not active_jobs:
                for observation in self._observations_for(
                    cause,
                    adding=cause in current_causes,
                    sample=current_sample,
                ):
                    events.append(
                        BackpressureEvent(
                            action=(
                                BackpressureAction.PRESSURE_CAUSE_ADDED
                                if cause in current_causes
                                else BackpressureAction.PRESSURE_CAUSE_CLEARED
                            ),
                            reason=cause,
                            metric=observation.metric,
                            observed_value=observation.observed_value,
                            threshold_value=observation.threshold_value,
                        )
                    )

        admission_allowed = current_state is BackpressureState.NORMAL
        if self._last_state is None or previous_state is not current_state:
            events.append(
                BackpressureEvent(
                    action=(
                        BackpressureAction.ADMISSION_ALLOWED
                        if admission_allowed
                        else BackpressureAction.ADMISSION_BLOCKED
                    ),
                    reason=self._primary_cause(current_causes),
                    metric=self._primary_metric(current_causes),
                    observed_value=self._primary_observation_value(
                        current_causes,
                        current_sample,
                    ),
                    threshold_value=self._primary_observation_threshold(
                        current_causes,
                        current_sample,
                    ),
                )
            )
        self._last_state = current_state
        return BackpressureUpdate(
            state=current_state,
            admission_allowed=admission_allowed,
            active_causes=current_causes,
            events=tuple(events),
        )

    def _sample_is_current(self, sample: ResourceSample | None, now: float) -> bool:
        if sample is None or type(sample) is not ResourceSample or not sample.complete:
            return False
        age = now - sample.sampled_at_monotonic
        return (
            0 <= age <= MAX_RESOURCE_SAMPLE_AGE_SECONDS
            and (
                self._last_sampled_at is None
                or sample.sampled_at_monotonic > self._last_sampled_at
            )
        )

    def _mark_monitor_unavailable(self) -> None:
        self._monitor_available = False
        self._monitor_recovery_samples = 0

    def _mark_monitor_available(self) -> None:
        if self._monitor_available:
            self._monitor_recovery_samples = 0
            return
        self._monitor_recovery_samples += 1
        required = (
            1
            if not self._ever_received_complete_sample
            else MONITOR_RECOVERY_SAMPLE_COUNT
        )
        if self._monitor_recovery_samples >= required:
            self._monitor_available = True
            self._ever_received_complete_sample = True
            self._monitor_recovery_samples = 0

    def _update_memory_pressure(self, sample: ResourceSample) -> None:
        critical_threshold = max(
            512 * MEBIBYTE,
            int(sample.total_memory_bytes * 0.10),
        )
        recovery_threshold = max(
            768 * MEBIBYTE,
            int(sample.total_memory_bytes * 0.15),
        )
        if not self._memory_pressure:
            if sample.available_memory_bytes <= critical_threshold:
                self._memory_pressure = True
                self._memory_recovery_samples = 0
        elif sample.available_memory_bytes >= recovery_threshold:
            self._memory_recovery_samples += 1
            if self._memory_recovery_samples >= MEMORY_RECOVERY_SAMPLE_COUNT:
                self._memory_pressure = False
                self._memory_recovery_samples = 0
        else:
            self._memory_recovery_samples = 0

    def _update_disk_pressure(self, sample: ResourceSample) -> None:
        assert sample.disk_busy_percent is not None
        assert sample.disk_latency_ms is not None
        disk_critical = (
            sample.disk_busy_percent >= DISK_BUSY_ENTER_PERCENT
            or sample.disk_latency_ms >= DISK_LATENCY_ENTER_MS
        )
        disk_recovered = (
            sample.disk_busy_percent <= DISK_BUSY_EXIT_PERCENT
            and sample.disk_latency_ms <= DISK_LATENCY_EXIT_MS
        )
        if not self._disk_pressure:
            self._disk_pressure_samples = (
                self._disk_pressure_samples + 1 if disk_critical else 0
            )
            if self._disk_pressure_samples >= DISK_PRESSURE_SAMPLE_COUNT:
                self._disk_pressure = True
                self._disk_pressure_samples = 0
        elif disk_recovered:
            self._disk_recovery_samples += 1
            if self._disk_recovery_samples >= DISK_RECOVERY_SAMPLE_COUNT:
                self._disk_pressure = False
                self._disk_recovery_samples = 0
        else:
            self._disk_recovery_samples = 0

    @staticmethod
    def _validate_active_jobs(active_jobs: tuple[ActiveJobSnapshot, ...]) -> None:
        if type(active_jobs) is not tuple:
            raise ValueError("active_jobs must be a tuple")
        if len(active_jobs) > MAX_DISPATCH_WINDOW_SIZE:
            raise ValueError("active_jobs must not exceed eight snapshots")
        if any(type(job) is not ActiveJobSnapshot for job in active_jobs):
            raise ValueError("active_jobs must contain ActiveJobSnapshot values")
        if len({job.job_id for job in active_jobs}) != len(active_jobs):
            raise ValueError("active_jobs must not contain duplicate job IDs")

    @staticmethod
    def _desired_pause_reasons(causes: frozenset[PressureCause]) -> frozenset[PauseReason]:
        reasons: set[PauseReason] = set()
        if {
            PressureCause.LOW_AVAILABLE_RAM,
            PressureCause.DISK_IO_SATURATION,
        }.intersection(causes):
            reasons.add(PauseReason.BACKPRESSURE)
        if PressureCause.MONITOR_UNAVAILABLE in causes:
            reasons.add(PauseReason.MONITOR_UNAVAILABLE)
        return frozenset(reasons)

    @staticmethod
    def _pause_reason_for(cause: PressureCause) -> PauseReason:
        return (
            PauseReason.MONITOR_UNAVAILABLE
            if cause is PressureCause.MONITOR_UNAVAILABLE
            else PauseReason.BACKPRESSURE
        )

    @staticmethod
    def _cause_change_action(
        *,
        cause: PressureCause,
        added: bool,
        pause_reason: PauseReason,
        prior_reasons: frozenset[PauseReason],
        desired_reasons: frozenset[PauseReason],
    ) -> BackpressureAction:
        if added:
            return (
                BackpressureAction.PAUSE_REQUESTED
                if pause_reason not in prior_reasons
                else BackpressureAction.PRESSURE_CAUSE_ADDED
            )
        return (
            BackpressureAction.RESUME_REQUESTED
            if pause_reason not in desired_reasons
            else BackpressureAction.PRESSURE_CAUSE_CLEARED
        )

    def _observations_for(
        self,
        cause: PressureCause,
        *,
        adding: bool,
        sample: ResourceSample | None,
    ) -> tuple[_Observation, ...]:
        if cause is PressureCause.MONITOR_UNAVAILABLE:
            return (
                _Observation(
                    metric=ResourceMetric.MONITOR_HEALTH,
                    observed_value=(
                        None
                        if sample is None
                        else MONITOR_RECOVERY_SAMPLE_COUNT
                    ),
                    threshold_value=(
                        MAX_RESOURCE_SAMPLE_AGE_SECONDS
                        if adding
                        else MONITOR_RECOVERY_SAMPLE_COUNT
                    ),
                ),
            )

        active_sample = sample or self._last_complete_sample
        if active_sample is None:
            return ()
        if cause is PressureCause.LOW_AVAILABLE_RAM:
            threshold = max(
                512 * MEBIBYTE,
                int(active_sample.total_memory_bytes * (0.10 if adding else 0.15)),
            )
            if not adding:
                threshold = max(
                    768 * MEBIBYTE,
                    int(active_sample.total_memory_bytes * 0.15),
                )
            return (
                _Observation(
                    ResourceMetric.AVAILABLE_RAM_BYTES,
                    active_sample.available_memory_bytes,
                    threshold,
                ),
            )

        assert active_sample.disk_busy_percent is not None
        assert active_sample.disk_latency_ms is not None
        if adding:
            observations: list[_Observation] = []
            if active_sample.disk_busy_percent >= DISK_BUSY_ENTER_PERCENT:
                observations.append(
                    _Observation(
                        ResourceMetric.DISK_BUSY_PERCENT,
                        active_sample.disk_busy_percent,
                        DISK_BUSY_ENTER_PERCENT,
                    )
                )
            if active_sample.disk_latency_ms >= DISK_LATENCY_ENTER_MS:
                observations.append(
                    _Observation(
                        ResourceMetric.DISK_LATENCY_MS,
                        active_sample.disk_latency_ms,
                        DISK_LATENCY_ENTER_MS,
                    )
                )
            return tuple(observations)
        return (
            _Observation(
                ResourceMetric.DISK_BUSY_PERCENT,
                active_sample.disk_busy_percent,
                DISK_BUSY_EXIT_PERCENT,
            ),
            _Observation(
                ResourceMetric.DISK_LATENCY_MS,
                active_sample.disk_latency_ms,
                DISK_LATENCY_EXIT_MS,
            ),
        )

    @staticmethod
    def _event_for_job(
        action: BackpressureAction,
        cause: PressureCause,
        observation: _Observation,
        job: ActiveJobSnapshot,
    ) -> BackpressureEvent:
        return BackpressureEvent(
            action=action,
            reason=cause,
            metric=observation.metric,
            observed_value=observation.observed_value,
            threshold_value=observation.threshold_value,
            job_id=job.job_id,
            stage=job.stage,
        )

    @staticmethod
    def _primary_cause(causes: frozenset[PressureCause]) -> PressureCause | None:
        if PressureCause.LOW_AVAILABLE_RAM in causes:
            return PressureCause.LOW_AVAILABLE_RAM
        if PressureCause.DISK_IO_SATURATION in causes:
            return PressureCause.DISK_IO_SATURATION
        if PressureCause.MONITOR_UNAVAILABLE in causes:
            return PressureCause.MONITOR_UNAVAILABLE
        return None

    @staticmethod
    def _primary_metric(causes: frozenset[PressureCause]) -> ResourceMetric:
        if PressureCause.LOW_AVAILABLE_RAM in causes:
            return ResourceMetric.AVAILABLE_RAM_BYTES
        if PressureCause.DISK_IO_SATURATION in causes:
            return ResourceMetric.DISK_BUSY_PERCENT
        return ResourceMetric.MONITOR_HEALTH

    def _primary_observation_value(
        self,
        causes: frozenset[PressureCause],
        sample: ResourceSample | None,
    ) -> int | float | None:
        cause = self._primary_cause(causes)
        if cause is None:
            active_sample = sample or self._last_complete_sample
            return (
                None
                if active_sample is None
                else active_sample.available_memory_bytes
            )
        observations = self._observations_for(cause, adding=True, sample=sample)
        return observations[0].observed_value if observations else None

    def _primary_observation_threshold(
        self,
        causes: frozenset[PressureCause],
        sample: ResourceSample | None,
    ) -> int | float | None:
        cause = self._primary_cause(causes)
        if cause is None:
            return None
        observations = self._observations_for(cause, adding=True, sample=sample)
        return observations[0].threshold_value if observations else None

    @staticmethod
    def _causes_for_pause_reason(
        reason: PauseReason,
        causes: frozenset[PressureCause],
    ) -> tuple[PressureCause, ...]:
        if reason is PauseReason.MONITOR_UNAVAILABLE:
            return (PressureCause.MONITOR_UNAVAILABLE,)
        return tuple(
            cause
            for cause in (
                PressureCause.LOW_AVAILABLE_RAM,
                PressureCause.DISK_IO_SATURATION,
            )
            if cause in causes
        )
