import json
import logging
from pathlib import Path

import pytest

from app.logging_config import configure_logging


def test_configured_rotation_keeps_only_the_requested_total_file_count(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "logs" / "yomira.log"
    configure_logging(
        "INFO",
        log_file_path=log_path,
        max_bytes=512,
        file_count=3,
    )
    logger = logging.getLogger("yomira.rotation-test")
    for sequence in range(40):
        logger.info("rotation event %03d %s", sequence, "x" * 90)
    for handler in logging.getLogger("yomira").handlers:
        handler.flush()

    log_files = tuple(log_path.parent.glob("yomira.log*"))
    assert len(log_files) <= 3
    assert len(log_files) >= 2
    assert sum(path.stat().st_size for path in log_files) <= 3 * 512
    assert all(
        json.loads(line)["component"] == "rotation-test"
        for path in log_files
        for line in path.read_text(encoding="utf-8").splitlines()
    )
    configure_logging("INFO")


def test_file_log_contains_no_private_paths_or_exception_messages(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "logs" / "yomira.log"
    configure_logging("INFO", log_file_path=log_path)
    logger = logging.getLogger("yomira.privacy-test")
    try:
        raise RuntimeError("secret-token-and-private-folder")
    except RuntimeError:
        logger.exception("diagnostic event")
    for handler in logging.getLogger("yomira").handlers:
        handler.flush()
    configure_logging("INFO")

    content = log_path.read_text(encoding="utf-8")
    event = json.loads(content)
    assert event["message"] == "diagnostic event"
    assert event["exception_type"] == "RuntimeError"
    assert "secret-token-and-private-folder" not in content
    assert str(tmp_path) not in content


def test_file_write_failure_falls_back_without_interrupting_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "logs" / "yomira.log"
    configure_logging("INFO", log_file_path=log_path)
    file_handler = next(
        handler
        for handler in logging.getLogger("yomira").handlers
        if hasattr(handler, "shouldRollover")
    )

    def fail_rotation(_record: logging.LogRecord) -> bool:
        raise PermissionError("private path must not reach logs")

    monkeypatch.setattr(file_handler, "shouldRollover", fail_rotation)
    logger = logging.getLogger("yomira.file-failure-test")
    logger.error("original operation failure")
    logger.info("subsequent operation continues")
    for handler in logging.getLogger("yomira").handlers:
        handler.flush()
    captured = capsys.readouterr()
    configure_logging("INFO")

    assert "logging.file_write_failed" in captured.err
    assert "private path must not reach logs" not in captured.err
    assert "subsequent operation continues" in captured.err
    assert "subsequent operation continues" not in log_path.read_text(encoding="utf-8")


def test_unavailable_log_directory_uses_console_without_changing_existing_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_directory = tmp_path / "logs"
    log_directory.write_text("preserve", encoding="utf-8")
    configure_logging("INFO", log_file_path=log_directory / "yomira.log")
    logging.getLogger("yomira.file-open-failure-test").info("console remains active")
    captured = capsys.readouterr()
    configure_logging("INFO")

    assert log_directory.read_text(encoding="utf-8") == "preserve"
    assert "logging.file_handler_unavailable" in captured.err
    assert "console remains active" in captured.err
    assert str(tmp_path) not in captured.err
