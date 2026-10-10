from alembic import op

revision = "0015_job_cancellation_intent"
down_revision = "0014_job_dispatch_fairness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE download_jobs ADD COLUMN cancel_requested_at_us BIGINT NULL "
        "CONSTRAINT ck_download_jobs_cancel_requested_at_ordered_integer CHECK ("
        "cancel_requested_at_us IS NULL OR ("
        "typeof(cancel_requested_at_us) = 'integer' "
        "AND cancel_requested_at_us >= created_at_us "
        "AND cancel_requested_at_us <= updated_at_us))"
    )


def downgrade() -> None:
    connection = op.get_bind()
    cancellation_exists = connection.exec_driver_sql(
        "SELECT 1 FROM download_jobs "
        "WHERE cancel_requested_at_us IS NOT NULL LIMIT 1"
    ).first()
    if cancellation_exists is not None:
        raise RuntimeError(
            "Job cancellation intent downgrade is blocked while audit timestamps exist."
        )
    op.drop_column("download_jobs", "cancel_requested_at_us")
