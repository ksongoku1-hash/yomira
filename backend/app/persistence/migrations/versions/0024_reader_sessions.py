"""Keep online reading plans and verified temporary pages outside the library."""

import sqlalchemy as sa
from alembic import op

revision = "0024_reader_sessions"
down_revision = "0023_whole_manga_export_size"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reader_sessions",
        sa.Column("session_id", sa.String(36), primary_key=True),
        sa.Column("chapter_id", sa.String(36), nullable=False),
        sa.Column("chapter_source_id", sa.String(36), nullable=False),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("connector_version", sa.Text(), nullable=False),
        sa.Column("remote_reference", sa.Text(), nullable=False),
        sa.Column("generation", sa.String(36), nullable=True),
        sa.Column("page_count", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("native_released", sa.Boolean(), nullable=False),
        sa.Column("last_access_us", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "typeof(page_count) = 'integer' AND page_count BETWEEN 0 AND 10000",
            name="page_count_bounded",
        ),
        sa.CheckConstraint(
            "state IN ('acquiring','complete','interrupted','failed','evicting','evicted')",
            name="state_supported",
        ),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.chapter_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["chapter_source_id"], ["chapter_sources.chapter_source_id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ux_reader_sessions_live_chapter",
        "reader_sessions",
        ["chapter_id"],
        unique=True,
        sqlite_where=sa.text("state <> 'evicted'"),
    )
    op.create_index("ix_reader_sessions_lru", "reader_sessions", ["last_access_us"])
    op.create_table(
        "reader_session_pages",
        sa.Column("session_id", sa.String(36), primary_key=True),
        sa.Column("ordinal", sa.BigInteger(), primary_key=True),
        sa.Column("remote_reference", sa.Text(), nullable=False),
        sa.Column("attempt", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("mime", sa.Text(), nullable=True),
        sa.Column("width", sa.BigInteger(), nullable=True),
        sa.Column("height", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"], ["reader_sessions.session_id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "typeof(ordinal) = 'integer' AND ordinal BETWEEN 1 AND 10000", name="ordinal_bounded"
        ),
        sa.CheckConstraint(
            "typeof(attempt) = 'integer' AND attempt BETWEEN 1 AND 4", name="attempt_bounded"
        ),
        sa.CheckConstraint("state IN ('downloading','publishing','ready')", name="state_supported"),
        sa.CheckConstraint(
            "(state = 'downloading' AND sha256 IS NULL AND size_bytes IS NULL "
            "AND mime IS NULL AND width IS NULL AND height IS NULL) OR "
            "(state IN ('publishing','ready') AND sha256 IS NOT NULL AND size_bytes IS NOT NULL "
            "AND mime IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL)",
            name="verified_metadata_required",
        ),
        sa.CheckConstraint(
            "sha256 IS NULL OR (typeof(sha256) = 'text' AND length(sha256) = 64 "
            "AND sha256 = lower(sha256) AND sha256 NOT GLOB '*[^0-9a-f]*')",
            name="sha256_valid",
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR (typeof(size_bytes) = 'integer' "
            "AND size_bytes BETWEEN 1 AND 67108864)",
            name="size_bounded",
        ),
        sa.CheckConstraint(
            "mime IS NULL OR mime IN ('image/jpeg','image/png','image/webp')", name="mime_supported"
        ),
        sa.CheckConstraint(
            "width IS NULL OR (typeof(width) = 'integer' AND width BETWEEN 1 AND 40000)",
            name="width_bounded",
        ),
        sa.CheckConstraint(
            "height IS NULL OR (typeof(height) = 'integer' AND height BETWEEN 1 AND 40000)",
            name="height_bounded",
        ),
    )


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("SELECT 1 FROM reader_sessions LIMIT 1").first() is not None:
        raise RuntimeError("Reader session evidence must be retained; downgrade would discard it.")
    op.drop_table("reader_session_pages")
    op.drop_index("ix_reader_sessions_lru", table_name="reader_sessions")
    op.drop_index("ux_reader_sessions_live_chapter", table_name="reader_sessions")
    op.drop_table("reader_sessions")
