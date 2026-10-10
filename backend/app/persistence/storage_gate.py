"""Process-wide fail-closed admission for shared storage mutations."""

from __future__ import annotations

import errno
from collections.abc import Callable, Generator
from contextlib import contextmanager
from threading import Condition, Lock
from time import monotonic
from uuid import UUID


class StorageGateClosedError(RuntimeError):
    """A storage mutation was attempted after shared admission had closed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("The shared storage gate is closed.")


StorageGateListener = Callable[[str], None]
_DISK_FULL_WINERRORS = frozenset({39, 112})


def is_disk_full_os_error(error: OSError) -> bool:
    return error.errno == errno.ENOSPC or getattr(error, "winerror", None) in _DISK_FULL_WINERRORS


class SharedStorageGate:
    """Atomically stop new mutation permits and track operations already admitted.

    The gate may reopen only after the recovery coordinator has verified worker
    quiescence, SQLite integrity, and the approved disk budget.
    """

    def __init__(self) -> None:
        self._condition = Condition(Lock())
        self._closed_reason: str | None = None
        self._active_mutations = 0
        self._next_listener_id = 1
        self._listeners: dict[int, StorageGateListener] = {}

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed_reason is not None

    @property
    def closed_reason(self) -> str | None:
        with self._condition:
            return self._closed_reason

    @property
    def active_mutations(self) -> int:
        with self._condition:
            return self._active_mutations

    def ensure_open(self) -> None:
        with self._condition:
            reason = self._closed_reason
        if reason is not None:
            raise StorageGateClosedError(reason)

    def close(self, reason: str) -> bool:
        if type(reason) is not str or not reason or len(reason) > 80:
            raise ValueError("reason must be a non-empty string of at most 80 characters")
        with self._condition:
            if self._closed_reason is not None:
                return False
            self._closed_reason = reason
            listeners = tuple(self._listeners.values())
            self._condition.notify_all()

        # The closed state is already visible before any callback is invoked.
        # One failing observer must not reopen admission or block other observers.
        for listener in listeners:
            try:
                listener(reason)
            except Exception:
                continue
        return True

    def reopen_after_recovery(self) -> bool:
        """Reopen admission only when every previously admitted mutation ended."""
        with self._condition:
            if self._closed_reason is None:
                return False
            if self._active_mutations:
                return False
            self._closed_reason = None
            self._condition.notify_all()
            return True

    def subscribe(self, listener: StorageGateListener) -> Callable[[], None]:
        if not callable(listener):
            raise TypeError("listener must be callable")
        with self._condition:
            listener_id = self._next_listener_id
            self._next_listener_id += 1
            self._listeners[listener_id] = listener
            reason = self._closed_reason

        if reason is not None:
            try:
                listener(reason)
            except Exception:
                self.unsubscribe(listener_id)
                raise

        def unsubscribe() -> None:
            self.unsubscribe(listener_id)

        return unsubscribe

    def unsubscribe(self, listener_id: int) -> None:
        if type(listener_id) is not int or listener_id <= 0:
            raise ValueError("listener_id must be a positive integer")
        with self._condition:
            self._listeners.pop(listener_id, None)

    @contextmanager
    def mutation(self) -> Generator[None]:
        """Grant one short mutation permit, or fail immediately when closed."""
        with self._condition:
            reason = self._closed_reason
            if reason is not None:
                raise StorageGateClosedError(reason)
            self._active_mutations += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_mutations -= 1
                self._condition.notify_all()

    @contextmanager
    def quiesced_recovery_mutation(
        self,
        *,
        job_id: UUID,
        quiescence_ack: object,
    ) -> Generator[None]:
        """Track a one-job recovery CAS authorized after worker quiescence."""
        from app.services.job_recovery import WorkerQuiescenceAck

        if type(job_id) is not UUID:
            raise TypeError("job_id must be a UUID")
        if type(quiescence_ack) is not WorkerQuiescenceAck:
            raise TypeError("quiescence_ack must be a WorkerQuiescenceAck")
        if quiescence_ack.job_id != job_id:
            raise ValueError("quiescence_ack belongs to a different job")
        with self._condition:
            self._active_mutations += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_mutations -= 1
                self._condition.notify_all()

    def wait_for_quiescence(self, timeout_seconds: float) -> bool:
        if type(timeout_seconds) not in {int, float} or timeout_seconds < 0:
            raise ValueError("timeout_seconds must be a non-negative number")
        deadline = monotonic() + timeout_seconds
        with self._condition:
            while self._active_mutations:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True
