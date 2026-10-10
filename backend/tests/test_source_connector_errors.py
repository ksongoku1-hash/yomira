from uuid import UUID

import pytest

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.errors import ErrorCode
from app.tracing import activate_trace_id, reset_trace_id


@pytest.mark.parametrize(
    ("code_name", "expected_retryable"),
    [
        ("SRC_CAPABILITY_UNSUPPORTED", False),
        ("SRC_SCHEMA_CHANGED", False),
        ("SRC_CIRCUIT_OPEN", True),
    ],
)
def test_source_connector_error_uses_catalogued_retry_policy(
    code_name: str,
    expected_retryable: bool,
) -> None:
    code = ErrorCode[code_name]
    error = SourceConnectorError(code=code, operation=SourceConnectorOperation.SEARCH)

    assert error.code is code
    assert error.retryable is expected_retryable
    assert error.operation is SourceConnectorOperation.SEARCH


@pytest.mark.parametrize("retryable", [False, True])
def test_unknown_connector_failure_requires_explicit_retry_classification(
    retryable: bool,
) -> None:
    error = SourceConnectorError(
        code=ErrorCode.SRC_CONNECTOR_FAILED,
        operation=SourceConnectorOperation.GET_PAGES,
        retryable=retryable,
    )

    assert error.retryable is retryable


def test_source_connector_error_preserves_trace_and_cause_without_exposing_cause_text() -> None:
    trace_id = "3b7b9d33-3f0a-4f5b-8f77-7c3e20ad2344"
    token = activate_trace_id(trace_id)
    cause = ValueError("cookie=private-fixture-value")
    try:
        try:
            raise SourceConnectorError(
                code=ErrorCode.SRC_CONNECTOR_FAILED,
                operation=SourceConnectorOperation.GET_SERIES,
                retryable=False,
            ) from cause
        except SourceConnectorError as error:
            assert error.trace_id == trace_id
            assert error.__cause__ is cause
            assert "private-fixture-value" not in str(error)
            assert error.code is ErrorCode.SRC_CONNECTOR_FAILED
    finally:
        reset_trace_id(token)


def test_source_connector_error_generates_trace_when_no_operation_context_exists() -> None:
    error = SourceConnectorError(
        code=ErrorCode.SRC_SCHEMA_CHANGED,
        operation=SourceConnectorOperation.GET_CHAPTERS,
    )

    assert UUID(error.trace_id).version == 4


def test_source_connector_error_rejects_non_source_code_and_invalid_retry_policy() -> None:
    with pytest.raises(ValueError, match="code must belong to the SRC family"):
        SourceConnectorError(
            code=ErrorCode.DB_BUSY_TIMEOUT,
            operation=SourceConnectorOperation.SEARCH,
        )

    with pytest.raises(ValueError, match="retryable is fixed for this code"):
        SourceConnectorError(
            code=ErrorCode.SRC_SCHEMA_CHANGED,
            operation=SourceConnectorOperation.SEARCH,
            retryable=True,
        )


def test_global_error_catalog_contains_source_connector_codes() -> None:
    codes = {code.value for code in ErrorCode}

    assert {
        "SRC_CAPABILITY_UNSUPPORTED",
        "SRC_CIRCUIT_OPEN",
        "SRC_CONNECTOR_FAILED",
        "SRC_SCHEMA_CHANGED",
    } <= codes
