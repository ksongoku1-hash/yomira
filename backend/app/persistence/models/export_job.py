from sqlalchemy import BigInteger, Boolean, CheckConstraint, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase

_UUID4_CHECK = (
    "typeof({column}) = 'text' "
    "AND length({column}) = 36 "
    "AND length(replace({column}, '-', '')) = 32 "
    "AND substr({column}, 9, 1) = '-' "
    "AND substr({column}, 14, 1) = '-' "
    "AND substr({column}, 19, 1) = '-' "
    "AND substr({column}, 24, 1) = '-' "
    "AND substr({column}, 15, 1) = '4' "
    "AND substr({column}, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND {column} = lower({column}) "
    "AND {column} NOT GLOB '*[^0-9a-f-]*'"
)
_SHA256_CHECK_TEMPLATE = (
    "typeof({column}) = 'text' "
    "AND length({column}) = 64 "
    "AND {column} = lower({column}) "
    "AND {column} NOT GLOB '*[^0-9a-f]*'"
)


class ExportJobRow(PersistenceBase):
    __tablename__ = "usb_export_jobs"
    __table_args__ = (
        CheckConstraint(_UUID4_CHECK.format(column="export_id"), name="export_id_uuid4"),
        CheckConstraint(_UUID4_CHECK.format(column="asset_id"), name="asset_id_uuid4"),
        CheckConstraint(_UUID4_CHECK.format(column="trace_id"), name="trace_id_uuid4"),
        CheckConstraint(
            _SHA256_CHECK_TEMPLATE.format(column="idempotency_key_hash"),
            name="idempotency_key_hash_sha256",
        ),
        CheckConstraint(
            _SHA256_CHECK_TEMPLATE.format(column="request_fingerprint"),
            name="request_fingerprint_sha256",
        ),
        CheckConstraint(
            _SHA256_CHECK_TEMPLATE.format(column="expected_sha256"),
            name="expected_sha256_valid",
        ),
        CheckConstraint(
            "length(target_id) BETWEEN 1 AND 128 AND instr(target_id, char(0)) = 0",
            name="target_id_bounded",
        ),
        CheckConstraint(
            "length(target_label) BETWEEN 1 AND 128 AND instr(target_label, char(0)) = 0",
            name="target_label_bounded",
        ),
        CheckConstraint(
            "length(file_system) BETWEEN 1 AND 32 AND instr(file_system, char(0)) = 0",
            name="file_system_bounded",
        ),
        CheckConstraint(
            "length(destination_name) BETWEEN 1 AND 255 "
            "AND instr(destination_name, '/') = 0 "
            "AND instr(destination_name, char(92)) = 0 "
            "AND instr(destination_name, ':') = 0 "
            "AND destination_name NOT IN ('.', '..')",
            name="destination_name_is_basename",
        ),
        CheckConstraint(
            "typeof(expected_size_bytes) = 'integer' "
            "AND expected_size_bytes BETWEEN 1 AND 8589934592",
            name="expected_size_bounded_integer",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'EXPORTING', 'EXPORTED', 'SKIPPED', 'FAILED', 'INTERRUPTED')",
            name="status_supported",
        ),
        CheckConstraint(
            "typeof(created_at_us) = 'integer' "
            "AND typeof(updated_at_us) = 'integer' "
            "AND (started_at_us IS NULL OR typeof(started_at_us) = 'integer') "
            "AND (terminal_at_us IS NULL OR typeof(terminal_at_us) = 'integer') "
            "AND updated_at_us >= created_at_us "
            "AND (started_at_us IS NULL OR started_at_us BETWEEN created_at_us AND updated_at_us) "
            "AND (terminal_at_us IS NULL OR terminal_at_us "
            "BETWEEN created_at_us AND updated_at_us)",
            name="timestamps_ordered_integer",
        ),
        CheckConstraint(
            "(status IN ('QUEUED', 'EXPORTING') AND terminal_at_us IS NULL) "
            "OR (status IN ('EXPORTED', 'SKIPPED', 'FAILED', 'INTERRUPTED') "
            "AND typeof(terminal_at_us) = 'integer')",
            name="terminal_timestamp_matches_status",
        ),
        CheckConstraint(
            "(status IN ('FAILED', 'INTERRUPTED') "
            "AND error_code IS NOT NULL AND failure_stage IS NOT NULL "
            "AND retryable IS NOT NULL AND cause_type IS NOT NULL) "
            "OR (status NOT IN ('FAILED', 'INTERRUPTED') "
            "AND error_code IS NULL AND failure_stage IS NULL "
            "AND retryable IS NULL AND cause_type IS NULL)",
            name="failure_details_match_status",
        ),
        CheckConstraint(
            "(status = 'INTERRUPTED' AND error_code = 'SYS_STARTUP_FAILED') "
            "OR (status = 'FAILED' AND error_code IN ("
            "'EXP_COPY_FAILED', 'EXP_DESTINATION_CONFLICT', 'EXP_DEVICE_DISCONNECTED', "
            "'EXP_DISK_FULL', 'EXP_HASH_MISMATCH', 'EXP_PUBLISH_FAILED', 'EXP_SOURCE_INVALID')) "
            "OR (status NOT IN ('FAILED', 'INTERRUPTED') AND error_code IS NULL)",
            name="error_code_matches_status",
        ),
        CheckConstraint(
            "failure_stage IS NULL OR length(failure_stage) BETWEEN 1 AND 64",
            name="failure_stage_bounded",
        ),
        CheckConstraint(
            "error_code IS NULL OR length(error_code) BETWEEN 1 AND 64",
            name="error_code_bounded",
        ),
        CheckConstraint(
            "cause_type IS NULL OR length(cause_type) BETWEEN 1 AND 64",
            name="cause_type_bounded",
        ),
        CheckConstraint(
            "retryable IS NULL OR (typeof(retryable) = 'integer' AND retryable IN (0, 1))",
            name="retryable_boolean",
        ),
        Index(
            "uq_usb_export_jobs_idempotency_key_hash",
            "idempotency_key_hash",
            unique=True,
        ),
        Index(
            "ix_usb_export_jobs_retention",
            "status",
            "terminal_at_us",
            "created_at_us",
            "export_id",
        ),
        Index("ix_usb_export_jobs_page", "created_at_us", "export_id"),
    )

    export_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_label: Mapped[str] = mapped_column(String(128), nullable=False)
    file_system: Mapped[str] = mapped_column(String(32), nullable=False)
    destination_name: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_size_bytes: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    updated_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    started_at_us: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    terminal_at_us: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    cause_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
