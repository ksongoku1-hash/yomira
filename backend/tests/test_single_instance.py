import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from app.single_instance import InstanceAlreadyRunningError, WindowsMutexGuard


@pytest.mark.skipif(os.name != "nt", reason="The application target is Windows.")
def test_windows_mutex_rejects_duplicate_and_is_released() -> None:
    mutex_name = rf"Local\Yomira.V1.Test.{uuid4()}"
    first_guard = WindowsMutexGuard(mutex_name)
    second_guard = WindowsMutexGuard(mutex_name)

    try:
        first_guard.acquire()
        with pytest.raises(InstanceAlreadyRunningError):
            second_guard.acquire()
    finally:
        first_guard.release()
        second_guard.release()

    second_guard.acquire()
    second_guard.release()


@pytest.mark.skipif(os.name != "nt", reason="The application target is Windows.")
def test_windows_mutex_is_released_after_owner_process_is_terminated(
    tmp_path: Path,
) -> None:
    mutex_name = rf"Local\Yomira.V1.ProcessTest.{uuid4()}"
    marker = tmp_path / "mutex-ready"
    child_code = (
        "import sys,time; sys.path.insert(0,sys.argv[3]); from pathlib import Path; "
        "from app.single_instance import WindowsMutexGuard; "
        "guard=WindowsMutexGuard(sys.argv[1]); guard.acquire(); "
        "Path(sys.argv[2]).write_text('locked', encoding='utf-8'); time.sleep(60)"
    )
    backend_root = str(Path(__file__).resolve().parents[1])
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, mutex_name, str(marker), backend_root],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    contender = WindowsMutexGuard(mutex_name)

    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            if child.poll() is not None:
                _, child_error = child.communicate(timeout=5)
                error_text = child_error.decode(errors="replace")
                raise AssertionError(
                    "Child process exited before acquiring the mutex: " + error_text
                )
            time.sleep(0.05)

        assert marker.exists(), "Child process did not acquire the mutex within 10 seconds."
        with pytest.raises(InstanceAlreadyRunningError):
            contender.acquire()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)

    contender.acquire()
    contender.release()
