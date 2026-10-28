"""Windows path identity helpers shared by persistence adapters."""

from __future__ import annotations

import ctypes
import ntpath
import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Final, cast

_DRIVE_REMOVABLE: Final = 2
_DRIVE_FIXED: Final = 3
_ALLOWED_DRIVE_TYPES: Final = frozenset({_DRIVE_REMOVABLE, _DRIVE_FIXED})
_FILE_ID_INFO_CLASS: Final = 18
_FILE_READ_ATTRIBUTES: Final = 0x0080
_FILE_SHARE_READ: Final = 0x00000001
_FILE_SHARE_WRITE: Final = 0x00000002
_FILE_SHARE_DELETE: Final = 0x00000004
_OPEN_EXISTING: Final = 3
_FILE_FLAG_BACKUP_SEMANTICS: Final = 0x02000000


class DataRootIdentityError(RuntimeError):
    """Raised when a data-root identity cannot be captured or has changed."""


@dataclass(slots=True)
class _IdentityState:
    lock: RLock = field(default_factory=RLock)
    invalidated: bool = False


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = (
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", ctypes.c_ubyte * 16),
    )


def _kernel32() -> Any:
    if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
        raise OSError("Windows volume identity APIs are unavailable on this platform.")
    return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]


def _get_volume_path_name(path: Path) -> str:
    """Return the Win32 mount path containing ``path``."""
    if os.name != "nt":
        from app.platform.linux.storage import local_mount

        return local_mount(path)
    kernel32 = _kernel32()
    function = kernel32.GetVolumePathNameW  # type: ignore[attr-defined]
    function.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
    function.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(32_768)
    if not function(str(path), buffer, len(buffer)):
        raise ctypes.WinError(ctypes.get_last_error())
    result = buffer.value
    if not result:
        raise OSError("GetVolumePathNameW returned an empty mount path.")
    return result


def _get_drive_type(volume_root: str) -> int:
    """Return the Win32 drive type for a volume mount path."""
    if os.name != "nt":
        from app.platform.linux.storage import local_mount

        local_mount(Path(volume_root))
        return _DRIVE_FIXED
    kernel32 = _kernel32()
    function = kernel32.GetDriveTypeW  # type: ignore[attr-defined]
    function.argtypes = (ctypes.c_wchar_p,)
    function.restype = ctypes.c_uint
    return int(function(volume_root))


def _get_file_id_info(path: Path) -> tuple[int, bytes]:
    """Read the volume serial and 128-bit file ID for a directory handle."""
    if os.name != "nt":
        from app.platform.linux.storage import file_identity

        return file_identity(path)
    kernel32 = _kernel32()
    create_file = kernel32.CreateFileW  # type: ignore[attr-defined]
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p

    handle = cast(
        int | None,
        create_file(
            str(path),
            _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS,
            None,
        ),
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        get_file_information = kernel32.GetFileInformationByHandleEx  # type: ignore[attr-defined]
        get_file_information.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        get_file_information.restype = ctypes.c_int
        info = _FILE_ID_INFO()
        if not get_file_information(
            handle,
            _FILE_ID_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(info.VolumeSerialNumber), bytes(info.FileId)
    finally:
        close_handle = kernel32.CloseHandle  # type: ignore[attr-defined]
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int
        if not close_handle(handle):
            # The identity is already read, but an unclosed handle means the
            # adapter did not complete reliably, so callers fail closed.
            raise ctypes.WinError(ctypes.get_last_error())


def _validate_file_id_info(value: object) -> tuple[int, bytes]:
    if not isinstance(value, tuple) or len(cast(tuple[object, ...], value)) != 2:
        raise DataRootIdentityError("The root file identity is incomplete.")
    serial, file_id = cast(tuple[object, object], value)
    if type(serial) is not int or not 0 <= serial < (1 << 64):
        raise DataRootIdentityError("The root volume serial is invalid.")
    if not isinstance(file_id, bytes) or len(file_id) != 16:
        raise DataRootIdentityError("The root file ID must contain exactly 16 bytes.")
    return serial, file_id


def windows_path_key(path: Path | str) -> str:
    """Normalize a Windows path lexically without consulting the filesystem."""
    if os.name != "nt":
        return os.path.normpath(os.fspath(path))
    value = os.fspath(path).replace("/", "\\")
    normalized = ntpath.normpath(value)
    return normalized.casefold()


def is_absolute_windows_path(path: Path | str) -> bool:
    if os.name != "nt":
        return os.path.isabs(path)
    value = os.fspath(path).replace("/", "\\")
    drive, tail = ntpath.splitdrive(value)
    return bool(drive and tail.startswith("\\")) or value.startswith("\\\\")


def _require_supported_volume_root(volume_root: str) -> None:
    if not volume_root:
        raise DataRootIdentityError("GetVolumePathNameW returned no volume mount path.")
    drive_type = _get_drive_type(volume_root)
    if type(drive_type) is not int or drive_type not in _ALLOWED_DRIVE_TYPES:
        raise DataRootIdentityError("The data root must be on a fixed or removable volume.")


def _require_supported_volume(path: Path) -> None:
    _require_supported_volume_root(_get_volume_path_name(path))


def validate_supported_volume_for_path(path: Path) -> None:
    """Reject network or unknown volumes before creating a missing data root."""
    if not is_absolute_windows_path(path):
        raise DataRootIdentityError("The data root must be an absolute Windows path.")
    value = os.fspath(path).replace("/", "\\")
    drive, _tail = ntpath.splitdrive(value)
    if drive.startswith("\\\\"):
        raise DataRootIdentityError("The data root must not use a network share.")

    probe = path
    while True:
        try:
            probe.lstat()
            break
        except FileNotFoundError:
            parent = probe.parent
            if parent == probe:
                raise DataRootIdentityError(
                    "No existing parent is available to validate the data-root volume."
                ) from None
            probe = parent
        except OSError as error:
            raise DataRootIdentityError(
                "The data-root volume could not be validated before creation."
            ) from error
    _require_supported_volume(probe)


def _capture_physical_identity(path: Path) -> tuple[str, int, bytes]:
    volume_root = _get_volume_path_name(path)
    _require_supported_volume_root(volume_root)
    volume_serial, file_id = _validate_file_id_info(_get_file_id_info(path))
    return volume_root, volume_serial, file_id


@dataclass(frozen=True, slots=True, init=False)
class DataRootIdentity:
    """One captured physical identity for all consumers of a data root.

    The public canonical path and captured identity never change. The private
    state is intentionally mutable only to latch an invalidation across every
    consumer holding this object.
    """

    canonical_path: Path
    _volume_root: str = field(repr=False)
    _volume_serial: int = field(repr=False)
    _file_id: bytes = field(repr=False)
    _state: _IdentityState = field(repr=False, compare=False)

    @property
    def volume_serial(self) -> int:
        """Return the captured volume serial number."""
        return self._volume_serial

    @property
    def file_id(self) -> bytes:
        """Return the captured 128-bit file ID."""
        return self._file_id

    @classmethod
    def capture(cls, path: Path) -> DataRootIdentity:
        try:
            canonical_path = Path(path).resolve()
            if not canonical_path.is_absolute() or not canonical_path.is_dir():
                raise DataRootIdentityError("The data root must resolve to an existing directory.")
            volume_root, volume_serial, file_id = _capture_physical_identity(canonical_path)
        except DataRootIdentityError:
            raise
        except Exception as error:
            raise DataRootIdentityError("The data-root identity could not be captured.") from error

        identity = object.__new__(cls)
        object.__setattr__(identity, "canonical_path", canonical_path)
        object.__setattr__(identity, "_volume_root", volume_root)
        object.__setattr__(identity, "_volume_serial", volume_serial)
        object.__setattr__(identity, "_file_id", file_id)
        object.__setattr__(identity, "_state", _IdentityState())
        return identity

    def assert_current(self) -> None:
        state = self._state
        with state.lock:
            if state.invalidated:
                raise DataRootIdentityError("The data-root identity was already invalidated.")

            try:
                resolved_path = self.canonical_path.resolve()
                if (
                    not resolved_path.is_absolute()
                    or windows_path_key(resolved_path) != windows_path_key(self.canonical_path)
                    or not resolved_path.is_dir()
                ):
                    raise DataRootIdentityError("The canonical data-root path has changed.")
                _require_supported_volume_root(self._volume_root)
                current_identity = _validate_file_id_info(_get_file_id_info(resolved_path))
                if current_identity != (self._volume_serial, self._file_id):
                    raise DataRootIdentityError("The physical data-root identity has changed.")
            except Exception as error:
                state.invalidated = True
                if isinstance(error, DataRootIdentityError):
                    raise
                raise DataRootIdentityError(
                    "The data-root identity is no longer available."
                ) from error
