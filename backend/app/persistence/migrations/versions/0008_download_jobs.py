import sqlalchemy as sa
from alembic import op

revision = "0008_download_jobs"
down_revision = "0007_assets"
branch_labels = None
depends_on = None

# Migration revisions are immutable snapshots and do not import current ORM code.
DOWNLOAD_JOB_ID_UUID4_CHECK = (
    "typeof(job_id) = 'text' "
    "AND length(job_id) = 36 "
    "AND length(replace(job_id, '-', '')) = 32 "
    "AND substr(job_id, 9, 1) = '-' "
    "AND substr(job_id, 14, 1) = '-' "
    "AND substr(job_id, 19, 1) = '-' "
    "AND substr(job_id, 24, 1) = '-' "
    "AND substr(job_id, 15, 1) = '4' "
    "AND substr(job_id, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND job_id = lower(job_id) "
    "AND job_id NOT GLOB '*[^0-9a-f-]*'"
)


def upgrade() -> None:
    op.create_table(
        "download_jobs",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("chapter_source_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False),
        sa.Column("created_at_us", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_us", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(DOWNLOAD_JOB_ID_UUID4_CHECK, name="job_id_uuid4"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'FETCHING_METADATA', 'DOWNLOADING', 'VALIDATING', "
            "'PROCESSING', 'PACKAGING', 'VERIFYING', 'INDEXING', 'COMPLETED', 'FAILED', "
            "'CANCELLED', 'INTERRUPTED')",
            name="status_supported",
        ),
        sa.CheckConstraint(
            "priority IN ('HIGH', 'NORMAL', 'LOW')",
            name="priority_supported",
        ),
        sa.CheckConstraint(
            "typeof(created_at_us) = 'integer' AND typeof(updated_at_us) = 'integer'",
            name="timestamps_integer",
        ),
        sa.ForeignKeyConstraint(
            ["chapter_source_id"],
            ["chapter_sources.chapter_source_id"],
            name="fk_download_jobs_chapter_source_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_download_jobs"),
    )
    op.create_index(
        "ix_download_jobs_chapter_source_id",
        "download_jobs",
        ["chapter_source_id"],
    )
    op.create_index("ix_download_jobs_status", "download_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_download_jobs_status", table_name="download_jobs")
    op.drop_index("ix_download_jobs_chapter_source_id", table_name="download_jobs")
    op.drop_table("download_jobs")
