import json
import logging
import stat
import sys
import traceback
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final

from app.tracing import current_trace_id

_OPTIONAL_FIELDS: Final[tuple[str, ...]] = (
    "error_code",
    "retryable",
    "job_id",
    "series_id",
    "chapter_id",
    "source_id",
    "connector_id",
    "operation",
    "stage",
    "attempt",
    "duration",
    "method",
    "status_code",
    "http_status",
    "invalid_fields",
    "resource_metric",
    "observed_value",
    "threshold_value",
    "reason",
    "action",
    "admission_allowed",
    "native_error_code",
    "cause_type",
    "failed_check",
    "path_utf16_units",
    "ordinal",
    "conflicting_job_id",
    "conflicting_ordinal",
    "byte_length",
    "utf16_code_units",
)
_MAX_MESSAGE_LENGTH: Final[int] = 512
_MAX_STACK_FRAMES: Final[int] = 12
_MAX_SERIALIZED_LOG_BYTES: Final[int] = 16 * 1024
DEFAULT_LOG_MAX_BYTES: Final[int] = 10 * 1024 * 1024
DEFAULT_LOG_FILE_COUNT: Final[int] = 5
MAX_LOG_MAX_BYTES: Final[int] = 25 * 1024 * 1024
MAX_LOG_FILE_COUNT: Final[int] = 10


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC)
        trace_id = getattr(record, "trace_id", None) or current_trace_id()
        component = record.name.removeprefix("yomira.")
        event: dict[str, object] = {
            "timestamp": timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "component": component,
            "trace_id": trace_id,
            "message": record.getMessage()[:_MAX_MESSAGE_LENGTH],
        }

        for field in _OPTIONAL_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                event[field] = value

        if record.exc_info is not None:
            exception_type, _, exception_traceback = record.exc_info
            if exception_type is not None:
                event["exception_type"] = exception_type.__name__
            if exception_traceback is not None:
                frames = traceback.extract_tb(exception_traceback)[-_MAX_STACK_FRAMES:]
                event["stack"] = [
                    {
                        "file": Path(frame.filename).name,
                        "function": frame.name,
                        "line": frame.lineno,
                    }
                    for frame in frames
                ]

        serialized = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) <= _MAX_SERIALIZED_LOG_BYTES:
            return serialized

        bounded_event: dict[str, object] = {
            "timestamp": event["timestamp"],
            "level": event["level"],
            "component": event["component"],
            "trace_id": event["trace_id"],
            "message": "log_record_truncated",
        }
        error_code = event.get("error_code")
        if isinstance(error_code, str) and len(error_code) <= 64:
            bounded_event["error_code"] = error_code
        stage = event.get("stage")
        if isinstance(stage, str) and len(stage) <= 64:
            bounded_event["stage"] = stage
        return json.dumps(bounded_event, ensure_ascii=False, separators=(",", ":"))


class _SafeRotatingFileHandler(RotatingFileHandler):
    def __init__(
        self,
        filename: Path,
        *,
        max_bytes: int,
        backup_count: int,
        fallback: logging.Handler,
    ) -> None:
        self._fallback = fallback
        self._disabled = False
        super().__init__(
            filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            errors="backslashreplace",
        )

    def emit(self, record: logging.LogRecord) -> None:
        if not self._disabled:
            super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        del record
        self._disabled = True
        exception_type = sys.exc_info()[0]
        failure = logging.LogRecord(
            name="yomira.logging",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="logging.file_write_failed",
            args=(),
            exc_info=None,
        )
        failure.stage = "file_log_write"
        if exception_type is not None:
            failure.cause_type = exception_type.__name__
        self._fallback.handle(failure)


def configure_logging(
    level: str,
    *,
    log_file_path: Path | None = None,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    file_count: int = DEFAULT_LOG_FILE_COUNT,
) -> None:
    if not 1 <= max_bytes <= MAX_LOG_MAX_BYTES:
        raise ValueError("max_bytes must be between 1 byte and 25 MiB")
    if not 2 <= file_count <= MAX_LOG_FILE_COUNT:
        raise ValueError("file_count must be between 2 and 10")

    formatter = JsonLogFormatter()
    target_loggers: list[logging.Logger] = []
    removed_handlers: dict[int, logging.Handler] = {}
    for logger_name in ("yomira", "uvicorn.error"):
        target_logger = logging.getLogger(logger_name)
        target_loggers.append(target_logger)
        for handler in tuple(target_logger.handlers):
            target_logger.removeHandler(handler)
            removed_handlers[id(handler)] = handler
        target_logger.setLevel(getattr(logging, level))
        target_logger.propagate = False

    for handler in removed_handlers.values():
        handler.close()

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    for target_logger in target_loggers:
        target_logger.addHandler(console_handler)

    if log_file_path is None:
        return

    try:
        log_file_path.parent.mkdir(exist_ok=True)
        directory_info = log_file_path.parent.lstat()
        directory_attributes = getattr(directory_info, "st_file_attributes", 0)
        if (
            stat.S_ISLNK(directory_info.st_mode)
            or bool(directory_attributes & 0x400)
            or not stat.S_ISDIR(directory_info.st_mode)
        ):
            raise OSError("The log directory is not a plain directory.")
        try:
            file_info = log_file_path.lstat()
        except FileNotFoundError:
            pass
        else:
            file_attributes = getattr(file_info, "st_file_attributes", 0)
            if (
                stat.S_ISLNK(file_info.st_mode)
                or bool(file_attributes & 0x400)
                or not stat.S_ISREG(file_info.st_mode)
            ):
                raise OSError("The log file is not a plain file.")
        file_handler = _SafeRotatingFileHandler(
            log_file_path,
            max_bytes=max_bytes,
            backup_count=file_count - 1,
            fallback=console_handler,
        )
    except OSError as error:
        logging.getLogger("yomira").error(
            "logging.file_handler_unavailable",
            extra={"stage": "file_log_open", "cause_type": type(error).__name__},
        )
        return

    file_handler.setFormatter(formatter)
    for target_logger in target_loggers:
        target_logger.addHandler(file_handler)
