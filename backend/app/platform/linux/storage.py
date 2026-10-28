"""Linux local-volume identity, advisory locks and no-clobber publication."""

import ctypes
import errno
import os
import posixpath
import re
import stat
import time
from pathlib import Path

LOCAL_FILESYSTEMS = frozenset(
    {
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "f2fs",
        "zfs",
        "overlay",
        "tmpfs",
        "ramfs",
        "vfat",
        "exfat",
        "ntfs3",
    }
)


def mount_for(path: str, mountinfo: str) -> tuple[str, str, str]:
    if not posixpath.isabs(path):
        raise ValueError("An absolute Linux path is required")
    candidates: list[tuple[str, str, str]] = []
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        filesystem = after.split()
        if not separator or len(fields) < 6 or len(filesystem) < 2:
            continue
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
        if path == mount or path.startswith(mount.rstrip("/") + "/"):
            source = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), filesystem[1])
            candidates.append((mount, filesystem[0], source))
    if not candidates:
        raise OSError("The data volume is not present in /proc/self/mountinfo")
    return max(candidates, key=lambda item: len(item[0]))


def local_mount(path: Path) -> str:
    with Path("/proc/self/mountinfo").open(encoding="utf-8") as stream:
        mountinfo = stream.read(1024 * 1024 + 1)
    if len(mountinfo) > 1024 * 1024:
        raise OSError("The mount table is too large")
    mount, filesystem, _source = mount_for(str(path), mountinfo)
    if filesystem not in LOCAL_FILESYSTEMS:
        raise OSError("Yomira requires a supported local filesystem, not network storage")
    return mount


def file_identity(path: Path) -> tuple[int, bytes]:
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("The storage root must be a directory")
    return info.st_dev, info.st_ino.to_bytes(16, "big")


def lock_file(path: Path, *, timeout: float) -> int:
    import fcntl

    path.parent.mkdir(mode=0o700, exist_ok=True)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise OSError("The lock directory must belong to this user and be private")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise OSError("Invalid lock file")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return descriptor
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another process holds this lock") from None
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
    except BaseException:
        os.close(descriptor)
        raise


def move_new(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "Linux renameat2 is required for safe publication")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))
    # File contents are synced by the publisher; persist both directory entries as well.
    for parent in {source.parent, destination.parent}:
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
