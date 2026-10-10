"""Advanced manifest reservation tests deferred to the V1.1 release gate."""

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from app.domain.download_page import DownloadPage
from app.domain.job_manifest import DownloadJobManifest, manifest_to_bytes
from app.persistence.manifest_store import JobManifestStore, ManifestStoreError
from app.persistence.windows_paths import DataRootIdentity

JOB_ID = UUID("00000000-0000-4000-8000-000000000001")
OTHER_JOB_ID = UUID("00000000-0000-4000-8000-000000000004")
SOURCE_ID = UUID("00000000-0000-4000-8000-000000000002")
CHAPTER_ID = UUID("00000000-0000-4000-8000-000000000003")

_ReplaceFile = Callable[[Path, Path, Path], None]


def _manifest(remote_reference: str, *, job_id: UUID = JOB_ID) -> DownloadJobManifest:
    page = DownloadPage.new_local(job_id, 1, remote_reference)
    return DownloadJobManifest(
        job_id=job_id,
        source_id=SOURCE_ID,
        chapter_id=CHAPTER_ID,
        expected_pages=1,
        pages=(page,),
    )


def _manifest_path(data_root: Path, job_id: UUID = JOB_ID) -> Path:
    return data_root / "database" / "manifests" / f"{job_id}.json"


def _manifest_store(data_root: Path) -> JobManifestStore:
    identity = DataRootIdentity.capture(data_root)
    return JobManifestStore(
        data_root,
        data_root_identity=identity,
        recovery_guard=lambda _job_id: None,
    )


def _manifest_write_reservation_arguments(
    current_manifest: DownloadJobManifest,
    new_manifest: DownloadJobManifest,
    *,
    expected_current_generation: int,
) -> dict[str, Any]:
    current_bytes = manifest_to_bytes(current_manifest)
    new_bytes = manifest_to_bytes(new_manifest)
    return {
        "job_id": new_manifest.job_id,
        "expected_current_generation": expected_current_generation,
        "current_sha256": hashlib.sha256(current_bytes).hexdigest(),
        "current_size_bytes": len(current_bytes),
        "new_sha256": hashlib.sha256(new_bytes).hexdigest(),
        "new_size_bytes": len(new_bytes),
    }


def _seed_manifest_target(data_root: Path, manifest: DownloadJobManifest) -> bytes:
    target = _manifest_path(data_root, manifest.job_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    value = manifest_to_bytes(manifest)
    target.write_bytes(value)
    return value


def _store_private_attribute(name: str) -> object:
    import app.persistence.manifest_store as store_module

    return getattr(store_module, name)


def test_manifest_write_reservation_is_bound_to_mutex_job_thread_and_generation(
    tmp_path: Path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import FrozenInstanceError

    from app.persistence.manifest_store import ManifestWriteReservation

    current = _manifest("reservation-current")
    proposed = _manifest("reservation-proposed")
    current_bytes = _seed_manifest_target(tmp_path, current)
    store = _manifest_store(tmp_path)
    arguments = _manifest_write_reservation_arguments(
        current,
        proposed,
        expected_current_generation=0,
    )
    namespace = _manifest_path(tmp_path).parent / ".recovery" / str(JOB_ID)

    # reserve_write is a preflight owned by the caller's job mutex, not a
    # helper that silently acquires a different lock or thread context.
    with pytest.raises(ManifestStoreError):
        store.reserve_write(**arguments)

    with store.lock_job(JOB_ID):
        reservation = store.reserve_write(**arguments)

        assert type(reservation) is ManifestWriteReservation
        assert reservation.job_id == JOB_ID
        assert reservation.expected_current_generation == 0
        assert reservation.new_generation == 1
        assert reservation.current_sha256 == hashlib.sha256(current_bytes).hexdigest()
        assert reservation.current_size_bytes == len(current_bytes)
        assert reservation.new_sha256 == hashlib.sha256(
            manifest_to_bytes(proposed)
        ).hexdigest()
        assert reservation.new_size_bytes == len(manifest_to_bytes(proposed))
        with pytest.raises((AttributeError, FrozenInstanceError)):
            reservation.new_generation = 99

        # A token cannot authorize a different manifest or a different job.
        with pytest.raises(ManifestStoreError):
            store.save(_manifest("different-proposal"), reservation=reservation)
        with store.lock_job(OTHER_JOB_ID):
            with pytest.raises(ManifestStoreError):
                store.save(
                    _manifest("different-job", job_id=OTHER_JOB_ID),
                    reservation=reservation,
                )

    # A reservation expires with the mutex acquisition that created it, even
    # when the same thread later calls save.
    with pytest.raises(ManifestStoreError):
        store.save(proposed, reservation=reservation)

    # A different thread must not be able to consume the original thread's
    # token by acquiring its own lock for the same job.
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(store.save, proposed, reservation=reservation)
        with pytest.raises(ManifestStoreError):
            future.result(timeout=10)

    assert _manifest_path(tmp_path).read_bytes() == current_bytes
    assert not _manifest_path(tmp_path, OTHER_JOB_ID).exists()
    if namespace.exists():
        assert not list(namespace.iterdir())


def test_manifest_save_without_reservation_fails_before_any_mutation(
    tmp_path: Path,
) -> None:
    store = _manifest_store(tmp_path)
    target = _manifest_path(tmp_path)

    with pytest.raises(ManifestStoreError):
        store.save(_manifest("reservation-required"))

    assert not target.exists()
    assert not (tmp_path / "database").exists()


def test_reserved_manifest_update_uses_generation_scoped_job_recovery_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.persistence.manifest_store as store_module

    current = _manifest("sidecar-current")
    proposed = _manifest("sidecar-proposed")
    _seed_manifest_target(tmp_path, current)
    store = _manifest_store(tmp_path)
    target = _manifest_path(tmp_path)
    recovery_directory = target.parent / ".recovery" / str(JOB_ID)
    replace_calls: list[tuple[Path, Path, Path]] = []
    original_replace = cast(
        _ReplaceFile,
        _store_private_attribute("_replace_file_win32"),
    )

    def record_replace(
        replaced_path: Path,
        replacement_path: Path,
        backup_path: Path,
    ) -> None:
        replace_calls.append((replaced_path, replacement_path, backup_path))
        original_replace(replaced_path, replacement_path, backup_path)

    monkeypatch.setattr(store_module, "_replace_file_win32", record_replace)

    with store.lock_job(JOB_ID):
        reservation = store.reserve_write(
            **_manifest_write_reservation_arguments(
                current,
                proposed,
                expected_current_generation=0,
            )
        )
        store.save(proposed, reservation=reservation)

    assert len(replace_calls) == 1
    replaced_path, staging_path, backup_path = replace_calls[0]
    generation_prefix = f"{1:020d}-"
    assert replaced_path == target
    assert staging_path.parent == recovery_directory
    assert backup_path.parent == recovery_directory
    assert staging_path.name.startswith(f".stg-{generation_prefix}")
    assert backup_path.name.startswith(f".bak-{generation_prefix}")
    staging_nonce = staging_path.name[len(f".stg-{generation_prefix}") :]
    backup_nonce = backup_path.name[len(f".bak-{generation_prefix}") :]
    assert str(UUID(staging_nonce)) == staging_nonce
    assert UUID(staging_nonce).version == 4
    assert str(UUID(backup_nonce)) == backup_nonce
    assert UUID(backup_nonce).version == 4


def test_reserved_manifest_update_retains_verified_previous_generation_backup(
    tmp_path: Path,
) -> None:
    current = _manifest("retained-backup-current")
    proposed = _manifest("retained-backup-proposed")
    current_bytes = _seed_manifest_target(tmp_path, current)
    store = _manifest_store(tmp_path)
    target = _manifest_path(tmp_path)
    recovery_directory = target.parent / ".recovery" / str(JOB_ID)

    with store.lock_job(JOB_ID):
        reservation = store.reserve_write(
            **_manifest_write_reservation_arguments(
                current,
                proposed,
                expected_current_generation=0,
            )
        )
        store.save(proposed, reservation=reservation)

    backups = sorted(recovery_directory.glob(f".bak-{1:020d}-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == current_bytes
    assert hashlib.sha256(backups[0].read_bytes()).hexdigest() == hashlib.sha256(
        current_bytes
    ).hexdigest()
    assert target.read_bytes() == manifest_to_bytes(proposed)
