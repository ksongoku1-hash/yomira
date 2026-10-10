import sqlalchemy as sa
from alembic import op

revision = "0006_chapter_sources"
down_revision = "0005_chapters"
branch_labels = None
depends_on = None

# Migration revisions are immutable snapshots and do not import current ORM code.
CHAPTER_SOURCE_ID_UUID4_CHECK = (
    "typeof(chapter_source_id) = 'text' "
    "AND length(chapter_source_id) = 36 "
    "AND length(replace(chapter_source_id, '-', '')) = 32 "
    "AND substr(chapter_source_id, 9, 1) = '-' "
    "AND substr(chapter_source_id, 14, 1) = '-' "
    "AND substr(chapter_source_id, 19, 1) = '-' "
    "AND substr(chapter_source_id, 24, 1) = '-' "
    "AND substr(chapter_source_id, 15, 1) = '4' "
    "AND substr(chapter_source_id, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND chapter_source_id = lower(chapter_source_id) "
    "AND chapter_source_id NOT GLOB '*[^0-9a-f-]*'"
)


def upgrade() -> None:
    op.create_table(
        "chapter_sources",
        sa.Column("chapter_source_id", sa.String(length=36), nullable=False),
        sa.Column("chapter_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("remote_id", sa.Text(), nullable=True),
        sa.Column("remote_url", sa.Text(), nullable=True),
        sa.Column("remote_label", sa.Text(), nullable=True),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at_us", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            CHAPTER_SOURCE_ID_UUID4_CHECK,
            name="ck_chapter_sources_chapter_source_id_uuid4",
        ),
        sa.CheckConstraint(
            "remote_id IS NULL OR length(remote_id) > 0",
            name="ck_chapter_sources_remote_id_nonempty_when_present",
        ),
        sa.CheckConstraint(
            "remote_url IS NULL OR length(remote_url) > 0",
            name="ck_chapter_sources_remote_url_nonempty_when_present",
        ),
        sa.CheckConstraint(
            "(remote_id IS NOT NULL AND length(remote_id) > 0) "
            "OR (remote_url IS NOT NULL AND length(remote_url) > 0)",
            name="ck_chapter_sources_remote_reference_required",
        ),
        sa.CheckConstraint("available IN (0, 1)", name="ck_chapter_sources_available_boolean"),
        sa.CheckConstraint(
            "last_seen_at_us IS NULL OR typeof(last_seen_at_us) = 'integer'",
            name="ck_chapter_sources_last_seen_integer",
        ),
        sa.ForeignKeyConstraint(
            ["chapter_id"],
            ["chapters.chapter_id"],
            name="fk_chapter_sources_chapter_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.source_id"],
            name="fk_chapter_sources_source_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("chapter_source_id", name="pk_chapter_sources"),
    )
    op.create_index("ix_chapter_sources_chapter_id", "chapter_sources", ["chapter_id"])
    op.create_index("ix_chapter_sources_source_id", "chapter_sources", ["source_id"])

def downgrade() -> None:
    op.drop_index("ix_chapter_sources_source_id", table_name="chapter_sources")
    op.drop_index("ix_chapter_sources_chapter_id", table_name="chapter_sources")
    op.drop_table("chapter_sources")
