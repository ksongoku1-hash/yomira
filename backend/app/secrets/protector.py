"""Windows DPAPI adapter with non-interactive, current-user protection."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any, Protocol


class DataProtectionUnavailable(RuntimeError):
    """Raised when Windows cannot protect or unprotect a data blob."""


class DataProtector(Protocol):
    def protect(self, data: bytes, *, entropy: bytes) -> bytes: ...

    def unprotect(self, data: bytes, *, entropy: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _make_blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data, max(1, len(data)))
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def _windows_libraries() -> tuple[Any, Any]:
    if os.name != "nt":
        raise DataProtectionUnavailable("Windows DPAPI is unavailable.")
    try:
        win_dll = ctypes.WinDLL
        crypt32 = win_dll("Crypt32.dll", use_last_error=True)
        kernel32 = win_dll("Kernel32.dll", use_last_error=True)
    except (AttributeError, OSError) as error:
        raise DataProtectionUnavailable("Windows DPAPI is unavailable.") from error
    return crypt32, kernel32


def _configure_api(crypt32: Any, kernel32: Any) -> tuple[Any, Any, Any, Any]:
    protect = crypt32.CryptProtectData
    protect.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    protect.restype = wintypes.BOOL

    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    unprotect.restype = wintypes.BOOL

    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL
    return protect, unprotect, local_free, kernel32


class WindowsDpapiProtector:
    """Encrypts local blobs for the current Windows user using DPAPI."""

    def protect(self, data: bytes, *, entropy: bytes) -> bytes:
        if os.name != "nt":
            raise DataProtectionUnavailable("Windows DPAPI is unavailable.")
        crypt32, kernel32 = _windows_libraries()
        protect, _, local_free, _ = _configure_api(crypt32, kernel32)
        input_blob, input_buffer = _make_blob(data)
        entropy_blob, entropy_buffer = _make_blob(entropy)
        output_blob = _DataBlob()
        _ = input_buffer, entropy_buffer

        if not protect(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        ):
            raise DataProtectionUnavailable("Windows DPAPI could not protect the record.")

        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            local_free(output_blob.pbData)

    def unprotect(self, data: bytes, *, entropy: bytes) -> bytes:
        if os.name != "nt":
            raise DataProtectionUnavailable("Windows DPAPI is unavailable.")
        crypt32, kernel32 = _windows_libraries()
        _, unprotect, local_free, _ = _configure_api(crypt32, kernel32)
        input_blob, input_buffer = _make_blob(data)
        entropy_blob, entropy_buffer = _make_blob(entropy)
        output_blob = _DataBlob()
        _ = input_buffer, entropy_buffer

        if not unprotect(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        ):
            raise DataProtectionUnavailable("Windows DPAPI could not unprotect the record.")

        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            local_free(output_blob.pbData)
