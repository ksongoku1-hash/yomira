from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.domain.job_fairness import (
    ConcurrencyLimiter,
    ConcurrencyLimits,
    DispatchCursor,
)


def test_concurrency_limiter_enforces_global_source_and_host_limits() -> None:
    limits = ConcurrencyLimits(global_limit=4, per_source_limit=2, per_host_limit=2)
    limiter = ConcurrencyLimiter(limits)
    source_a = uuid4()
    source_b = uuid4()

    first = limiter.try_acquire(source_a, "https://a.example:443")
    second = limiter.try_acquire(source_a, "https://a.example:443")
    assert first is not None
    assert second is not None
    assert limiter.try_acquire(source_a, "https://b.example:443") is None
    assert limiter.try_acquire(source_b, "https://a.example:443") is None
    assert limiter.saturated_source_ids == frozenset({source_a})
    assert limiter.saturated_host_keys == frozenset({"https://a.example:443"})

    third = limiter.try_acquire(source_b, "https://b.example:443")
    fourth = limiter.try_acquire(uuid4(), "https://c.example:443")
    assert third is not None
    assert fourth is not None
    assert limiter.active_count == 4
    assert limiter.try_acquire(uuid4(), "https://d.example:443") is None

    limiter.release(first)
    assert limiter.saturated_source_ids == frozenset()
    assert limiter.saturated_host_keys == frozenset()
    with pytest.raises(RuntimeError, match="unknown or already released"):
        limiter.release(first)
    assert limiter.try_acquire(source_b, "https://a.example:443") is not None


def test_concurrency_limiter_releases_reservations_after_exceptions() -> None:
    limiter = ConcurrencyLimiter(
        ConcurrencyLimits(global_limit=2, per_source_limit=1, per_host_limit=1)
    )
    source_id = uuid4()

    with pytest.raises(RuntimeError, match="operation failed"):
        with limiter.reserve(source_id, "https://reader.example:443") as permit:
            assert permit is not None
            assert limiter.active_count == 1
            raise RuntimeError("operation failed")

    assert limiter.active_count == 0
    assert limiter.try_acquire(source_id, "https://reader.example:443") is not None


@pytest.mark.parametrize("host_key", ["", " ", " https://reader.example:443", "reader.example "])
def test_concurrency_limiter_rejects_invalid_host_key_without_mutation(host_key: str) -> None:
    limiter = ConcurrencyLimiter(
        ConcurrencyLimits(global_limit=2, per_source_limit=1, per_host_limit=1)
    )

    with pytest.raises(ValueError, match="host_key must be a non-empty, trimmed origin key"):
        limiter.try_acquire(uuid4(), host_key)

    assert limiter.active_count == 0


@pytest.mark.parametrize("next_priority_slot", [-1, 8, True, 1.5])
def test_dispatch_cursor_rejects_invalid_wheel_positions(next_priority_slot: object) -> None:
    with pytest.raises(ValueError, match="next_priority_slot must be between 0 and 7"):
        DispatchCursor(next_priority_slot=next_priority_slot)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("global_limit", "per_source_limit", "per_host_limit"),
    [
        (0, 1, 1),
        (1, 0, 1),
        (1, 1, -1),
        (True, 1, 1),
        (9, 4, 4),
        (2, 3, 1),
        (2, 1, 3),
    ],
)
def test_concurrency_limits_reject_invalid_values(
    global_limit: object,
    per_source_limit: object,
    per_host_limit: object,
) -> None:
    with pytest.raises(ValueError):
        ConcurrencyLimits(
            global_limit=global_limit,  # type: ignore[arg-type]
            per_source_limit=per_source_limit,  # type: ignore[arg-type]
            per_host_limit=per_host_limit,  # type: ignore[arg-type]
        )


def test_concurrency_limiter_capacity_check_is_atomic_across_threads() -> None:
    limiter = ConcurrencyLimiter(
        ConcurrencyLimits(global_limit=4, per_source_limit=4, per_host_limit=4)
    )
    source_id = uuid4()

    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = tuple(
            executor.submit(limiter.try_acquire, source_id, "https://reader.example:443")
            for _ in range(64)
        )
        results = tuple(future.result() for future in futures)

    granted = tuple(permit for permit in results if permit is not None)
    assert len(granted) == 4
    assert limiter.active_count == 4
    for permit in granted:
        limiter.release(permit)
    assert limiter.active_count == 0
