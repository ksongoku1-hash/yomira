import sqlalchemy as sa
from alembic import op

revision = "0017_job_recovery_checkpoint"
down_revision = "0016_temporary_path_key"
branch_labels = None
depends_on = None

MAX_MANIFEST_BYTES = 8 * 1024 * 1024


def upgrade() -> None:
    op.execute(
        "ALTER TABLE download_jobs ADD COLUMN interrupted_from_status TEXT NULL "
        "CONSTRAINT ck_download_jobs_interrupted_from_status_supported CHECK ("
        "interrupted_from_status IS NULL OR ("
        "typeof(interrupted_from_status) = 'text' AND interrupted_from_status IN ("
        "'FETCHING_METADATA', 'DOWNLOADING', 'VALIDATING', 'PROCESSING', "
        "'PACKAGING', 'VERIFYING', 'INDEXING')))"
    )
    op.execute(
        "ALTER TABLE download_jobs ADD COLUMN recovery_blocked INTEGER NOT NULL DEFAULT 0 "
        "CONSTRAINT ck_download_jobs_recovery_blocked_boolean CHECK ("
        "typeof(recovery_blocked) = 'integer' AND recovery_blocked IN (0, 1))"
    )
    op.create_table(
        "download_manifest_state",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("previous_sha256", sa.String(length=64), nullable=True),
        sa.Column("publication_pending", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "typeof(generation) = 'integer' "
            "AND generation BETWEEN 1 AND 9223372036854775807",
            name="generation_positive_sqlite_integer",
        ),
        sa.CheckConstraint(
            "typeof(manifest_sha256) = 'text' "
            "AND length(manifest_sha256) = 64 "
            "AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="manifest_sha256_valid",
        ),
        sa.CheckConstraint(
            "typeof(manifest_size_bytes) = 'integer' "
            f"AND manifest_size_bytes BETWEEN 1 AND {MAX_MANIFEST_BYTES}",
            name="manifest_size_bounded_integer",
        ),
        sa.CheckConstraint(
            "previous_sha256 IS NULL OR ("
            "typeof(previous_sha256) = 'text' "
            "AND length(previous_sha256) = 64 "
            "AND previous_sha256 NOT GLOB '*[^0-9a-f]*')",
            name="previous_sha256_valid",
        ),
        sa.CheckConstraint(
            "typeof(publication_pending) = 'integer' "
            "AND publication_pending IN (0, 1)",
            name="publication_pending_boolean",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["download_jobs.job_id"],
            name="fk_download_manifest_state_job_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_download_manifest_state"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    manifest_state_exists = connection.exec_driver_sql(
        "SELECT 1 FROM download_manifest_state LIMIT 1"
    ).first()
    checkpoint_data_exists = connection.exec_driver_sql(
        "SELECT 1 FROM download_jobs "
        "WHERE interrupted_from_status IS NOT NULL OR recovery_blocked <> 0 LIMIT 1"
    ).first()
    if manifest_state_exists is not None or checkpoint_data_exists is not None:
        raise RuntimeError(
            "Job recovery checkpoint downgrade is blocked while recovery evidence exists."
        )

    op.drop_table("download_manifest_state")
    op.drop_column("download_jobs", "recovery_blocked")
    op.drop_column("download_jobs", "interrupted_from_status")
