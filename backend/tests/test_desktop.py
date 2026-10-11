import io
from collections.abc import Iterator
from pathlib import Path

from pytest import MonkeyPatch

from app import desktop


def test_browser_waits_for_readiness_and_opens_once(monkeypatch: MonkeyPatch) -> None:
    states = iter([False, False, True])
    opened: list[str] = []
    def ready(_origin: str) -> bool:
        return next(states)

    def no_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(desktop, "interface_is_ready", ready)
    monkeypatch.setattr(desktop, "sleep", no_sleep)
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    desktop.open_when_ready("http://127.0.0.1:8123")
    assert opened == ["http://127.0.0.1:8123/library"]


def test_timeout_does_not_open_browser(monkeypatch: MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    desktop.open_when_ready("http://127.0.0.1:8123", timeout=0)
    assert opened == []


def test_other_server_or_not_ready_is_rejected(monkeypatch: MonkeyPatch) -> None:
    for version, readiness in [
        (b'{}', b'{"status":"ready"}'),
        (b'{"api_version":1,"app_version":"0.1.10"}', b'{"status":"not_ready"}'),
        (b'not json', b'{"status":"ready"}'),
    ]:
        replies = iter([version, readiness])

        def fake_urlopen(
            _url: str, timeout: float = 1, responses: Iterator[bytes] = replies
        ) -> io.BytesIO:
            return io.BytesIO(next(responses))

        monkeypatch.setattr(desktop, "urlopen", fake_urlopen)
        assert not desktop.interface_is_ready("http://127.0.0.1:8123")


def test_bundled_motor_preserves_explicit_configuration(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    motor = tmp_path / "Motor" / "fmd.exe"
    motor.parent.mkdir()
    motor.touch()
    monkeypatch.delenv("YOMIRA_FMD2_EXECUTABLE", raising=False)
    desktop.configure_bundled_motor(tmp_path / "Yomira.exe")
    assert desktop.os.environ["YOMIRA_FMD2_EXECUTABLE"] == str(motor)
    monkeypatch.setenv("YOMIRA_FMD2_EXECUTABLE", "custom.exe")
    desktop.configure_bundled_motor(tmp_path / "Yomira.exe")
    assert desktop.os.environ["YOMIRA_FMD2_EXECUTABLE"] == "custom.exe"
