import sqlalchemy as sa
from alembic import op

revision = "0009_download_pages"
down_revision = "0008_download_jobs"
branch_labels = None
depends_on = None

# Migration revisions are immutable snapshots and do not import current ORM code.
DOWNLOAD_PAGE_SHA256_CHECK = (
    "typeof(sha256) = 'text' "
    "AND length(sha256) = 64 "
    "AND sha256 = lower(sha256) "
    "AND sha256 NOT GLOB '*[^0-9a-f]*'"
)
DOWNLOAD_PAGE_TEMPORARY_PATH_CHECK = (
    "length(temporary_path) > 0 "
    "AND substr(temporary_path, 1, 1) <> '/' "
    "AND substr(temporary_path, -1, 1) <> '/' "
    "AND instr(temporary_path, char(92)) = 0 "
    "AND instr(temporary_path, ':') = 0 "
    "AND instr(temporary_path, '//') = 0 "
    "AND instr('/' || temporary_path || '/', '/../') = 0 "
    "AND instr('/' || temporary_path || '/', '/./') = 0 "
    "AND instr(temporary_path, char(0)) = 0 "
    "AND temporary_path NOT GLOB '*[<>:\"|?*]*'"
)


def upgrade() -> None:
    op.create_table(
        "download_pages",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("remote_reference", sa.Text(), nullable=False),
        sa.Column("temporary_path", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("width", sa.BigInteger(), nullable=True),
        sa.Column("height", sa.BigInteger(), nullable=True),
        sa.Column("mime", sa.Text(), nullable=True),
        sa.Column("attempts", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "typeof(ordinal) = 'integer' AND ordinal > 0",
            name="ordinal_positive_integer",
        ),
        sa.CheckConstraint(
            "typeof(remote_reference) = 'text' AND length(remote_reference) > 0",
            name="remote_reference_nonempty",
        ),
        sa.CheckConstraint(
            "typeof(status) = 'text' AND length(status) > 0",
            name="status_nonempty",
        ),
        sa.CheckConstraint(
            "temporary_path IS NULL OR (typeof(temporary_path) = 'text' AND "
            f"{DOWNLOAD_PAGE_TEMPORARY_PATH_CHECK})",
            name="temporary_path_safe",
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR (typeof(size_bytes) = 'integer' AND size_bytes >= 0)",
            name="size_bytes_nonnegative_integer",
        ),
        sa.CheckConstraint(
            f"sha256 IS NULL OR ({DOWNLOAD_PAGE_SHA256_CHECK})",
            name="sha256_valid",
        ),
        sa.CheckConstraint(
            "(width IS NULL AND height IS NULL) OR "
            "(typeof(width) = 'integer' AND width > 0 AND "
            "typeof(height) = 'integer' AND height > 0)",
            name="dimensions_positive_pair",
        ),
        sa.CheckConstraint(
            "mime IS NULL OR (typeof(mime) = 'text' AND length(mime) > 0)",
            name="mime_nonempty",
        ),
        sa.CheckConstraint(
            "typeof(attempts) = 'integer' AND attempts >= 0",
            name="attempts_nonnegative_integer",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["download_jobs.job_id"],
            name="fk_download_pages_job_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("job_id", "ordinal", name="pk_download_pages"),
    )
    op.create_index(
        "ux_download_pages_temporary_path",
        "download_pages",
        ["temporary_path"],
        unique=True,
    )
    op.create_index(
        "ix_download_pages_job_status",
        "download_pages",
        ["job_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_download_pages_job_status", table_name="download_pages")
    op.drop_index("ux_download_pages_temporary_path", table_name="download_pages")
    op.drop_table("download_pages")
