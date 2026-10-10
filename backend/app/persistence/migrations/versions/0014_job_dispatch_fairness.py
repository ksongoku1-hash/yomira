from alembic import op

revision = "0014_job_dispatch_fairness"
down_revision = "0013_metadata_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_download_jobs_dispatch_source",
        "download_jobs",
        ["chapter_source_id", "status", "priority", "created_at_us", "job_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_download_jobs_dispatch_source", table_name="download_jobs")
