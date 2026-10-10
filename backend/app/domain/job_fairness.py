"""Bounded, thread-safe admission limits for download work."""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import TypeVar
from uuid import UUID, uuid4

from app.domain.download_job import DownloadJob, DownloadJobStatus, DownloadPriority

MAX_DISPATCH_WINDOW_SIZE = 8
PRIORITY_WHEEL = (
    DownloadPriority.HIGH,
    DownloadPriority.HIGH,
    DownloadPriority.HIGH,
    DownloadPriority.HIGH,
    DownloadPriority.NORMAL,
    DownloadPriority.NORMAL,
    DownloadPriority.NORMAL,
    DownloadPriority.LOW,
)
_CountKey = TypeVar("_CountKey", UUID, str)


@dataclass(frozen=True, slots=True)
class DispatchCursor:
    next_priority_slot: int = 0
    high_source_id: UUID | None = None
    normal_source_id: UUID | None = None
    low_source_id: UUID | None = None

    def __post_init__(self) -> None:
        if type(self.next_priority_slot) is not int or not 0 <= self.next_priority_slot < len(
            PRIORITY_WHEEL
        ):
            raise ValueError("next_priority_slot must be between 0 and 7")
        for field_name in ("high_source_id", "normal_source_id", "low_source_id"):
            source_id = getattr(self, field_name)
            if source_id is not None and (type(source_id) is not UUID or source_id.version != 4):
                raise ValueError(f"{field_name} must be a UUIDv4 when provided")

    def source_after(self, priority: DownloadPriority) -> UUID | None:
        match priority:
            case DownloadPriority.HIGH:
                return self.high_source_id
            case DownloadPriority.NORMAL:
                return self.normal_source_id
            case DownloadPriority.LOW:
                return self.low_source_id

    def advance(
        self,
        *,
        selected_slot: int,
        priority: DownloadPriority,
        source_id: UUID,
    ) -> DispatchCursor:
        if type(selected_slot) is not int or not 0 <= selected_slot < len(PRIORITY_WHEEL):
            raise ValueError("selected_slot must be between 0 and 7")
        if PRIORITY_WHEEL[selected_slot] is not priority:
            raise ValueError("selected_slot does not correspond to priority")
        if type(source_id) is not UUID or source_id.version != 4:
            raise ValueError("source_id must be a UUIDv4")

        return DispatchCursor(
            next_priority_slot=(selected_slot + 1) % len(PRIORITY_WHEEL),
            high_source_id=(
                source_id if priority is DownloadPriority.HIGH else self.high_source_id
            ),
            normal_source_id=(
                source_id if priority is DownloadPriority.NORMAL else self.normal_source_id
            ),
            low_source_id=(source_id if priority is DownloadPriority.LOW else self.low_source_id),
        )


@dataclass(frozen=True, slots=True)
class QueuedJobCandidate:
    job: DownloadJob
    source_id: UUID
    series_id: UUID | None = None

    def __post_init__(self) -> None:
        if type(self.job) is not DownloadJob:
            raise ValueError("job must be DownloadJob")
        if self.job.status is not DownloadJobStatus.QUEUED:
            raise ValueError("dispatch candidate job must be QUEUED")
        if type(self.source_id) is not UUID or self.source_id.version != 4:
            raise ValueError("source_id must be a UUIDv4")
        if self.series_id is not None and (
            type(self.series_id) is not UUID or self.series_id.version != 4
        ):
            raise ValueError("series_id must be a UUIDv4 or None")


@dataclass(frozen=True, slots=True)
class DispatchWindow:
    candidates: tuple[QueuedJobCandidate, ...]
    cursor: DispatchCursor

    def __post_init__(self) -> None:
        if type(self.candidates) is not tuple:
            raise ValueError("candidates must be a tuple")
        if len(self.candidates) > MAX_DISPATCH_WINDOW_SIZE:
            raise ValueError("dispatch window must not exceed 8 candidates")
        if any(type(candidate) is not QueuedJobCandidate for candidate in self.candidates):
            raise ValueError("candidates must contain QueuedJobCandidate values")
        if len({candidate.job.job_id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("dispatch window must not contain duplicate jobs")
        if type(self.cursor) is not DispatchCursor:
            raise ValueError("cursor must be DispatchCursor")


@dataclass(frozen=True, slots=True)
class ConcurrencyLimits:
    global_limit: int
    per_source_limit: int
    per_host_limit: int

    def __post_init__(self) -> None:
        for field_name in ("global_limit", "per_source_limit", "per_host_limit"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.global_limit > MAX_DISPATCH_WINDOW_SIZE:
            raise ValueError("global_limit must not exceed the dispatch window size 8")
        if self.per_source_limit > self.global_limit:
            raise ValueError("per_source_limit must not exceed global_limit")
        if self.per_host_limit > self.global_limit:
            raise ValueError("per_host_limit must not exceed global_limit")


@dataclass(frozen=True, slots=True)
class ConcurrencyPermit:
    permit_id: UUID
    source_id: UUID
    host_key: str


class ConcurrencyLimiter:
    """Atomically admit operations within global, source, and host caps."""

    def __init__(self, limits: ConcurrencyLimits) -> None:
        if type(limits) is not ConcurrencyLimits:
            raise TypeError("limits must be ConcurrencyLimits")
        self._limits = limits
        self._lock = Lock()
        self._active: dict[UUID, ConcurrencyPermit] = {}
        self._active_by_source: dict[UUID, int] = {}
        self._active_by_host: dict[str, int] = {}

    @property
    def limits(self) -> ConcurrencyLimits:
        return self._limits

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    @property
    def saturated_source_ids(self) -> frozenset[UUID]:
        with self._lock:
            return frozenset(
                source_id
                for source_id, count in self._active_by_source.items()
                if count >= self._limits.per_source_limit
            )

    @property
    def saturated_host_keys(self) -> frozenset[str]:
        with self._lock:
            return frozenset(
                host_key
                for host_key, count in self._active_by_host.items()
                if count >= self._limits.per_host_limit
            )

    def try_acquire(self, source_id: UUID, host_key: str) -> ConcurrencyPermit | None:
        self._validate_source_id(source_id)
        self._validate_host_key(host_key)

        with self._lock:
            if len(self._active) >= self._limits.global_limit:
                return None
            if self._active_by_source.get(source_id, 0) >= self._limits.per_source_limit:
                return None
            if self._active_by_host.get(host_key, 0) >= self._limits.per_host_limit:
                return None

            permit = ConcurrencyPermit(uuid4(), source_id, host_key)
            self._active[permit.permit_id] = permit
            self._active_by_source[source_id] = self._active_by_source.get(source_id, 0) + 1
            self._active_by_host[host_key] = self._active_by_host.get(host_key, 0) + 1
            return permit

    def release(self, permit: ConcurrencyPermit) -> None:
        if type(permit) is not ConcurrencyPermit:
            raise TypeError("permit must be ConcurrencyPermit")

        with self._lock:
            active_permit = self._active.get(permit.permit_id)
            if active_permit != permit:
                raise RuntimeError("Concurrency permit is unknown or already released.")

            del self._active[permit.permit_id]
            self._decrement(self._active_by_source, permit.source_id)
            self._decrement(self._active_by_host, permit.host_key)

    @contextmanager
    def reserve(
        self,
        source_id: UUID,
        host_key: str,
    ) -> Generator[ConcurrencyPermit | None]:
        permit = self.try_acquire(source_id, host_key)
        try:
            yield permit
        finally:
            if permit is not None:
                self.release(permit)

    @staticmethod
    def _validate_source_id(source_id: UUID) -> None:
        if type(source_id) is not UUID or source_id.version != 4:
            raise ValueError("source_id must be a UUIDv4")

    @staticmethod
    def _validate_host_key(host_key: str) -> None:
        if type(host_key) is not str or not host_key or host_key != host_key.strip():
            raise ValueError("host_key must be a non-empty, trimmed origin key")

    @staticmethod
    def _decrement(counts: dict[_CountKey, int], key: _CountKey) -> None:
        remaining = counts[key] - 1
        if remaining:
            counts[key] = remaining
        else:
            del counts[key]
