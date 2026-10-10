import json
import logging
import sys

from app.logging_config import JsonLogFormatter, configure_logging
from app.tracing import activate_trace_id, reset_trace_id


def test_formatter_emits_bounded_json_with_trace_and_request_fields() -> None:
    record = logging.LogRecord(
        name="yomira.api",
        level=logging.INFO,
        pathname="C:/Users/private-user/backend/app/factory.py",
        lineno=10,
        msg="api.request.completed",
        args=(),
        exc_info=None,
    )
    record.trace_id = "f4d4c9d4-7f3a-4b01-9db4-60e1f61ba274"
    record.method = "GET"
    record.status_code = 200
    record.duration = 0.012345
    record.message_data = "not serialized"

    serialized = JsonLogFormatter().format(record)
    event = json.loads(serialized)

    assert event["level"] == "info"
    assert event["component"] == "api"
    assert event["trace_id"] == "f4d4c9d4-7f3a-4b01-9db4-60e1f61ba274"
    assert event["method"] == "GET"
    assert event["status_code"] == 200
    assert event["duration"] == 0.012345
    assert event["timestamp"].endswith("Z")
    assert "private-user" not in serialized
    assert "message_data" not in event


def test_formatter_records_exception_type_and_bounded_frames_without_message() -> None:
    try:
        raise RuntimeError("private-secret")
    except RuntimeError:
        exception_info = sys.exc_info()

    record = logging.LogRecord(
        name="yomira.api",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="api.unhandled_exception",
        args=(),
        exc_info=exception_info,
    )

    serialized = JsonLogFormatter().format(record)
    event = json.loads(serialized)

    assert event["exception_type"] == "RuntimeError"
    assert len(event["stack"]) <= 12
    assert "private-secret" not in serialized
    assert all(
        "file" in frame and "line" in frame and "function" in frame
        for frame in event["stack"]
    )


def test_formatter_uses_context_trace_when_record_has_none() -> None:
    trace_id = "f4d4c9d4-7f3a-4b01-9db4-60e1f61ba274"
    token = activate_trace_id(trace_id)
    try:
        record = logging.LogRecord(
            name="yomira.lifecycle",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="application.ready",
            args=(),
            exc_info=None,
        )
        event = json.loads(JsonLogFormatter().format(record))
    finally:
        reset_trace_id(token)

    assert event["trace_id"] == trace_id


def test_formatter_preserves_manifest_persistence_diagnostics() -> None:
    record = logging.LogRecord(
        name="yomira.manifest_store",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="manifest_store.operation_failed",
        args=(),
        exc_info=None,
    )
    record.cause_type = "PermissionError"
    record.path_utf16_units = 260

    event = json.loads(JsonLogFormatter().format(record))

    assert event["cause_type"] == "PermissionError"
    assert event["path_utf16_units"] == 260


def test_formatter_preserves_safe_connector_diagnostics_without_response_data() -> None:
    record = logging.LogRecord(
        name="yomira.connector.http_client",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="source.connector.http_request_rejected",
        args=(),
        exc_info=None,
    )
    record.__dict__.update(
        {
            "operation": "SEARCH",
            "connector_id": "yomira.fmd2.webtoons",
            "http_status": 403,
            "status_code": 502,
            "stage": "http_status_rejection",
            "reason": "NON_SUCCESS_HTTP_STATUS",
            "url": "https://source.example/?token=PRIVATE",
            "body": "PRIVATE response",
            "headers": {"cookie": "PRIVATE"},
        }
    )

    serialized = JsonLogFormatter().format(record)
    event = json.loads(serialized)

    assert event["operation"] == "SEARCH"
    assert event["connector_id"] == "yomira.fmd2.webtoons"
    assert event["http_status"] == 403
    assert event["status_code"] == 502
    assert event["stage"] == "http_status_rejection"
    assert event["reason"] == "NON_SUCCESS_HTTP_STATUS"
    assert "PRIVATE" not in serialized
    assert all(field not in event for field in ("url", "body", "headers"))


def test_formatter_preserves_temporary_path_migration_diagnostics() -> None:
    record = logging.LogRecord(
        name="yomira.database",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="database.temporary_path_migration_rejected",
        args=(),
        exc_info=None,
    )
    job_id = "00000000-0000-4000-8000-000000000001"
    conflicting_job_id = "00000000-0000-4000-8000-000000000002"
    record.__dict__.update(
        {
            "job_id": job_id,
            "ordinal": 7,
            "conflicting_job_id": conflicting_job_id,
            "conflicting_ordinal": 3,
            "byte_length": 4097,
            "utf16_code_units": 260,
        }
    )

    event = json.loads(JsonLogFormatter().format(record))

    assert event["job_id"] == job_id
    assert event["ordinal"] == 7
    assert event["conflicting_job_id"] == conflicting_job_id
    assert event["conflicting_ordinal"] == 3
    assert event["byte_length"] == 4097
    assert event["utf16_code_units"] == 260


def test_logging_configuration_is_idempotent() -> None:
    configure_logging("INFO")
    configure_logging("WARNING")

    assert len(logging.getLogger("yomira").handlers) == 1
    assert logging.getLogger("yomira").level == logging.WARNING
