import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.platform.windows_process_job import WindowsChildJob

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows job lifecycle")


def test_closing_job_stops_its_child() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        job = WindowsChildJob(process.pid)
        assert process.poll() is None
        job.close()
        job.close()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_child_dies_when_parent_is_terminated(tmp_path: Path) -> None:
    marker = tmp_path / "child-pid.txt"
    script = """
import subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from app.platform.windows_process_job import WindowsChildJob
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                         creationflags=subprocess.CREATE_NO_WINDOW)
job = WindowsChildJob(child.pid)
Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(60)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", script, str(marker), str(Path(__file__).resolve().parents[1])],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    handle = None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            assert parent.poll() is None
            time.sleep(0.05)
        assert marker.exists()
        handle = kernel.OpenProcess(0x100000, False, int(marker.read_text()))
        assert handle
        parent.terminate()
        parent.wait(timeout=5)
        assert kernel.WaitForSingleObject(handle, 5000) == 0
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        if handle:
            kernel.CloseHandle(handle)
