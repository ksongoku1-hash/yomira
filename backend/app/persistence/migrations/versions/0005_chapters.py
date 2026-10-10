import sqlalchemy as sa
from alembic import op

revision = "0005_chapters"
down_revision = "0004_series_sources"
branch_labels = None
depends_on = None

# Migration revisions are immutable snapshots and do not import current ORM code.
CHAPTER_ID_UUID4_CHECK = (
    "typeof(chapter_id) = 'text' "
    "AND length(chapter_id) = 36 "
    "AND length(replace(chapter_id, '-', '')) = 32 "
    "AND substr(chapter_id, 9, 1) = '-' "
    "AND substr(chapter_id, 14, 1) = '-' "
    "AND substr(chapter_id, 19, 1) = '-' "
    "AND substr(chapter_id, 24, 1) = '-' "
    "AND substr(chapter_id, 15, 1) = '4' "
    "AND substr(chapter_id, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND chapter_id = lower(chapter_id) "
    "AND chapter_id NOT GLOB '*[^0-9a-f-]*'"
)
CHAPTER_SORT_KEY_JSON_CHECK = (
    "typeof(chapter_sort_key) = 'text' "
    "AND CASE WHEN json_valid(chapter_sort_key) = 1 "
    "THEN json_type(chapter_sort_key) = 'array' ELSE 0 END"
)


def upgrade() -> None:
    op.create_table(
        "chapters",
        sa.Column("chapter_id", sa.String(length=36), nullable=False),
        sa.Column("series_id", sa.String(length=36), nullable=False),
        sa.Column("chapter_raw_label", sa.Text(), nullable=False),
        sa.Column("chapter_major", sa.BigInteger(), nullable=True),
        sa.Column("chapter_minor", sa.BigInteger(), nullable=True),
        sa.Column("chapter_variant", sa.Text(), nullable=True),
        sa.Column("chapter_type", sa.Text(), nullable=True),
        sa.Column("chapter_sort_key", sa.Text(), nullable=False),
        sa.Column("volume_number", sa.BigInteger(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("display_label", sa.Text(), nullable=False),
        sa.CheckConstraint(CHAPTER_ID_UUID4_CHECK, name="ck_chapters_chapter_id_uuid4"),
        sa.CheckConstraint(
            "length(chapter_raw_label) > 0",
            name="ck_chapters_chapter_raw_label_nonempty",
        ),
        sa.CheckConstraint(
            "length(display_label) > 0",
            name="ck_chapters_display_label_nonempty",
        ),
        sa.CheckConstraint(
            "chapter_major IS NULL OR typeof(chapter_major) = 'integer'",
            name="ck_chapters_chapter_major_integer",
        ),
        sa.CheckConstraint(
            "chapter_minor IS NULL OR typeof(chapter_minor) = 'integer'",
            name="ck_chapters_chapter_minor_integer",
        ),
        sa.CheckConstraint(
            "volume_number IS NULL OR typeof(volume_number) = 'integer'",
            name="ck_chapters_volume_number_integer",
        ),
        sa.CheckConstraint(
            CHAPTER_SORT_KEY_JSON_CHECK,
            name="ck_chapters_chapter_sort_key_json_array",
        ),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["series.series_id"],
            name="fk_chapters_series_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("chapter_id", name="pk_chapters"),
    )
    op.create_index("ix_chapters_series_id", "chapters", ["series_id"])


def downgrade() -> None:
    op.drop_index("ix_chapters_series_id", table_name="chapters")
    op.drop_table("chapters")
