import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import settings
from app.factory import create_app
from app.persistence.windows_paths import DataRootIdentity, DataRootIdentityError, windows_path_key
from app.platform.linux.resources import disk_totals, memory_bytes
from app.platform.linux.storage import lock_file, mount_for, move_new
from app.settings import Settings
from app.single_instance import InstanceAlreadyRunningError, LinuxFileGuard


def test_mount_parser_selects_longest_mount_and_decodes_spaces() -> None:
    mounts = (
        "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "2 1 8:2 / /home/Manga\\040disk rw - btrfs /dev/sda2 rw\n"
        "3 1 0:40 / /home/remote rw - nfs server:/data rw\n"
    )
    assert mount_for("/home/Manga disk/books", mounts) == ("/home/Manga disk", "btrfs")
    assert mount_for("/home/Manga disk-two/books", mounts) == ("/", "ext4")
    assert mount_for("/home/remote/books", mounts)[1] == "nfs"
    with pytest.raises(ValueError):
        mount_for("relative/path", mounts)


def test_linux_memory_and_disk_counters_are_validated() -> None:
    assert memory_bytes("MemAvailable: 100 kB\nMemTotal: 1000 kB\n") == (102400, 1024000)
    with pytest.raises(ValueError):
        memory_bytes("MemTotal: 1000 kB")
    counters = "8 1 sda1 10 0 0 20 30 0 0 40 0 50 0\n8 2 sda2 100 0 0 200 300 0 0 400 0 500 0"
    assert disk_totals(counters, {(8, 1)}) == (50, 60, 40)
    with pytest.raises(ValueError):
        disk_totals(counters, {(8, 9)})


def test_xdg_data_home_ignores_relative_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "os", SimpleNamespace(name="posix", environ={"XDG_DATA_HOME": str(tmp_path)})
    )
    assert settings.Settings().data_root == tmp_path / "yomira"
    monkeypatch.setattr(
        settings, "os", SimpleNamespace(name="posix", environ={"XDG_DATA_HOME": "relative"})
    )
    assert settings.Settings().data_root == Path.home() / ".local/share/yomira"


native = pytest.mark.skipif(sys.platform != "linux", reason="Requires a real Linux kernel")


@native
def test_native_identity_is_case_sensitive_and_replacement_latches(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    identity = DataRootIdentity.capture(root)
    identity.assert_current()
    assert windows_path_key(root / "A") != windows_path_key(root / "a")
    previous = tmp_path / "previous"
    root.rename(previous)
    root.mkdir()
    with pytest.raises(DataRootIdentityError):
        identity.assert_current()
    root.rmdir()
    previous.rename(root)
    with pytest.raises(DataRootIdentityError):
        identity.assert_current()


@native
def test_native_publication_never_overwrites(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"new")
    target.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        move_new(source, target)
    assert source.read_bytes() == b"new" and target.read_bytes() == b"original"
    target.unlink()
    move_new(source, target)
    assert not source.exists() and target.read_bytes() == b"new"


@native
def test_native_file_lock_excludes_another_process(tmp_path: Path) -> None:
    lock = tmp_path / "locks" / "job.lock"
    descriptor = lock_file(lock, timeout=0)
    try:
        code = (
            "from pathlib import Path; from app.platform.linux.storage import lock_file; "
            "lock_file(Path(__import__('sys').argv[1]), timeout=0)"
        )
        result = subprocess.run([sys.executable, "-c", code, str(lock)], capture_output=True)
        assert result.returncode != 0 and b"TimeoutError" in result.stderr
    finally:
        os.close(descriptor)
    os.close(lock_file(lock, timeout=0))
    link = lock.parent / "bad.lock"
    link.symlink_to(lock)
    with pytest.raises(OSError):
        lock_file(link, timeout=0)


@native
def test_native_single_instance_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    first, second = LinuxFileGuard(), LinuxFileGuard()
    first.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunningError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


@native
@pytest.mark.parametrize("environment", ["test", "prod"])
def test_native_application_starts_with_local_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    app = create_app(
        Settings.model_validate(
            {
                "app_env": environment,
                "data_root": tmp_path / "data",
                "library_root": tmp_path / "library",
            }
        )
    )

    async def check() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
            ) as client:
                if environment == "prod":
                    assert (await client.get("/")).status_code == 200
                assert (await client.get("/api/v1/system/readiness")).status_code == 200
                assert (await client.get("/api/v1/library")).status_code == 200
                assert (await client.get("/api/v1/settings/reader")).status_code == 200

    asyncio.run(check())
