"""Bounded Windows memory and disk-I/O sampling without third-party packages."""

import ctypes
import os
import time
from collections.abc import Callable
from ctypes import wintypes
from typing import Protocol

from app.domain.job_backpressure import ResourceSample

_PDH_SUCCESS = 0
_PDH_CSTATUS_VALID_DATA = 0
_PDH_CSTATUS_NEW_DATA = 1
_PDH_FMT_DOUBLE = 0x00000200
_PDH_FMT_NOCAP100 = 0x00008000
_PDH_DISK_BUSY_COUNTER = r"\PhysicalDisk(_Total)\% Disk Time"
_PDH_DISK_LATENCY_COUNTER = r"\PhysicalDisk(_Total)\Avg. Disk sec/Transfer"


class ResourceSamplingUnavailable(RuntimeError):
    def __init__(self, reason: str, native_error_code: int | None = None) -> None:
        self.reason = reason
        self.native_error_code = native_error_code
        super().__init__("Windows resource counters are unavailable.")


class _NativeResourceApi(Protocol):
    def read_memory(self) -> tuple[int, int]: ...

    def read_disk(self) -> tuple[float, float]: ...

    def close(self) -> None: ...


class WindowsResourceSampler:
    """Read system memory and PhysicalDisk aggregate metrics through Win32/PDH."""

    def __init__(
        self,
        *,
        native_api: _NativeResourceApi | None = None,
        native_api_factory: Callable[[], _NativeResourceApi] | None = None,
    ) -> None:
        self._native_api = native_api
        self._native_api_factory = native_api_factory or _Win32ResourceApi

    def sample(self) -> ResourceSample:
        api = self._get_native_api()
        try:
            available_memory_bytes, total_memory_bytes = api.read_memory()
            disk_busy_percent, disk_latency_ms = api.read_disk()
        except ResourceSamplingUnavailable:
            raise
        except (OSError, ValueError, OverflowError) as error:
            raise ResourceSamplingUnavailable(
                "NATIVE_QUERY_FAILED",
                getattr(error, "winerror", None),
            ) from None

        return ResourceSample(
            available_memory_bytes=available_memory_bytes,
            total_memory_bytes=total_memory_bytes,
            disk_busy_percent=disk_busy_percent,
            disk_latency_ms=disk_latency_ms,
            sampled_at_monotonic=time.monotonic(),
        )

    def close(self) -> None:
        api = self._native_api
        self._native_api = None
        if api is not None:
            api.close()

    def _get_native_api(self) -> _NativeResourceApi:
        if self._native_api is None:
            if os.name != "nt":
                raise ResourceSamplingUnavailable("WINDOWS_ONLY")
            self._native_api = self._native_api_factory()
        return self._native_api


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _PdhCounterValue(ctypes.Union):
    _fields_ = [
        ("long_value", wintypes.LONG),
        ("double_value", ctypes.c_double),
        ("large_value", ctypes.c_longlong),
        ("ansi_string_value", wintypes.LPCSTR),
        ("wide_string_value", wintypes.LPCWSTR),
    ]


class _PdhFormattedCounterValue(ctypes.Structure):
    _fields_ = [("status", wintypes.DWORD), ("value", _PdhCounterValue)]


class _Win32ResourceApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise ResourceSamplingUnavailable("WINDOWS_ONLY")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._pdh = ctypes.WinDLL("pdh", use_last_error=True)
        self._query = ctypes.c_void_p()
        self._disk_busy_counter = ctypes.c_void_p()
        self._disk_latency_counter = ctypes.c_void_p()
        self._collections = 0
        self._configure_memory_api()
        self._configure_pdh_api()
        self._open_disk_query()

    def read_memory(self) -> tuple[int, int]:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not self._global_memory_status(ctypes.byref(status)):
            raise ResourceSamplingUnavailable(
                "GLOBAL_MEMORY_STATUS_FAILED",
                ctypes.get_last_error(),
            )
        if status.ullTotalPhys <= 0 or status.ullAvailPhys > status.ullTotalPhys:
            raise ResourceSamplingUnavailable("GLOBAL_MEMORY_STATUS_INVALID")
        return int(status.ullAvailPhys), int(status.ullTotalPhys)

    def read_disk(self) -> tuple[float, float]:
        if self._query.value is None:
            raise ResourceSamplingUnavailable("PDH_QUERY_CLOSED")
        status = self._pdh.PdhCollectQueryData(self._query)
        if status != _PDH_SUCCESS:
            raise ResourceSamplingUnavailable("PDH_COLLECT_FAILED", int(status))
        self._collections += 1
        if self._collections < 2:
            raise ResourceSamplingUnavailable("PDH_COUNTERS_WARMING_UP")

        busy_percent = self._read_counter(self._disk_busy_counter)
        latency_seconds = self._read_counter(self._disk_latency_counter)
        if latency_seconds < 0:
            raise ResourceSamplingUnavailable("PDH_COUNTER_DATA_INVALID")
        return busy_percent, latency_seconds * 1000.0

    def close(self) -> None:
        if self._query.value is None:
            return
        status = self._pdh.PdhCloseQuery(self._query)
        self._query = ctypes.c_void_p()
        self._disk_busy_counter = ctypes.c_void_p()
        self._disk_latency_counter = ctypes.c_void_p()
        if status != _PDH_SUCCESS:
            raise ResourceSamplingUnavailable("PDH_CLOSE_FAILED", int(status))

    def _configure_memory_api(self) -> None:
        self._global_memory_status = self._kernel32.GlobalMemoryStatusEx
        self._global_memory_status.argtypes = (ctypes.POINTER(_MemoryStatusEx),)
        self._global_memory_status.restype = wintypes.BOOL

    def _configure_pdh_api(self) -> None:
        self._open_query = self._pdh.PdhOpenQueryW
        self._open_query.argtypes = (
            wintypes.LPCWSTR,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._open_query.restype = wintypes.DWORD
        self._add_english_counter = self._pdh.PdhAddEnglishCounterW
        self._add_english_counter.argtypes = (
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._add_english_counter.restype = wintypes.DWORD
        self._collect_query = self._pdh.PdhCollectQueryData
        self._collect_query.argtypes = (ctypes.c_void_p,)
        self._collect_query.restype = wintypes.DWORD
        self._get_formatted_counter = self._pdh.PdhGetFormattedCounterValue
        self._get_formatted_counter.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_PdhFormattedCounterValue),
        )
        self._get_formatted_counter.restype = wintypes.DWORD
        self._close_query = self._pdh.PdhCloseQuery
        self._close_query.argtypes = (ctypes.c_void_p,)
        self._close_query.restype = wintypes.DWORD

    def _open_disk_query(self) -> None:
        status = self._open_query(None, 0, ctypes.byref(self._query))
        if status != _PDH_SUCCESS:
            raise ResourceSamplingUnavailable("PDH_OPEN_FAILED", int(status))
        try:
            self._add_counter(_PDH_DISK_BUSY_COUNTER, self._disk_busy_counter)
            self._add_counter(_PDH_DISK_LATENCY_COUNTER, self._disk_latency_counter)
        except ResourceSamplingUnavailable:
            self.close()
            raise

    def _add_counter(self, path: str, counter: ctypes.c_void_p) -> None:
        status = self._add_english_counter(self._query, path, 0, ctypes.byref(counter))
        if status != _PDH_SUCCESS:
            raise ResourceSamplingUnavailable("PDH_ADD_COUNTER_FAILED", int(status))
        if path == _PDH_DISK_BUSY_COUNTER:
            self._disk_busy_counter = counter
        else:
            self._disk_latency_counter = counter

    def _read_counter(self, counter: ctypes.c_void_p) -> float:
        result = _PdhFormattedCounterValue()
        status = self._get_formatted_counter(
            counter,
            _PDH_FMT_DOUBLE | _PDH_FMT_NOCAP100,
            None,
            ctypes.byref(result),
        )
        if status != _PDH_SUCCESS or result.status not in {
            _PDH_CSTATUS_VALID_DATA,
            _PDH_CSTATUS_NEW_DATA,
        }:
            error_code = int(status if status != _PDH_SUCCESS else result.status)
            raise ResourceSamplingUnavailable("PDH_COUNTER_DATA_INVALID", error_code)
        value = float(result.value.double_value)
        if value < 0 or value != value or value in {float("inf"), float("-inf")}:
            raise ResourceSamplingUnavailable("PDH_COUNTER_DATA_INVALID")
        return value
