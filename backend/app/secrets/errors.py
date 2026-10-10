"""Sanitized failures emitted by the protected local secret vault."""

from enum import StrEnum

from app.errors import ErrorCode
from app.tracing import create_trace_id, current_trace_id


class SecretVaultOperation(StrEnum):
    STORE = "STORE"
    READ = "READ"
    REVOKE = "REVOKE"


_SECRET_ERROR_MESSAGES = {
    ErrorCode.AUTH_SECRET_CORRUPT: "El registro protegido no supera la validación.",
    ErrorCode.AUTH_SECRET_EXPIRED: "La credencial protegida ha vencido.",
    ErrorCode.AUTH_SECRET_NOT_FOUND: "No se encontró la credencial protegida.",
    ErrorCode.AUTH_SECRET_REVOKE_FAILED: "No se pudo revocar la credencial protegida.",
    ErrorCode.AUTH_SECRET_STORE_FAILED: "No se pudo guardar la credencial protegida.",
    ErrorCode.AUTH_SECRET_UNAVAILABLE: "La credencial protegida no está disponible en esta sesión.",
}
_SECRET_ERROR_RETRYABILITY = {
    ErrorCode.AUTH_SECRET_CORRUPT: False,
    ErrorCode.AUTH_SECRET_EXPIRED: False,
    ErrorCode.AUTH_SECRET_NOT_FOUND: False,
    ErrorCode.AUTH_SECRET_REVOKE_FAILED: True,
    ErrorCode.AUTH_SECRET_STORE_FAILED: True,
    ErrorCode.AUTH_SECRET_UNAVAILABLE: True,
}


class SecretVaultError(RuntimeError):
    """Secret failure with stable classification and no caller-controlled text."""

    def __init__(
        self,
        *,
        code: ErrorCode,
        operation: SecretVaultOperation,
        cause_type: str | None = None,
    ) -> None:
        if type(code) is not ErrorCode or code not in _SECRET_ERROR_MESSAGES:
            raise ValueError("code must belong to the secret vault error family")
        if type(operation) is not SecretVaultOperation:
            raise TypeError("operation must be a SecretVaultOperation")
        if cause_type is not None and (
            type(cause_type) is not str
            or not cause_type.isascii()
            or not cause_type.replace("_", "").isalnum()
            or len(cause_type) > 64
        ):
            raise ValueError("cause_type must be a sanitized type name")

        self.code = code
        self.operation = operation
        self.retryable = _SECRET_ERROR_RETRYABILITY[code]
        self.trace_id = current_trace_id() or create_trace_id()
        self.cause_type = cause_type
        super().__init__(_SECRET_ERROR_MESSAGES[code])
