import sqlalchemy as sa
from alembic import op

revision = "0019_usb_export_history"
down_revision = "0018_chapter_cbz_assets"
branch_labels = None
depends_on = None

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
_SHA256_CHECK = (
    "typeof({column}) = 'text' "
    "AND length({column}) = 64 "
    "AND {column} = lower({column}) "
    "AND {column} NOT GLOB '*[^0-9a-f]*'"
)


def upgrade() -> None:
    op.create_table(
        "usb_export_jobs",
        sa.Column("export_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("target_label", sa.String(length=128), nullable=False),
        sa.Column("file_system", sa.String(length=32), nullable=False),
        sa.Column("destination_name", sa.String(length=255), nullable=False),
        sa.Column("expected_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at_us", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_us", sa.BigInteger(), nullable=False),
        sa.Column("started_at_us", sa.BigInteger(), nullable=True),
        sa.Column("terminal_at_us", sa.BigInteger(), nullable=True),
        sa.Column("trace_id", sa.String(length=36), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("failure_stage", sa.String(length=64), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("cause_type", sa.String(length=64), nullable=True),
        sa.CheckConstraint(_UUID4_CHECK.format(column="export_id"), name="export_id_uuid4"),
        sa.CheckConstraint(_UUID4_CHECK.format(column="asset_id"), name="asset_id_uuid4"),
        sa.CheckConstraint(_UUID4_CHECK.format(column="trace_id"), name="trace_id_uuid4"),
        sa.CheckConstraint(
            _SHA256_CHECK.format(column="idempotency_key_hash"),
            name="idempotency_key_hash_sha256",
        ),
        sa.CheckConstraint(
            _SHA256_CHECK.format(column="request_fingerprint"),
            name="request_fingerprint_sha256",
        ),
        sa.CheckConstraint(
            _SHA256_CHECK.format(column="expected_sha256"),
            name="expected_sha256_valid",
        ),
        sa.CheckConstraint(
            "length(target_id) BETWEEN 1 AND 128 AND instr(target_id, char(0)) = 0",
            name="target_id_bounded",
        ),
        sa.CheckConstraint(
            "length(target_label) BETWEEN 1 AND 128 AND instr(target_label, char(0)) = 0",
            name="target_label_bounded",
        ),
        sa.CheckConstraint(
            "length(file_system) BETWEEN 1 AND 32 AND instr(file_system, char(0)) = 0",
            name="file_system_bounded",
        ),
        sa.CheckConstraint(
            "length(destination_name) BETWEEN 1 AND 255 "
            "AND instr(destination_name, '/') = 0 "
            "AND instr(destination_name, char(92)) = 0 "
            "AND instr(destination_name, ':') = 0 "
            "AND destination_name NOT IN ('.', '..')",
            name="destination_name_is_basename",
        ),
        sa.CheckConstraint(
            "typeof(expected_size_bytes) = 'integer' "
            "AND expected_size_bytes BETWEEN 1 AND 2147483648",
            name="expected_size_bounded_integer",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'EXPORTING', 'EXPORTED', 'SKIPPED', 'FAILED', 'INTERRUPTED')",
            name="status_supported",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "(status IN ('QUEUED', 'EXPORTING') AND terminal_at_us IS NULL) "
            "OR (status IN ('EXPORTED', 'SKIPPED', 'FAILED', 'INTERRUPTED') "
            "AND typeof(terminal_at_us) = 'integer')",
            name="terminal_timestamp_matches_status",
        ),
        sa.CheckConstraint(
            "(status IN ('FAILED', 'INTERRUPTED') "
            "AND error_code IS NOT NULL AND failure_stage IS NOT NULL "
            "AND retryable IS NOT NULL AND cause_type IS NOT NULL) "
            "OR (status NOT IN ('FAILED', 'INTERRUPTED') "
            "AND error_code IS NULL AND failure_stage IS NULL "
            "AND retryable IS NULL AND cause_type IS NULL)",
            name="failure_details_match_status",
        ),
        sa.CheckConstraint(
            "(status = 'INTERRUPTED' AND error_code = 'SYS_STARTUP_FAILED') "
            "OR (status = 'FAILED' AND error_code IN ("
            "'EXP_COPY_FAILED', 'EXP_DESTINATION_CONFLICT', 'EXP_DEVICE_DISCONNECTED', "
            "'EXP_DISK_FULL', 'EXP_HASH_MISMATCH', 'EXP_PUBLISH_FAILED', 'EXP_SOURCE_INVALID')) "
            "OR (status NOT IN ('FAILED', 'INTERRUPTED') AND error_code IS NULL)",
            name="error_code_matches_status",
        ),
        sa.CheckConstraint(
            "failure_stage IS NULL OR length(failure_stage) BETWEEN 1 AND 64",
            name="failure_stage_bounded",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR length(error_code) BETWEEN 1 AND 64",
            name="error_code_bounded",
        ),
        sa.CheckConstraint(
            "cause_type IS NULL OR length(cause_type) BETWEEN 1 AND 64",
            name="cause_type_bounded",
        ),
        sa.CheckConstraint(
            "retryable IS NULL OR (typeof(retryable) = 'integer' AND retryable IN (0, 1))",
            name="retryable_boolean",
        ),
        sa.PrimaryKeyConstraint("export_id", name="pk_usb_export_jobs"),
    )
    op.create_index(
        "uq_usb_export_jobs_idempotency_key_hash",
        "usb_export_jobs",
        ["idempotency_key_hash"],
        unique=True,
    )
    op.create_index(
        "ix_usb_export_jobs_retention",
        "usb_export_jobs",
        ["status", "terminal_at_us", "created_at_us", "export_id"],
    )
    op.create_index(
        "ix_usb_export_jobs_page",
        "usb_export_jobs",
        ["created_at_us", "export_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.exec_driver_sql("SELECT 1 FROM usb_export_jobs LIMIT 1").first() is not None:
        raise RuntimeError("USB export history downgrade is blocked while job evidence exists.")
    op.drop_table("usb_export_jobs")
