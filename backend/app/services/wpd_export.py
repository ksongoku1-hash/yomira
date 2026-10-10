"""Bounded, read-back-verified export for Windows Portable Devices (MTP/WPD)."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import logging
import os
import stat
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, NoReturn, Protocol, cast
from uuid import UUID

from app.domain.asset import Asset
from app.errors import ErrorCode
from app.services.usb_export import (
    COPY_CHUNK_BYTES,
    MAX_EXPORT_BYTES,
    ExportFailure,
    ExportTarget,
)
from app.tracing import create_trace_id

logger = logging.getLogger("yomira.usb_export")

MAX_WPD_DEVICES = 64
MAX_WPD_STORAGES_PER_DEVICE = 16
MAX_WPD_OBJECTS_PER_STORAGE = 20_000
STGM_READ = 0

_OBJECT_PROPERTIES_GUID = "{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}"
_FUNCTIONAL_OBJECT_PROPERTIES_GUID = "{8F052D93-ABCA-4FC5-A5AC-B01DF4DBE598}"
_STORAGE_PROPERTIES_GUID = "{01A3057A-74D6-4E80-BEA7-DC4C212CE50A}"
_DEVICE_PROPERTIES_GUID = "{26D4979A-E643-4626-9E2B-736DC0C92FDC}"
_RESOURCE_DEFAULT_GUID = "{E81E79BE-34F0-41BF-B53F-F1A06AE87842}"
_STORAGE_FUNCTIONAL_CATEGORY = "{23F05BBC-15DE-4C2A-A55B-A9AF5CE412EF}"
_GENERIC_FILE_CONTENT_TYPE = "{0085E0A6-8D34-45D7-BC5C-447E59C73D48}"
_FOLDER_CONTENT_TYPE = "{27E2E392-A111-48E0-AB0C-E17705A05F85}"
_UNSPECIFIED_OBJECT_FORMAT = "{30000000-AE6C-4804-98BA-C57B46965FE7}"
_IPORTABLE_DEVICE_DATA_STREAM_IID = "{88E04DB3-1012-4D64-9996-F703A950D3F4}"


@dataclass(frozen=True, slots=True)
class PortableDeviceFile:
    object_id: str
    name: str
    size_bytes: int


class PortableDeviceWriteStream(Protocol):
    def write(self, content: bytes) -> int: ...

    def commit(self) -> str:
        """Commit the staged object and return its device-local object ID."""
        ...

    def close(self) -> None: ...


class PortableDeviceReadStream(Protocol):
    def __enter__(self) -> PortableDeviceReadStream: ...

    def __exit__(self, *_args: object) -> bool | None: ...

    def read(self, size: int = -1) -> bytes: ...


class PortableDeviceSession(Protocol):
    def ensure_folder(self, parent_object_id: str, name: str) -> str: ...

    def list_files(self, storage_object_id: str) -> tuple[PortableDeviceFile, ...]: ...

    def get_file(self, object_id: str) -> PortableDeviceFile: ...

    def create_file(
        self,
        storage_object_id: str,
        name: str,
        size_bytes: int,
    ) -> PortableDeviceWriteStream: ...

    def open_read(self, object_id: str) -> PortableDeviceReadStream: ...

    def rename_file(self, object_id: str, name: str) -> None: ...

    def delete_file(self, object_id: str) -> None: ...


class PortableDeviceBackend(Protocol):
    def list_targets(self) -> tuple[ExportTarget, ...]: ...

    def open_session(
        self,
        target: ExportTarget,
    ) -> AbstractContextManager[PortableDeviceSession]: ...


ExportStatus = Literal["EXPORTED", "SKIPPED"]


class WpdExportService:
    """Export one verified CBZ or CBR through a portable-device transport."""

    def __init__(
        self,
        backend: PortableDeviceBackend,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> None:
        if not callable(getattr(backend, "list_targets", None)) or not callable(
            getattr(backend, "open_session", None)
        ):
            raise TypeError("backend must implement list_targets and open_session")
        self._backend = backend
        self._progress_callback = progress_callback
        self._progress_size = 0

    def _report_progress(self, stage: str, processed_bytes: int) -> None:
        if self._progress_callback is not None:
            self._progress_callback(stage, processed_bytes, self._progress_size)

    def export_asset(
        self,
        asset: Asset,
        *,
        source_path: Path,
        target: ExportTarget,
        export_id: UUID,
        trace_id: str,
    ) -> ExportStatus:
        """Copy, read back, then rename. Every uncertain mutation fails closed."""
        stage = "preflight"
        self._progress_size = asset.size_bytes
        staging_name: str | None = None
        try:
            current_target = self._resolve_target(target.target_id, trace_id=trace_id)
            self._validate_portable_target(current_target, trace_id=trace_id)
            if asset.size_bytes <= 0 or asset.size_bytes > MAX_EXPORT_BYTES:
                self._raise(
                    ErrorCode.EXP_SOURCE_INVALID,
                    "source_size_limit",
                    trace_id=trace_id,
                    retryable=False,
                )

            source_info = self._inspect_source(source_path, trace_id=trace_id)
            if source_info.st_size != asset.size_bytes:
                self._raise(
                    ErrorCode.EXP_HASH_MISMATCH,
                    "verify_source",
                    trace_id=trace_id,
                    retryable=False,
                )
            source_size, source_hash = self._hash_source(
                source_path,
                trace_id=trace_id,
                stage="verify_source",
            )
            if source_size != asset.size_bytes or source_hash != asset.sha256:
                self._raise(
                    ErrorCode.EXP_HASH_MISMATCH,
                    "verify_source",
                    trace_id=trace_id,
                    retryable=False,
                )

            destination_name = Path(asset.relative_path).name
            staging_name = f".yomira-{asset.asset_id.hex}.part"
            with self._backend.open_session(current_target) as session:
                stage = "destination_folder"
                books_id = session.ensure_folder(current_target.storage_object_id or "", "Books")
                current_target = replace(current_target, storage_object_id=books_id)
                stage = "destination_preflight"
                existing = self._named_files(
                    session,
                    current_target,
                    destination_name,
                    trace_id=trace_id,
                    stage=stage,
                )
                if existing:
                    if len(existing) != 1:
                        self._raise(
                            ErrorCode.EXP_DESTINATION_CONFLICT,
                            "destination_conflict",
                            trace_id=trace_id,
                            retryable=False,
                        )
                    existing_size, existing_hash = self._hash_remote_file(
                        session,
                        existing[0],
                        trace_id=trace_id,
                        stage="destination_verify",
                    )
                    if existing_size == asset.size_bytes and existing_hash == asset.sha256:
                        self._assert_source_unchanged(
                            source_path,
                            source_info,
                            trace_id=trace_id,
                            stage="verify_source",
                        )
                        return "SKIPPED"
                    self._raise(
                        ErrorCode.EXP_DESTINATION_CONFLICT,
                        "destination_conflict",
                        trace_id=trace_id,
                        retryable=False,
                    )

                stage = "resume_staging"
                staged = self._named_files(
                    session,
                    current_target,
                    staging_name,
                    trace_id=trace_id,
                    stage=stage,
                )
                if len(staged) > 1:
                    self._raise(
                        ErrorCode.EXP_DESTINATION_CONFLICT,
                        stage,
                        trace_id=trace_id,
                        retryable=False,
                        staging_name=staging_name,
                    )
                staged_file = staged[0] if staged else None
                if staged_file is not None:
                    staged_size, staged_hash = self._hash_remote_file(
                        session,
                        staged_file,
                        trace_id=trace_id,
                        stage=stage,
                        staging_name=staging_name,
                    )
                    if staged_size != asset.size_bytes or staged_hash != asset.sha256:
                        if (
                            staged_size >= asset.size_bytes
                            or self._hash_source_prefix(source_path, staged_size) != staged_hash
                        ):
                            self._raise(
                                ErrorCode.EXP_HASH_MISMATCH,
                                stage,
                                trace_id=trace_id,
                                retryable=False,
                                staging_name=staging_name,
                            )
                        self._assert_source_unchanged(
                            source_path,
                            source_info,
                            trace_id=trace_id,
                            stage="verify_source",
                        )
                        if session.get_file(
                            staged_file.object_id
                        ) != staged_file or self._named_files(
                            session, current_target, staging_name, trace_id=trace_id, stage=stage
                        ) != (staged_file,):
                            self._raise(
                                ErrorCode.EXP_DESTINATION_CONFLICT,
                                stage,
                                trace_id=trace_id,
                                retryable=False,
                                staging_name=staging_name,
                            )
                        # Only our partial file, verified against the source prefix, is removed.
                        session.delete_file(staged_file.object_id)
                        if self._named_files(
                            session,
                            current_target,
                            staging_name,
                            trace_id=trace_id,
                            stage=stage,
                        ):
                            self._raise(
                                ErrorCode.EXP_PUBLISH_FAILED,
                                stage,
                                trace_id=trace_id,
                                retryable=False,
                                staging_name=staging_name,
                            )
                        staged_file = None
                        current_target = replace(
                            current_target,
                            free_bytes=self._resolve_target(
                                target.target_id,
                                trace_id=trace_id,
                            ).free_bytes,
                        )

                staged_verified = staged_file is not None
                if staged_file is None and current_target.free_bytes < asset.size_bytes:
                    self._raise(
                        ErrorCode.EXP_DISK_FULL,
                        "space_preflight",
                        trace_id=trace_id,
                        retryable=True,
                    )

                if staged_file is None:
                    stage = "create_staging"
                    writer = session.create_file(
                        current_target.storage_object_id or "",
                        staging_name,
                        asset.size_bytes,
                    )
                    try:
                        stage = "copy"
                        self._copy_to_device(
                            source_path,
                            writer,
                            asset=asset,
                            source_info=source_info,
                            trace_id=trace_id,
                            staging_name=staging_name,
                        )
                        stage = "commit_staging"
                        staged_object_id = writer.commit()
                    finally:
                        writer.close()
                    staged_file = session.get_file(staged_object_id)
                else:
                    staged_object_id = staged_file.object_id

                stage = "verify_staging"
                if staged_file.name != staging_name:
                    self._raise(
                        ErrorCode.EXP_HASH_MISMATCH,
                        stage,
                        trace_id=trace_id,
                        retryable=False,
                        staging_name=staging_name,
                    )
                if not staged_verified:
                    staged_size, staged_hash = self._hash_remote_file(
                        session,
                        staged_file,
                        trace_id=trace_id,
                        stage=stage,
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

                stage = "publish_preflight"
                existing = self._named_files(
                    session,
                    current_target,
                    destination_name,
                    trace_id=trace_id,
                    stage=stage,
                )
                if existing:
                    self._raise(
                        ErrorCode.EXP_DESTINATION_CONFLICT,
                        "destination_conflict",
                        trace_id=trace_id,
                        retryable=False,
                        staging_name=staging_name,
                    )

                stage = "publish"
                self._report_progress(stage, 0)
                session.rename_file(staged_object_id, destination_name)

                stage = "verify_final"
                final_name_matches = self._named_files(
                    session,
                    current_target,
                    destination_name,
                    trace_id=trace_id,
                    stage=stage,
                )
                if (
                    len(final_name_matches) != 1
                    or final_name_matches[0].object_id != staged_object_id
                ):
                    self._raise(
                        ErrorCode.EXP_PUBLISH_FAILED,
                        stage,
                        trace_id=trace_id,
                        retryable=False,
                        staging_name=staging_name,
                    )
                final_size, final_hash = self._hash_remote_file(
                    session,
                    final_name_matches[0],
                    trace_id=trace_id,
                    stage=stage,
                )
                self._assert_source_unchanged(
                    source_path,
                    source_info,
                    trace_id=trace_id,
                    stage="verify_source",
                )
                if final_size != asset.size_bytes or final_hash != asset.sha256:
                    self._raise(
                        ErrorCode.EXP_HASH_MISMATCH,
                        stage,
                        trace_id=trace_id,
                        retryable=False,
                    )
            return "EXPORTED"
        except ExportFailure:
            raise
        except Exception as error:
            code, retryable = _classify_wpd_error(error)
            self._raise(
                code,
                stage,
                trace_id=trace_id,
                retryable=retryable,
                staging_name=staging_name,
                cause_type=type(error).__name__,
            )

    def _resolve_target(self, target_id: str, *, trace_id: str) -> ExportTarget:
        for target in self._backend.list_targets():
            if target.target_id == target_id and target.transport == "wpd":
                return target
        raise ExportFailure(
            ErrorCode.EXP_DEVICE_DISCONNECTED,
            "target_lookup",
            trace_id=trace_id,
            retryable=True,
        )

    @staticmethod
    def _validate_portable_target(target: ExportTarget, *, trace_id: str) -> None:
        if (
            target.transport != "wpd"
            or target.mount_path is not None
            or not target.portable_device_id
            or not target.storage_object_id
            or not target.storage_persistent_id
        ):
            WpdExportService._raise(
                ErrorCode.EXP_DEVICE_DISCONNECTED,
                "target_identity",
                trace_id=trace_id,
                retryable=True,
            )

    @staticmethod
    def _inspect_source(source_path: Path, *, trace_id: str) -> os.stat_result:
        try:
            info = source_path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise OSError("Source CBZ is not a plain file.")
            return info
        except OSError as error:
            WpdExportService._raise(
                ErrorCode.LIB_PATH_UNAVAILABLE,
                "source_preflight",
                trace_id=trace_id,
                retryable=False,
                cause_type=type(error).__name__,
            )

    def _hash_source(
        self,
        source_path: Path,
        *,
        trace_id: str,
        stage: str,
    ) -> tuple[int, str]:
        digest = hashlib.sha256()
        total_bytes = 0
        self._report_progress(stage, 0)
        try:
            with source_path.open("rb") as source:
                before = os.fstat(source.fileno())
                while chunk := source.read(COPY_CHUNK_BYTES):
                    total_bytes += len(chunk)
                    digest.update(chunk)
                    self._report_progress(stage, total_bytes)
                    if total_bytes > MAX_EXPORT_BYTES:
                        self._raise(
                            ErrorCode.EXP_SOURCE_INVALID,
                            "source_size_limit",
                            trace_id=trace_id,
                            retryable=False,
                        )
                after = os.fstat(source.fileno())
                if not self._same_source_version(before, after):
                    self._raise(
                        ErrorCode.EXP_HASH_MISMATCH,
                        stage,
                        trace_id=trace_id,
                        retryable=False,
                    )
        except ExportFailure:
            raise
        except OSError as error:
            self._raise(
                ErrorCode.LIB_PATH_UNAVAILABLE,
                stage,
                trace_id=trace_id,
                retryable=False,
                cause_type=type(error).__name__,
            )
        return total_bytes, digest.hexdigest()

    @staticmethod
    def _hash_source_prefix(source_path: Path, size_bytes: int) -> str:
        digest = hashlib.sha256()
        remaining = size_bytes
        with source_path.open("rb") as stream:
            while remaining:
                chunk = stream.read(min(COPY_CHUNK_BYTES, remaining))
                if not chunk:
                    raise OSError("Source prefix is truncated.")
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.hexdigest()

    def _copy_to_device(
        self,
        source_path: Path,
        writer: PortableDeviceWriteStream,
        *,
        asset: Asset,
        source_info: os.stat_result,
        trace_id: str,
        staging_name: str,
    ) -> None:
        digest = hashlib.sha256()
        copied_bytes = 0
        try:
            with source_path.open("rb") as source:
                opened_info = os.fstat(source.fileno())
                if not self._same_source_version(source_info, opened_info):
                    self._raise(
                        ErrorCode.EXP_HASH_MISMATCH,
                        "verify_source",
                        trace_id=trace_id,
                        retryable=False,
                        staging_name=staging_name,
                    )
                while chunk := source.read(COPY_CHUNK_BYTES):
                    if copied_bytes + len(chunk) > MAX_EXPORT_BYTES:
                        self._raise(
                            ErrorCode.EXP_SOURCE_INVALID,
                            "source_size_limit",
                            trace_id=trace_id,
                            retryable=False,
                            staging_name=staging_name,
                        )
                    written = writer.write(chunk)
                    if written != len(chunk):
                        raise OSError("Portable-device write returned a short count.")
                    copied_bytes += written
                    digest.update(chunk)
                    self._report_progress("copy", copied_bytes)
                closed_info = os.fstat(source.fileno())
        except ExportFailure:
            raise
        except OSError as error:
            code, retryable = _classify_wpd_error(error)
            self._raise(
                code,
                "copy",
                trace_id=trace_id,
                retryable=retryable,
                staging_name=staging_name,
                cause_type=type(error).__name__,
            )
        if (
            copied_bytes != asset.size_bytes
            or digest.hexdigest() != asset.sha256
            or not self._same_source_version(source_info, closed_info)
        ):
            self._raise(
                ErrorCode.EXP_HASH_MISMATCH,
                "verify_source",
                trace_id=trace_id,
                retryable=False,
                staging_name=staging_name,
            )

    def _hash_remote_file(
        self,
        session: PortableDeviceSession,
        remote_file: PortableDeviceFile,
        *,
        trace_id: str,
        stage: str,
        staging_name: str | None = None,
    ) -> tuple[int, str]:
        digest = hashlib.sha256()
        total_bytes = 0
        self._report_progress(stage, 0)
        try:
            with session.open_read(remote_file.object_id) as stream:
                while chunk := stream.read(COPY_CHUNK_BYTES):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_EXPORT_BYTES:
                        self._raise(
                            ErrorCode.EXP_SOURCE_INVALID,
                            "remote_size_limit",
                            trace_id=trace_id,
                            retryable=False,
                            staging_name=staging_name,
                        )
                    digest.update(chunk)
                    self._report_progress(stage, total_bytes)
        except ExportFailure:
            raise
        except Exception as error:
            code, retryable = _classify_wpd_error(error)
            self._raise(
                code,
                stage,
                trace_id=trace_id,
                retryable=retryable,
                staging_name=staging_name,
                cause_type=type(error).__name__,
            )
        return total_bytes, digest.hexdigest()

    def _named_files(
        self,
        session: PortableDeviceSession,
        target: ExportTarget,
        name: str,
        *,
        trace_id: str,
        stage: str,
    ) -> tuple[PortableDeviceFile, ...]:
        storage_id = target.storage_object_id
        if not storage_id:
            self._raise(
                ErrorCode.EXP_DEVICE_DISCONNECTED,
                stage,
                trace_id=trace_id,
                retryable=True,
            )
        try:
            return tuple(
                item
                for item in session.list_files(storage_id)
                if item.name.casefold() == name.casefold()
            )
        except ExportFailure:
            raise
        except Exception as error:
            code, retryable = _classify_wpd_error(error)
            self._raise(
                code,
                stage,
                trace_id=trace_id,
                retryable=retryable,
                cause_type=type(error).__name__,
            )

    @staticmethod
    def _assert_source_unchanged(
        source_path: Path,
        source_info: os.stat_result,
        *,
        trace_id: str,
        stage: str,
    ) -> None:
        try:
            current_info = source_path.stat()
        except OSError as error:
            WpdExportService._raise(
                ErrorCode.LIB_PATH_UNAVAILABLE,
                stage,
                trace_id=trace_id,
                retryable=False,
                cause_type=type(error).__name__,
            )
        if not WpdExportService._same_source_version(source_info, current_info):
            WpdExportService._raise(
                ErrorCode.EXP_HASH_MISMATCH,
                stage,
                trace_id=trace_id,
                retryable=False,
            )

    @staticmethod
    def _same_source_version(first: os.stat_result, second: os.stat_result) -> bool:
        return (
            first.st_dev == second.st_dev
            and first.st_ino == second.st_ino
            and first.st_size == second.st_size
            and first.st_mtime_ns == second.st_mtime_ns
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


class WindowsWpdBackend:
    """Read and write portable-device files using Windows' WPD COM API."""

    def list_targets(self) -> tuple[ExportTarget, ...]:
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            return ()
        comtypes, api, types = _load_wpd_modules()
        comtypes.CoInitialize()
        manager = None
        try:
            manager = _create_device_manager(api)
            manager.RefreshDeviceList()
            pnp_device_ids = _get_pnp_device_ids(manager)
            targets: list[ExportTarget] = []
            for pnp_device_id in pnp_device_ids[:MAX_WPD_DEVICES]:
                device = None
                try:
                    device = _open_device(api, types, pnp_device_id)
                    targets.extend(_read_device_targets(api, device, pnp_device_id))
                except Exception as error:
                    _log_wpd_enumeration_failure(error)
                finally:
                    if device is not None:
                        try:
                            device.Close()
                        except Exception:
                            pass
                        device = None
            unique = {target.target_id: target for target in targets}
            return tuple(unique[key] for key in sorted(unique))
        finally:
            if manager is not None:
                manager = None
            comtypes.CoUninitialize()

    @contextmanager
    def open_session(self, target: ExportTarget) -> Generator[_WindowsWpdSession]:
        if (
            os.name != "nt"
            or not hasattr(ctypes, "WinDLL")
            or target.transport != "wpd"
            or not target.portable_device_id
            or not target.storage_persistent_id
        ):
            raise OSError("Portable-device target is unavailable.")
        comtypes, api, types = _load_wpd_modules()
        comtypes.CoInitialize()
        device = None
        content = None
        session = None
        try:
            device = _open_device(api, types, target.portable_device_id)
            content = device.Content()
            storage_id = _resolve_storage_object_id(
                api,
                content,
                target.storage_persistent_id,
                target.storage_object_id,
            )
            session = _WindowsWpdSession(comtypes, api, types, content)
            session.storage_object_id = storage_id
            yield session
        finally:
            try:
                if session is not None:
                    session.close()
            finally:
                session = None
                content = None
                try:
                    if device is not None:
                        try:
                            device.Close()
                        except Exception:
                            pass
                        device = None
                finally:
                    comtypes.CoUninitialize()


class _WindowsWpdSession:
    def __init__(self, comtypes: Any, api: Any, types: Any, content: Any) -> None:
        self._comtypes = comtypes
        self._api = api
        self._types = types
        self._content: Any = content
        self._properties: Any = content.Properties()
        self.storage_object_id = ""

    def close(self) -> None:
        self._properties = None
        self._content = None

    def list_files(self, storage_object_id: str) -> tuple[PortableDeviceFile, ...]:
        objects = _enumerate_children(self._content, storage_object_id)
        return tuple(self.get_file(object_id) for object_id in objects)

    def ensure_folder(self, parent_object_id: str, name: str) -> str:
        if name != "Books" or not parent_object_id:
            raise ValueError("Only the Books export folder is supported.")
        matches = [
            item
            for item in self.list_files(parent_object_id)
            if item.name.casefold() == name.casefold()
        ]
        if len(matches) > 1:
            raise ValueError("The Books folder is ambiguous.")
        if matches:
            folder_id = matches[0].object_id
        else:
            values = _create_values(self._comtypes, self._api, self._types.PortableDeviceValues)
            for property_id, value in ((3, parent_object_id), (4, name), (12, name)):
                values.SetStringValue(
                    ctypes.byref(
                        _property_key(
                            self._comtypes, self._api, _OBJECT_PROPERTIES_GUID, property_id
                        )
                    ),
                    value,
                )
            values.SetGuidValue(
                ctypes.byref(_property_key(self._comtypes, self._api, _OBJECT_PROPERTIES_GUID, 7)),
                self._comtypes.GUID(_FOLDER_CONTENT_TYPE),
            )
            folder_id = self._content.CreateObjectWithPropertiesOnly(values)
        properties = self._properties.GetValues(folder_id, None)
        content_type = properties.GetGuidValue(
            ctypes.byref(_property_key(self._comtypes, self._api, _OBJECT_PROPERTIES_GUID, 7))
        )
        parent = _get_wpd_string_value(
            properties, _property_key(self._comtypes, self._api, _OBJECT_PROPERTIES_GUID, 3)
        )
        if str(content_type).upper() != _FOLDER_CONTENT_TYPE or parent != parent_object_id:
            raise ValueError("The destination is not a Books folder in this storage.")
        return folder_id

    def get_file(self, object_id: str) -> PortableDeviceFile:
        values = self._properties.GetValues(object_id, None)
        object_name = _get_wpd_string_value(
            values,
            _property_key(self._comtypes, self._api, _OBJECT_PROPERTIES_GUID, 4),
        )
        try:
            size_bytes = int(
                values.GetUnsignedLargeIntegerValue(
                    ctypes.byref(
                        _property_key(self._comtypes, self._api, _OBJECT_PROPERTIES_GUID, 11)
                    )
                )
            )
        except Exception:
            size_bytes = 0
        # Some drivers leave ORIGINAL_FILE_NAME stale; match the published visible name.
        return PortableDeviceFile(object_id, object_name, size_bytes)

    def create_file(
        self,
        storage_object_id: str,
        name: str,
        size_bytes: int,
    ) -> _WindowsWpdWriteStream:
        portable_values = self._types.PortableDeviceValues
        properties = _create_values(self._comtypes, self._api, portable_values)
        properties.SetStringValue(
            ctypes.byref(_property_key(self._comtypes, self._api, _OBJECT_PROPERTIES_GUID, 3)),
            storage_object_id,
        )
        properties.SetUnsignedLargeIntegerValue(
            ctypes.byref(_property_key(self._comtypes, self._api, _OBJECT_PROPERTIES_GUID, 11)),
            size_bytes,
        )
        properties.SetStringValue(
            ctypes.byref(_property_key(self._comtypes, self._api, _OBJECT_PROPERTIES_GUID, 4)),
            name,
        )
        properties.SetStringValue(
            ctypes.byref(_property_key(self._comtypes, self._api, _OBJECT_PROPERTIES_GUID, 12)),
            name,
        )
        properties.SetGuidValue(
            ctypes.byref(_property_key(self._comtypes, self._api, _OBJECT_PROPERTIES_GUID, 7)),
            self._comtypes.GUID(_GENERIC_FILE_CONTENT_TYPE),
        )
        properties.SetGuidValue(
            ctypes.byref(_property_key(self._comtypes, self._api, _OBJECT_PROPERTIES_GUID, 6)),
            self._comtypes.GUID(_UNSPECIFIED_OBJECT_FORMAT),
        )
        optimal_buffer_size = ctypes.c_ulong(COPY_CHUNK_BYTES)
        cookie = ctypes.c_wchar_p()
        stream_interface = cast(type[Any], self._api.IStream)
        stream = ctypes.POINTER(stream_interface)()
        try:
            create_object = (
                self._content._IPortableDeviceContent__com_CreateObjectWithPropertiesAndData
            )
            _check_hresult(
                create_object(
                    properties,
                    ctypes.byref(stream),
                    ctypes.byref(optimal_buffer_size),
                    ctypes.byref(cookie),
                )
            )
            if not stream:
                raise OSError("Portable-device API returned no write stream.")
            return _WindowsWpdWriteStream(self._comtypes, stream, self._api.IStream)
        finally:
            _free_cotaskmem(cookie)

    def open_read(self, object_id: str) -> _WindowsWpdReadStream:
        resources = self._content.Transfer()
        resource_key = _property_key(self._comtypes, self._api, _RESOURCE_DEFAULT_GUID, 0)
        _optimal_buffer_size, stream = resources.GetStream(
            object_id,
            ctypes.byref(resource_key),
            STGM_READ,
            COPY_CHUNK_BYTES,
        )
        return _WindowsWpdReadStream(stream)

    def rename_file(self, object_id: str, name: str) -> None:
        values = _create_values(
            self._comtypes,
            self._api,
            self._types.PortableDeviceValues,
        )
        # MTP readers may reject the display name but accept the actual file name.
        keys = tuple(
            _property_key(self._comtypes, self._api, _OBJECT_PROPERTIES_GUID, property_id)
            for property_id in (4, 12)
        )
        for key in keys:
            values.SetStringValue(ctypes.byref(key), name)
        results = self._properties.SetValues(object_id, values)
        errors = tuple(int(results.GetErrorValue(ctypes.byref(key))) for key in keys)
        if all(error < 0 for error in errors):
            _check_hresult(errors[0])

    def delete_file(self, object_id: str) -> None:
        runtime, create_object = _load_runtime_functions()
        ids = create_object(
            self._types.PortableDevicePropVariantCollection,
            interface=self._api.IPortableDevicePropVariantCollection,
            clsctx=runtime.CLSCTX_INPROC_SERVER,
        )
        value = self._api.tag_inner_PROPVARIANT()
        value.vt = 31  # VT_LPWSTR
        getattr(value, value._fields_[-1][0]).pwszVal = object_id
        ids.Add(ctypes.byref(value))
        collection_type = cast(type[Any], self._api.IPortableDevicePropVariantCollection)
        results = ctypes.POINTER(collection_type)()
        delete = self._content._IPortableDeviceContent__com_Delete
        _check_hresult(delete(0, ids, ctypes.byref(results)))
        if not results:
            raise OSError("Portable-device deletion returned no results.")
        result = self._api.tag_inner_PROPVARIANT()
        cast(Any, results).GetAt(0, ctypes.byref(result))
        if result.vt != 10:  # VT_ERROR
            raise OSError("Portable-device deletion returned no result.")
        _check_hresult(int(getattr(result, result._fields_[-1][0]).scode))


class _WindowsWpdWriteStream:
    def __init__(self, comtypes: Any, stream: Any, stream_interface: Any) -> None:
        self._comtypes = comtypes
        self._stream = stream
        self._stream_interface = stream_interface

    def write(self, content: bytes) -> int:
        if not content:
            return 0
        if self._stream is None:
            raise OSError("Portable-device write stream is closed.")
        buffer = (ctypes.c_ubyte * len(content)).from_buffer_copy(content)
        return int(self._stream.RemoteWrite(buffer, len(content)))

    def commit(self) -> str:
        stream = self._stream
        if stream is None:
            raise OSError("Portable-device write stream is closed.")
        try:
            stream.Commit(0)
            return _get_object_id_from_committed_stream(
                self._comtypes,
                stream,
                self._stream_interface,
            )
        finally:
            self.close()

    def close(self) -> None:
        self._stream = None


class _WindowsWpdReadStream:
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def __enter__(self) -> _WindowsWpdReadStream:
        return self

    def __exit__(self, *_args: object) -> None:
        self._stream = None

    def read(self, size: int = -1) -> bytes:
        read_size = COPY_CHUNK_BYTES if size < 0 else min(size, COPY_CHUNK_BYTES)
        if read_size <= 0:
            return b""
        stream = self._stream
        if stream is None:
            raise OSError("Portable-device read stream is closed.")
        buffer, count = stream.RemoteRead(read_size)
        return bytes(buffer[:count])


def _load_wpd_modules() -> tuple[Any, Any, Any]:
    try:
        import comtypes
        from comtypes.client import CreateObject, GetModule
    except ImportError as error:
        raise RuntimeError("Windows Portable Devices support is not installed.") from error
    api = GetModule(("{1F001332-1A57-4934-BE31-AFFC99F4EE0A}", 1, 0))
    types = GetModule(("{2B00BA2F-E750-4BEB-9235-97142EDE1D3E}", 1, 0))
    return _ComtypesRuntime(comtypes, CreateObject), api, types


@dataclass(frozen=True, slots=True)
class _ComtypesRuntime:
    comtypes: Any
    create_object: Any

    def __getattr__(self, name: str) -> Any:
        return getattr(self.comtypes, name)


def _create_device_manager(api: Any) -> Any:
    runtime, create_object = _load_runtime_functions()
    return create_object(
        api.PortableDeviceManager,
        interface=api.IPortableDeviceManager,
        clsctx=runtime.CLSCTX_INPROC_SERVER,
    )


def _load_runtime_functions() -> tuple[Any, Any]:
    import comtypes
    from comtypes.client import CreateObject

    return comtypes, CreateObject


def _open_device(api: Any, types: Any, pnp_device_id: str) -> Any:
    runtime, create_object = _load_runtime_functions()
    device = create_object(
        api.PortableDevice,
        interface=api.IPortableDevice,
        clsctx=runtime.CLSCTX_INPROC_SERVER,
    )
    client_info = _create_values(
        runtime,
        api,
        types.PortableDeviceValues,
    )
    device.Open(pnp_device_id, client_info)
    return device


def _create_values(runtime: Any, api: Any, coclass: Any) -> Any:
    _runtime, create_object = _load_runtime_functions()
    return create_object(
        coclass,
        # Both type libraries define PROPERTYKEY; use the interface matching our keys.
        interface=api.IPortableDeviceValues,
        clsctx=runtime.CLSCTX_INPROC_SERVER,
    )


def _get_pnp_device_ids(manager: Any) -> tuple[str, ...]:
    get_devices = manager._IPortableDeviceManager__com_GetDevices  # noqa: B009
    count = ctypes.c_ulong(0)
    _check_hresult(get_devices(None, ctypes.byref(count)))
    capacity = min(count.value, MAX_WPD_DEVICES)
    if capacity == 0:
        return ()
    allocated_ids = (ctypes.c_void_p * capacity)()
    filled_count = ctypes.c_ulong(capacity)
    try:
        _check_hresult(
            get_devices(
                ctypes.cast(allocated_ids, ctypes.POINTER(ctypes.c_wchar_p)),
                ctypes.byref(filled_count),
            )
        )
        return tuple(
            ctypes.wstring_at(allocated_ids[index])
            for index in range(min(filled_count.value, capacity))
            if allocated_ids[index]
        )
    finally:
        for index in range(capacity):
            if allocated_ids[index]:
                _free_cotaskmem(ctypes.c_void_p(allocated_ids[index]))


def _read_device_targets(api: Any, device: Any, pnp_device_id: str) -> list[ExportTarget]:
    content = device.Content()
    properties = content.Properties()
    device_values = properties.GetValues("DEVICE", None)
    device_name = (
        _optional_string(
            device_values,
            _property_key(None, api, _DEVICE_PROPERTIES_GUID, 12),
        )
        or "Dispositivo portátil"
    )
    storage_ids = _enumerate_children(content, "DEVICE", limit=MAX_WPD_STORAGES_PER_DEVICE + 1)
    targets: list[ExportTarget] = []
    for storage_id in storage_ids[:MAX_WPD_STORAGES_PER_DEVICE]:
        values = properties.GetValues(storage_id, None)
        try:
            category = values.GetGuidValue(
                ctypes.byref(_property_key(None, api, _FUNCTIONAL_OBJECT_PROPERTIES_GUID, 2))
            )
        except Exception:
            continue
        if (
            str(category).strip("{}").casefold()
            != _STORAGE_FUNCTIONAL_CATEGORY.strip("{}").casefold()
        ):
            continue
        name = (
            _optional_string(
                values,
                _property_key(None, api, _OBJECT_PROPERTIES_GUID, 4),
            )
            or "Almacenamiento"
        )
        persistent_id = _optional_string(
            values,
            _property_key(None, api, _OBJECT_PROPERTIES_GUID, 5),
        )
        identity = persistent_id or storage_id
        total_bytes = int(
            values.GetUnsignedLargeIntegerValue(
                ctypes.byref(_property_key(None, api, _STORAGE_PROPERTIES_GUID, 4))
            )
        )
        free_bytes = int(
            values.GetUnsignedLargeIntegerValue(
                ctypes.byref(_property_key(None, api, _STORAGE_PROPERTIES_GUID, 5))
            )
        )
        if total_bytes <= 0 or free_bytes < 0 or free_bytes > total_bytes:
            continue
        stable_id = hashlib.sha256(f"{pnp_device_id}\0{identity}".encode()).hexdigest()
        targets.append(
            ExportTarget(
                target_id=f"wpd:{stable_id}",
                mount_path=None,
                display_name=f"{device_name} - {name}"[:128],
                file_system="MTP/WPD",
                total_bytes=total_bytes,
                free_bytes=free_bytes,
                transport="wpd",
                portable_device_id=pnp_device_id,
                storage_object_id=storage_id,
                storage_persistent_id=identity,
            )
        )
    return targets


def _resolve_storage_object_id(
    api: Any,
    content: Any,
    persistent_id: str,
    original_storage_id: str | None,
) -> str:
    properties = content.Properties()
    for storage_id in _enumerate_children(content, "DEVICE", limit=MAX_WPD_STORAGES_PER_DEVICE + 1):
        values = properties.GetValues(storage_id, None)
        try:
            category = values.GetGuidValue(
                ctypes.byref(_property_key(None, api, _FUNCTIONAL_OBJECT_PROPERTIES_GUID, 2))
            )
        except Exception:
            continue
        if (
            str(category).strip("{}").casefold()
            != _STORAGE_FUNCTIONAL_CATEGORY.strip("{}").casefold()
        ):
            continue
        current_persistent_id = _optional_string(
            values,
            _property_key(None, api, _OBJECT_PROPERTIES_GUID, 5),
        )
        if current_persistent_id == persistent_id:
            return storage_id
        if not current_persistent_id and storage_id == original_storage_id:
            return storage_id
    raise OSError("Portable-device storage is no longer available.")


def _enumerate_children(
    content: Any,
    parent_object_id: str,
    *,
    limit: int = MAX_WPD_OBJECTS_PER_STORAGE,
) -> tuple[str, ...]:
    enumerator = content.EnumObjects(0, parent_object_id, None)
    next_object = enumerator._IEnumPortableDeviceObjectIDs__com_Next  # noqa: B009
    object_ids: list[str] = []
    while len(object_ids) <= limit:
        allocated_ids = (ctypes.c_void_p * 1)()
        fetched = ctypes.c_ulong(0)
        try:
            _check_hresult(
                next_object(
                    1,
                    ctypes.cast(allocated_ids, ctypes.POINTER(ctypes.c_wchar_p)),
                    ctypes.byref(fetched),
                )
            )
            if fetched.value == 0:
                return tuple(object_ids)
            if fetched.value != 1 or not allocated_ids[0]:
                raise OSError("Portable-device enumeration returned an invalid object ID.")
            object_id = ctypes.wstring_at(allocated_ids[0])
        finally:
            if allocated_ids[0]:
                _free_cotaskmem(ctypes.c_void_p(allocated_ids[0]))
        object_ids.append(object_id)
    raise OSError("Portable-device directory exceeds the bounded inspection limit.")


def _property_key(runtime: Any | None, api: Any, guid: str, property_id: int) -> Any:
    if runtime is None:
        import comtypes

        runtime = comtypes
    return api._tagpropertykey(runtime.GUID(guid), property_id)


def _optional_string(values: Any, key: Any) -> str | None:
    try:
        value = _get_wpd_string_value(values, key)
    except Exception:
        return None
    return value or None


def _get_wpd_string_value(values: Any, key: Any) -> str:
    get_string_value = values._IPortableDeviceValues__com_GetStringValue  # noqa: B009
    value = ctypes.c_wchar_p()
    try:
        _check_hresult(get_string_value(ctypes.byref(key), ctypes.byref(value)))
        if not value.value:
            raise OSError("Windows Portable Devices returned an empty string value.")
        return value.value
    finally:
        _free_cotaskmem(value)


def _get_object_id_from_committed_stream(
    runtime: Any,
    stream: Any,
    stream_interface: Any,
) -> str:
    import comtypes
    from comtypes import HRESULT

    commethod: Any = getattr(comtypes, "COMMETHOD")  # noqa: B009

    interface_guid = runtime.GUID(_IPORTABLE_DEVICE_DATA_STREAM_IID)
    interface_type = type(
        "IPortableDeviceDataStream",
        (stream_interface,),
        {
            "_iid_": interface_guid,
            "_idlflags_": [],
            "_methods_": [
                commethod(
                    [],
                    HRESULT,
                    "GetObjectID",
                    (["out"], ctypes.POINTER(ctypes.c_wchar_p), "ppszObjectID"),
                )
            ],
        },
    )
    unknown = stream.QueryInterface(interface_type)
    get_object_id = unknown._IPortableDeviceDataStream__com_GetObjectID  # noqa: B009
    object_id = ctypes.c_wchar_p()
    try:
        _check_hresult(get_object_id(ctypes.byref(object_id)))
        if not object_id.value:
            raise OSError("Portable-device commit did not return an object ID.")
        return object_id.value
    finally:
        _free_cotaskmem(object_id)


def _free_cotaskmem(pointer: Any) -> None:
    address = ctypes.cast(pointer, ctypes.c_void_p).value
    if not address:
        return
    task_memory = ctypes.WinDLL("ole32", use_last_error=True).CoTaskMemFree
    task_memory.argtypes = (ctypes.c_void_p,)
    task_memory.restype = None
    task_memory(ctypes.c_void_p(address))


def _check_hresult(result: int) -> None:
    if result < 0:
        raise OSError(f"Windows Portable Devices returned HRESULT 0x{result & 0xFFFFFFFF:08X}.")


def _classify_wpd_error(error: Exception) -> tuple[ErrorCode, bool]:
    hresult = getattr(error, "hresult", None)
    native_code: int | None = None
    if type(hresult) is int:
        unsigned = hresult & 0xFFFFFFFF
        if unsigned == 0x802A0006:  # WPD device hung; reconnect before retrying.
            return ErrorCode.EXP_DEVICE_DISCONNECTED, True
        if unsigned & 0xFFFF0000 == 0x80070000:
            native_code = unsigned & 0xFFFF
    winerror = getattr(error, "winerror", native_code)
    if winerror in {39, 112} or getattr(error, "errno", None) == errno.ENOSPC:
        return ErrorCode.EXP_DISK_FULL, True
    if winerror in {2, 3, 15, 21, 31, 53, 55, 64, 1167, 1117}:
        return ErrorCode.EXP_DEVICE_DISCONNECTED, True
    return ErrorCode.EXP_COPY_FAILED, False


def _log_wpd_enumeration_failure(error: Exception) -> None:
    code, retryable = _classify_wpd_error(error)
    hresult = getattr(error, "hresult", None)
    winerror = getattr(error, "winerror", None)
    logger.warning(
        "usb_export.portable_target_unavailable",
        extra={
            "error_code": code.value,
            "retryable": retryable,
            "stage": "target_enumeration",
            "cause_type": type(error).__name__,
            "hresult": hresult if type(hresult) is int else None,
            "winerror": winerror if type(winerror) is int else None,
            "trace_id": create_trace_id(),
        },
    )
