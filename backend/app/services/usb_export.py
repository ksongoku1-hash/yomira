"""Verified, no-replace export of library CBZ/CBR assets to removable Windows drives."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import logging
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from shutil import disk_usage
from typing import Literal, NoReturn, Protocol, cast
from uuid import UUID, uuid4

from app.domain.asset import Asset, AssetType, validate_relative_path
from app.errors import ErrorCode
from app.persistence.atomic_file_publisher import (
    ADR_0087_RETRY_POLICY,
    AtomicFilePublisher,
    AtomicFilePublishError,
)
from app.persistence.windows_paths import (
    DataRootIdentity,
    DataRootIdentityError,
    windows_path_key,
)
from app.tracing import create_trace_id, current_trace_id

logger = logging.getLogger("yomira.usb_export")

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
MAX_EXPORT_BYTES = 8 * 1024 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_DISK_FULL_WINERRORS = frozenset({39, 112})
_DEVICE_UNAVAILABLE_WINERRORS = frozenset({2, 3, 15, 21, 53, 55, 64, 67, 1117})


@dataclass(frozen=True, slots=True)
class ExportTarget:
    """A snapshot of a removable destination returned by a trusted provider."""

    target_id: str
    mount_path: Path | None
    display_name: str
    file_system: str
    total_bytes: int
    free_bytes: int
    transport: Literal["volume", "wpd"] = "volume"
    portable_device_id: str | None = field(default=None, repr=False)
    storage_object_id: str | None = field(default=None, repr=False)
    storage_persistent_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.target_id) is not str or not self.target_id or len(self.target_id) > 128:
            raise ValueError("target_id must contain between 1 and 128 characters")
        if self.transport == "volume":
            if self.mount_path is None or not self.mount_path.is_absolute():
                raise ValueError("volume targets require an absolute mount_path")
            if any(
                value is not None
                for value in (
                    self.portable_device_id,
                    self.storage_object_id,
                    self.storage_persistent_id,
                )
            ):
                raise ValueError("volume targets cannot contain portable-device identifiers")
        elif self.transport == "wpd":
            if self.mount_path is not None:
                raise ValueError("portable-device targets cannot have a mount_path")
            if not all(
                (
                    self.portable_device_id,
                    self.storage_object_id,
                    self.storage_persistent_id,
                )
            ):
                raise ValueError(
                    "portable-device targets require private device and storage identity"
                )
        else:
            raise ValueError("transport must be volume or wpd")
        for name, value, maximum in (
            ("display_name", self.display_name, 128),
            ("file_system", self.file_system, 32),
        ):
            if type(value) is not str or not value or len(value) > maximum:
                raise ValueError(f"{name} must contain between 1 and {maximum} characters")
        for name, value in (("total_bytes", self.total_bytes), ("free_bytes", self.free_bytes)):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.free_bytes > self.total_bytes:
            raise ValueError("free_bytes must not exceed total_bytes")


class RemovableTargetProvider(Protocol):
    def list_targets(self) -> tuple[ExportTarget, ...]:
        """Return the currently mounted removable targets and fresh free space."""
        ...


class PortableDeviceExporter(Protocol):
    def export_asset(
        self,
        asset: Asset,
        *,
        source_path: Path,
        target: ExportTarget,
        export_id: UUID,
        trace_id: str,
    ) -> Literal["EXPORTED", "SKIPPED"]: ...


ExportStatus = Literal["EXPORTED", "SKIPPED"]


@dataclass(frozen=True, slots=True)
class ExportResult:
    export_id: UUID
    asset_id: UUID
    target_id: str
    destination_name: str
    status: ExportStatus
    sha256: str
    size_bytes: int
    trace_id: str


class ExportFailure(RuntimeError):
    """Sanitized, traceable failure that leaves the source and staging evidence intact."""

    def __init__(
        self,
        code: ErrorCode,
        stage: str,
        *,
        trace_id: str,
        retryable: bool,
        staging_name: str | None = None,
        cause_type: str | None = None,
    ) -> None:
        self.code = code
        self.stage = stage
        self.trace_id = trace_id
        self.retryable = retryable
        self.staging_name = staging_name
        self.cause_type = cause_type
        super().__init__(f"USB export failed during {stage}.")


class WindowsRemovableDriveProvider:
    """Enumerate removable volumes and portable-device storage; fixed disks stay excluded."""

    def __init__(self, portable_device_provider: RemovableTargetProvider | None = None) -> None:
        if portable_device_provider is None:
            from app.services.wpd_export import WindowsWpdBackend

            portable_device_provider = WindowsWpdBackend()
        if not callable(getattr(portable_device_provider, "list_targets", None)):
            raise TypeError("portable_device_provider must implement list_targets")
        self._portable_device_provider = portable_device_provider

    def list_targets(self) -> tuple[ExportTarget, ...]:
        targets: list[ExportTarget] = []
        for root in _enumerate_logical_drives():
            if _drive_type(root) != DRIVE_REMOVABLE:
                continue
            try:
                label, file_system, volume_serial = _read_volume_details(root)
                usage = disk_usage(root)
                targets.append(
                    ExportTarget(
                        target_id=f"{root[:2].upper()}:{volume_serial:08X}",
                        mount_path=Path(root),
                        display_name=label or root[:2].upper(),
                        file_system=file_system or "unknown",
                        total_bytes=usage.total,
                        free_bytes=usage.free,
                    )
                )
            except OSError as error:
                _log_provider_failure(error)
        try:
            targets.extend(self._portable_device_provider.list_targets())
        except Exception as error:
            logger.warning(
                "usb_export.portable_target_enumeration_failed",
                extra={
                    "error_code": ErrorCode.EXP_DEVICE_DISCONNECTED.value,
                    "retryable": True,
                    "stage": "target_enumeration",
                    "cause_type": type(error).__name__,
                    "trace_id": current_trace_id() or create_trace_id(),
                },
            )
        return tuple(targets)


class UsbExportService:
    """Copy one verified library Asset to an explicitly selected removable target."""

    def __init__(
        self,
        *,
        target_provider: RemovableTargetProvider,
        library_root: Path,
        data_root: Path | None = None,
        portable_exporter: PortableDeviceExporter | None = None,
        copy_chunk_bytes: int = COPY_CHUNK_BYTES,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(getattr(target_provider, "list_targets", None)):
            raise TypeError("target_provider must implement list_targets")
        if not library_root.is_absolute():
            raise ValueError("library_root must be an absolute path")
        if data_root is not None and not data_root.is_absolute():
            raise ValueError("data_root must be an absolute path")
        if type(copy_chunk_bytes) is not int or not 1 <= copy_chunk_bytes <= COPY_CHUNK_BYTES:
            raise ValueError("copy_chunk_bytes must be between 1 and 1048576")
        if not callable(sleep) or not callable(clock):
            raise TypeError("sleep and clock must be callable")
        self._target_provider = target_provider
        self._library_root = library_root
        self._data_root = data_root or library_root
        if portable_exporter is None:
            from app.services.wpd_export import WindowsWpdBackend, WpdExportService

            portable_exporter = WpdExportService(WindowsWpdBackend())
        if not callable(getattr(portable_exporter, "export_asset", None)):
            raise TypeError("portable_exporter must implement export_asset")
        self._portable_exporter = portable_exporter
        self._copy_chunk_bytes = copy_chunk_bytes
        self._sleep = sleep
        self._clock = clock

        """Expose only removable destinations; errors are logged without paths."""
    def progress_for_trace(self, trace_id: str) -> dict[str, object] | None:
        callback = getattr(self._portable_exporter, "progress_for_trace", None)
        return cast(dict[str, object] | None, callback(trace_id)) if callable(callback) else None

    def list_targets(self) -> tuple[ExportTarget, ...]:
        """Expose only removable destinations; errors are logged without paths."""
        try:
            return tuple(self._target_provider.list_targets())
        except Exception as error:
            trace_id = current_trace_id() or create_trace_id()
            logger.error(
                "usb_export.target_enumeration_failed",
                extra={
                    "error_code": ErrorCode.EXP_DEVICE_DISCONNECTED.value,
                    "retryable": True,
                    "stage": "target_enumeration",
                    "cause_type": type(error).__name__,
                    "trace_id": trace_id,
                },
            )
            raise ExportFailure(
                ErrorCode.EXP_DEVICE_DISCONNECTED,
                "target_enumeration",
                trace_id=trace_id,
                retryable=True,
                cause_type=type(error).__name__,
            ) from None

    def export_asset(self, asset: Asset, *, target_id: str) -> ExportResult:
        trace_id = current_trace_id() or create_trace_id()
        export_id = uuid4()
        staging_name: str | None = None
        stage = "preflight"

        try:
            source_path = self._verified_source_path(asset, trace_id=trace_id)
            target = self._resolve_target(target_id, trace_id=trace_id, stage="target_lookup")
            source_info = self._inspect_plain_file(
                source_path,
                code=ErrorCode.LIB_PATH_UNAVAILABLE,
                stage="source_preflight",
                trace_id=trace_id,
            )
            if source_info.st_size != asset.size_bytes:
                self._raise(
                    ErrorCode.EXP_HASH_MISMATCH,
                    "verify_source",
                    trace_id=trace_id,
                    retryable=False,
                )
            if asset.size_bytes > MAX_EXPORT_BYTES:
                self._raise(
                    ErrorCode.EXP_SOURCE_INVALID,
                    "source_size_limit",
                    trace_id=trace_id,
                    retryable=False,
                )

            if target.transport == "wpd":
                status = self._portable_exporter.export_asset(
                    asset,
                    source_path=source_path,
                    target=target,
                    export_id=export_id,
                    trace_id=trace_id,
                )
                self._log_success(
                    status=status,
                    asset=asset,
                    target=target,
                    trace_id=trace_id,
                )
                return ExportResult(
                    export_id=export_id,
                    asset_id=asset.asset_id,
                    target_id=target.target_id,
                    destination_name=Path(asset.relative_path).name,
                    status=status,
                    sha256=asset.sha256,
                    size_bytes=asset.size_bytes,
                    trace_id=trace_id,
                )

            if target.file_system.upper() == "FAT32" and asset.size_bytes > 0xFFFFFFFF:
                self._raise(
                    ErrorCode.EXP_SOURCE_INVALID,
                    "destination_file_size_limit",
                    trace_id=trace_id,
                    retryable=False,
                )

            identity = self._capture_target_identity(target, trace_id=trace_id)
            self._assert_target_current(
                target,
                identity,
                trace_id=trace_id,
                stage="target_preflight",
            )

            assert target.mount_path is not None
            destination = target.mount_path / Path(asset.relative_path).name
            existing = self._existing_destination_digest(
                destination,
                expected_size=asset.size_bytes,
                trace_id=trace_id,
            )
            if existing is not None:
                existing_size, existing_hash = existing
                if existing_size == asset.size_bytes and existing_hash == asset.sha256:
                    source_size, source_hash = self._hash_plain_file(
                        source_path,
                        code=ErrorCode.LIB_PATH_UNAVAILABLE,
                        stage="verify_source",
                        trace_id=trace_id,
                    )
                    if source_size != asset.size_bytes or source_hash != asset.sha256:
                        self._raise(
                            ErrorCode.EXP_HASH_MISMATCH,
                            "verify_source",
                            trace_id=trace_id,
                            retryable=False,
                        )
                    self._assert_target_current(
                        target,
                        identity,
                        trace_id=trace_id,
                        stage="existing_verify",
                    )
                    self._log_success(
                        status="SKIPPED",
                        asset=asset,
                        target=target,
                        trace_id=trace_id,
                    )
                    return ExportResult(
                        export_id=export_id,
                        asset_id=asset.asset_id,
                        target_id=target.target_id,
                        destination_name=destination.name,
                        status="SKIPPED",
                        sha256=asset.sha256,
                        size_bytes=asset.size_bytes,
                        trace_id=trace_id,
                    )
                self._raise(
                    ErrorCode.EXP_DESTINATION_CONFLICT,
                    "destination_conflict",
                    trace_id=trace_id,
                    retryable=False,
                )

            current_target = self._assert_target_current(
                target,
                identity,
                trace_id=trace_id,
                stage="space_preflight",
            )
            if current_target.free_bytes < asset.size_bytes:
                self._raise(
                    ErrorCode.EXP_DISK_FULL,
                    "space_preflight",
                    trace_id=trace_id,
                    retryable=True,
                )

            staging = target.mount_path / f"{destination.name}.{export_id.hex}.part"
            staging_name = staging.name
            stage = "copy"
            self._copy_to_staging(
                source_path,
                staging,
                asset=asset,
                target=target,
                identity=identity,
                trace_id=trace_id,
                source_info=source_info,
            )

            stage = "verify_staging"
            self._assert_target_current(
                target,
                identity,
                trace_id=trace_id,
                stage=stage,
                staging_name=staging_name,
            )
            staged_size, staged_hash = self._hash_plain_file(
                staging,
                code=ErrorCode.EXP_DEVICE_DISCONNECTED,
                stage=stage,
                trace_id=trace_id,
                staging_name=staging_name,
            )
            if staged_size != asset.size_bytes or staged_hash != asset.sha256:
                self._raise(
                    ErrorCode.EXP_HASH_MISMATCH,
                    stage,
                    trace_id=trace_id,
                    retryable=False,
                    staging_name=staging_name,
                )

            stage = "publish"
            self._assert_target_current(
                target,
                identity,
                trace_id=trace_id,
                stage=stage,
                staging_name=staging_name,
            )
            publisher = AtomicFilePublisher(
                data_root_identity=identity,
                retry_policy=ADR_0087_RETRY_POLICY,
                sleep=self._sleep,
                clock=self._clock,
            )
            try:
                publisher.publish_no_replace(staging, destination)
            except AtomicFilePublishError as error:
                code = (
                    ErrorCode.EXP_DESTINATION_CONFLICT
                    if error.stage == "destination_conflict"
                    else ErrorCode.EXP_DEVICE_DISCONNECTED
                    if error.stage == "root_identity_invalid"
                    else ErrorCode.EXP_DISK_FULL
                    if error.winerror in _DISK_FULL_WINERRORS
                    else ErrorCode.EXP_PUBLISH_FAILED
                )
                self._raise(
                    code,
                    f"publish_{error.stage}",
                    trace_id=trace_id,
                    retryable=code is ErrorCode.EXP_DEVICE_DISCONNECTED,
                    staging_name=staging_name,
                    cause_type=error.cause_type,
                )

            self._log_success(
                status="EXPORTED",
                asset=asset,
                target=target,
                trace_id=trace_id,
            )
            return ExportResult(
                export_id=export_id,
                asset_id=asset.asset_id,
                target_id=target.target_id,
                destination_name=destination.name,
                status="EXPORTED",
                sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                trace_id=trace_id,
            )
        except ExportFailure as error:
            logger.error(
                f"usb_export.failed asset={asset.asset_id} target={target_id[:128]}",
                extra={
                    "error_code": error.code.value,
                    "retryable": error.retryable,
                    "stage": error.stage,
                    "attempt": 1,
                    "cause_type": error.cause_type,
                    "trace_id": error.trace_id,
                },
            )
            raise
        except OSError as error:
            code, retryable = _classify_io_error(error)
            self._raise(
                code,
                stage,
                trace_id=trace_id,
                retryable=retryable,
                staging_name=staging_name,
                cause_type=type(error).__name__,
            )
        except Exception as error:
            self._raise(
                ErrorCode.EXP_COPY_FAILED,
                stage,
                trace_id=trace_id,
                retryable=False,
                staging_name=staging_name,
                cause_type=type(error).__name__,
            )

    def _verified_source_path(self, asset: Asset, *, trace_id: str) -> Path:
        if (
            asset.asset_type not in {AssetType.CBZ, AssetType.CBR}
            or asset.verified_at is None
            or asset.size_bytes <= 0
        ):
            self._raise(
                ErrorCode.EXP_SOURCE_INVALID,
                "source_asset",
                trace_id=trace_id,
                retryable=False,
            )
        try:
            validate_relative_path(asset.relative_path)
            root = (
                self._data_root
                if asset.relative_path.startswith("library/chapters/")
                else self._library_root
            )
            root_info = root.lstat()
            if self._is_reparse_point(root_info) or not stat.S_ISDIR(root_info.st_mode):
                raise OSError("Library root is not a plain directory.")
            resolved_root = root.resolve(strict=True)
            candidate = resolved_root.joinpath(*asset.relative_path.split("/"))
            relative = candidate.relative_to(resolved_root)
            current = resolved_root
            for index, component in enumerate(relative.parts):
                current = current / component
                info = current.lstat()
                final_component = index + 1 == len(relative.parts)
                if self._is_reparse_point(info):
                    raise OSError("Asset path contains a reparse point.")
                if final_component:
                    if not stat.S_ISREG(info.st_mode):
                        raise OSError("Asset path is not a regular file.")
                elif not stat.S_ISDIR(info.st_mode):
                    raise OSError("Asset path contains a non-directory component.")
            return candidate
        except (OSError, ValueError) as error:
            self._raise(
                ErrorCode.LIB_PATH_UNAVAILABLE,
                "source_path",
                trace_id=trace_id,
                retryable=False,
                cause_type=type(error).__name__,
            )

    def _resolve_target(
        self,
        target_id: str,
        *,
        trace_id: str,
        stage: str,
    ) -> ExportTarget:
        if not target_id:
            self._raise(
                ErrorCode.EXP_DEVICE_DISCONNECTED,
                stage,
                trace_id=trace_id,
                retryable=True,
            )
        try:
            target = next(
                (
                    candidate
                    for candidate in self._target_provider.list_targets()
                    if candidate.target_id == target_id
                ),
                None,
            )
        except Exception as error:
            self._raise(
                ErrorCode.EXP_DEVICE_DISCONNECTED,
                stage,
                trace_id=trace_id,
                retryable=True,
                cause_type=type(error).__name__,
            )
        if target is None:
            self._raise(
                ErrorCode.EXP_DEVICE_DISCONNECTED,
                stage,
                trace_id=trace_id,
                retryable=True,
            )
        return target

    def _capture_target_identity(
        self,
        target: ExportTarget,
        *,
        trace_id: str,
    ) -> DataRootIdentity:
        if target.mount_path is None:
            self._raise(
                ErrorCode.EXP_DEVICE_DISCONNECTED,
                "target_identity",
                trace_id=trace_id,
                retryable=True,
            )
        try:
            return DataRootIdentity.capture(target.mount_path)
        except (DataRootIdentityError, OSError, RuntimeError) as error:
            self._raise(
                ErrorCode.EXP_DEVICE_DISCONNECTED,
                "target_identity",
                trace_id=trace_id,
                retryable=True,
                cause_type=type(error).__name__,
            )

    def _assert_target_current(
        self,
        target: ExportTarget,
        identity: DataRootIdentity,
        *,
        trace_id: str,
        stage: str,
        staging_name: str | None = None,
    ) -> ExportTarget:
        try:
            if target.mount_path is None:
                raise DataRootIdentityError("The selected destination is not a mounted volume.")
            identity.assert_current()
            current = self._resolve_target(target.target_id, trace_id=trace_id, stage=stage)
            if current.mount_path is None:
                raise DataRootIdentityError("The destination transport changed.")
            if windows_path_key(current.mount_path) != windows_path_key(target.mount_path):
                raise DataRootIdentityError("The removable target mount path changed.")
            identity.assert_current()
            return current
        except ExportFailure as error:
            self._raise(
                ErrorCode.EXP_DEVICE_DISCONNECTED,
                stage,
                trace_id=trace_id,
                retryable=True,
                staging_name=staging_name,
                cause_type=error.cause_type,
            )
        except (DataRootIdentityError, OSError, RuntimeError) as error:
            self._raise(
                ErrorCode.EXP_DEVICE_DISCONNECTED,
                stage,
                trace_id=trace_id,
                retryable=True,
                staging_name=staging_name,
                cause_type=type(error).__name__,
            )

    def _copy_to_staging(
        self,
        source: Path,
        staging: Path,
        *,
        asset: Asset,
        target: ExportTarget,
        identity: DataRootIdentity,
        trace_id: str,
        source_info: os.stat_result,
    ) -> None:
        stage = "copy"
        staging_name = staging.name
        digest = hashlib.sha256()
        copied_bytes = 0
        final_source_info: os.stat_result | None = None
        try:
            source_stream = source.open("rb")
        except OSError as error:
            self._raise(
                ErrorCode.LIB_PATH_UNAVAILABLE,
                "source_open",
                trace_id=trace_id,
                retryable=False,
                staging_name=staging_name,
                cause_type=type(error).__name__,
            )
        with source_stream:
            try:
                stage_stream = staging.open("xb")
            except OSError as error:
                code, retryable = _classify_io_error(error)
                self._raise(
                    code,
                    stage,
                    trace_id=trace_id,
                    retryable=retryable,
                    staging_name=staging_name,
                    cause_type=type(error).__name__,
                )
            try:
                with stage_stream:
                    opened_source_info = os.fstat(source_stream.fileno())
                    if not self._same_source_version(source_info, opened_source_info):
                        self._raise(
                            ErrorCode.EXP_HASH_MISMATCH,
                            "verify_source",
                            trace_id=trace_id,
                            retryable=False,
                            staging_name=staging_name,
                        )
                    while True:
                        self._assert_target_current(
                            target,
                            identity,
                            trace_id=trace_id,
                            stage=stage,
                            staging_name=staging_name,
                        )
                        try:
                            chunk = source_stream.read(self._copy_chunk_bytes)
                        except OSError as error:
                            self._raise(
                                ErrorCode.LIB_PATH_UNAVAILABLE,
                                "source_read",
                                trace_id=trace_id,
                                retryable=False,
                                staging_name=staging_name,
                                cause_type=type(error).__name__,
                            )
                        if not chunk:
                            break
                        written = stage_stream.write(chunk)
                        if written != len(chunk):
                            raise OSError("Staging write was incomplete.")
                        digest.update(chunk)
                        copied_bytes += written
                        self._assert_target_current(
                            target,
                            identity,
                            trace_id=trace_id,
                            stage=stage,
                            staging_name=staging_name,
                        )
                    stage_stream.flush()
                    os.fsync(stage_stream.fileno())
                    final_source_info = os.fstat(source_stream.fileno())
            except ExportFailure:
                raise
            except OSError as error:
                code, retryable = _classify_io_error(error)
                self._raise(
                    code,
                    stage,
                    trace_id=trace_id,
                    retryable=retryable,
                    staging_name=staging_name,
                    cause_type=type(error).__name__,
                )

        if (
            copied_bytes != asset.size_bytes
            or digest.hexdigest() != asset.sha256
            or not self._same_source_version(source_info, final_source_info)
        ):
            self._raise(
                ErrorCode.EXP_HASH_MISMATCH,
                "verify_source",
                trace_id=trace_id,
                retryable=False,
                staging_name=staging_name,
            )

    def _existing_destination_digest(
        self,
        destination: Path,
        *,
        expected_size: int,
        trace_id: str,
    ) -> tuple[int, str] | None:
        try:
            info = destination.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            code, retryable = _classify_io_error(error)
            self._raise(
                code,
                "destination_preflight",
                trace_id=trace_id,
                retryable=retryable,
                cause_type=type(error).__name__,
            )
        if self._is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
            self._raise(
                ErrorCode.EXP_DESTINATION_CONFLICT,
                "destination_conflict",
                trace_id=trace_id,
                retryable=False,
            )
        if info.st_size != expected_size or info.st_size > MAX_EXPORT_BYTES:
            return info.st_size, ""
        return self._hash_plain_file(
            destination,
            code=ErrorCode.EXP_COPY_FAILED,
            stage="destination_verify",
            trace_id=trace_id,
        )

    def _hash_plain_file(
        self,
        path: Path,
        *,
        code: ErrorCode,
        stage: str,
        trace_id: str,
        staging_name: str | None = None,
    ) -> tuple[int, str]:
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            info = self._inspect_plain_file(
                path,
                code=code,
                stage=stage,
                trace_id=trace_id,
                staging_name=staging_name,
            )
            with path.open("rb") as stream:
                opened_info = os.fstat(stream.fileno())
                if not self._same_source_version(info, opened_info):
                    self._raise(
                        ErrorCode.EXP_HASH_MISMATCH,
                        stage,
                        trace_id=trace_id,
                        retryable=False,
                        staging_name=staging_name,
                    )
                while chunk := stream.read(COPY_CHUNK_BYTES):
                    size_bytes += len(chunk)
                    digest.update(chunk)
                closed_info = os.fstat(stream.fileno())
                if not self._same_source_version(opened_info, closed_info):
                    self._raise(
                        ErrorCode.EXP_HASH_MISMATCH,
                        stage,
                        trace_id=trace_id,
                        retryable=False,
                        staging_name=staging_name,
                    )
        except ExportFailure:
            raise
        except OSError as error:
            mapped_code, retryable = _classify_io_error(error)
            self._raise(
                mapped_code if mapped_code is not ErrorCode.EXP_COPY_FAILED else code,
                stage,
                trace_id=trace_id,
                retryable=retryable,
                staging_name=staging_name,
                cause_type=type(error).__name__,
            )
        return size_bytes, digest.hexdigest()

    def _inspect_plain_file(
        self,
        path: Path,
        *,
        code: ErrorCode,
        stage: str,
        trace_id: str,
        staging_name: str | None = None,
    ) -> os.stat_result:
        try:
            info = path.lstat()
        except OSError as error:
            self._raise(
                code,
                stage,
                trace_id=trace_id,
                retryable=code is ErrorCode.EXP_DEVICE_DISCONNECTED,
                staging_name=staging_name,
                cause_type=type(error).__name__,
            )
        if self._is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
            self._raise(
                code,
                stage,
                trace_id=trace_id,
                retryable=False,
                staging_name=staging_name,
            )
        return info

    @staticmethod
    def _same_source_version(first: os.stat_result, second: os.stat_result) -> bool:
        return (
            first.st_dev == second.st_dev
            and first.st_ino == second.st_ino
            and first.st_size == second.st_size
            and first.st_mtime_ns == second.st_mtime_ns
        )

    @staticmethod
    def _is_reparse_point(info: os.stat_result) -> bool:
        attributes = getattr(info, "st_file_attributes", 0)
        return stat.S_ISLNK(info.st_mode) or bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)

    @staticmethod
    def _log_success(
        *,
        status: ExportStatus,
        asset: Asset,
        target: ExportTarget,
        trace_id: str,
    ) -> None:
        logger.info(
            f"usb_export.{status.lower()} asset={asset.asset_id} target={target.target_id}",
            extra={
                "stage": "complete",
                "trace_id": trace_id,
            },
        )

    @staticmethod
    def _raise(
        code: ErrorCode,
        stage: str,
        *,
        trace_id: str,
        retryable: bool,
        staging_name: str | None = None,
        cause_type: str | None = None,
    ) -> NoReturn:
        raise ExportFailure(
            code,
            stage,
            trace_id=trace_id,
            retryable=retryable,
            staging_name=staging_name,
            cause_type=cause_type,
        )


def _enumerate_logical_drives() -> tuple[str, ...]:
    if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
        return ()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    get_logical_drives = kernel32.GetLogicalDrives  # type: ignore[attr-defined]
    get_logical_drives.argtypes = ()
    get_logical_drives.restype = ctypes.c_uint32
    drive_mask = int(get_logical_drives())
    if drive_mask == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    return tuple(f"{chr(ord('A') + index)}:\\" for index in range(26) if drive_mask & (1 << index))


def _drive_type(root: str) -> int:
    if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
        return 0
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    get_drive_type = kernel32.GetDriveTypeW  # type: ignore[attr-defined]
    get_drive_type.argtypes = (ctypes.c_wchar_p,)
    get_drive_type.restype = ctypes.c_uint
    return int(get_drive_type(root))


def _read_volume_details(root: str) -> tuple[str, str, int]:
    if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
        raise OSError("Windows volume information is unavailable.")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    get_volume_information = kernel32.GetVolumeInformationW  # type: ignore[attr-defined]
    get_volume_information.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    )
    get_volume_information.restype = ctypes.c_int
    volume_label = ctypes.create_unicode_buffer(261)
    file_system = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint32()
    maximum_component_length = ctypes.c_uint32()
    flags = ctypes.c_uint32()
    if not get_volume_information(
        root,
        volume_label,
        len(volume_label),
        ctypes.byref(serial),
        ctypes.byref(maximum_component_length),
        ctypes.byref(flags),
        file_system,
        len(file_system),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return volume_label.value, file_system.value, int(serial.value)


def _classify_io_error(error: OSError) -> tuple[ErrorCode, bool]:
    winerror = getattr(error, "winerror", None)
    if winerror in _DISK_FULL_WINERRORS or error.errno == errno.ENOSPC:
        return ErrorCode.EXP_DISK_FULL, True
    if (
        isinstance(error, (FileNotFoundError, NotADirectoryError))
        or winerror in _DEVICE_UNAVAILABLE_WINERRORS
        or error.errno in {errno.ENODEV, errno.ENXIO}
    ):
        return ErrorCode.EXP_DEVICE_DISCONNECTED, True
    return ErrorCode.EXP_COPY_FAILED, False


def _log_provider_failure(error: OSError) -> None:
    trace_id = current_trace_id() or create_trace_id()
    code, retryable = _classify_io_error(error)
    logger.warning(
        "usb_export.target_unavailable",
        extra={
            "error_code": code.value,
            "retryable": retryable,
            "stage": "target_enumeration",
            "cause_type": type(error).__name__,
            "trace_id": trace_id,
        },
    )
