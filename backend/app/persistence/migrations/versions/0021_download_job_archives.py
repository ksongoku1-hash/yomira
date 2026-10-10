import sqlalchemy as sa
from alembic import op

revision = "0021_download_job_archives"
down_revision = "0020_home_recent_views"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "download_job_archives",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("archived_at_us", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "typeof(archived_at_us) = 'integer' AND archived_at_us >= 0",
            name="archived_at_us_nonnegative_integer",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["download_jobs.job_id"],
            name="fk_job_archives_job_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_download_job_archives"),
    )
    op.create_index(
        "ix_download_job_archives_archived_at",
        "download_job_archives",
        ["archived_at_us", "job_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_download_job_archives_archived_at", table_name="download_job_archives")
    op.drop_table("download_job_archives")
