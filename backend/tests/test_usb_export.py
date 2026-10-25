from __future__ import annotations

import errno
import hashlib
import logging
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, BinaryIO, cast

import pytest

from app.domain.asset import Asset, AssetType
from app.services.usb_export import (
    ExportFailure,
    ExportTarget,
    UsbExportService,
)


@dataclass
class _TargetProvider:
    target: ExportTarget
    checks_before_disconnect: int | None = None
    checks: int = 0

    def list_targets(self) -> tuple[ExportTarget, ...]:
        self.checks += 1
        if (
            self.checks_before_disconnect is not None
            and self.checks > self.checks_before_disconnect
        ):
            return ()
        return (self.target,)


def _asset(path: Path, content: bytes) -> Asset:
    path.write_bytes(content)
    return Asset.new_local(
        AssetType.CBZ,
        path.name,
        hashlib.sha256(content).hexdigest(),
        len(content),
        verified_at=datetime.now(UTC),
    )


def _service(
    library_root: Path,
    target_root: Path,
    *,
    data_root: Path | None = None,
    free_bytes: int | None = None,
    checks_before_disconnect: int | None = None,
) -> tuple[UsbExportService, _TargetProvider]:
    target_root.mkdir(parents=True, exist_ok=True)
    target = ExportTarget(
        target_id="volume-test-1",
        mount_path=target_root,
        display_name="Fixture USB",
        file_system="exFAT",
        total_bytes=16 * 1024 * 1024,
        free_bytes=free_bytes if free_bytes is not None else 16 * 1024 * 1024,
    )
    provider = _TargetProvider(target, checks_before_disconnect)
    if data_root is None:
        service = UsbExportService(target_provider=provider, library_root=library_root)
    else:
        service = UsbExportService(
            target_provider=provider,
            library_root=library_root,
            data_root=data_root,
        )
    return service, provider


def test_export_copies_verified_cbz_and_keeps_library_source_unchanged(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    destination_root = tmp_path / "usb"
    library_root.mkdir()
    content = b"verified CBZ bytes" * 4096
    asset = _asset(library_root / "chapter-1.cbz", content)
    service, _provider = _service(library_root, destination_root)

    result = service.export_asset(asset, target_id="volume-test-1")

    assert result.status == "EXPORTED"
    assert result.sha256 == asset.sha256
    assert result.size_bytes == asset.size_bytes
    assert (destination_root / "chapter-1.cbz").read_bytes() == content
    assert (library_root / "chapter-1.cbz").read_bytes() == content
    assert not tuple(destination_root.glob("*.part"))


def test_fat32_rejects_a_single_file_over_4_gib_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir()
    destination = tmp_path / "usb"
    asset = replace(_asset(library_root / "manga.cbz", b"original"), size_bytes=1 << 32)
    service, provider = _service(library_root, destination)
    provider.target = replace(provider.target, file_system="FAT32")
    def inspect_large_file(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(st_size=asset.size_bytes)

    monkeypatch.setattr(service, "_inspect_plain_file", inspect_large_file)
    with pytest.raises(ExportFailure) as raised:
        service.export_asset(asset, target_id=provider.target.target_id)
    assert raised.value.stage == "destination_file_size_limit"
    assert list(destination.iterdir()) == []
    assert (library_root / "manga.cbz").read_bytes() == b"original"


def test_export_resolves_legacy_cbz_path_under_operational_data_root(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    library_root = tmp_path / "Mangas"
    destination_root = tmp_path / "usb"
    legacy_path = data_root / "library" / "chapters" / "series-1" / "chapter-1.cbz"
    content = b"legacy verified CBZ bytes"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(content)
    asset = Asset.new_local(
        AssetType.CBZ,
        "library/chapters/series-1/chapter-1.cbz",
        hashlib.sha256(content).hexdigest(),
        len(content),
        verified_at=datetime.now(UTC),
    )
    service, _provider = _service(
        library_root,
        destination_root,
        data_root=data_root,
    )

    result = service.export_asset(asset, target_id="volume-test-1")

    assert result.status == "EXPORTED"
    assert (destination_root / "chapter-1.cbz").read_bytes() == content
    assert legacy_path.read_bytes() == content


def test_export_skips_identical_existing_file_and_never_replaces_different_file(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    destination_root = tmp_path / "usb"
    library_root.mkdir()
    content = b"verified CBZ bytes"
    asset = _asset(library_root / "chapter-1.cbz", content)
    service, _provider = _service(library_root, destination_root)
    (destination_root / "chapter-1.cbz").write_bytes(content)

    skipped = service.export_asset(asset, target_id="volume-test-1")

    assert skipped.status == "SKIPPED"
    assert (destination_root / "chapter-1.cbz").read_bytes() == content

    conflict = b"existing user content"
    (destination_root / "chapter-1.cbz").write_bytes(conflict)
    with pytest.raises(ExportFailure) as captured:
        service.export_asset(asset, target_id="volume-test-1")

    assert captured.value.code == "EXP_DESTINATION_CONFLICT"
    assert captured.value.trace_id
    assert (destination_root / "chapter-1.cbz").read_bytes() == conflict


def test_skip_still_rejects_a_source_that_no_longer_matches_its_asset(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    destination_root = tmp_path / "usb"
    library_root.mkdir()
    content = b"original bytes"
    asset = _asset(library_root / "chapter-1.cbz", content)
    service, _provider = _service(library_root, destination_root)
    (destination_root / "chapter-1.cbz").write_bytes(content)
    (library_root / "chapter-1.cbz").write_bytes(b"tampered bytes")

    with pytest.raises(ExportFailure) as captured:
        service.export_asset(asset, target_id="volume-test-1")

    assert captured.value.code == "EXP_HASH_MISMATCH"
    assert captured.value.stage == "verify_source"
    assert (destination_root / "chapter-1.cbz").read_bytes() == content
    assert not tuple(destination_root.glob("*.part"))


def test_export_preflights_space_before_creating_staging_file(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    destination_root = tmp_path / "usb"
    library_root.mkdir()
    content = b"x" * 8192
    asset = _asset(library_root / "chapter-1.cbz", content)
    service, _provider = _service(library_root, destination_root, free_bytes=100)

    with pytest.raises(ExportFailure) as captured:
        service.export_asset(asset, target_id="volume-test-1")

    assert captured.value.code == "EXP_DISK_FULL"
    assert not (destination_root / "chapter-1.cbz").exists()
    assert not tuple(destination_root.iterdir())


def test_disconnect_preserves_partial_staging_and_source_with_traceable_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    library_root = tmp_path / "library"
    destination_root = tmp_path / "usb"
    library_root.mkdir()
    content = os.urandom(3 * 1024 * 1024)
    asset = _asset(library_root / "chapter-1.cbz", content)
    service, provider = _service(
        library_root,
        destination_root,
        checks_before_disconnect=4,
    )
    caplog.set_level(logging.ERROR, logger="yomira.usb_export")

    with pytest.raises(ExportFailure) as captured:
        service.export_asset(asset, target_id="volume-test-1")

    stages = tuple(destination_root.glob("*.part"))
    assert captured.value.code == "EXP_DEVICE_DISCONNECTED"
    assert captured.value.trace_id
    assert captured.value.stage == "copy"
    assert stages
    assert 0 < stages[0].stat().st_size < len(content)
    assert not (destination_root / "chapter-1.cbz").exists()
    assert (library_root / "chapter-1.cbz").read_bytes() == content
    assert provider.checks > 2
    event = next(record for record in caplog.records if record.name == "yomira.usb_export")
    assert cast(str, getattr(event, "error_code", None)) == "EXP_DEVICE_DISCONNECTED"
    assert cast(str, getattr(event, "stage", None)) == "copy"
    assert cast(str, getattr(event, "trace_id", None)) == captured.value.trace_id


def test_disk_full_during_copy_keeps_partial_staging_and_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_root = tmp_path / "library"
    destination_root = tmp_path / "usb"
    library_root.mkdir()
    content = os.urandom(3 * 1024 * 1024)
    asset = _asset(library_root / "chapter-1.cbz", content)
    service, _provider = _service(library_root, destination_root)
    original_open = Path.open

    class _FailingStageWriter:
        def __init__(self, stream: BinaryIO) -> None:
            self._stream = stream
            self._write_calls = 0

        def __enter__(self) -> _FailingStageWriter:
            self._stream.__enter__()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool | None:
            return self._stream.__exit__(exc_type, exc_value, traceback)

        def write(self, content_bytes: bytes) -> int:
            self._write_calls += 1
            if self._write_calls == 2:
                raise OSError(errno.ENOSPC, "simulated disk full")
            return self._stream.write(content_bytes)

        def flush(self) -> None:
            self._stream.flush()

        def fileno(self) -> int:
            return self._stream.fileno()

    def open_with_disk_full(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> BinaryIO:
        stream = cast(BinaryIO, original_open(path, *cast(Any, args), **cast(Any, kwargs)))
        if path.parent == destination_root and path.name.endswith(".part"):
            return cast(BinaryIO, _FailingStageWriter(stream))
        return stream

    monkeypatch.setattr(Path, "open", cast(Any, open_with_disk_full))

    with pytest.raises(ExportFailure) as captured:
        service.export_asset(asset, target_id="volume-test-1")

    stages = tuple(destination_root.glob("*.part"))
    assert captured.value.code == "EXP_DISK_FULL"
    assert captured.value.stage == "copy"
    assert stages
    assert 0 < stages[0].stat().st_size < len(content)
    assert not (destination_root / "chapter-1.cbz").exists()
    assert (library_root / "chapter-1.cbz").read_bytes() == content


def test_unverified_source_hash_is_never_published_and_stage_is_preserved(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    destination_root = tmp_path / "usb"
    library_root.mkdir()
    content = b"library source"
    asset = _asset(library_root / "chapter-1.cbz", content)
    (library_root / "chapter-1.cbz").write_bytes(b"other bytes!!!")
    service, _provider = _service(library_root, destination_root)

    with pytest.raises(ExportFailure) as captured:
        service.export_asset(asset, target_id="volume-test-1")

    assert captured.value.code == "EXP_HASH_MISMATCH"
    assert captured.value.stage == "verify_source"
    assert captured.value.trace_id
    assert tuple(destination_root.glob("*.part"))
    assert not (destination_root / "chapter-1.cbz").exists()


def test_windows_provider_filters_fixed_drives(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import usb_export

    def enumerate_logical_drives() -> tuple[str, ...]:
        return "C:\\", "E:\\"

    monkeypatch.setattr(usb_export, "_enumerate_logical_drives", enumerate_logical_drives)

    def drive_type(root: str) -> int:
        return usb_export.DRIVE_FIXED if root == "C:\\" else usb_export.DRIVE_REMOVABLE

    monkeypatch.setattr(usb_export, "_drive_type", drive_type)

    def volume_details(root: str) -> tuple[str, str, int]:
        return ("System" if root == "C:\\" else "Reader", "exFAT", 1234)

    monkeypatch.setattr(usb_export, "_read_volume_details", volume_details)

    class _Usage:
        total = 1000
        free = 500

    def disk_usage(_path: str | Path) -> _Usage:
        return _Usage()

    monkeypatch.setattr(usb_export, "disk_usage", cast(Any, disk_usage))

    class _EmptyPortableProvider:
        def list_targets(self) -> tuple[ExportTarget, ...]:
            return ()

    targets = usb_export.WindowsRemovableDriveProvider(
        portable_device_provider=_EmptyPortableProvider(),
    ).list_targets()

    assert len(targets) == 1
    assert targets[0].mount_path == Path("E:\\")
    assert targets[0].file_system == "exFAT"
