"""Read Linux memory and storage pressure without optional Python packages."""

import os
import time
from pathlib import Path

from app.domain.job_backpressure import ResourceSample
from app.platform.windows_resources import ResourceSamplingUnavailable


def memory_bytes(text: str) -> tuple[int, int]:
    fields = {
        line.split(":", 1)[0]: line.split(":", 1)[1].split()
        for line in text.splitlines()
        if ":" in line
    }
    values: list[int] = []
    for name in ("MemAvailable", "MemTotal"):
        value = fields.get(name, [])
        if len(value) != 2 or value[1] != "kB" or not value[0].isdigit():
            raise ValueError("Incomplete Linux memory counters")
        values.append(int(value[0]) * 1024)
    return values[0], values[1]


def disk_totals(text: str, devices: set[tuple[int, int]]) -> tuple[int, int, int]:
    values: dict[tuple[int, int], tuple[int, int, int]] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 14 or not all(value.isdigit() for value in (fields[0], fields[1])):
            continue
        key = int(fields[0]), int(fields[1])
        if key in devices:
            values[key] = (
                int(fields[12]),
                int(fields[6]) + int(fields[10]),
                int(fields[3]) + int(fields[7]),
            )
    if values.keys() != devices or not devices:
        raise ValueError("Storage I/O counters are unavailable for the application volumes")
    return tuple(sum(value[index] for value in values.values()) for index in range(3))  # type: ignore[return-value]


class LinuxResourceSampler:
    def __init__(self, roots: tuple[Path, ...]) -> None:
        self._roots = roots
        self._last: tuple[float, tuple[int, int, int]] | None = None

    def sample(self) -> ResourceSample:
        try:
            available, total = memory_bytes(Path("/proc/meminfo").read_text(encoding="ascii"))
            devices = {
                (os.major(path.stat().st_dev), os.minor(path.stat().st_dev)) for path in self._roots
            }
            counters = disk_totals(Path("/proc/diskstats").read_text(encoding="ascii"), devices)
            now = time.monotonic()
            previous = self._last
            self._last = now, counters
            busy = latency = None
            if previous is not None:
                seconds = now - previous[0]
                delta = tuple(
                    current - old for current, old in zip(counters, previous[1], strict=True)
                )
                if seconds <= 0 or any(value < 0 for value in delta):
                    raise ValueError("Linux I/O counters reset")
                busy = min(100.0, delta[0] / (seconds * 10 * len(devices)))
                latency = delta[1] / delta[2] if delta[2] else 0.0
            return ResourceSample(available, total, busy, latency, now)
        except (OSError, ValueError, IndexError) as error:
            raise ResourceSamplingUnavailable("LINUX_COUNTERS_UNAVAILABLE") from error

    def close(self) -> None:
        self._last = None
