from pathlib import Path

from app.errors import ApiErrorBody, ApiErrorEnvelope, ErrorCode

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_base_error_codes_are_unique_and_documented() -> None:
    codes = [error_code.value for error_code in ErrorCode]
    catalog = (PROJECT_ROOT / "docs" / "ERROR_CODES.md").read_text(encoding="utf-8")

    assert len(codes) == len(set(codes))
    assert set(codes) == {
        "API_REQUEST_REJECTED",
        "API_VALIDATION_FAILED",
        "AUTH_SECRET_CORRUPT",
        "AUTH_SECRET_EXPIRED",
        "AUTH_SECRET_NOT_FOUND",
        "AUTH_SECRET_REVOKE_FAILED",
        "AUTH_SECRET_STORE_FAILED",
        "AUTH_SECRET_UNAVAILABLE",
        "DB_BACKUP_FAILED",
        "DB_BUSY_TIMEOUT",
        "DB_CONNECTION_FAILED",
        "DB_INTEGRITY_FAILED",
        "DB_MAINTENANCE_FAILED",
        "DB_MIGRATION_FAILED",
        "DL_JOB_RECOVERY_BLOCKED",
        "DL_JOB_RECOVERY_CONFLICT",
        "DL_MANIFEST_PERSIST_FAILED",
        "DL_MANIFEST_STALE_SNAPSHOT",
        "DL_TEMPORARY_BUDGET_EXCEEDED",
        "DL_TEMPORARY_PATH_CONFLICT",
        "DL_TEMPORARY_PATH_UNAVAILABLE",
        "EXP_COPY_FAILED",
        "EXP_DESTINATION_CONFLICT",
        "EXP_DEVICE_DISCONNECTED",
        "EXP_DISK_FULL",
        "EXP_HASH_MISMATCH",
        "EXP_PUBLISH_FAILED",
        "EXP_SOURCE_INVALID",
        "IMG_THUMBNAIL_FAILED",
        "LIB_PATH_UNAVAILABLE",
        "SRC_CAPABILITY_UNSUPPORTED",
        "SRC_CIRCUIT_OPEN",
        "SRC_CONNECTOR_FAILED",
        "SRC_SCHEMA_CHANGED",
        "SYS_INTERNAL_ERROR",
        "SYS_STARTUP_FAILED",
        "SYS_INVALID_CONFIG",
        "SYS_DISK_BUDGET_EXCEEDED",
        "SYS_RESOURCE_MONITOR_UNAVAILABLE",
    }
    for code in codes:
        assert code in catalog


def test_api_error_envelope_matches_the_approved_contract() -> None:
    response = ApiErrorEnvelope(
        error=ApiErrorBody(
            code=ErrorCode.API_VALIDATION_FAILED,
            message="Request does not match the published schema.",
            trace_id="trace-123",
            retryable=False,
        )
    )

    assert response.model_dump(mode="json", exclude_none=True) == {
        "error": {
            "code": "API_VALIDATION_FAILED",
            "message": "Request does not match the published schema.",
            "trace_id": "trace-123",
            "retryable": False,
        }
    }
