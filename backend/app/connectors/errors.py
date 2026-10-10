"""Stable, sanitized failure types for source connector operations."""

from enum import StrEnum

from app.errors import ErrorCode
from app.tracing import create_trace_id, current_trace_id


class SourceConnectorOperation(StrEnum):
    HEALTH_CHECK = "HEALTH_CHECK"
    SEARCH = "SEARCH"
    GET_SERIES = "GET_SERIES"
    GET_CHAPTERS = "GET_CHAPTERS"
    GET_CHAPTER = "GET_CHAPTER"
    GET_PAGES = "GET_PAGES"
    RESOLVE_PAGE = "RESOLVE_PAGE"
    NORMALIZE_METADATA = "NORMALIZE_METADATA"


_SOURCE_ERROR_MESSAGES = {
    ErrorCode.SRC_CAPABILITY_UNSUPPORTED: "La fuente no admite esta operación.",
    ErrorCode.SRC_CIRCUIT_OPEN: "El conector de la fuente está pausado temporalmente.",
    ErrorCode.SRC_CONNECTOR_FAILED: "La operación del conector falló.",
    ErrorCode.SRC_SCHEMA_CHANGED: "La respuesta de la fuente ya no cumple el contrato.",
}
_FIXED_RETRYABILITY = {
    ErrorCode.SRC_CAPABILITY_UNSUPPORTED: False,
    ErrorCode.SRC_CIRCUIT_OPEN: True,
    ErrorCode.SRC_SCHEMA_CHANGED: False,
}


def _is_error_code(value: object) -> bool:
    return isinstance(value, ErrorCode)


def _is_operation(value: object) -> bool:
    return isinstance(value, SourceConnectorOperation)


class SourceConnectorError(RuntimeError):
    """Sanitized connector failure with code, operation, trace, and retry policy."""

    def __init__(
        self,
        *,
        code: ErrorCode,
        operation: SourceConnectorOperation,
        retryable: bool | None = None,
    ) -> None:
        if not _is_error_code(code):
            raise TypeError("code must be an ErrorCode")
        if code not in _SOURCE_ERROR_MESSAGES:
            raise ValueError("code must belong to the SRC family")
        if not _is_operation(operation):
            raise TypeError("operation must be a SourceConnectorOperation")
        if retryable is not None and type(retryable) is not bool:
            raise TypeError("retryable must be a boolean or None")

        if code is ErrorCode.SRC_CONNECTOR_FAILED:
            if retryable is None:
                raise ValueError("retryable must be classified for SRC_CONNECTOR_FAILED")
            resolved_retryable = retryable
        else:
            resolved_retryable = _FIXED_RETRYABILITY[code]
            if retryable is not None and retryable is not resolved_retryable:
                raise ValueError("retryable is fixed for this code")

        self.code = code
        self.operation = operation
        self.retryable = resolved_retryable
        self.trace_id = current_trace_id() or create_trace_id()
        super().__init__(_SOURCE_ERROR_MESSAGES[code])
