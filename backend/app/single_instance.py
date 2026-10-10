import ctypes
import os
from ctypes import wintypes
from time import sleep

INSTANCE_MUTEX_NAME = r"Local\Yomira.V1.Backend"
ERROR_ALREADY_EXISTS = 183
MUTEX_ACQUIRE_ATTEMPTS = 10
MUTEX_ACQUIRE_RETRY_SECONDS = 0.01


class InstanceAlreadyRunningError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Another Yomira backend instance is already running.")


class WindowsMutexGuard:
    def __init__(self, mutex_name: str = INSTANCE_MUTEX_NAME) -> None:
        self._mutex_name = mutex_name
        self._handle: int | None = None

    def acquire(self) -> None:
        if os.name != "nt":
            raise OSError("Yomira V1 single-instance guard requires Windows.")
        if self._handle is not None:
            return

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        create_mutex.restype = wintypes.HANDLE

        for attempt in range(MUTEX_ACQUIRE_ATTEMPTS):
            ctypes.set_last_error(0)
            handle = create_mutex(None, False, self._mutex_name)
            error_code = ctypes.get_last_error()
            if handle is None:
                raise OSError(error_code, "CreateMutexW failed.")
            if error_code == ERROR_ALREADY_EXISTS:
                self._close_handle(handle)
                if attempt + 1 < MUTEX_ACQUIRE_ATTEMPTS:
                    sleep(MUTEX_ACQUIRE_RETRY_SECONDS)
                    continue
                raise InstanceAlreadyRunningError()

            self._handle = handle
            return

    def release(self) -> None:
        if self._handle is None:
            return

        handle = self._handle
        self._close_handle(handle)
        self._handle = None

    @staticmethod
    def _close_handle(handle: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        if not close_handle(handle):
            error_code = ctypes.get_last_error()
            raise OSError(error_code, "CloseHandle failed.")


def create_instance_guard() -> WindowsMutexGuard:
    return WindowsMutexGuard()
