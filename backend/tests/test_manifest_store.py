import ctypes
import errno
import hashlib
import json
import logging
import multiprocessing
import os
import sqlite3
import subprocess
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any, BinaryIO, Protocol, cast
from uuid import UUID

import pytest

from app.domain.download_page import DownloadPage
from app.domain.job_manifest import (
    DownloadJobManifest,
    manifest_from_bytes,
    manifest_to_bytes,
)
from app.errors import ErrorCode
from app.logging_config import JsonLogFormatter
from app.persistence.database import DatabaseInitializationError, SQLiteDatabase
from app.persistence.manifest_store import (
    JobManifestStore,
    ManifestStoreError,
    ManifestWriteReservation,
    TemporaryPageStore,
    _temporary_page_io_error_code,  # pyright: ignore[reportPrivateUsage]
)
from app.persistence.storage_gate import SharedStorageGate, StorageGateClosedError
from app.persistence.windows_paths import DataRootIdentity, DataRootIdentityError

JOB_ID = UUID("00000000-0000-4000-8000-000000000001")
OTHER_JOB_ID = UUID("00000000-0000-4000-8000-000000000004")
SOURCE_ID = UUID("00000000-0000-4000-8000-000000000002")
CHAPTER_ID = UUID("00000000-0000-4000-8000-000000000003")

_MoveFileEx = Callable[[Path, Path], None]
_NewBackupPath = Callable[[Path, int], Path]
_ReplaceFile = Callable[[Path, Path, Path], None]


class _ReadManifestBytes(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        max_bytes: int,
        expected_identity: tuple[int, int] | None = None,
        expected_size: int | None = None,
        expected_parent_identity: tuple[int, int] | None = None,
        job_id: UUID | None = None,
        trace_id: str | None = None,
    ) -> bytes: ...


class _InjectedWin32Lock(OSError):
    def __init__(self, code: int) -> None:
        super().__init__(f"injected Win32 error {code}")
        self.winerror = code


def _no_wait(_delay: float) -> None:
    return


def _fixed_backup_path(path: Path) -> _NewBackupPath:
    return lambda _directory, _generation: path


def _store_private_attribute(name: str) -> object:
    import app.persistence.manifest_store as store_module

    return getattr(store_module, name)


def _injected_disk_full(winerror: int | None) -> OSError:
    if winerror is None:
        return OSError(errno.ENOSPC, "injected disk full")
    return _InjectedWin32Lock(winerror)


def _manifest(remote_reference: str, *, job_id: UUID = JOB_ID) -> DownloadJobManifest:
    page = DownloadPage.new_local(job_id, 1, remote_reference)
    return DownloadJobManifest(
        job_id=job_id,
        source_id=SOURCE_ID,
        chapter_id=CHAPTER_ID,
        expected_pages=1,
        pages=(page,),
    )


def _empty_manifest(*, job_id: UUID) -> DownloadJobManifest:
    return DownloadJobManifest(
        job_id=job_id,
        source_id=SOURCE_ID,
        chapter_id=CHAPTER_ID,
        expected_pages=0,
        pages=(),
    )


def _manifest_path(data_root: Path, job_id: UUID = JOB_ID) -> Path:
    return data_root / "database" / "manifests" / f"{job_id}.json"


def _save_with_reservation(
    store: JobManifestStore,
    manifest: DownloadJobManifest,
    *,
    expected_sha256: str | None = None,
) -> str:
    """Exercise the public lock → preflight token → save sequence in legacy tests."""
    target = _manifest_path(store.data_root, manifest.job_id)
    with store.lock_job(manifest.job_id):
        if target.exists():
            current_bytes = _read_manifest_bytes_for_test(target)
            actual_current_digest = hashlib.sha256(current_bytes).hexdigest()
            if expected_sha256 is None:
                raise ManifestStoreError(
                    ErrorCode.DL_MANIFEST_PERSIST_FAILED,
                    "manifest_destination_exists",
                )
            if (
                type(expected_sha256) is str
                and len(expected_sha256) == 64
                and all(character in "0123456789abcdef" for character in expected_sha256)
                and expected_sha256 != actual_current_digest
            ):
                raise ManifestStoreError(
                    ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                    "manifest_snapshot",
                )
            current_size = len(current_bytes)
            current_digest = (
                expected_sha256
                if (
                    type(expected_sha256) is not str
                    or len(expected_sha256) != 64
                    or any(character not in "0123456789abcdef" for character in expected_sha256)
                )
                else actual_current_digest
            )
        else:
            if (
                expected_sha256 is not None
                and type(expected_sha256) is str
                and len(expected_sha256) == 64
                and all(character in "0123456789abcdef" for character in expected_sha256)
            ):
                raise ManifestStoreError(
                    ErrorCode.DL_MANIFEST_STALE_SNAPSHOT,
                    "manifest_snapshot",
                )
            current_bytes = manifest_to_bytes(_empty_manifest(job_id=manifest.job_id))
            current_digest = (
                expected_sha256
                if expected_sha256 is not None
                else hashlib.sha256(current_bytes).hexdigest()
            )
            current_size = len(current_bytes)

        new_bytes = manifest_to_bytes(manifest)
        reservation = store.reserve_write(
            job_id=manifest.job_id,
            expected_current_generation=0,
            current_sha256=current_digest,
            current_size_bytes=current_size,
            new_sha256=hashlib.sha256(new_bytes).hexdigest(),
            new_size_bytes=len(new_bytes),
        )
        return store.save(manifest, reservation=reservation)


@contextmanager
def _manifest_backup_cleanup_case(
    data_root: Path,
    gate: SharedStorageGate,
) -> Generator[tuple[JobManifestStore, ManifestWriteReservation, Path, bytes, str, str]]:
    store = JobManifestStore(
        data_root,
        data_root_identity=_capture_data_root_identity(data_root),
        recovery_guard=_noop_recovery_guard,
        storage_gate=gate,
    )
    current_bytes = manifest_to_bytes(_empty_manifest(job_id=JOB_ID))
    current_digest = hashlib.sha256(current_bytes).hexdigest()
    generation_one_digest = ""
    generation_three_reservation: ManifestWriteReservation | None = None
    generation_two_digest = ""

    with store.lock_job(JOB_ID):
        for generation, reference in (
            (1, "backup-cleanup-one"),
            (2, "backup-cleanup-two"),
            (3, "backup-cleanup-three"),
        ):
            candidate = _manifest(reference)
            candidate_bytes = manifest_to_bytes(candidate)
            candidate_digest = hashlib.sha256(candidate_bytes).hexdigest()
            retained_digest = generation_one_digest if generation == 3 else None
            reservation = store.reserve_write(
                job_id=JOB_ID,
                expected_current_generation=generation - 1,
                current_sha256=current_digest,
                current_size_bytes=len(current_bytes),
                new_sha256=candidate_digest,
                new_size_bytes=len(candidate_bytes),
                retained_backup_sha256=retained_digest,
            )
            assert store.save(candidate, reservation=reservation) == candidate_digest

            current_bytes = candidate_bytes
            current_digest = candidate_digest
            if generation == 1:
                generation_one_digest = candidate_digest
            elif generation == 2:
                generation_two_digest = candidate_digest
            else:
                generation_three_reservation = reservation

        assert generation_three_reservation is not None
        recovery_directory = (
            data_root / "database" / "manifests" / ".recovery" / str(JOB_ID)
        )
        obsolete_backups = tuple(
            recovery_directory.glob(".bak-00000000000000000002-*")
        )
        assert len(obsolete_backups) == 1
        yield (
            store,
            generation_three_reservation,
            obsolete_backups[0],
            current_bytes,
            generation_two_digest,
            current_digest,
        )


def _read_manifest_bytes_for_test(target: Path) -> bytes:
    return target.read_bytes()


def _noop_recovery_guard(_job_id: UUID) -> None:
    return None


def _changed_file_id_info(_path: Path) -> tuple[int, bytes]:
    return 0x5678, bytes(reversed(range(16)))


def _crash_at_manifest_publication_boundary(
    data_root: str,
    operation: str,
    boundary: str,
    boundary_reached: Any,
) -> None:
    """Hold a child immediately before/after the Win32 publication call."""
    from threading import Event

    import app.persistence.manifest_store as store_module

    store = _manifest_store(Path(data_root))
    if operation == "update":
        original_digest = _save_with_reservation(store, _manifest("crash-before-update"))
        original_wrapper = cast(
            _ReplaceFile,
            _store_private_attribute("_replace_file_win32"),
        )

        def pause_replace(
            replaced_path: Path,
            replacement_path: Path,
            backup_path: Path,
        ) -> None:
            if boundary == "before":
                boundary_reached.set()
                Event().wait()
            original_wrapper(replaced_path, replacement_path, backup_path)
            boundary_reached.set()
            Event().wait()

        store_module.__dict__["_replace_file_win32"] = pause_replace
        _save_with_reservation(
            store,
            _manifest("crash-after-update"),
            expected_sha256=original_digest,
        )
        return

    if operation == "create":
        original_wrapper = cast(
            _MoveFileEx,
            _store_private_attribute("_move_file_ex_win32"),
        )

        def pause_move(source_path: Path, destination_path: Path) -> None:
            if boundary == "before":
                boundary_reached.set()
                Event().wait()
            original_wrapper(source_path, destination_path)
            boundary_reached.set()
            Event().wait()

        store_module.__dict__["_move_file_ex_win32"] = pause_move
        _save_with_reservation(store, _manifest("crash-after-create"))
        return

    raise AssertionError(f"unsupported crash scenario: {operation!r}")


def _manifest_candidates_except_target(target: Path) -> list[Path]:
    if not target.parent.exists():
        return []
    return sorted(
        path
        for path in target.parent.rglob("*")
        if path != target and path.is_file()
    )


def _capture_data_root_identity(data_root: Path) -> DataRootIdentity:
    return DataRootIdentity.capture(data_root)


def _patch_windows_root_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    volume_root: str = "C:\\",
    drive_type: int = 3,
    file_id_info: Any = (0x1234, bytes(range(16))),
) -> tuple[ModuleType, dict[str, list[Any]]]:
    import app.persistence.windows_paths as windows_paths

    calls: dict[str, list[Any]] = {
        "volume_path": [],
        "drive_type": [],
        "file_id_info": [],
    }

    def get_volume_path_name(path: Path) -> str:
        calls["volume_path"].append(path)
        return volume_root

    def get_drive_type(path: str) -> int:
        calls["drive_type"].append(path)
        return drive_type

    def get_file_id_info(path: Path) -> Any:
        calls["file_id_info"].append(path)
        if isinstance(file_id_info, BaseException):
            raise file_id_info
        return file_id_info

    monkeypatch.setattr(windows_paths, "_get_volume_path_name", get_volume_path_name)
    monkeypatch.setattr(windows_paths, "_get_drive_type", get_drive_type)
    monkeypatch.setattr(windows_paths, "_get_file_id_info", get_file_id_info)
    return windows_paths, calls


def _spy_path_resolutions(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    resolved: list[Path] = []
    original_resolve = Path.resolve

    def record_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        resolved.append(path)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", record_resolve)
    return resolved


def _manifest_store(
    data_root: Path,
    *,
    data_root_identity: DataRootIdentity | None = None,
    recovery_guard: Callable[[UUID], None] = _noop_recovery_guard,
) -> JobManifestStore:
    identity = (
        _capture_data_root_identity(data_root)
        if data_root_identity is None
        else data_root_identity
    )
    return JobManifestStore(
        data_root,
        data_root_identity=identity,
        recovery_guard=recovery_guard,
    )


def _temporary_page_store(
    data_root: Path,
    *,
    data_root_identity: DataRootIdentity | None = None,
) -> TemporaryPageStore:
    identity = (
        _capture_data_root_identity(data_root)
        if data_root_identity is None
        else data_root_identity
    )
    return TemporaryPageStore(data_root, data_root_identity=identity)


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def test_manifest_directory_validation_reuses_verified_parent_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path
    database_directory = data_root / "database"
    manifests_directory = database_directory / "manifests"
    manifests_directory.mkdir(parents=True)
    store = _manifest_store(data_root)

    original_lstat = Path.lstat
    database_lstat_calls = 0

    def count_database_lstat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal database_lstat_calls
        if path == database_directory:
            database_lstat_calls += 1
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", count_database_lstat)

    assert store._assert_manifest_directories_safe(JOB_ID) is not None  # pyright: ignore[reportPrivateUsage]
    # One candidate check plus the child probe's exact-parent checks before
    # and after lstat. The already-verified parent must not be snapshotted
    # recursively a second time.
    assert database_lstat_calls == 3


def test_manifest_target_checks_reuse_verified_directory_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    manifests_directory = tmp_path / "database" / "manifests"
    manifests_directory.mkdir(parents=True)
    target = _manifest_path(tmp_path)
    expected_parent_identity = store_module._managed_file_identity(  # pyright: ignore[reportPrivateUsage]
        manifests_directory.lstat()
    )
    assert expected_parent_identity is not None

    observed_parent_identities: list[tuple[int, int] | None] = []
    original_assert_no_reparse = store_module._assert_no_reparse  # pyright: ignore[reportPrivateUsage]

    def observe_target_parent(
        path: Path,
        **kwargs: Any,
    ) -> os.stat_result | None:
        if path == target:
            observed_parent_identities.append(kwargs.get("expected_parent_identity"))
        return original_assert_no_reparse(path, **kwargs)

    monkeypatch.setattr(store_module, "_assert_no_reparse", observe_target_parent)
    store = _manifest_store(tmp_path)

    first_digest = _save_with_reservation(store, _manifest("first-generation"))
    _save_with_reservation(
        store,
        _manifest("second-generation"),
        expected_sha256=first_digest,
    )

    assert observed_parent_identities == [expected_parent_identity] * 10


def test_manifest_target_read_reuses_verified_directory_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    target = _manifest_path(tmp_path)
    _save_with_reservation(store, _manifest("read-parent-identity"))
    expected_parent_identity = store_module._managed_file_identity(  # pyright: ignore[reportPrivateUsage]
        target.parent.lstat()
    )
    assert expected_parent_identity is not None

    observed_parent_identities: list[tuple[int, int] | None] = []
    original_read = store_module._read_manifest_bytes  # pyright: ignore[reportPrivateUsage]

    def observe_read_parent(path: Path, **kwargs: Any) -> bytes:
        if path == target:
            observed_parent_identities.append(
                kwargs.get("expected_parent_identity")
            )
        return original_read(path, **kwargs)

    monkeypatch.setattr(store_module, "_read_manifest_bytes", observe_read_parent)

    assert store._read_target(target, JOB_ID)  # pyright: ignore[reportPrivateUsage]
    assert observed_parent_identities == [expected_parent_identity]


def test_recovery_namespace_validation_reuses_verified_parent_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path
    recovery_root = data_root / "database" / "manifests" / ".recovery"
    recovery_directory = recovery_root / str(JOB_ID)
    recovery_directory.mkdir(parents=True)
    store = _manifest_store(data_root)

    original_lstat = Path.lstat
    recovery_root_lstat_calls = 0

    def count_recovery_root_lstat(
        path: Path,
        *args: Any,
        **kwargs: Any,
    ) -> os.stat_result:
        nonlocal recovery_root_lstat_calls
        if path == recovery_root:
            recovery_root_lstat_calls += 1
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", count_recovery_root_lstat)

    assert store._assert_recovery_namespace_expected(JOB_ID, ()) == {}  # pyright: ignore[reportPrivateUsage]
    # The recovery root is checked once as a candidate, then as the exact
    # parent before and after probing the job directory.
    assert recovery_root_lstat_calls == 3


def test_recovery_namespace_validation_discovers_manifests_parent_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    _save_with_reservation(store, _manifest("recovery-namespace-parent-handoff"))
    manifests_directory = tmp_path / "database" / "manifests"
    recovery_root = manifests_directory / ".recovery"
    expected_manifests_identity = store_module._managed_file_identity(  # pyright: ignore[reportPrivateUsage]
        manifests_directory.lstat()
    )
    assert expected_manifests_identity is not None

    observed_parent_identities: list[tuple[int, int] | None] = []
    original_assert_no_reparse = store_module._assert_no_reparse  # pyright: ignore[reportPrivateUsage]

    def record_recovery_root_check(path: Path, **kwargs: Any) -> os.stat_result | None:
        info = original_assert_no_reparse(path, **kwargs)
        if path == recovery_root and kwargs.get("stage") == "recovery_namespace":
            observed_parent_identities.append(kwargs.get("expected_parent_identity"))
        return info

    monkeypatch.setattr(store_module, "_assert_no_reparse", record_recovery_root_check)

    with store.lock_job(JOB_ID):
        assert store._assert_recovery_namespace_expected(JOB_ID, ()) == {}  # pyright: ignore[reportPrivateUsage]

    assert observed_parent_identities == [expected_manifests_identity]


def test_manifest_publication_hands_off_verified_parent_to_recovery_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    manifests_directory = tmp_path / "database" / "manifests"
    manifests_directory.mkdir(parents=True)
    store = _manifest_store(tmp_path)
    verified_parent_identities: list[tuple[int, int] | None] = []
    observed: list[tuple[tuple[int, int] | None, tuple[int, int] | None]] = []

    original_manifest_directories_safe = store._assert_manifest_directories_safe  # pyright: ignore[reportPrivateUsage]

    def record_manifest_directories_safe(job_id: UUID) -> os.stat_result | None:
        info = original_manifest_directories_safe(job_id)
        verified_parent_identities.append(
            store_module._managed_file_identity(info) if info is not None else None  # pyright: ignore[reportPrivateUsage]
        )
        return info

    original_recovery_namespace_expected = store._assert_recovery_namespace_expected  # pyright: ignore[reportPrivateUsage]

    def record_recovery_namespace_expected(
        job_id: UUID,
        expected_backups: tuple[tuple[int, str], ...],
        **kwargs: Any,
    ) -> dict[int, tuple[Path, int]]:
        verified_parent = (
            verified_parent_identities[-1] if verified_parent_identities else None
        )
        observed.append(
            (kwargs.get("expected_manifests_identity"), verified_parent)
        )
        return original_recovery_namespace_expected(
            job_id,
            expected_backups,
            **kwargs,
        )

    monkeypatch.setattr(
        store,
        "_assert_manifest_directories_safe",
        record_manifest_directories_safe,
    )
    monkeypatch.setattr(
        store,
        "_assert_recovery_namespace_expected",
        record_recovery_namespace_expected,
    )

    _save_with_reservation(store, _manifest("recovery-parent-handoff"))

    # One check in reserve_write and two in save (before and after directory setup).
    assert len(observed) == 3
    assert all(
        received is not None and received == verified
        for received, verified in observed
    )


def test_manifest_directory_check_reuses_captured_data_root_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    expected_data_root_identity = store_module._managed_file_identity(  # pyright: ignore[reportPrivateUsage]
        tmp_path.lstat()
    )
    assert expected_data_root_identity is not None
    observed_parent_identities: list[tuple[int, int] | None] = []
    original_assert_no_reparse = store_module._assert_no_reparse  # pyright: ignore[reportPrivateUsage]

    def record_database_directory_check(
        path: Path,
        **kwargs: Any,
    ) -> os.stat_result | None:
        info = original_assert_no_reparse(path, **kwargs)
        if (
            path == tmp_path / "database"
            and kwargs.get("stage") == "manifest_directory"
        ):
            observed_parent_identities.append(
                kwargs.get("expected_parent_identity")
            )
        return info

    monkeypatch.setattr(
        store_module,
        "_assert_no_reparse",
        record_database_directory_check,
    )

    assert store._assert_manifest_directories_safe(JOB_ID) is None  # pyright: ignore[reportPrivateUsage]

    assert observed_parent_identities == [expected_data_root_identity]


def test_prepare_manifest_directories_hands_off_verified_parent_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    database_directory = tmp_path / "database"
    manifests_directory = database_directory / "manifests"
    expected_root_identity = store_module._managed_file_identity(tmp_path.lstat())  # pyright: ignore[reportPrivateUsage]
    assert expected_root_identity is not None
    observed_parent_identities: list[tuple[Path, tuple[int, int] | None]] = []
    original_directory_state = store_module._directory_path_state  # pyright: ignore[reportPrivateUsage]

    def record_directory_state(
        identity: DataRootIdentity,
        path: Path,
        **kwargs: Any,
    ) -> os.stat_result:
        if path in {database_directory, manifests_directory}:
            observed_parent_identities.append(
                (path, kwargs.get("expected_parent_identity"))
            )
        return original_directory_state(identity, path, **kwargs)

    monkeypatch.setattr(
        store_module,
        "_directory_path_state",
        record_directory_state,
    )

    store._prepare_manifest_directories(JOB_ID)  # pyright: ignore[reportPrivateUsage]

    expected_database_identity = store_module._managed_file_identity(  # pyright: ignore[reportPrivateUsage]
        database_directory.lstat()
    )
    assert expected_database_identity is not None
    assert observed_parent_identities == [
        (database_directory, expected_root_identity),
        (manifests_directory, expected_database_identity),
    ]


def test_staging_and_backup_path_checks_reuse_validated_recovery_directory_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    recovery_directory = (
        tmp_path / "database" / "manifests" / ".recovery" / str(JOB_ID)
    )
    recovery_root = recovery_directory.parent
    manifests_directory = recovery_root.parent
    verified_directory_identities: list[tuple[int, int] | None] = []
    observed_recovery_parent_identities: list[
        tuple[Path, tuple[int, int] | None]
    ] = []
    observed_checks: list[tuple[str, tuple[int, int] | None]] = []
    observed_reads: list[tuple[str, tuple[int, int] | None]] = []
    original_directory_state = store_module._directory_path_state  # pyright: ignore[reportPrivateUsage]
    original_assert_no_reparse = store_module._assert_no_reparse  # pyright: ignore[reportPrivateUsage]
    original_read_manifest_bytes = store_module._read_manifest_bytes  # pyright: ignore[reportPrivateUsage]

    def record_directory_state(
        identity: DataRootIdentity,
        path: Path,
        **kwargs: Any,
    ) -> os.stat_result:
        info = original_directory_state(identity, path, **kwargs)
        if path in {recovery_root, recovery_directory}:
            observed_recovery_parent_identities.append(
                (path, kwargs.get("expected_parent_identity"))
            )
        if path == recovery_directory:
            verified_directory_identities.append(store_module._managed_file_identity(info))  # pyright: ignore[reportPrivateUsage]
        return info

    def record_path_check(path: Path, **kwargs: Any) -> os.stat_result | None:
        info = original_assert_no_reparse(path, **kwargs)
        stage = kwargs.get("stage")
        if stage in {
            "staging_path",
            "staging_verify",
            "backup_candidate",
            "staging_replace_snapshot",
            "backup_verify",
        }:
            observed_checks.append((stage, kwargs.get("expected_parent_identity")))
        return info

    def record_manifest_read(path: Path, **kwargs: Any) -> bytes:
        if path.name.startswith((".stg-", ".bak-")):
            observed_reads.append(
                (path.name[:5], kwargs.get("expected_parent_identity"))
            )
        return original_read_manifest_bytes(path, **kwargs)

    monkeypatch.setattr(store_module, "_directory_path_state", record_directory_state)
    monkeypatch.setattr(store_module, "_assert_no_reparse", record_path_check)
    monkeypatch.setattr(store_module, "_read_manifest_bytes", record_manifest_read)

    first_digest = _save_with_reservation(store, _manifest("staging-parent-handoff"))
    second_digest = _save_with_reservation(
        store,
        _manifest("backup-parent-handoff"),
        expected_sha256=first_digest,
    )

    assert len(verified_directory_identities) == 2
    first_identity, second_identity = verified_directory_identities
    assert first_identity is not None and second_identity is not None
    manifests_identity = store_module._managed_file_identity(  # pyright: ignore[reportPrivateUsage]
        manifests_directory.lstat()
    )
    recovery_root_identity = store_module._managed_file_identity(recovery_root.lstat())  # pyright: ignore[reportPrivateUsage]
    assert manifests_identity is not None and recovery_root_identity is not None
    assert observed_recovery_parent_identities == [
        (recovery_root, manifests_identity),
        (recovery_directory, recovery_root_identity),
        (recovery_root, manifests_identity),
        (recovery_directory, recovery_root_identity),
    ]
    assert second_digest != first_digest
    assert first_identity == second_identity
    assert observed_checks == [
        ("staging_path", first_identity),
        ("staging_verify", first_identity),
        ("staging_path", second_identity),
        ("staging_verify", second_identity),
        ("backup_candidate", second_identity),
        ("staging_replace_snapshot", second_identity),
        ("backup_verify", second_identity),
    ]
    assert sum(prefix == ".stg-" for prefix, _ in observed_reads) >= 2
    assert any(prefix == ".bak-" for prefix, _ in observed_reads)
    assert all(parent_identity == second_identity for _, parent_identity in observed_reads)


def test_verify_published_reuses_verified_manifests_parent_for_recovery_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    manifest = _manifest("verify-recovery-parent-handoff")
    current_bytes = manifest_to_bytes(_empty_manifest(job_id=JOB_ID))
    proposed_bytes = manifest_to_bytes(manifest)
    reservation: ManifestWriteReservation
    with store.lock_job(JOB_ID):
        reservation = store.reserve_write(
            job_id=JOB_ID,
            expected_current_generation=0,
            current_sha256=hashlib.sha256(current_bytes).hexdigest(),
            current_size_bytes=len(current_bytes),
            new_sha256=hashlib.sha256(proposed_bytes).hexdigest(),
            new_size_bytes=len(proposed_bytes),
        )
        store.save(manifest, reservation=reservation)

        verified_parent_identities: list[tuple[int, int] | None] = []
        recovery_parent_identities: list[
            tuple[tuple[int, int] | None, tuple[int, int] | None]
        ] = []
        original_directories_safe = store._assert_manifest_directories_safe  # pyright: ignore[reportPrivateUsage]
        original_recovery_check = store._assert_recovery_namespace_expected  # pyright: ignore[reportPrivateUsage]

        def record_directories_safe(job_id: UUID) -> os.stat_result | None:
            info = original_directories_safe(job_id)
            verified_parent_identities.append(
                store_module._managed_file_identity(info) if info is not None else None  # pyright: ignore[reportPrivateUsage]
            )
            return info

        def record_recovery_check(
            job_id: UUID,
            expected_backups: tuple[tuple[int, str], ...],
            **kwargs: Any,
        ) -> dict[int, tuple[Path, int]]:
            verified_parent = (
                verified_parent_identities[-1] if verified_parent_identities else None
            )
            recovery_parent_identities.append(
                (kwargs.get("expected_manifests_identity"), verified_parent)
            )
            return original_recovery_check(job_id, expected_backups, **kwargs)

        monkeypatch.setattr(store, "_assert_manifest_directories_safe", record_directories_safe)
        monkeypatch.setattr(store, "_assert_recovery_namespace_expected", record_recovery_check)

        backups = store.verify_published(reservation)

    assert backups == {}
    assert len(verified_parent_identities) == 1
    assert verified_parent_identities[0] is not None
    assert recovery_parent_identities == [
        (verified_parent_identities[0], verified_parent_identities[0])
    ]


def test_recovery_backup_validation_reuses_verified_directory_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as manifest_store_module

    data_root = tmp_path
    recovery_root = data_root / "database" / "manifests" / ".recovery"
    recovery_directory = recovery_root / str(JOB_ID)
    recovery_directory.mkdir(parents=True)
    backup_path = recovery_directory / (
        ".bak-00000000000000000001-00000000-0000-4000-8000-000000000099"
    )
    backup_bytes = manifest_to_bytes(_manifest("recovery-backup"))
    backup_path.write_bytes(backup_bytes)
    backup_sha256 = hashlib.sha256(backup_bytes).hexdigest()
    directory_info = recovery_directory.lstat()
    expected_parent_identity = (directory_info.st_dev, directory_info.st_ino)
    assert expected_parent_identity[1] != 0
    store = _manifest_store(data_root)

    original_assert_no_reparse = manifest_store_module._assert_no_reparse  # pyright: ignore[reportPrivateUsage]
    observed_parent_identities: list[tuple[int, int] | None] = []

    def record_backup_parent_identity(
        path: Path,
        **kwargs: Any,
    ) -> os.stat_result | None:
        if path == backup_path:
            observed_parent_identities.append(kwargs.get("expected_parent_identity"))
        return original_assert_no_reparse(path, **kwargs)

    monkeypatch.setattr(
        manifest_store_module,
        "_assert_no_reparse",
        record_backup_parent_identity,
    )

    assert store._assert_recovery_namespace_expected(  # pyright: ignore[reportPrivateUsage]
        JOB_ID,
        ((1, backup_sha256),),
    ) == {1: (backup_path, len(backup_bytes))}
    assert observed_parent_identities == [expected_parent_identity]


def test_no_reparse_path_walk_hands_off_each_verified_parent_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    root = tmp_path / "data"
    target = root / "database" / "manifests" / "nested" / "manifest.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"manifest")
    identity = DataRootIdentity.capture(root)
    real_retry_lstat = store_module.retry_lstat
    observed: list[tuple[Path, object, bool]] = []

    def record_retry_lstat(path: Path, **kwargs: Any) -> os.stat_result:
        observed.append(
            (
                path,
                kwargs.get("expected_parent_identity"),
                callable(kwargs.get("validate_path")),
            )
        )
        return real_retry_lstat(path, **kwargs)

    monkeypatch.setattr(store_module, "retry_lstat", record_retry_lstat)

    info = store_module._assert_no_reparse(  # pyright: ignore[reportPrivateUsage]
        target,
        expected="file",
        allow_missing=False,
        stage="manifest_test",
        identity=identity,
        job_id=JOB_ID,
        trace_id="test-trace",
    )

    paths = (
        root,
        root / "database",
        root / "database" / "manifests",
        target.parent,
        target,
    )
    expected_parents = (
        None,
        store_module._managed_file_identity(root.lstat()),  # pyright: ignore[reportPrivateUsage]
        store_module._managed_file_identity((root / "database").lstat()),  # pyright: ignore[reportPrivateUsage]
        store_module._managed_file_identity((root / "database" / "manifests").lstat()),  # pyright: ignore[reportPrivateUsage]
        store_module._managed_file_identity(target.parent.lstat()),  # pyright: ignore[reportPrivateUsage]
    )
    assert tuple(path for path, _parent, _validates_root in observed) == paths
    assert tuple(parent for _path, parent, _validates_root in observed) == expected_parents
    assert all(validates for _path, _parent, validates in observed)
    assert info is not None and info.st_size == len(b"manifest")


def test_no_reparse_direct_parent_path_checks_root_around_candidate_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    root = tmp_path / "data"
    target = root / "database" / "manifests" / "manifest.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"manifest")
    identity = DataRootIdentity.capture(root)
    parent_identity = store_module._managed_file_identity(target.parent.lstat())  # pyright: ignore[reportPrivateUsage]
    assert parent_identity is not None
    assert_root_identity = store_module._assert_root_identity  # pyright: ignore[reportPrivateUsage]
    root_checks = 0

    def count_root_checks(
        candidate_identity: DataRootIdentity,
        **kwargs: Any,
    ) -> None:
        nonlocal root_checks
        root_checks += 1
        assert_root_identity(candidate_identity, **kwargs)

    monkeypatch.setattr(store_module, "_assert_root_identity", count_root_checks)

    info = store_module._assert_no_reparse(  # pyright: ignore[reportPrivateUsage]
        target,
        expected="file",
        allow_missing=False,
        stage="manifest_test",
        identity=identity,
        job_id=JOB_ID,
        expected_parent_identity=parent_identity,
    )

    assert root_checks == 2
    assert info is not None and info.st_size == len(b"manifest")


@pytest.mark.parametrize("invalid_guard", [None, 0, "not-callable"])
def test_manifest_store_requires_a_callable_recovery_guard_before_io(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    invalid_guard: Any,
) -> None:
    _windows_paths, calls = _patch_windows_root_adapter(monkeypatch)
    identity = _capture_data_root_identity(tmp_path)
    for recorded_calls in calls.values():
        recorded_calls.clear()
    path_resolutions = _spy_path_resolutions(monkeypatch)
    caplog.set_level("ERROR")

    with pytest.raises(ManifestStoreError) as raised:
        JobManifestStore(
            tmp_path,
            data_root_identity=identity,
            recovery_guard=invalid_guard,
        )

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert any(
        getattr(record, "stage", None) == "recovery_guard_configuration"
        for record in caplog.records
    )
    assert all(not recorded_calls for recorded_calls in calls.values())
    assert path_resolutions == []
    assert not (tmp_path / "database").exists()
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


def test_manifest_store_requires_a_captured_root_identity_before_io(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_windows_root_adapter(monkeypatch)
    path_resolutions = _spy_path_resolutions(monkeypatch)
    caplog.set_level("ERROR")

    with pytest.raises(ManifestStoreError) as raised:
        JobManifestStore(
            tmp_path,
            data_root_identity=None,
            recovery_guard=_noop_recovery_guard,
        )

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert any(
        getattr(record, "stage", None) == "root_identity"
        for record in caplog.records
    )
    assert path_resolutions == []
    assert not (tmp_path / "database").exists()
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


def test_temporary_page_store_requires_a_captured_root_identity_before_io(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _windows_paths, calls = _patch_windows_root_adapter(monkeypatch)
    path_resolutions = _spy_path_resolutions(monkeypatch)
    caplog.set_level("ERROR")

    with pytest.raises(ManifestStoreError) as raised:
        TemporaryPageStore(tmp_path, data_root_identity=None)

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert any(
        getattr(record, "stage", None) == "root_identity"
        for record in caplog.records
    )
    assert all(not recorded_calls for recorded_calls in calls.values())
    assert path_resolutions == []
    assert not (tmp_path / "temp").exists()
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows volume identity contract.")
@pytest.mark.parametrize(
    ("drive_type", "volume_root", "accepted"),
    [
        (2, "E:\\", True),  # DRIVE_REMOVABLE
        (3, "C:\\", True),  # DRIVE_FIXED
        (3, "C:\\mounted\\Yomira\\", True),  # Mounted-volume root
        (0, "Z:\\", False),  # DRIVE_UNKNOWN
        (1, "Z:\\", False),  # DRIVE_NO_ROOT_DIR
        (4, "\\\\server\\share\\", False),  # DRIVE_REMOTE / UNC
        (5, "Z:\\", False),  # DRIVE_CDROM
        (6, "Z:\\", False),  # DRIVE_RAMDISK
    ],
)
def test_data_root_identity_accepts_only_fixed_or_removable_volume_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drive_type: int,
    volume_root: str,
    accepted: bool,
) -> None:
    data_root = tmp_path / "nested" / "data-root"
    data_root.mkdir(parents=True)
    _windows_paths, calls = _patch_windows_root_adapter(
        monkeypatch,
        volume_root=volume_root,
        drive_type=drive_type,
    )

    if accepted:
        identity = _capture_data_root_identity(data_root)
        digest = _save_with_reservation(
            _manifest_store(data_root, data_root_identity=identity),
            _manifest("local-volume"),
        )
        assert hashlib.sha256(_manifest_path(data_root).read_bytes()).hexdigest() == digest
    else:
        with pytest.raises(DataRootIdentityError):
            _capture_data_root_identity(data_root)
        assert not _manifest_path(data_root).exists()
        assert not (data_root / "database").exists()

    assert calls["volume_path"]
    assert all(Path(path) == data_root.resolve() for path in calls["volume_path"])
    assert calls["drive_type"]
    assert all(path.endswith("\\") for path in calls["drive_type"])
    assert set(calls["drive_type"]) == {volume_root}


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows volume identity contract.")
@pytest.mark.parametrize(
    ("failure", "volume_path_error", "drive_type_error"),
    [
        ("volume_path", True, False),
        ("drive_type", False, True),
    ],
)
def test_data_root_identity_fails_closed_when_volume_classification_api_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    volume_path_error: bool,
    drive_type_error: bool,
) -> None:
    windows_paths, calls = _patch_windows_root_adapter(monkeypatch)
    for recorded_calls in calls.values():
        recorded_calls.clear()
    if volume_path_error:
        def fail_volume_path(_path: Path) -> str:
            calls["volume_path"].append(_path)
            raise OSError("injected GetVolumePathNameW failure")

        monkeypatch.setattr(windows_paths, "_get_volume_path_name", fail_volume_path)
    if drive_type_error:
        def fail_drive_type(path: str) -> int:
            calls["drive_type"].append(path)
            raise OSError("injected GetDriveTypeW failure")

        monkeypatch.setattr(windows_paths, "_get_drive_type", fail_drive_type)
    with pytest.raises(DataRootIdentityError):
        _capture_data_root_identity(tmp_path)

    assert not _manifest_path(tmp_path).exists()
    assert not (tmp_path / "database").exists()
    assert calls["volume_path"]
    if failure == "drive_type":
        assert calls["drive_type"] == ["C:\\"]


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows volume identity contract.")
@pytest.mark.parametrize(
    "file_id_info",
    [
        OSError("injected GetFileInformationByHandleEx failure"),
        (-1, bytes(range(16))),
        (1 << 64, bytes(range(16))),
        (0x1234, bytes(range(15))),
        (0x1234, bytes(range(17))),
    ],
    ids=["api-failure", "negative-serial", "oversized-serial", "short-file-id", "long-file-id"],
)
def test_data_root_identity_rejects_failed_or_incomplete_file_id_info(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_id_info: Any,
) -> None:
    windows_paths, _calls = _patch_windows_root_adapter(monkeypatch)
    def invalid_file_id_info(path: Path) -> Any:
        _calls["file_id_info"].append(path)
        if isinstance(file_id_info, BaseException):
            raise file_id_info
        return file_id_info

    monkeypatch.setattr(windows_paths, "_get_file_id_info", invalid_file_id_info)
    with pytest.raises(DataRootIdentityError):
        _capture_data_root_identity(tmp_path)

    assert not _manifest_path(tmp_path).exists()
    assert not (tmp_path / "database").exists()


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows volume identity contract.")
def test_job_manifest_store_rejects_a_mismatched_root_before_adapter_or_filesystem_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canonical_root = tmp_path / "canonical-root"
    mismatched_root = tmp_path / "not-created-root"
    canonical_root.mkdir()
    _windows_paths, calls = _patch_windows_root_adapter(monkeypatch)
    identity = _capture_data_root_identity(canonical_root)
    for recorded_calls in calls.values():
        recorded_calls.clear()
    path_resolutions = _spy_path_resolutions(monkeypatch)
    caplog.set_level("ERROR")

    with pytest.raises(ManifestStoreError) as raised:
        JobManifestStore(
            mismatched_root,
            data_root_identity=identity,
            recovery_guard=_noop_recovery_guard,
        )

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert any(
        getattr(record, "stage", None) == "root_identity"
        for record in caplog.records
    )
    assert all(not recorded_calls for recorded_calls in calls.values())
    assert path_resolutions == []
    assert not mismatched_root.exists()
    assert not (canonical_root / "database").exists()


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows volume identity contract.")
def test_lexically_equivalent_root_is_accepted_without_filesystem_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_root = tmp_path / "MixedCaseRoot"
    canonical_root.mkdir()
    _windows_paths, calls = _patch_windows_root_adapter(monkeypatch)
    identity = _capture_data_root_identity(canonical_root)
    expected_canonical_path = identity.canonical_path
    for recorded_calls in calls.values():
        recorded_calls.clear()
    mixed_case = str(expected_canonical_path).swapcase()
    lexical_alias = Path(mixed_case.replace("\\", "/") + "/./unused/../")
    path_resolutions = _spy_path_resolutions(monkeypatch)

    store = JobManifestStore(
        lexical_alias,
        data_root_identity=identity,
        recovery_guard=_noop_recovery_guard,
    )

    assert store is not None
    assert identity.canonical_path == expected_canonical_path
    assert all(not recorded_calls for recorded_calls in calls.values())
    assert path_resolutions == []

    digest = _save_with_reservation(store, _manifest("lexically-equivalent-root"))

    target = _manifest_path(expected_canonical_path)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == digest


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows volume identity contract.")
def test_temporary_page_store_rejects_a_mismatched_root_before_adapter_or_filesystem_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canonical_root = tmp_path / "canonical-root"
    mismatched_root = tmp_path / "not-created-root"
    canonical_root.mkdir()
    _windows_paths, calls = _patch_windows_root_adapter(monkeypatch)
    identity = _capture_data_root_identity(canonical_root)
    for recorded_calls in calls.values():
        recorded_calls.clear()
    path_resolutions = _spy_path_resolutions(monkeypatch)
    caplog.set_level("ERROR")

    with pytest.raises(ManifestStoreError) as raised:
        TemporaryPageStore(
            mismatched_root,
            data_root_identity=identity,
        )

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert any(
        getattr(record, "stage", None) == "root_identity"
        for record in caplog.records
    )
    assert all(not recorded_calls for recorded_calls in calls.values())
    assert path_resolutions == []
    assert not mismatched_root.exists()
    assert not (canonical_root / "temp").exists()


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows volume identity contract.")
@pytest.mark.parametrize(
    "path_kind",
    ["different_root", "different_database_filename", "relative_path"],
)
def test_sqlite_database_rejects_a_path_outside_identity_database_location_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_kind: str,
) -> None:
    canonical_root = tmp_path / "canonical-root"
    canonical_root.mkdir()
    (canonical_root / "database").mkdir()
    _windows_paths, calls = _patch_windows_root_adapter(monkeypatch)
    identity = _capture_data_root_identity(canonical_root)
    for recorded_calls in calls.values():
        recorded_calls.clear()
    path_resolutions = _spy_path_resolutions(monkeypatch)

    if path_kind == "different_root":
        path = tmp_path / "other-root" / "database" / "yomira.sqlite3"
    elif path_kind == "different_database_filename":
        path = canonical_root / "database" / "other.sqlite3"
    else:
        path = Path("database") / "yomira.sqlite3"

    with pytest.raises(DatabaseInitializationError) as raised:
        SQLiteDatabase(path, data_root_identity=identity)

    assert raised.value.code is ErrorCode.DB_CONNECTION_FAILED
    assert all(not recorded_calls for recorded_calls in calls.values())
    assert path_resolutions == []
    assert not (tmp_path / "other-root").exists()
    assert not (canonical_root / "database" / "other.sqlite3").exists()
    assert not (canonical_root / "database" / "yomira.sqlite3").exists()


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows volume identity contract.")
def test_one_root_identity_latches_invalid_and_blocks_all_three_consumers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sqlalchemy import text

    data_root = tmp_path / "data-root"
    data_root.mkdir()
    database_directory = data_root / "database"
    database_directory.mkdir()
    windows_paths, _calls = _patch_windows_root_adapter(monkeypatch)
    identity = _capture_data_root_identity(data_root)
    database_path = database_directory / "yomira.sqlite3"
    database = SQLiteDatabase(database_path, data_root_identity=identity)
    manifest_store = _manifest_store(data_root, data_root_identity=identity)
    page_store = TemporaryPageStore(data_root, data_root_identity=identity)

    with database.write_session() as session:
        session.execute(text("CREATE TABLE preserved (value TEXT NOT NULL)"))
        session.execute(text("INSERT INTO preserved(value) VALUES ('keep')"))

    stable_file_id_info = cast(
        Callable[[Path], tuple[int, bytes]],
        windows_paths._get_file_id_info,
    )
    monkeypatch.setattr(
        windows_paths,
        "_get_file_id_info",
        _changed_file_id_info,
    )
    caplog.set_level("ERROR")

    try:
        with pytest.raises(ManifestStoreError) as manifest_error:
            _save_with_reservation(
                manifest_store,
                _manifest("must-not-publish-after-root-change"),
            )
        assert manifest_error.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
        assert any(
            getattr(record, "stage", None) == "root_identity"
            for record in caplog.records
        )

        # Restoring the old provider result cannot reactivate a latched identity.
        monkeypatch.setattr(windows_paths, "_get_file_id_info", stable_file_id_info)
        with pytest.raises(ManifestStoreError) as page_error:
            with page_store.create("jobs/page-after-root-change.part"):
                pass
        assert page_error.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED

        with pytest.raises(DatabaseInitializationError) as database_error:
            with database.write_session() as session:
                session.execute(text("INSERT INTO preserved(value) VALUES ('must-not-write')"))
        assert database_error.value.code is ErrorCode.DB_CONNECTION_FAILED

        assert not _manifest_path(data_root).exists()
        assert not (data_root / "temp" / "jobs" / "page-after-root-change.part").exists()
    finally:
        database.close()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM preserved").fetchall() == [("keep",)]


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows volume identity contract.")
def test_temporary_page_store_revalidates_root_before_exclusive_file_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data_root = tmp_path / "data-root"
    data_root.mkdir()
    windows_paths, _calls = _patch_windows_root_adapter(monkeypatch)
    identity = _capture_data_root_identity(data_root)
    page_store = TemporaryPageStore(data_root, data_root_identity=identity)
    monkeypatch.setattr(
        windows_paths,
        "_get_file_id_info",
        _changed_file_id_info,
    )
    caplog.set_level("ERROR")

    with pytest.raises(ManifestStoreError) as raised:
        with page_store.create("jobs/page-after-root-change.part"):
            pass

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert any(
        getattr(record, "stage", None) == "root_identity"
        for record in caplog.records
    )
    assert not (data_root / "temp" / "jobs" / "page-after-root-change.part").exists()


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows volume identity contract.")
def test_confirmed_sqlite_page_reservation_survives_root_change_before_file_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sqlalchemy import text

    data_root = tmp_path / "data-root"
    database_directory = data_root / "database"
    database_directory.mkdir(parents=True)
    database_path = database_directory / "yomira.sqlite3"
    relative_path = f"jobs/{JOB_ID}/001.part"
    reserved_path = data_root / "temp" / "jobs" / str(JOB_ID) / "001.part"
    _patch_windows_root_adapter(monkeypatch)
    identity = _capture_data_root_identity(data_root)
    database = SQLiteDatabase(database_path, data_root_identity=identity)
    page_store = TemporaryPageStore(data_root, data_root_identity=identity)

    try:
        # This transaction models the caller's durable reservation commit.
        with database.write_session() as session:
            session.execute(
                text(
                    "CREATE TABLE download_pages ("
                    "job_id TEXT NOT NULL, ordinal INTEGER NOT NULL, "
                    "temporary_path TEXT NOT NULL, temporary_path_key TEXT NOT NULL, "
                    "PRIMARY KEY (job_id, ordinal), UNIQUE (temporary_path_key))"
                )
            )
            session.execute(
                text(
                    "INSERT INTO download_pages "
                    "(job_id, ordinal, temporary_path, temporary_path_key) "
                    "VALUES (:job_id, :ordinal, :temporary_path, :temporary_path_key)"
                ),
                {
                    "job_id": str(JOB_ID),
                    "ordinal": 1,
                    "temporary_path": relative_path,
                    "temporary_path_key": relative_path.casefold(),
                },
            )

        monkeypatch.setattr(
            "app.persistence.windows_paths._get_file_id_info",
            _changed_file_id_info,
        )
        caplog.set_level("ERROR")

        with pytest.raises(ManifestStoreError) as raised:
            with page_store.create(relative_path):
                pass

        assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
        assert any(
            getattr(record, "stage", None) == "root_identity"
            for record in caplog.records
        )
        assert not reserved_path.exists()
        assert not reserved_path.parent.exists()
    finally:
        database.close()

    # Bypass the invalidated runtime identity for verification: the committed
    # reservation must remain in the physical SQLite file unchanged.
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT job_id, ordinal, temporary_path, temporary_path_key "
            "FROM download_pages"
        ).fetchall()
    assert rows == [
        (str(JOB_ID), 1, relative_path, relative_path.casefold())
    ]


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows volume identity contract.")
def test_sqlite_database_revalidates_root_before_new_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import text

    data_root = tmp_path / "data-root"
    database_directory = data_root / "database"
    database_directory.mkdir(parents=True)
    windows_paths, _calls = _patch_windows_root_adapter(monkeypatch)
    identity = _capture_data_root_identity(data_root)
    database_path = database_directory / "yomira.sqlite3"
    database = SQLiteDatabase(database_path, data_root_identity=identity)
    try:
        with database.write_session() as session:
            session.execute(text("CREATE TABLE preserved (value TEXT NOT NULL)"))
            session.execute(text("INSERT INTO preserved(value) VALUES ('keep')"))

        monkeypatch.setattr(
            windows_paths,
            "_get_file_id_info",
            _changed_file_id_info,
        )
        with pytest.raises(DatabaseInitializationError) as raised:
            with database.write_session() as session:
                session.execute(text("INSERT INTO preserved(value) VALUES ('must-not-write')"))

        assert raised.value.code is ErrorCode.DB_CONNECTION_FAILED
    finally:
        database.close()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM preserved").fetchall() == [("keep",)]


def test_recovery_guard_runs_once_per_outer_mutex_acquisition(tmp_path: Path) -> None:
    calls: list[UUID] = []

    def recovery_guard(job_id: UUID) -> None:
        calls.append(job_id)

    store = _manifest_store(tmp_path, recovery_guard=recovery_guard)
    with store.lock_job(JOB_ID):
        with store.lock_job(JOB_ID):
            digest = _save_with_reservation(store, _manifest("opaque-page-v1"))

    assert calls == [JOB_ID]

    _save_with_reservation(store, _manifest("opaque-page-v2"), expected_sha256=digest)

    assert calls == [JOB_ID, JOB_ID]


def test_recovery_guard_exception_blocks_manifest_publication(tmp_path: Path) -> None:
    def block_job(_job_id: UUID) -> None:
        raise RuntimeError("recovery is unresolved")

    store = _manifest_store(tmp_path, recovery_guard=block_job)

    with pytest.raises(ManifestStoreError):
        _save_with_reservation(store, _manifest("must-not-publish"))

    assert not _manifest_path(tmp_path).exists()
    assert not (tmp_path / "database").exists()


@pytest.mark.skipif(os.name != "nt", reason="requires the Win32 named mutex contract.")
def test_manifest_store_revalidates_root_after_mutex_acquisition_before_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import app.persistence.manifest_store as manifest_store_module

    data_root = tmp_path / "data-root"
    data_root.mkdir()
    windows_paths, _calls = _patch_windows_root_adapter(monkeypatch)
    identity = _capture_data_root_identity(data_root)
    guard_calls: list[UUID] = []
    released_handles: list[int] = []
    acquired_handle = 0x1234

    def recovery_guard(job_id: UUID) -> None:
        guard_calls.append(job_id)

    def acquire_mutex_after_root_change(_name: str) -> int:
        monkeypatch.setattr(
            windows_paths,
            "_get_file_id_info",
            _changed_file_id_info,
        )
        return acquired_handle

    def release_and_close(handle: int) -> None:
        released_handles.append(handle)

    monkeypatch.setattr(
        manifest_store_module,
        "_named_mutex_create_and_wait",
        acquire_mutex_after_root_change,
    )
    monkeypatch.setattr(
        manifest_store_module,
        "_named_mutex_release_and_close",
        release_and_close,
    )
    store = _manifest_store(
        data_root,
        data_root_identity=identity,
        recovery_guard=recovery_guard,
    )
    caplog.set_level("ERROR")

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(store, _manifest("must-not-run-guard-after-root-change"))

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert any(
        getattr(record, "stage", None) == "root_identity"
        for record in caplog.records
    )
    assert guard_calls == []
    assert released_handles == [acquired_handle]
    assert not _manifest_path(data_root).exists()
    assert not (data_root / "database").exists()


def test_manifest_store_error_logs_trace_id_without_active_context(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.tracing import current_trace_id

    assert current_trace_id() is None
    store = _manifest_store(tmp_path)
    caplog.set_level("ERROR", logger="yomira.manifest_store")

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(
            store,
            _manifest("invalid-expected-digest"),
            expected_sha256="invalid",
        )

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    records = [
        record
        for record in caplog.records
        if record.name == "yomira.manifest_store"
        and record.msg == "manifest_store.operation_failed"
    ]
    assert len(records) == 1
    event = json.loads(JsonLogFormatter().format(records[0]))
    assert event["stage"] == "current_digest_validation"
    assert isinstance(event["trace_id"], str) and event["trace_id"].strip()
    assert not (tmp_path / "database").exists()


def test_implicit_save_retries_recovery_guard_after_a_prior_guard_failure(
    tmp_path: Path,
) -> None:
    guard_calls: list[UUID] = []

    def resume_recovery(job_id: UUID) -> None:
        guard_calls.append(job_id)
        if len(guard_calls) == 1:
            raise RuntimeError("recovery stopped before reconciliation completed")

    store = _manifest_store(tmp_path, recovery_guard=resume_recovery)
    target = _manifest_path(tmp_path)

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(store, _manifest("must-not-publish-before-recovery"))

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert not target.exists()
    assert guard_calls == [JOB_ID]

    digest = _save_with_reservation(store, _manifest("publish-after-recovery-resumes"))

    assert guard_calls == [JOB_ID, JOB_ID]
    assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
    assert manifest_from_bytes(target.read_bytes()) == _manifest(
        "publish-after-recovery-resumes"
    )


def _page_path_for_absolute_length(
    data_root: Path,
    target_units: int,
    *,
    supplementary: bool = False,
) -> tuple[str, Path]:
    prefix = f"{(data_root / 'temp').resolve()}{os.sep}"
    remaining_units = target_units - _utf16_units(prefix)
    suffix = "😀.part" if supplementary else ".part"
    component_units = remaining_units - _utf16_units(suffix)
    assert 0 < component_units <= 255
    relative_path = f"{'x' * component_units}{suffix}"
    assert _utf16_units(relative_path) <= 255
    absolute_path = Path(prefix + relative_path)
    assert _utf16_units(str(absolute_path)) == target_units
    return relative_path, absolute_path


def _create_directory_reparse_point(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (NotImplementedError, OSError):
        if os.name != "nt":
            pytest.skip("directory symlinks and junctions are unavailable")

    environment = os.environ.copy()
    environment["YOMIRA_TEST_LINK"] = str(link)
    environment["YOMIRA_TEST_TARGET"] = str(target)
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; try { New-Item -ItemType Junction "
                "-Path $env:YOMIRA_TEST_LINK -Target $env:YOMIRA_TEST_TARGET "
                "| Out-Null } catch { exit 1 }",
            ],
            capture_output=True,
            check=False,
            env=environment,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        pytest.skip(f"directory junctions are unavailable: {type(error).__name__}")
    if result.returncode != 0 or not link.exists():
        pytest.skip("directory symlinks and junctions are unavailable")


def _create_file_reparse_point(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"file symlinks are unavailable: {type(error).__name__}")


def _hold_job_mutex(
    data_root: str,
    job_id: str,
    acquired: Any,
    release: Any,
) -> None:
    from support.windows_paths import install_deterministic_windows_root_adapters

    install_deterministic_windows_root_adapters()
    store = _manifest_store(Path(data_root))
    with store.lock_job(UUID(job_id)):
        acquired.set()
        release.wait(10)


def _wait_for_abandoned_mutex_with_guard(
    data_root: str,
    job_id: str,
    waiter_id: str,
    attempting: Any,
    first_guard_started: Any,
    first_guard_claim_lock: Any,
    release_first_guard: Any,
    operation_started: Any,
    events: Any,
) -> None:
    from support.windows_paths import install_deterministic_windows_root_adapters

    install_deterministic_windows_root_adapters()

    def recovery_guard(guarded_job_id: UUID) -> None:
        if guarded_job_id != UUID(job_id):
            raise AssertionError("recovery guard received another job ID")

        with first_guard_claim_lock:
            is_first_guard = not first_guard_started.is_set()
            if is_first_guard:
                first_guard_started.set()

        events.put(("guard_enter", waiter_id))
        if is_first_guard and not release_first_guard.wait(timeout=10):
            raise TimeoutError("test did not release the first recovery guard")
        events.put(("guard_exit", waiter_id))

    store = _manifest_store(Path(data_root), recovery_guard=recovery_guard)
    attempting.set()
    try:
        with store.lock_job(UUID(job_id)):
            operation_started.set()
            events.put(("operation", waiter_id))
            digest = _save_with_reservation(store, _manifest(f"abandoned-mutex-{waiter_id}"))
            events.put(("result", waiter_id, "published", digest))
    except Exception as error:
        events.put(
            (
                "result",
                waiter_id,
                "failed",
                getattr(error, "code", None),
            )
        )


def _assert_parent_waits_for_child_mutex_owner(
    child_data_root: Path,
    parent_data_root: Path,
    *,
    parent_data_root_identity: Any | None = None,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    context = multiprocessing.get_context("spawn")
    child_acquired = context.Event()
    release_child = context.Event()
    child = context.Process(
        target=_hold_job_mutex,
        args=(str(child_data_root), str(JOB_ID), child_acquired, release_child),
    )
    child.start()
    try:
        assert child_acquired.wait(timeout=10)
        store = _manifest_store(
            parent_data_root,
            data_root_identity=parent_data_root_identity,
        )
        parent_acquired = Event()

        def acquire_in_parent() -> None:
            with store.lock_job(JOB_ID):
                parent_acquired.set()

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(acquire_in_parent)
            assert not parent_acquired.wait(timeout=0.25)
            release_child.set()
            assert parent_acquired.wait(timeout=5)
            future.result(timeout=5)
        finally:
            release_child.set()
            executor.shutdown(wait=True, cancel_futures=True)
    finally:
        release_child.set()
        child.join(timeout=10)
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
    assert child.exitcode == 0


def test_initial_manifest_publish_writes_exact_bytes_and_returns_verified_digest(
    tmp_path: Path,
) -> None:
    manifest = _manifest("opaque-page-v1")
    store = _manifest_store(tmp_path)

    digest = _save_with_reservation(store, manifest)

    target = _manifest_path(tmp_path)
    serialized = manifest_to_bytes(manifest)
    assert target.read_bytes() == serialized
    assert digest == hashlib.sha256(serialized).hexdigest()
    assert manifest_from_bytes(target.read_bytes()) == manifest


def test_initial_manifest_publish_does_not_replace_an_existing_target(
    tmp_path: Path,
) -> None:
    target = _manifest_path(tmp_path)
    target.parent.mkdir(parents=True)
    original = b"preserve-existing-manifest-target"
    target.write_bytes(original)

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(_manifest_store(tmp_path), _manifest("opaque-page-v2"))

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert target.read_bytes() == original


def test_initial_manifest_publish_allows_only_one_racing_creator(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = _manifest_store(tmp_path)
    candidates = (_manifest("opaque-page-v1"), _manifest("opaque-page-v2"))
    barrier = Barrier(3)

    def publish(candidate: DownloadJobManifest) -> tuple[str, object]:
        barrier.wait(timeout=5)
        try:
            return "published", _save_with_reservation(store, candidate)
        except Exception as error:
            return "failed", error

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish, candidate) for candidate in candidates]
        barrier.wait(timeout=5)
        outcomes = [future.result(timeout=10) for future in futures]

    published = [value for status, value in outcomes if status == "published"]
    failed = [value for status, value in outcomes if status == "failed"]
    target = _manifest_path(tmp_path)
    assert len(published) == 1
    assert len(failed) == 1
    assert getattr(failed[0], "code", None) is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert hashlib.sha256(target.read_bytes()).hexdigest() == published[0]
    assert manifest_from_bytes(target.read_bytes()) in candidates


def test_manifest_update_replaces_only_the_expected_valid_snapshot(tmp_path: Path) -> None:
    store = _manifest_store(tmp_path)
    original = _manifest("opaque-page-v1")
    original_digest = _save_with_reservation(store, original)
    updated = _manifest("opaque-page-v2")

    returned_digest = _save_with_reservation(
        store,
        updated,
        expected_sha256=original_digest,
    )

    target = _manifest_path(tmp_path)
    serialized = manifest_to_bytes(updated)
    assert target.read_bytes() == serialized
    assert returned_digest == hashlib.sha256(serialized).hexdigest()
    assert manifest_from_bytes(target.read_bytes()) == updated


def test_manifest_read_retries_from_zero_only_after_closing_locked_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.filesystem_retry as retry_module
    import app.persistence.manifest_store as store_module

    target = tmp_path / "read-retry.json"
    expected = b"verified-manifest-bytes"
    target.write_bytes(expected)
    real_open: Any = Path.open
    handles: list[dict[str, Any]] = []
    starts: list[int] = []
    sleeps: list[float] = []

    class ReadProxy:
        def __init__(self, stream: Any, *, fail: bool) -> None:
            self._stream = stream
            self._fail = fail
            self.closed = False

        def __enter__(self) -> ReadProxy:
            if handles:
                assert handles[-1]["closed"]
            handles.append({"closed": False})
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def read(self, size: int = -1) -> bytes:
            starts.append(self._stream.tell())
            if self._fail:
                self._fail = False
                self._stream.read(5)
                raise _InjectedWin32Lock(32)
            return self._stream.read(size)

        def close(self) -> None:
            self._stream.close()
            self.closed = True
            if handles:
                handles[-1]["closed"] = True

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    open_calls = 0

    def patched_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        nonlocal open_calls
        stream = real_open(path, mode, *args, **kwargs)
        if path == target and mode == "rb":
            open_calls += 1
            return ReadProxy(stream, fail=open_calls == 1)
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    observed = store_module._read_manifest_bytes(target, max_bytes=1024)  # pyright: ignore[reportPrivateUsage]

    assert observed == expected
    assert open_calls == 2
    assert starts == [0, 0]
    assert handles[0]["closed"] and handles[1]["closed"]
    assert sleeps == [0.1]


def test_manifest_read_does_not_retry_after_unverified_handle_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.filesystem_retry as retry_module
    import app.persistence.manifest_store as store_module

    target = tmp_path / "read-close-unverified.json"
    expected = b"manifest bytes with an unverified close"
    target.write_bytes(expected)
    real_open: Any = Path.open
    handles: list[dict[str, Any]] = []
    sleeps: list[float] = []

    class ReadProxy:
        def __init__(self, stream: Any, *, fail_read: bool, fail_close: bool) -> None:
            self._stream = stream
            self._fail_read = fail_read
            self._fail_close = fail_close
            self.closed = False
            handles.append({"proxy": self, "closed": False})

        def __enter__(self) -> ReadProxy:
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def read(self, size: int = -1) -> bytes:
            if self._fail_read:
                self._stream.read(4)
                raise _InjectedWin32Lock(32)
            return self._stream.read(size)

        def close(self) -> None:
            if self._fail_close:
                raise _InjectedWin32Lock(32)
            self._stream.close()
            self.closed = True
            handles[-1]["closed"] = True

        def cleanup(self) -> None:
            self._stream.close()
            self.closed = True

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patched_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        stream = real_open(path, mode, *args, **kwargs)
        if path == target and mode == "rb":
            attempt = len(handles)
            return ReadProxy(
                stream,
                fail_read=attempt == 0,
                fail_close=attempt == 0,
            )
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    try:
        with pytest.raises(OSError):
            store_module._read_manifest_bytes(target, max_bytes=1024)  # pyright: ignore[reportPrivateUsage]
    finally:
        for record in handles:
            record["proxy"].cleanup()

    assert len(handles) == 1
    assert handles[0]["closed"] is False
    assert sleeps == []


def test_temporary_page_directory_retries_transient_mkdir_with_job_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import app.persistence.filesystem_retry as retry_module

    store = _temporary_page_store(tmp_path)
    target_directory = tmp_path / "temp"
    target_file = target_directory / "jobs" / str(JOB_ID) / "page.part"
    real_mkdir: Any = Path.mkdir
    mkdir_calls = 0
    sleeps: list[float] = []
    trace_id = "00000000-0000-4000-8000-000000000099"

    def lock_once(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal mkdir_calls
        if path == target_directory and mkdir_calls == 0:
            mkdir_calls += 1
            raise _InjectedWin32Lock(32)
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", lock_once)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)
    caplog.set_level(logging.WARNING, logger="yomira.filesystem_retry")

    with store.create(
        f"jobs/{JOB_ID}/page.part",
        job_id=JOB_ID,
        trace_id=trace_id,
    ) as stream:
        stream.write(b"page survives a transient directory lock")

    retry_records = [
        record
        for record in caplog.records
        if record.getMessage() == "filesystem_lock_retry"
        and getattr(record, "stage", None) == "temporary_path_directory"
    ]
    assert mkdir_calls == 1
    assert sleeps == [0.1]
    assert target_file.read_bytes() == b"page survives a transient directory lock"
    assert len(retry_records) == 1
    assert retry_records[0].__dict__["trace_id"] == trace_id
    assert retry_records[0].__dict__["job_id"] == str(JOB_ID)


def test_manifest_directory_retries_transient_mkdir_with_job_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import app.persistence.filesystem_retry as retry_module

    store = _manifest_store(tmp_path)
    manifest = _manifest("manifest-directory-lock")
    target_directory = _manifest_path(tmp_path).parent
    real_mkdir: Any = Path.mkdir
    mkdir_calls = 0
    sleeps: list[float] = []

    def lock_once(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal mkdir_calls
        if path == target_directory and mkdir_calls == 0:
            mkdir_calls += 1
            raise _InjectedWin32Lock(33)
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", lock_once)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)
    caplog.set_level(logging.WARNING, logger="yomira.filesystem_retry")

    _save_with_reservation(store, manifest)

    retry_records = [
        record
        for record in caplog.records
        if record.getMessage() == "filesystem_lock_retry"
        and getattr(record, "stage", None) == "manifest_directory"
    ]
    assert mkdir_calls == 1
    assert sleeps == [0.1]
    assert _manifest_path(tmp_path).is_file()
    assert len(retry_records) == 1
    assert retry_records[0].__dict__["job_id"] == str(manifest.job_id)


def test_manifest_read_does_not_retry_permanent_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.filesystem_retry as retry_module
    import app.persistence.manifest_store as store_module

    target = tmp_path / "read-denied.json"
    target.write_bytes(b"content")
    real_open: Any = Path.open
    opens = 0
    sleeps: list[float] = []

    class DeniedReader:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        def __enter__(self) -> DeniedReader:
            return self

        def __exit__(self, *_args: object) -> None:
            self._stream.close()

        def read(self, _size: int = -1) -> bytes:
            error = OSError("injected access denied")
            error.winerror = 5  # type: ignore[attr-defined]
            raise error

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patched_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        nonlocal opens
        stream = real_open(path, mode, *args, **kwargs)
        if path == target and mode == "rb":
            opens += 1
            return DeniedReader(stream)
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)
    with pytest.raises(OSError) as raised:
        store_module._read_manifest_bytes(target, max_bytes=1024)  # pyright: ignore[reportPrivateUsage]

    assert getattr(raised.value, "winerror", None) == 5
    assert opens == 1
    assert sleeps == []


def test_manifest_match_rejects_path_identity_swap_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    target = tmp_path / "identity-swap.json"
    displaced = tmp_path / "identity-swap.original"
    replacement = tmp_path / "identity-swap.replacement"
    expected = b"same-bytes-different-file-id"
    target.write_bytes(expected)
    original_info = target.lstat()
    identity = store_module._managed_file_identity(original_info)  # pyright: ignore[reportPrivateUsage]
    assert identity is not None
    replacement.write_bytes(expected)
    real_open: Any = Path.open
    swapped = False

    def patched_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        nonlocal swapped
        if path == target and mode == "rb" and not swapped:
            swapped = True
            target.replace(displaced)
            replacement.replace(target)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", patched_open)
    assert not store_module._manifest_file_matches(  # pyright: ignore[reportPrivateUsage]
        target,
        identity=identity,
        expected_bytes=expected,
    )
    assert target.read_bytes() == expected
    assert displaced.read_bytes() == expected


@pytest.mark.skipif(os.name != "nt", reason="requires the Win32 publication wrappers.")
@pytest.mark.parametrize(
    "ambiguous_after_mutation",
    [False, True],
    ids=["before-move", "after-move"],
)
def test_initial_publish_move_fault_preserves_the_complete_manifest_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ambiguous_after_mutation: bool,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    new_manifest = _manifest("move-fault-new")
    new_bytes = manifest_to_bytes(new_manifest)
    target = _manifest_path(tmp_path)
    original_move = cast(_MoveFileEx, _store_private_attribute("_move_file_ex_win32"))

    def injected_move(source_path: Path, destination_path: Path) -> None:
        if ambiguous_after_mutation:
            original_move(source_path, destination_path)
        raise OSError("injected MoveFileExW failure/ambiguous result")

    monkeypatch.setattr(store_module, "_move_file_ex_win32", injected_move)

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(store, new_manifest)

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    if ambiguous_after_mutation:
        assert target.read_bytes() == new_bytes
        assert _manifest_candidates_except_target(target) == []
    else:
        assert not target.exists()
        candidates = _manifest_candidates_except_target(target)
        assert len(candidates) == 1
        assert candidates[0].read_bytes() == new_bytes


@pytest.mark.parametrize("winerror", [None, 39, 112], ids=["enospc", "handle-full", "disk-full"])
def test_initial_publish_disk_full_closes_gate_and_preserves_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    winerror: int | None,
) -> None:
    import app.persistence.manifest_store as store_module

    gate = SharedStorageGate()
    store = JobManifestStore(
        tmp_path,
        data_root_identity=_capture_data_root_identity(tmp_path),
        recovery_guard=_noop_recovery_guard,
        storage_gate=gate,
    )
    manifest = _manifest("initial-publish-disk-full")
    new_bytes = manifest_to_bytes(manifest)
    target = _manifest_path(tmp_path)
    calls = 0

    def fail_move(_source: Path, _destination: Path) -> None:
        nonlocal calls
        calls += 1
        raise _injected_disk_full(winerror)

    monkeypatch.setattr(store_module, "_move_file_ex_win32", fail_move)

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(store, manifest)

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert calls == 1
    assert gate.closed
    assert gate.closed_reason == "FILESYSTEM_DISK_FULL"
    with pytest.raises(StorageGateClosedError):
        with gate.mutation():
            pass
    assert not target.exists()
    candidates = _manifest_candidates_except_target(target)
    assert len(candidates) == 1
    assert candidates[0].read_bytes() == new_bytes


def test_initial_manifest_publish_retries_proven_transient_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    manifest = _manifest("transient-lock-retry")
    target = _manifest_path(tmp_path)
    original_move = cast(_MoveFileEx, _store_private_attribute("_move_file_ex_win32"))
    calls = 0

    def lock_then_move(source_path: Path, destination_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert source_path.is_file()
            assert not destination_path.exists()
            raise ctypes.WinError(32)
        original_move(source_path, destination_path)

    monkeypatch.setattr(store_module, "_move_file_ex_win32", lock_then_move)

    _save_with_reservation(store, manifest)

    assert calls == 2
    assert manifest_from_bytes(target.read_bytes()) == manifest
    assert _manifest_candidates_except_target(target) == []


def test_initial_publish_rechecks_candidate_at_syscall_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.filesystem_retry as retry_module
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    manifest = _manifest("publish-boundary-prestate")
    target = _manifest_path(tmp_path)
    original_move = cast(_MoveFileEx, _store_private_attribute("_move_file_ex_win32"))
    original_absent = store_module._path_is_absent  # pyright: ignore[reportPrivateUsage]
    move_calls = 0
    absent_checks = 0
    staging_path: Path | None = None

    def lock_then_move(source_path: Path, destination_path: Path) -> None:
        nonlocal move_calls, staging_path
        move_calls += 1
        staging_path = source_path
        if move_calls == 1:
            raise _InjectedWin32Lock(32)
        original_move(source_path, destination_path)

    def mutate_after_post_wait_proof(path: Path) -> bool:
        nonlocal absent_checks
        absent = original_absent(path)
        if path == target:
            absent_checks += 1
            if absent_checks == 2:
                assert staging_path is not None
                staging_path.write_bytes(b"changed after retry proof")
        return absent

    monkeypatch.setattr(store_module, "_move_file_ex_win32", lock_then_move)
    monkeypatch.setattr(store_module, "_path_is_absent", mutate_after_post_wait_proof)
    monkeypatch.setattr(retry_module.time, "sleep", _no_wait)

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(store, manifest)

    assert move_calls == 1
    assert raised.value.stage == "manifest_publish"
    assert not target.exists()
    assert staging_path is not None
    assert staging_path.read_bytes() == b"changed after retry proof"


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 ReplaceFileW publication.")
def test_manifest_replace_retries_only_when_old_and_new_candidates_are_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    old_manifest = _manifest("replace-lock-old")
    new_manifest = _manifest("replace-lock-new")
    old_digest = _save_with_reservation(store, old_manifest)
    old_bytes = manifest_to_bytes(old_manifest)
    new_bytes = manifest_to_bytes(new_manifest)
    target = _manifest_path(tmp_path)
    recovery_directory = target.parent / ".recovery" / str(JOB_ID)
    backup_path = recovery_directory / (
        ".bak-00000000000000000001-00000000-0000-4000-8000-000000000001"
    )
    original_replace = cast(_ReplaceFile, _store_private_attribute("_replace_file_win32"))
    calls = 0

    monkeypatch.setattr(
        store_module,
        "_new_backup_path",
        _fixed_backup_path(backup_path),
    )

    def lock_once(replaced: Path, replacement: Path, backup: Path) -> None:
        nonlocal calls
        calls += 1
        assert replaced.read_bytes() == old_bytes
        assert replacement.read_bytes() == new_bytes
        assert backup == backup_path
        assert not backup.exists()
        if calls == 1:
            raise ctypes.WinError(32)
        original_replace(replaced, replacement, backup)

    monkeypatch.setattr(store_module, "_replace_file_win32", lock_once)
    _save_with_reservation(store, new_manifest, expected_sha256=old_digest)

    assert calls == 2
    assert target.read_bytes() == new_bytes
    assert backup_path.read_bytes() == old_bytes
    assert _manifest_candidates_except_target(target) == [backup_path]


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 ReplaceFileW publication.")
def test_manifest_replace_rechecks_candidate_at_syscall_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.filesystem_retry as retry_module
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    old_manifest = _manifest("replace-boundary-old")
    new_manifest = _manifest("replace-boundary-new")
    old_digest = _save_with_reservation(store, old_manifest)
    old_bytes = manifest_to_bytes(old_manifest)
    target = _manifest_path(tmp_path)
    recovery_directory = target.parent / ".recovery" / str(JOB_ID)
    backup_path = recovery_directory / ".bak-boundary.json"
    original_replace = cast(_ReplaceFile, _store_private_attribute("_replace_file_win32"))
    original_absent = store_module._path_is_absent  # pyright: ignore[reportPrivateUsage]
    replace_calls = 0
    absent_checks = 0
    staging_path: Path | None = None

    monkeypatch.setattr(
        store_module,
        "_new_backup_path",
        _fixed_backup_path(backup_path),
    )

    def lock_then_replace(replaced: Path, replacement: Path, backup: Path) -> None:
        nonlocal replace_calls, staging_path
        replace_calls += 1
        staging_path = replacement
        if replace_calls == 1:
            raise _InjectedWin32Lock(32)
        original_replace(replaced, replacement, backup)

    def mutate_after_post_wait_proof(path: Path) -> bool:
        nonlocal absent_checks
        absent = original_absent(path)
        if path == backup_path:
            absent_checks += 1
            if absent_checks == 2:
                assert staging_path is not None
                staging_path.write_bytes(b"changed after retry proof")
        return absent

    monkeypatch.setattr(store_module, "_replace_file_win32", lock_then_replace)
    monkeypatch.setattr(store_module, "_path_is_absent", mutate_after_post_wait_proof)
    monkeypatch.setattr(retry_module.time, "sleep", _no_wait)

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(
            store,
            new_manifest,
            expected_sha256=old_digest,
        )

    assert replace_calls == 1
    assert raised.value.stage == "manifest_replace"
    assert target.read_bytes() == old_bytes
    assert not backup_path.exists()
    assert staging_path is not None
    assert staging_path.read_bytes() == b"changed after retry proof"


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 ReplaceFileW publication.")
def test_manifest_replace_lock_error_after_exact_replace_is_verified_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    old_manifest = _manifest("replace-applied-old")
    new_manifest = _manifest("replace-applied-new")
    old_digest = _save_with_reservation(store, old_manifest)
    old_bytes = manifest_to_bytes(old_manifest)
    new_bytes = manifest_to_bytes(new_manifest)
    target = _manifest_path(tmp_path)
    recovery_directory = target.parent / ".recovery" / str(JOB_ID)
    backup_path = recovery_directory / (
        ".bak-00000000000000000001-00000000-0000-4000-8000-000000000001"
    )
    original_replace = cast(_ReplaceFile, _store_private_attribute("_replace_file_win32"))
    calls = 0

    monkeypatch.setattr(
        store_module,
        "_new_backup_path",
        _fixed_backup_path(backup_path),
    )

    def replace_then_lock(replaced: Path, replacement: Path, backup: Path) -> None:
        nonlocal calls
        calls += 1
        original_replace(replaced, replacement, backup)
        raise ctypes.WinError(33)

    monkeypatch.setattr(store_module, "_replace_file_win32", replace_then_lock)
    _save_with_reservation(store, new_manifest, expected_sha256=old_digest)

    assert calls == 1
    assert target.read_bytes() == new_bytes
    assert backup_path.read_bytes() == old_bytes
    assert _manifest_candidates_except_target(target) == [backup_path]


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 ReplaceFileW publication.")
def test_manifest_replace_partial_lock_outcome_preserves_all_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    old_manifest = _manifest("replace-partial-old")
    new_manifest = _manifest("replace-partial-new")
    old_digest = _save_with_reservation(store, old_manifest)
    old_bytes = manifest_to_bytes(old_manifest)
    new_bytes = manifest_to_bytes(new_manifest)
    target = _manifest_path(tmp_path)
    recovery_directory = target.parent / ".recovery" / str(JOB_ID)
    backup_path = recovery_directory / (
        ".bak-00000000000000000001-00000000-0000-4000-8000-000000000001"
    )
    calls = 0

    monkeypatch.setattr(
        store_module,
        "_new_backup_path",
        _fixed_backup_path(backup_path),
    )

    def backup_then_lock(replaced: Path, _replacement: Path, backup: Path) -> None:
        nonlocal calls
        calls += 1
        os.link(replaced, backup)
        raise ctypes.WinError(32)

    monkeypatch.setattr(store_module, "_replace_file_win32", backup_then_lock)
    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(store, new_manifest, expected_sha256=old_digest)

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert calls == 1
    assert target.read_bytes() == old_bytes
    assert backup_path.read_bytes() == old_bytes
    candidates = _manifest_candidates_except_target(target)
    assert len(candidates) == 2
    assert {candidate.read_bytes() for candidate in candidates} == {old_bytes, new_bytes}


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 ReplaceFileW publication.")
@pytest.mark.parametrize(
    "ambiguous_after_mutation",
    [False, True],
    ids=["before-replace", "after-replace"],
)
def test_manifest_update_replace_fault_preserves_old_and_new_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ambiguous_after_mutation: bool,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    old_manifest = _manifest("replace-fault-old")
    new_manifest = _manifest("replace-fault-new")
    old_digest = _save_with_reservation(store, old_manifest)
    old_bytes = manifest_to_bytes(old_manifest)
    new_bytes = manifest_to_bytes(new_manifest)
    target = _manifest_path(tmp_path)
    recovery_directory = target.parent / ".recovery" / str(JOB_ID)
    backup_path = recovery_directory / f".bak-{1:020d}-00000000-0000-4000-8000-000000000001"
    original_move = cast(_ReplaceFile, _store_private_attribute("_replace_file_win32"))
    replace_calls: list[tuple[Path, Path, Path]] = []

    def fixed_backup_path(directory: Path, generation: int) -> Path:
        assert directory == recovery_directory
        assert generation == 1
        return backup_path

    def injected_replace(
        replaced_path: Path,
        replacement_path: Path,
        actual_backup_path: Path,
    ) -> None:
        replace_calls.append((replaced_path, replacement_path, actual_backup_path))
        assert actual_backup_path == backup_path
        if ambiguous_after_mutation:
            original_move(replaced_path, replacement_path, actual_backup_path)
        raise OSError("injected ReplaceFileW failure/ambiguous result")

    monkeypatch.setattr(store_module, "_new_backup_path", fixed_backup_path)
    monkeypatch.setattr(store_module, "_replace_file_win32", injected_replace)

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(store, new_manifest, expected_sha256=old_digest)

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert len(replace_calls) == 1
    if ambiguous_after_mutation:
        assert target.read_bytes() == new_bytes
        assert backup_path.read_bytes() == old_bytes
        assert _manifest_candidates_except_target(target) == [backup_path]
    else:
        assert target.read_bytes() == old_bytes
        assert not backup_path.exists()
        candidates = _manifest_candidates_except_target(target)
        assert len(candidates) == 1
        assert candidates[0].read_bytes() == new_bytes


@pytest.mark.parametrize("winerror", [None, 39, 112], ids=["enospc", "handle-full", "disk-full"])
def test_manifest_replace_disk_full_closes_gate_and_preserves_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    winerror: int | None,
) -> None:
    import app.persistence.manifest_store as store_module

    gate = SharedStorageGate()
    store = JobManifestStore(
        tmp_path,
        data_root_identity=_capture_data_root_identity(tmp_path),
        recovery_guard=_noop_recovery_guard,
        storage_gate=gate,
    )
    old_manifest = _manifest("replace-disk-full-old")
    new_manifest = _manifest("replace-disk-full-new")
    old_digest = _save_with_reservation(store, old_manifest)
    old_bytes = manifest_to_bytes(old_manifest)
    new_bytes = manifest_to_bytes(new_manifest)
    target = _manifest_path(tmp_path)
    assert target.read_bytes() == old_bytes
    calls = 0

    def fail_replace(_target: Path, _staging: Path, _backup: Path) -> None:
        nonlocal calls
        calls += 1
        raise _injected_disk_full(winerror)

    monkeypatch.setattr(store_module, "_replace_file_win32", fail_replace)

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(store, new_manifest, expected_sha256=old_digest)

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert calls == 1
    assert gate.closed
    assert gate.closed_reason == "FILESYSTEM_DISK_FULL"
    with pytest.raises(StorageGateClosedError):
        with gate.mutation():
            pass
    assert target.read_bytes() == old_bytes
    candidates = _manifest_candidates_except_target(target)
    assert len(candidates) == 1
    assert candidates[0].read_bytes() == new_bytes


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 ReplaceFileW publication.")
def test_manifest_update_rejects_a_readable_backup_with_the_wrong_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    old_manifest = _manifest("backup-digest-expected")
    new_manifest = _manifest("backup-digest-new")
    wrong_backup_manifest = _manifest("backup-digest-other-complete-manifest")
    old_digest = _save_with_reservation(store, old_manifest)
    old_bytes = manifest_to_bytes(old_manifest)
    new_bytes = manifest_to_bytes(new_manifest)
    wrong_backup_bytes = manifest_to_bytes(wrong_backup_manifest)
    assert _manifest_path(tmp_path).read_bytes() == old_bytes
    assert hashlib.sha256(wrong_backup_bytes).hexdigest() != old_digest

    target = _manifest_path(tmp_path)
    recovery_directory = target.parent / ".recovery" / str(JOB_ID)
    backup_path = recovery_directory / f".bak-{1:020d}-00000000-0000-4000-8000-000000000002"
    original_replace = cast(
        _ReplaceFile,
        _store_private_attribute("_replace_file_win32"),
    )
    replace_calls: list[tuple[Path, Path, Path]] = []

    def fixed_backup_path(directory: Path, generation: int) -> Path:
        assert directory == recovery_directory
        assert generation == 1
        return backup_path

    def replace_then_substitute_backup(
        replaced_path: Path,
        replacement_path: Path,
        actual_backup_path: Path,
    ) -> None:
        replace_calls.append((replaced_path, replacement_path, actual_backup_path))
        assert actual_backup_path == backup_path
        original_replace(replaced_path, replacement_path, actual_backup_path)
        backup_path.write_bytes(wrong_backup_bytes)

    monkeypatch.setattr(store_module, "_new_backup_path", fixed_backup_path)
    monkeypatch.setattr(
        store_module,
        "_replace_file_win32",
        replace_then_substitute_backup,
    )

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(store, new_manifest, expected_sha256=old_digest)

    assert raised.value.code is ErrorCode.DL_MANIFEST_STALE_SNAPSHOT
    assert len(replace_calls) == 1
    assert target.read_bytes() == new_bytes
    assert backup_path.read_bytes() == wrong_backup_bytes
    assert manifest_from_bytes(backup_path.read_bytes()) == wrong_backup_manifest
    assert hashlib.sha256(backup_path.read_bytes()).hexdigest() != old_digest
    assert _manifest_candidates_except_target(target) == [backup_path]


@pytest.mark.skipif(os.name != "nt", reason="requires the Win32 publication wrappers.")
@pytest.mark.parametrize(
    ("operation", "verification_point"),
    [
        ("create", "staging"),
        ("create", "destination"),
        ("update", "staging"),
        ("update", "destination"),
        ("update", "backup"),
    ],
    ids=[
        "create-staging-read",
        "create-destination-readback",
        "update-staging-read",
        "update-destination-readback",
        "update-backup-readback",
    ],
)
def test_manifest_readback_fault_preserves_all_complete_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    verification_point: str,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    old_manifest = _manifest("readback-old")
    new_manifest = _manifest("readback-new")
    old_digest: str | None = None
    old_bytes = manifest_to_bytes(old_manifest)
    if operation == "update":
        old_digest = _save_with_reservation(store, old_manifest)
    new_bytes = manifest_to_bytes(new_manifest)
    target = _manifest_path(tmp_path)
    recovery_directory = target.parent / ".recovery" / str(JOB_ID)
    backup_path = recovery_directory / f".bak-{1:020d}-00000000-0000-4000-8000-000000000003"
    if verification_point == "backup":
        def fixed_backup_path(directory: Path, generation: int) -> Path:
            assert directory == recovery_directory
            assert generation == 1
            return backup_path

        monkeypatch.setattr(
            store_module,
            "_new_backup_path",
            fixed_backup_path,
        )

    published = False
    if verification_point in {"destination", "backup"}:
        if operation == "create":
            original_publish = cast(
                _MoveFileEx,
                _store_private_attribute("_move_file_ex_win32"),
            )

            def observe_move(source_path: Path, destination_path: Path) -> None:
                nonlocal published
                original_publish(source_path, destination_path)
                published = True

            monkeypatch.setattr(store_module, "_move_file_ex_win32", observe_move)
        else:
            original_publish = cast(
                _ReplaceFile,
                _store_private_attribute("_replace_file_win32"),
            )

            def observe_replace(
                replaced_path: Path,
                replacement_path: Path,
                actual_backup_path: Path,
            ) -> None:
                nonlocal published
                original_publish(replaced_path, replacement_path, actual_backup_path)
                published = True

            monkeypatch.setattr(store_module, "_replace_file_win32", observe_replace)

    original_read = cast(
        _ReadManifestBytes,
        _store_private_attribute("_read_manifest_bytes"),
    )
    injected_paths: list[Path] = []

    def injected_read(
        path: Path,
        *,
        max_bytes: int,
        expected_identity: tuple[int, int] | None = None,
        expected_size: int | None = None,
        expected_parent_identity: tuple[int, int] | None = None,
        job_id: UUID | None = None,
        trace_id: str | None = None,
    ) -> bytes:
        should_fail = (
            verification_point == "staging" and path != target
        ) or (
            verification_point == "destination"
            and path == target
            and published
        ) or (
            verification_point == "backup" and path == backup_path
        )
        if should_fail:
            injected_paths.append(path)
            raise OSError(f"injected {verification_point} readback failure")
        return original_read(
            path,
            max_bytes=max_bytes,
            expected_identity=expected_identity,
            expected_size=expected_size,
            expected_parent_identity=expected_parent_identity,
            job_id=job_id,
            trace_id=trace_id,
        )

    monkeypatch.setattr(store_module, "_read_manifest_bytes", injected_read)

    with pytest.raises(ManifestStoreError) as raised:
        if operation == "create":
            _save_with_reservation(store, new_manifest)
        else:
            assert old_digest is not None
            _save_with_reservation(store, new_manifest, expected_sha256=old_digest)

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert len(injected_paths) == 1
    if verification_point == "staging":
        assert not published
        assert injected_paths[0] != target
        assert (target.read_bytes() if target.exists() else None) == (
            old_bytes if operation == "update" else None
        )
        candidates = _manifest_candidates_except_target(target)
        assert len(candidates) == 1
        assert candidates[0].read_bytes() == new_bytes
    elif operation == "create":
        assert published
        assert injected_paths == [target]
        assert target.read_bytes() == new_bytes
        assert _manifest_candidates_except_target(target) == []
    elif verification_point == "destination":
        assert published
        assert injected_paths == [target]
        assert target.read_bytes() == new_bytes
        candidates = _manifest_candidates_except_target(target)
        assert len(candidates) == 1
        assert candidates[0].read_bytes() == old_bytes
    else:
        assert published
        assert injected_paths == [backup_path]
        assert target.read_bytes() == new_bytes
        assert backup_path.read_bytes() == old_bytes
        assert _manifest_candidates_except_target(target) == [backup_path]


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 publication and process termination.")
@pytest.mark.parametrize(
    ("operation", "boundary"),
    [
        ("create", "before"),
        ("create", "after"),
        ("update", "before"),
        ("update", "after"),
    ],
    ids=[
        "create-before-move",
        "create-after-move",
        "update-before-replace",
        "update-after-replace",
    ],
)
def test_process_termination_at_publication_boundary_keeps_recoverable_bytes(
    tmp_path: Path,
    operation: str,
    boundary: str,
) -> None:
    context = multiprocessing.get_context("spawn")
    boundary_reached = context.Event()
    child = context.Process(
        target=_crash_at_manifest_publication_boundary,
        args=(str(tmp_path), operation, boundary, boundary_reached),
    )
    old_manifest = _manifest("crash-before-update")
    new_manifest = _manifest(
        "crash-after-create" if operation == "create" else "crash-after-update"
    )
    old_bytes = manifest_to_bytes(old_manifest)
    new_bytes = manifest_to_bytes(new_manifest)
    target = _manifest_path(tmp_path)

    child.start()
    try:
        assert boundary_reached.wait(timeout=20)
        child.terminate()
        child.join(timeout=10)
        assert not child.is_alive()
        assert child.exitcode != 0

        candidates = _manifest_candidates_except_target(target)
        if boundary == "before":
            assert (target.read_bytes() if target.exists() else None) == (
                old_bytes if operation == "update" else None
            )
            assert len(candidates) == 1
            assert candidates[0].read_bytes() == new_bytes
        elif operation == "create":
            assert target.read_bytes() == new_bytes
            assert candidates == []
        else:
            assert target.read_bytes() == new_bytes
            assert len(candidates) == 1
            assert candidates[0].read_bytes() == old_bytes
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows ReplaceFileW publication.")
@pytest.mark.parametrize("candidate_kind", ["existing_file", "reparse_point"])
def test_manifest_update_blocks_an_unattributed_recovery_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_kind: str,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    original_manifest = _manifest("before-backup-conflict")
    original_digest = _save_with_reservation(store, original_manifest)
    target = _manifest_path(tmp_path)
    original_target = target.read_bytes()
    recovery_directory = target.parent / ".recovery" / str(JOB_ID)
    backup_candidate = recovery_directory / (
        ".bak-00000000000000000001-00000000-0000-4000-8000-000000000004"
    )
    outside_backup = tmp_path / "outside-backup.bak"

    if candidate_kind == "existing_file":
        backup_candidate.write_bytes(b"preserve-existing-backup-candidate")
        original_candidate = backup_candidate.read_bytes()
    else:
        outside_backup.write_bytes(b"preserve-external-backup-target")
        _create_file_reparse_point(backup_candidate, outside_backup)
        original_candidate = None

    replace_calls: list[tuple[Path, Path, Path]] = []

    def record_replace(
        replaced_path: Path,
        replacement_path: Path,
        backup_path: Path,
    ) -> None:
        replace_calls.append((replaced_path, replacement_path, backup_path))

    monkeypatch.setattr(store_module, "_replace_file_win32", record_replace)

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(
            store,
            _manifest("after-backup-conflict"),
            expected_sha256=original_digest,
        )

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert replace_calls == []
    assert target.read_bytes() == original_target
    if candidate_kind == "existing_file":
        assert backup_candidate.read_bytes() == original_candidate
    else:
        assert backup_candidate.is_symlink()
        assert outside_backup.read_bytes() == b"preserve-external-backup-target"


@pytest.mark.parametrize("target_state", ["digest_mismatch", "missing"])
def test_manifest_update_rejects_a_stale_snapshot_without_writing(
    tmp_path: Path,
    target_state: str,
) -> None:
    store = _manifest_store(tmp_path)
    original = _manifest("opaque-page-v1")
    original_digest = _save_with_reservation(store, original)
    target = _manifest_path(tmp_path)
    if target_state == "digest_mismatch":
        target.write_bytes(b"externally-replaced-target")
        prior_bytes = target.read_bytes()
        expected_digest = original_digest
    else:
        target.unlink()
        prior_bytes = None
        expected_digest = original_digest
    prior_names = {path.name for path in target.parent.iterdir()}

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(store, _manifest("opaque-page-v2"), expected_sha256=expected_digest)

    assert raised.value.code is ErrorCode.DL_MANIFEST_STALE_SNAPSHOT
    assert (target.read_bytes() if target.exists() else None) == prior_bytes
    assert {path.name for path in target.parent.iterdir()} == prior_names


@pytest.mark.parametrize("existing_kind", ["invalid_json", "different_job"])
def test_manifest_update_rejects_a_digest_matching_non_v1_or_wrong_job_target(
    tmp_path: Path,
    existing_kind: str,
) -> None:
    target = _manifest_path(tmp_path)
    target.parent.mkdir(parents=True)
    existing = (
        b"not-json"
        if existing_kind == "invalid_json"
        else manifest_to_bytes(_manifest("other-job", job_id=OTHER_JOB_ID))
    )
    target.write_bytes(existing)
    digest = hashlib.sha256(existing).hexdigest()

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(
            _manifest_store(tmp_path),
            _manifest("replacement"),
            expected_sha256=digest,
        )

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert target.read_bytes() == existing


def test_manifest_update_serializes_competing_same_job_snapshots(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = _manifest_store(tmp_path)
    old_digest = _save_with_reservation(store, _manifest("opaque-page-v1"))
    candidates = (_manifest("opaque-page-v2"), _manifest("opaque-page-v3"))
    barrier = Barrier(3)

    def publish(candidate: DownloadJobManifest) -> tuple[str, object]:
        barrier.wait(timeout=5)
        try:
            return "published", _save_with_reservation(
                store,
                candidate,
                expected_sha256=old_digest,
            )
        except Exception as error:
            return "failed", error

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish, candidate) for candidate in candidates]
        barrier.wait(timeout=5)
        outcomes = [future.result(timeout=10) for future in futures]

    published = [value for status, value in outcomes if status == "published"]
    failed = [value for status, value in outcomes if status == "failed"]
    assert len(published) == 1
    assert len(failed) == 1
    assert getattr(failed[0], "code", None) is ErrorCode.DL_MANIFEST_STALE_SNAPSHOT
    final_manifest = manifest_from_bytes(_manifest_path(tmp_path).read_bytes())
    assert final_manifest in candidates
    assert hashlib.sha256(_manifest_path(tmp_path).read_bytes()).hexdigest() == published[0]


def test_lock_job_is_reentrant_within_a_process(tmp_path: Path) -> None:
    store = _manifest_store(tmp_path)

    with store.lock_job(JOB_ID):
        with store.lock_job(JOB_ID):
            pass


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 mutex thread ownership.")
def test_manifest_save_cannot_be_handed_to_a_different_thread(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeoutError

    store = _manifest_store(tmp_path)
    caplog.set_level("ERROR")
    executor = ThreadPoolExecutor(max_workers=1)
    future = None
    observed_error: ManifestStoreError | None = None
    try:
        with store.lock_job(JOB_ID):
            candidate = _manifest("wrong-thread")
            current_bytes = manifest_to_bytes(_empty_manifest(job_id=JOB_ID))
            new_bytes = manifest_to_bytes(candidate)
            reservation = store.reserve_write(
                job_id=JOB_ID,
                expected_current_generation=0,
                current_sha256=hashlib.sha256(current_bytes).hexdigest(),
                current_size_bytes=len(current_bytes),
                new_sha256=hashlib.sha256(new_bytes).hexdigest(),
                new_size_bytes=len(new_bytes),
            )
            future = executor.submit(store.save, candidate, reservation=reservation)
            try:
                future.result(timeout=7)
            except FutureTimeoutError:
                pass
            except Exception as error:
                if isinstance(error, ManifestStoreError):
                    observed_error = error
            assert not _manifest_path(tmp_path).exists()
            assert not (tmp_path / "database").exists()
    finally:
        if future is not None and not future.done():
            try:
                future.result(timeout=7)
            except Exception as error:
                if isinstance(error, ManifestStoreError):
                    observed_error = error
        executor.shutdown(wait=True, cancel_futures=True)

    assert observed_error is not None
    assert observed_error.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert any(
        getattr(record, "stage", None) == "job_mutex_required"
        for record in caplog.records
    )
    assert not _manifest_path(tmp_path).exists()
    assert not (tmp_path / "database").exists()


@pytest.mark.skipif(os.name != "nt", reason="requires the Win32 Global mutex contract.")
def test_lock_job_serializes_processes(tmp_path: Path) -> None:
    _assert_parent_waits_for_child_mutex_owner(tmp_path, tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows directory reparse point.")
def test_lock_job_aliases_share_the_same_physical_root_mutex(tmp_path: Path) -> None:
    data_root = tmp_path / "data-root"
    alias = tmp_path / "data-root-alias"
    data_root.mkdir()
    _create_directory_reparse_point(alias, data_root)
    identity = _capture_data_root_identity(alias)

    assert identity.canonical_path == data_root.resolve()
    _assert_parent_waits_for_child_mutex_owner(
        data_root,
        identity.canonical_path,
        parent_data_root_identity=identity,
    )


@pytest.mark.skipif(os.name != "nt", reason="requires a Win32 abandoned mutex.")
def test_lock_job_runs_recovery_guard_before_publish_after_owner_abandons_mutex(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    child_acquired = context.Event()
    never_release = context.Event()
    child = context.Process(
        target=_hold_job_mutex,
        args=(str(tmp_path), str(JOB_ID), child_acquired, never_release),
    )
    child.start()
    try:
        assert child_acquired.wait(timeout=10)
        child.terminate()
        child.join(timeout=10)
        assert not child.is_alive()

        guard_calls: list[UUID] = []

        def recovery_guard(job_id: UUID) -> None:
            assert job_id == JOB_ID
            assert not _manifest_path(tmp_path).exists()
            guard_calls.append(job_id)

        store = _manifest_store(tmp_path, recovery_guard=recovery_guard)
        digest = _save_with_reservation(store, _manifest("recovered-before-publish"))

        assert guard_calls == [JOB_ID]
        assert hashlib.sha256(_manifest_path(tmp_path).read_bytes()).hexdigest() == digest
        assert manifest_from_bytes(_manifest_path(tmp_path).read_bytes()) == _manifest(
            "recovered-before-publish"
        )
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="requires a Win32 abandoned mutex.")
def test_abandoned_mutex_runs_each_queued_waiters_guard_before_its_operation(
    tmp_path: Path,
) -> None:
    from time import sleep

    context = multiprocessing.get_context("spawn")
    child_acquired = context.Event()
    never_release = context.Event()
    child = context.Process(
        target=_hold_job_mutex,
        args=(str(tmp_path), str(JOB_ID), child_acquired, never_release),
    )

    first_guard_started = context.Event()
    first_guard_claim_lock = context.Lock()
    release_first_guard = context.Event()
    event_queue: Any = context.Queue()
    waiters: list[multiprocessing.Process] = []
    attempting = [context.Event(), context.Event()]
    operation_started = [context.Event(), context.Event()]

    child.start()
    try:
        assert child_acquired.wait(timeout=10)
        for index, waiter_id in enumerate(("waiter-a", "waiter-b")):
            waiter = cast(
                multiprocessing.Process,
                context.Process(
                    target=_wait_for_abandoned_mutex_with_guard,
                    args=(
                        str(tmp_path),
                        str(JOB_ID),
                        waiter_id,
                        attempting[index],
                        first_guard_started,
                        first_guard_claim_lock,
                        release_first_guard,
                        operation_started[index],
                        event_queue,
                    ),
                ),
            )
            waiter.start()
            waiters.append(waiter)

        assert all(event.wait(timeout=10) for event in attempting)
        # Let both processes enter the Win32 wait while the owner still holds
        # the mutex. Their attempt events are emitted immediately before it.
        sleep(0.25)
        assert all(waiter.is_alive() for waiter in waiters)

        child.terminate()
        child.join(timeout=10)
        assert not child.is_alive()

        assert first_guard_started.wait(timeout=10)
        assert not _manifest_path(tmp_path).exists()
        assert not any(event.is_set() for event in operation_started)

        # The first granted waiter remains inside its guard while its peer is
        # already queued on the same mutex. No manifest can publish yet.
        sleep(0.25)
        assert not _manifest_path(tmp_path).exists()
        assert not any(event.is_set() for event in operation_started)
        release_first_guard.set()

        for waiter in waiters:
            waiter.join(timeout=15)
            assert not waiter.is_alive()
            assert waiter.exitcode == 0

        records: list[tuple[object, ...]] = []
        while len([item for item in records if item[0] == "result"]) < 2:
            records.append(event_queue.get(timeout=5))

        for waiter_id in ("waiter-a", "waiter-b"):
            waiter_records = [
                item for item in records if len(item) > 1 and item[1] == waiter_id
            ]
            kinds = [item[0] for item in waiter_records]
            assert kinds.count("guard_enter") == 1
            assert kinds.count("guard_exit") == 1
            assert kinds.count("operation") == 1
            assert kinds.index("guard_exit") < kinds.index("operation")

        results = [item for item in records if item[0] == "result"]
        published = [item for item in results if item[2] == "published"]
        failed = [item for item in results if item[2] == "failed"]
        assert len(published) == 1
        assert len(failed) == 1
        assert failed[0][3] is ErrorCode.DL_MANIFEST_PERSIST_FAILED
        final_bytes = _manifest_path(tmp_path).read_bytes()
        assert hashlib.sha256(final_bytes).hexdigest() == published[0][3]
        assert manifest_from_bytes(final_bytes) in (
            _manifest("abandoned-mutex-waiter-a"),
            _manifest("abandoned-mutex-waiter-b"),
        )
    finally:
        release_first_guard.set()
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
        for waiter in waiters:
            if waiter.is_alive():
                waiter.terminate()
                waiter.join(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="requires a Win32 Global mutex timeout.")
def test_lock_job_times_out_after_five_seconds_without_filesystem_io(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from threading import Timer
    from time import monotonic

    context = multiprocessing.get_context("spawn")
    child_acquired = context.Event()
    release_child = context.Event()
    child = context.Process(
        target=_hold_job_mutex,
        args=(str(tmp_path), str(JOB_ID), child_acquired, release_child),
    )
    child.start()
    try:
        assert child_acquired.wait(timeout=10)
        caplog.set_level("ERROR")
        safety_release = Timer(7.5, release_child.set)
        safety_release.start()
        started = monotonic()
        try:
            with pytest.raises(ManifestStoreError) as raised:
                _save_with_reservation(
                    _manifest_store(tmp_path),
                    _manifest("must-not-publish"),
                )
        finally:
            safety_release.cancel()
            release_child.set()
        elapsed = monotonic() - started

        assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
        assert 4.5 <= elapsed < 7.5
        assert any(
            getattr(record, "stage", None) == "job_mutex_wait"
            for record in caplog.records
        )
        assert not _manifest_path(tmp_path).exists()
        assert not (tmp_path / "database").exists()
    finally:
        release_child.set()
        child.join(timeout=10)
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)


def test_temporary_page_create_is_exclusive_and_preserves_existing_bytes(
    tmp_path: Path,
) -> None:
    temporary_path = "jobs/page-001.part"
    store = _temporary_page_store(tmp_path)
    with store.create(temporary_path) as created:
        created.write(b"page-bytes-that-must-not-be-truncated")
    target = tmp_path / "temp" / "jobs" / "page-001.part"
    original = target.read_bytes()

    with pytest.raises(ManifestStoreError) as raised:
        with store.create(temporary_path):
            pass

    assert raised.value.code is ErrorCode.DL_TEMPORARY_PATH_CONFLICT
    assert target.read_bytes() == original


def test_temporary_page_destination_reuses_verified_parent_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    target = tmp_path / "temp" / "jobs" / "page-parent-proof.part"
    target.parent.mkdir(parents=True)
    expected_parent_identity = store_module._managed_file_identity(  # pyright: ignore[reportPrivateUsage]
        target.parent.lstat()
    )
    assert expected_parent_identity is not None

    observed_parent_identities: list[tuple[int, int] | None] = []
    original_assert_no_reparse = store_module._assert_no_reparse  # pyright: ignore[reportPrivateUsage]

    def observe_destination_parent(
        path: Path,
        **kwargs: Any,
    ) -> os.stat_result | None:
        if path == target and kwargs.get("stage") == "temporary_path_destination":
            observed_parent_identities.append(kwargs.get("expected_parent_identity"))
        return original_assert_no_reparse(path, **kwargs)

    monkeypatch.setattr(store_module, "_assert_no_reparse", observe_destination_parent)

    with _temporary_page_store(tmp_path).create("jobs/page-parent-proof.part") as created:
        created.write(b"page content remains intact")

    assert observed_parent_identities == [expected_parent_identity]
    assert target.read_bytes() == b"page content remains intact"


def test_temporary_page_preflight_retries_transient_stat_lock_and_preserves_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.filesystem_retry as filesystem_retry_module

    temporary_path = "jobs/page-stat-lock.part"
    store = _temporary_page_store(tmp_path)
    target = tmp_path / "temp" / temporary_path
    real_lstat = Path.lstat
    target_stat_calls = 0
    sleeps: list[float] = []

    def lock_once(candidate: Path) -> os.stat_result:
        nonlocal target_stat_calls
        if candidate == target:
            target_stat_calls += 1
            if target_stat_calls == 1:
                raise _InjectedWin32Lock(32)
        return real_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", lock_once)
    monkeypatch.setattr(filesystem_retry_module.time, "sleep", sleeps.append)

    with store.create(temporary_path, job_id=JOB_ID) as created:
        created.write(b"candidate remains intact")

    assert target_stat_calls >= 2
    assert sleeps == [0.1]
    assert target.read_bytes() == b"candidate remains intact"


def test_temporary_page_create_allows_only_one_racing_creator(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = _temporary_page_store(tmp_path)
    temporary_path = "jobs/race.part"
    barrier = Barrier(3)
    payloads = (b"first-complete-page", b"second-complete-page")

    def create(payload: bytes) -> tuple[str, object]:
        barrier.wait(timeout=5)
        try:
            with store.create(temporary_path) as created:
                created.write(payload)
            return "created", payload
        except Exception as error:
            return "failed", error

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create, payload) for payload in payloads]
        barrier.wait(timeout=5)
        outcomes = [future.result(timeout=10) for future in futures]

    created = [value for status, value in outcomes if status == "created"]
    failed = [value for status, value in outcomes if status == "failed"]
    target = tmp_path / "temp" / "jobs" / "race.part"
    assert len(created) == 1
    assert len(failed) == 1
    assert getattr(failed[0], "code", None) is ErrorCode.DL_TEMPORARY_PATH_CONFLICT
    assert target.read_bytes() == created[0]


def test_temporary_page_create_rejects_escape_without_touching_outside_file(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.part"
    outside.write_bytes(b"preserve-outside-content")

    with pytest.raises(ManifestStoreError) as raised:
        with _temporary_page_store(tmp_path).create("../outside.part"):
            pass

    assert raised.value.code is ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE
    assert outside.read_bytes() == b"preserve-outside-content"


@pytest.mark.skipif(os.name != "nt", reason="Windows disk-full error codes.")
@pytest.mark.parametrize("winerror", [39, 112], ids=["handle-disk-full", "disk-full"])
def test_temporary_page_store_classifies_windows_disk_full_errors(
    winerror: int,
) -> None:
    error = ctypes.WinError(winerror)

    assert _temporary_page_io_error_code(error) is ErrorCode.SYS_DISK_BUDGET_EXCEEDED


@pytest.mark.parametrize("supplementary", [False, True], ids=["ascii", "surrogate-pair"])
def test_temporary_page_create_accepts_259_absolute_utf16_units(
    tmp_path: Path,
    supplementary: bool,
) -> None:
    data_root = tmp_path / "data-root"
    data_root.mkdir()
    relative_path, absolute_path = _page_path_for_absolute_length(
        data_root,
        259,
        supplementary=supplementary,
    )

    with _temporary_page_store(data_root).create(relative_path) as created:
        created.write(b"within-legacy-absolute-path-limit")

    assert _utf16_units(str(absolute_path)) == 259
    assert absolute_path.read_bytes() == b"within-legacy-absolute-path-limit"


@pytest.mark.parametrize("supplementary", [False, True], ids=["ascii", "surrogate-pair"])
def test_temporary_page_create_rejects_260_absolute_utf16_units_before_file_write(
    tmp_path: Path,
    supplementary: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data_root = tmp_path / "data-root"
    data_root.mkdir()
    relative_path, absolute_path = _page_path_for_absolute_length(
        data_root,
        260,
        supplementary=supplementary,
    )
    caplog.set_level(logging.ERROR, logger="yomira.manifest_store")

    with pytest.raises(ManifestStoreError) as raised:
        with _temporary_page_store(data_root).create(relative_path):
            pass

    assert raised.value.code is ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE
    path_limit_record = next(
        record
        for record in caplog.records
        if record.name == "yomira.manifest_store"
        and record.msg == "manifest_store.path_limit_exceeded"
    )
    assert isinstance(getattr(path_limit_record, "trace_id", None), str)
    assert getattr(path_limit_record, "trace_id", "").strip()
    assert _utf16_units(str(absolute_path)) == 260
    assert not any(path.is_file() for path in data_root.rglob("*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point contract.")
def test_temporary_page_create_rejects_a_reparse_point_under_temp(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data-root"
    outside = tmp_path / "outside"
    data_root.mkdir()
    outside.mkdir()
    outside_file = outside / "page.part"
    outside_file.write_bytes(b"preserve-outside-content")
    temp_root = data_root / "temp"
    temp_root.mkdir()
    _create_directory_reparse_point(temp_root / "escape", outside)

    with pytest.raises(ManifestStoreError) as raised:
        with _temporary_page_store(data_root).create("escape/page.part"):
            pass

    assert raised.value.code is ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE
    assert outside_file.read_bytes() == b"preserve-outside-content"


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point contract.")
@pytest.mark.parametrize("reparse_component", ["database", "manifests"])
def test_manifest_save_rejects_reparse_points_under_manifest_directory(
    tmp_path: Path,
    reparse_component: str,
) -> None:
    data_root = tmp_path / "data-root"
    outside = tmp_path / "outside"
    data_root.mkdir()
    outside.mkdir()
    if reparse_component == "database":
        _create_directory_reparse_point(data_root / "database", outside)
    else:
        database_directory = data_root / "database"
        database_directory.mkdir()
        _create_directory_reparse_point(database_directory / "manifests", outside)

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(_manifest_store(data_root), _manifest("opaque-page"))

    assert raised.value.code is ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE
    assert not any(path.is_file() for path in outside.rglob("*"))


def test_manifest_save_rejects_a_reparse_point_at_the_target(tmp_path: Path) -> None:
    data_root = tmp_path / "data-root"
    outside = tmp_path / "outside.json"
    data_root.mkdir()
    outside.write_bytes(b"preserve-outside-target")
    target = _manifest_path(data_root)
    target.parent.mkdir(parents=True)
    try:
        target.symlink_to(outside)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"file symlinks are unavailable: {type(error).__name__}")

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(_manifest_store(data_root), _manifest("opaque-page"))

    assert raised.value.code is ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE
    assert outside.read_bytes() == b"preserve-outside-target"


def test_store_rejects_a_manifest_above_the_absolute_path_limit_before_writing(
    tmp_path: Path,
) -> None:
    base_root = tmp_path / "root"
    base_root.mkdir()
    fixed_suffix = f"{os.sep}database{os.sep}manifests{os.sep}{JOB_ID}.json"
    component_units = (
        260
        - _utf16_units(str(base_root.resolve()))
        - _utf16_units(os.sep)
        - _utf16_units(fixed_suffix)
    )
    assert 0 < component_units <= 255
    data_root = base_root / ("r" * component_units)
    data_root.mkdir()
    target = _manifest_path(data_root)
    assert _utf16_units(str(target.resolve())) == 260

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(_manifest_store(data_root), _manifest("opaque-page"))

    assert raised.value.code is ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE
    assert not target.exists()
    assert not any(path.is_file() for path in data_root.rglob("*"))


@pytest.mark.parametrize("winerror", [None, 39, 112], ids=["enospc", "handle-full", "disk-full"])
def test_temporary_page_store_mkdir_disk_full_closes_shared_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    winerror: int | None,
) -> None:
    data_root = tmp_path / "data-root"
    data_root.mkdir()
    gate = SharedStorageGate()
    store = TemporaryPageStore(
        data_root,
        data_root_identity=_capture_data_root_identity(data_root),
        storage_gate=gate,
    )
    real_mkdir: Any = Path.mkdir
    failing_directory = data_root / "temp"

    def fail_temp_directory(
        path: Path,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if path == failing_directory:
            if winerror is None:
                raise OSError(errno.ENOSPC, "injected disk full")
            raise ctypes.WinError(winerror)
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_temp_directory)

    with pytest.raises(ManifestStoreError):
        with store.create(f"jobs/{JOB_ID}/page.part"):
            pass

    assert gate.closed
    assert not failing_directory.exists()
    assert not any(path.is_file() for path in data_root.rglob("*"))


def test_manifest_directory_mkdir_disk_full_closes_gate_before_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = SharedStorageGate()
    store = JobManifestStore(
        tmp_path,
        data_root_identity=_capture_data_root_identity(tmp_path),
        recovery_guard=_noop_recovery_guard,
        storage_gate=gate,
    )
    manifest_directory = _manifest_path(tmp_path).parent
    real_mkdir: Any = Path.mkdir

    def fail_manifest_directory(
        path: Path,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if path == manifest_directory:
            raise OSError(errno.ENOSPC, "injected manifest directory disk full")
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_manifest_directory)

    with pytest.raises(ManifestStoreError):
        _save_with_reservation(store, _manifest("mkdir-full"))

    assert gate.closed
    assert (tmp_path / "database").is_dir()
    assert not manifest_directory.exists()
    assert not _manifest_path(tmp_path).exists()


@pytest.mark.parametrize("cleanup_method", ["obsolete", "retained"])
@pytest.mark.parametrize("failure_mode", ["closed_gate", "disk_full"])
def test_manifest_backup_unlink_is_gated_and_disk_full_closes_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_method: str,
    failure_mode: str,
) -> None:
    gate = SharedStorageGate()
    with _manifest_backup_cleanup_case(tmp_path, gate) as cleanup_case:
        (
            store,
            reservation,
            obsolete_backup,
            current_bytes,
            previous_digest,
            current_digest,
        ) = cleanup_case
        backup_bytes = obsolete_backup.read_bytes()

        def run_cleanup() -> None:
            if cleanup_method == "obsolete":
                store.prune_obsolete_backups(
                    JOB_ID,
                    current_generation=3,
                    current_sha256=current_digest,
                    current_size_bytes=len(current_bytes),
                    previous_sha256=previous_digest,
                )
            else:
                store.prune_retained_backup(reservation)

        if failure_mode == "closed_gate":
            gate.close("FILESYSTEM_DISK_FULL")
            with pytest.raises(StorageGateClosedError):
                run_cleanup()
        else:
            real_unlink: Any = Path.unlink

            def fail_backup_unlink(
                path: Path,
                *args: Any,
                **kwargs: Any,
            ) -> None:
                if path == obsolete_backup:
                    raise OSError(errno.ENOSPC, "injected backup unlink disk full")
                real_unlink(path, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", fail_backup_unlink)
            with pytest.raises(ManifestStoreError) as raised:
                run_cleanup()
            assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
            assert gate.closed
            assert gate.closed_reason == "FILESYSTEM_DISK_FULL"

        assert obsolete_backup.is_file()
        assert obsolete_backup.read_bytes() == backup_bytes


def test_prune_retained_backup_reuses_validated_recovery_directory_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    with _manifest_backup_cleanup_case(tmp_path, SharedStorageGate()) as cleanup_case:
        store, reservation, _obsolete_backup, _current, _previous, _digest = cleanup_case
        recovery_directory = (
            tmp_path / "database" / "manifests" / ".recovery" / str(JOB_ID)
        )
        recovery_root = recovery_directory.parent
        manifests_directory = recovery_root.parent
        parent_identities: list[tuple[int, int] | None] = []
        observed_directory_checks: list[
            tuple[Path, str | None, tuple[int, int] | None]
        ] = []
        observed_cleanup_checks: list[tuple[str, tuple[int, int] | None]] = []
        original_assert_no_reparse = store_module._assert_no_reparse  # pyright: ignore[reportPrivateUsage]

        def record_path_check(path: Path, **kwargs: Any) -> os.stat_result | None:
            info = original_assert_no_reparse(path, **kwargs)
            stage = kwargs.get("stage")
            if path in {recovery_root, recovery_directory} and stage in {
                "retained_backup_cleanup_root",
                "retained_backup_cleanup_directory",
            }:
                observed_directory_checks.append(
                    (path, stage, kwargs.get("expected_parent_identity"))
                )
            if path == recovery_directory and stage == "retained_backup_cleanup_directory":
                parent_identities.append(
                    store_module._managed_file_identity(cast(os.stat_result, info))  # pyright: ignore[reportPrivateUsage]
                )
            elif stage in {
                "retained_backup_cleanup",
                "retained_backup_cleanup_verify",
            }:
                observed_cleanup_checks.append(
                    (stage, kwargs.get("expected_parent_identity"))
                )
            return info

        monkeypatch.setattr(store_module, "_assert_no_reparse", record_path_check)

        store.prune_retained_backup(reservation)

        assert len(parent_identities) == 1
        parent_identity = parent_identities[0]
        assert parent_identity is not None
        expected_manifests_identity = store_module._managed_file_identity(  # pyright: ignore[reportPrivateUsage]
            manifests_directory.lstat()
        )
        expected_recovery_root_identity = store_module._managed_file_identity(  # pyright: ignore[reportPrivateUsage]
            recovery_root.lstat()
        )
        assert expected_manifests_identity is not None
        assert expected_recovery_root_identity is not None
        assert observed_directory_checks == [
            (recovery_root, "retained_backup_cleanup_root", expected_manifests_identity),
            (
                recovery_directory,
                "retained_backup_cleanup_directory",
                expected_recovery_root_identity,
            ),
        ]
        assert observed_cleanup_checks == [
            ("retained_backup_cleanup", parent_identity),
            ("retained_backup_cleanup_verify", parent_identity),
        ]


def test_backup_unlink_does_not_retry_after_parent_namespace_changes_during_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.filesystem_retry as retry_module

    store = _manifest_store(tmp_path)
    parent = tmp_path / "database" / "manifests" / ".recovery" / str(JOB_ID)
    parent.mkdir(parents=True)
    backup = parent / ".bak-test.json"
    backup_bytes = manifest_to_bytes(_manifest("unlink-parent-swap"))
    backup.write_bytes(backup_bytes)
    detached_parent = parent.with_name(parent.name + ".detached")
    real_unlink: Any = Path.unlink
    unlink_calls = 0
    sleeps: list[float] = []

    def injected_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal unlink_calls
        if path == backup:
            unlink_calls += 1
            if unlink_calls == 1:
                raise _InjectedWin32Lock(32)
        real_unlink(path, *args, **kwargs)

    def swap_parent_and_preserve_file(delay: float) -> None:
        sleeps.append(delay)
        parent.rename(detached_parent)
        parent.mkdir()
        os.link(detached_parent / backup.name, backup)

    monkeypatch.setattr(Path, "unlink", injected_unlink)
    monkeypatch.setattr(retry_module.time, "sleep", swap_parent_and_preserve_file)
    with pytest.raises(ManifestStoreError) as raised:
        store._unlink_recovery_backup(backup, stage="test_unlink", job_id=JOB_ID)  # pyright: ignore[reportPrivateUsage]

    assert raised.value.stage == "test_unlink"
    assert unlink_calls == 1
    assert backup.read_bytes() == backup_bytes
    assert (detached_parent / backup.name).read_bytes() == backup_bytes
    assert sleeps == [0.1]


def test_backup_unlink_does_not_accept_absence_after_parent_namespace_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _manifest_store(tmp_path)
    parent = tmp_path / "database" / "manifests" / ".recovery" / str(JOB_ID)
    parent.mkdir(parents=True)
    backup = parent / ".bak-test.json"
    backup.write_bytes(manifest_to_bytes(_manifest("unlink-applied-parent-swap")))
    detached_parent = parent.with_name(parent.name + ".detached")
    real_unlink: Any = Path.unlink
    unlink_calls = 0

    def unlink_then_swap_parent(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal unlink_calls
        if path == backup:
            unlink_calls += 1
            real_unlink(path, *args, **kwargs)
            parent.rename(detached_parent)
            parent.mkdir()
            raise _InjectedWin32Lock(33)
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_then_swap_parent)
    with pytest.raises(ManifestStoreError) as raised:
        store._unlink_recovery_backup(backup, stage="test_unlink", job_id=JOB_ID)  # pyright: ignore[reportPrivateUsage]

    assert raised.value.stage == "test_unlink"
    assert unlink_calls == 1
    assert not backup.exists()
    assert not (detached_parent / backup.name).exists()


def test_backup_unlink_rechecks_candidate_at_syscall_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.filesystem_retry as retry_module
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    parent = tmp_path / "database" / "manifests" / ".recovery" / str(JOB_ID)
    parent.mkdir(parents=True)
    backup = parent / ".bak-boundary.json"
    original_bytes = manifest_to_bytes(_manifest("unlink-boundary-original"))
    changed_bytes = b"changed after retry proof"
    backup.write_bytes(original_bytes)
    real_unlink: Any = Path.unlink
    real_matches = store_module._manifest_file_matches  # pyright: ignore[reportPrivateUsage]
    unlink_calls = 0
    match_calls = 0

    def lock_once(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal unlink_calls
        if path == backup:
            unlink_calls += 1
            if unlink_calls == 1:
                raise _InjectedWin32Lock(32)
        real_unlink(path, *args, **kwargs)

    def mutate_after_post_wait_proof(
        path: Path,
        *,
        identity: tuple[int, int],
        expected_bytes: bytes,
    ) -> bool:
        nonlocal match_calls
        result = real_matches(
            path,
            identity=identity,
            expected_bytes=expected_bytes,
        )
        if path == backup:
            match_calls += 1
            if match_calls == 2 and result:
                backup.write_bytes(changed_bytes)
        return result

    monkeypatch.setattr(Path, "unlink", lock_once)
    monkeypatch.setattr(store_module, "_manifest_file_matches", mutate_after_post_wait_proof)
    monkeypatch.setattr(retry_module.time, "sleep", _no_wait)

    with pytest.raises(ManifestStoreError) as raised:
        store._unlink_recovery_backup(backup, stage="test_unlink", job_id=JOB_ID)  # pyright: ignore[reportPrivateUsage]

    assert raised.value.stage == "test_unlink"
    assert match_calls == 3
    assert unlink_calls == 1
    assert backup.read_bytes() == changed_bytes


@pytest.mark.parametrize("failure_stage", ["write", "flush"])
def test_temporary_page_store_closes_stream_with_active_gate_after_disk_full(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    gate = SharedStorageGate()
    store = TemporaryPageStore(
        tmp_path,
        data_root_identity=_capture_data_root_identity(tmp_path),
        storage_gate=gate,
    )
    temporary_path = "jobs/close-after-full.part"
    target = tmp_path / "temp" / temporary_path
    active_mutations_on_close: list[int] = []
    real_open: Any = Path.open

    class DiskFullStream:
        def __init__(self, stream: Any) -> None:
            self._stream = stream
            self._failed = False

        def write(self, value: bytes) -> int:
            if failure_stage == "write" and not self._failed:
                self._failed = True
                self._stream.write(value[: min(8, len(value))])
                raise OSError(errno.ENOSPC, "injected page write disk full")
            return self._stream.write(value)

        def flush(self) -> None:
            self._stream.flush()
            if failure_stage == "flush" and not self._failed:
                self._failed = True
                raise OSError(errno.ENOSPC, "injected page flush disk full")

        def close(self) -> None:
            active_mutations_on_close.append(gate.active_mutations)
            self._stream.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patch_open(
        path: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        stream = real_open(path, mode, *args, **kwargs)
        if path == target and mode == "x+b":
            return DiskFullStream(stream)
        return stream

    monkeypatch.setattr(Path, "open", patch_open)
    payload = b"preserve-this-page-prefix"
    with pytest.raises(ManifestStoreError) as raised:
        with store.create(temporary_path) as stream:
            stream.write(payload)
            if failure_stage == "flush":
                stream.flush()

    assert raised.value.code is ErrorCode.SYS_DISK_BUDGET_EXCEEDED
    assert gate.closed
    assert active_mutations_on_close == [1]
    assert target.read_bytes().startswith(payload[:8] if failure_stage == "write" else payload)


def test_temporary_page_store_aborts_stream_with_active_gate_permit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = SharedStorageGate()
    store = TemporaryPageStore(
        tmp_path,
        data_root_identity=_capture_data_root_identity(tmp_path),
        storage_gate=gate,
    )
    temporary_path = "jobs/abort-close.part"
    target = tmp_path / "temp" / temporary_path
    active_mutations_on_close: list[int] = []
    real_open: Any = Path.open

    class CloseObservingStream:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        def write(self, value: bytes) -> int:
            return self._stream.write(value)

        def close(self) -> None:
            active_mutations_on_close.append(gate.active_mutations)
            self._stream.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patch_open(
        path: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        stream = real_open(path, mode, *args, **kwargs)
        if path == target and mode == "x+b":
            return CloseObservingStream(stream)
        return stream

    class InjectedAbort(BaseException):
        pass

    monkeypatch.setattr(Path, "open", patch_open)
    with pytest.raises(InjectedAbort):
        with store.create(temporary_path) as stream:
            stream.write(b"preserve-before-abort")
            raise InjectedAbort()

    assert not gate.closed
    assert active_mutations_on_close == [1]
    assert target.read_bytes() == b"preserve-before-abort"


def test_temporary_page_store_close_disk_full_preserves_abort_and_closes_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = SharedStorageGate()
    store = TemporaryPageStore(
        tmp_path,
        data_root_identity=_capture_data_root_identity(tmp_path),
        storage_gate=gate,
    )
    temporary_path = "jobs/abort-close-full.part"
    target = tmp_path / "temp" / temporary_path
    active_mutations_on_close: list[int] = []
    real_open: Any = Path.open

    class FailingCloseStream:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        def write(self, value: bytes) -> int:
            return self._stream.write(value)

        def close(self) -> None:
            active_mutations_on_close.append(gate.active_mutations)
            self._stream.close()
            raise OSError(errno.ENOSPC, "injected cleanup close disk full")

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patch_open(
        path: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        stream = real_open(path, mode, *args, **kwargs)
        if path == target and mode == "x+b":
            return FailingCloseStream(stream)
        return stream

    class InjectedAbort(BaseException):
        pass

    abort = InjectedAbort("preserve-cancellation")
    monkeypatch.setattr(Path, "open", patch_open)
    observed_exception: BaseException | None = None
    try:
        with store.create(temporary_path) as stream:
            stream.write(b"preserve-before-close-full")
            raise abort
    except BaseException as error:
        observed_exception = error

    assert observed_exception is abort
    assert gate.closed
    assert gate.closed_reason == "FILESYSTEM_DISK_FULL"
    assert active_mutations_on_close == [1]
    assert target.read_bytes() == b"preserve-before-close-full"
    assert any("OSError" in note for note in getattr(abort, "__notes__", ()))


def test_temporary_page_store_write_error_cleanup_disk_full_closes_gate_and_traces_both(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = SharedStorageGate()
    store = TemporaryPageStore(
        tmp_path,
        data_root_identity=_capture_data_root_identity(tmp_path),
        storage_gate=gate,
    )
    temporary_path = "jobs/write-error-close-full.part"
    target = tmp_path / "temp" / temporary_path
    active_mutations_on_close: list[int] = []
    real_open: Any = Path.open
    original_error = PermissionError(errno.EACCES, "injected page write permission error")
    cleanup_error = OSError(errno.ENOSPC, "injected close disk full")

    class WriteAndCloseFailingStream:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        def write(self, value: bytes) -> int:
            self._stream.write(value[:9])
            raise original_error

        def close(self) -> None:
            active_mutations_on_close.append(gate.active_mutations)
            self._stream.close()
            raise cleanup_error

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patch_open(
        path: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        stream = real_open(path, mode, *args, **kwargs)
        if path == target and mode == "x+b":
            return WriteAndCloseFailingStream(stream)
        return stream

    caplog.set_level(logging.ERROR, logger="yomira.manifest_store")
    monkeypatch.setattr(Path, "open", patch_open)
    with pytest.raises(ManifestStoreError) as raised:
        with store.create(temporary_path) as stream:
            stream.write(b"preserve-original-write-prefix")

    assert raised.value.code is ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE
    assert raised.value.stage == "temporary_path_write"
    assert gate.closed
    assert gate.closed_reason == "FILESYSTEM_DISK_FULL"
    assert active_mutations_on_close == [1]
    assert target.read_bytes() == b"preserve-"
    failure_record = next(
        record
        for record in caplog.records
        if record.name == "yomira.manifest_store"
        and record.msg == "manifest_store.operation_failed"
        and getattr(record, "stage", None) == "temporary_path_write"
    )
    assert getattr(failure_record, "cause_type", None) == "PermissionError"
    assert getattr(failure_record, "cleanup_cause_type", None) == "OSError"


def test_manifest_fsync_disk_full_closes_gate_and_preserves_target_and_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    gate = SharedStorageGate()
    store = JobManifestStore(
        tmp_path,
        data_root_identity=_capture_data_root_identity(tmp_path),
        recovery_guard=_noop_recovery_guard,
        storage_gate=gate,
    )
    original_manifest = _manifest("original-before-full")
    _save_with_reservation(store, original_manifest)
    target = _manifest_path(tmp_path)
    original_bytes = target.read_bytes()

    def fail_fsync(_descriptor: int) -> None:
        raise OSError(errno.ENOSPC, "injected staging fsync disk full")

    monkeypatch.setattr(store_module.os, "fsync", fail_fsync)

    with pytest.raises(ManifestStoreError):
        _save_with_reservation(
            store,
            _manifest("new-staging-before-full"),
            expected_sha256=hashlib.sha256(original_bytes).hexdigest(),
        )

    recovery_directory = target.parent / ".recovery" / str(JOB_ID)
    staging_candidates = tuple(recovery_directory.glob(".stg-*"))
    assert gate.closed
    assert target.read_bytes() == original_bytes
    assert len(staging_candidates) == 1
    staged_manifest = manifest_from_bytes(staging_candidates[0].read_bytes())
    assert staged_manifest == _manifest("new-staging-before-full")


def test_manifest_staging_disk_full_preserves_prior_manifest_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _manifest_store(tmp_path)
    original = _manifest("opaque-page-v1")
    _save_with_reservation(store, original)
    target = _manifest_path(tmp_path)
    original_bytes = target.read_bytes()
    original_page_bytes = b"page-file-must-remain-untouched"
    page_path = tmp_path / "temp" / "jobs" / "page.part"
    page_path.parent.mkdir(parents=True)
    page_path.write_bytes(original_page_bytes)

    real_open = Path.open

    class PartialWriter:
        def __init__(self, stream: BinaryIO) -> None:
            self._stream = stream

        def __enter__(self) -> PartialWriter:
            self._stream.__enter__()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool | None:
            return self._stream.__exit__(exc_type, exc_value, traceback)

        def write(self, value: bytes) -> int:
            self._stream.write(value[: max(1, len(value) // 2)])
            raise OSError(errno.ENOSPC, "injected disk full")

        def __getattr__(self, name: str) -> object:
            return getattr(self._stream, name)

    def fail_staging_write(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> object:
        stream = real_open(path, mode, buffering, encoding, errors, newline)
        if (
            path.parent == target.parent / ".recovery" / str(JOB_ID)
            and path != target
            and ("w" in mode or "x" in mode)
            and "b" in mode
        ):
            return PartialWriter(cast(BinaryIO, stream))
        return stream

    monkeypatch.setattr(Path, "open", fail_staging_write)

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(
            store,
            _manifest("opaque-page-v2"),
            expected_sha256=hashlib.sha256(original_bytes).hexdigest(),
        )

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert str(tmp_path) not in str(raised.value)
    assert target.read_bytes() == original_bytes
    assert page_path.read_bytes() == original_page_bytes


def test_manifest_fsync_failure_preserves_prior_manifest_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    store = _manifest_store(tmp_path)
    original_bytes = manifest_to_bytes(_manifest("opaque-page-v1"))
    digest = _save_with_reservation(store, _manifest("opaque-page-v1"))
    target = _manifest_path(tmp_path)

    def fail_fsync(_descriptor: int) -> None:
        raise OSError(errno.ENOSPC, "injected fsync disk full")

    monkeypatch.setattr(store_module.os, "fsync", fail_fsync)

    with pytest.raises(ManifestStoreError) as raised:
        _save_with_reservation(store, _manifest("opaque-page-v2"), expected_sha256=digest)

    assert raised.value.code is ErrorCode.DL_MANIFEST_PERSIST_FAILED
    assert target.read_bytes() == original_bytes
