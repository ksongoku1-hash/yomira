"""Read Linux memory and storage pressure without optional Python packages."""

import os
import stat
import time
from pathlib import Path

from app.domain.job_backpressure import ResourceSample
from app.platform.linux.storage import mount_for
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


def btrfs_devices(device: tuple[int, int], sysfs: Path) -> set[tuple[int, int]]:
    for directory in sysfs.glob("*/devices"):
        devices: set[tuple[int, int]] = set()
        for entry in directory.iterdir():
            major, minor = (entry / "dev").read_text(encoding="ascii").strip().split(":")
            devices.add((int(major), int(minor)))
        if device in devices:
            return devices
    raise ValueError("Btrfs backing devices are unavailable")


def disk_devices(roots: tuple[Path, ...]) -> set[tuple[int, int]]:
    devices: set[tuple[int, int]] = set()
    for path in roots:
        device = path.stat().st_dev
        if os.major(device) != 0:
            devices.add((os.major(device), os.minor(device)))
            continue
        _mount, filesystem, source = mount_for(
            str(path), Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        )
        if filesystem != "btrfs" or not Path(source).is_absolute():
            raise ValueError("Storage has no block-device I/O counters")
        # Btrfs st_dev is virtual; sample every real device, including multi-device volumes.
        info = Path(source).stat()
        if not stat.S_ISBLK(info.st_mode):
            raise ValueError("The Btrfs mount source is not a block device")
        devices.update(btrfs_devices(
            (os.major(info.st_rdev), os.minor(info.st_rdev)), Path("/sys/fs/btrfs")
        ))
    return devices


class LinuxResourceSampler:
    def __init__(self, roots: tuple[Path, ...]) -> None:
        self._roots = roots
        self._last: tuple[float, tuple[int, int, int]] | None = None

    def sample(self) -> ResourceSample:
        try:
            available, total = memory_bytes(Path("/proc/meminfo").read_text(encoding="ascii"))
            devices = disk_devices(self._roots)
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
