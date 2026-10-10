import sqlalchemy as sa
from alembic import op

revision = "0002_series"
down_revision = "0001_schema_history"
branch_labels = None
depends_on = None

# Migration revisions are immutable snapshots and must not depend on current ORM code.
SERIES_ID_UUID4_CHECK = (
    "typeof(series_id) = 'text' "
    "AND length(series_id) = 36 "
    "AND length(replace(series_id, '-', '')) = 32 "
    "AND substr(series_id, 9, 1) = '-' "
    "AND substr(series_id, 14, 1) = '-' "
    "AND substr(series_id, 19, 1) = '-' "
    "AND substr(series_id, 24, 1) = '-' "
    "AND substr(series_id, 15, 1) = '4' "
    "AND substr(series_id, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND series_id = lower(series_id) "
    "AND series_id NOT GLOB '*[^0-9a-f-]*'"
)


def upgrade() -> None:
    op.create_table(
        "series",
        sa.Column("series_id", sa.String(length=36), nullable=False),
        sa.Column("canonical_title", sa.Text(), nullable=False),
        sa.Column("display_title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("cover_asset_id", sa.String(length=36), nullable=True),
        sa.Column("created_at_us", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_us", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(SERIES_ID_UUID4_CHECK, name="ck_series_series_id_uuid4"),
        sa.CheckConstraint(
            "length(canonical_title) > 0",
            name="ck_series_canonical_title_nonempty",
        ),
        sa.CheckConstraint(
            "length(display_title) > 0",
            name="ck_series_display_title_nonempty",
        ),
        sa.CheckConstraint(
            "typeof(created_at_us) = 'integer' AND typeof(updated_at_us) = 'integer'",
            name="ck_series_timestamps_integer",
        ),
        sa.CheckConstraint(
            "updated_at_us >= created_at_us",
            name="ck_series_timestamps_ordered",
        ),
        sa.PrimaryKeyConstraint("series_id", name="pk_series"),
    )
    op.create_index("ix_series_canonical_title", "series", ["canonical_title"])
    op.create_index("ix_series_updated_at_us", "series", ["updated_at_us"])


def downgrade() -> None:
    op.drop_index("ix_series_updated_at_us", table_name="series")
    op.drop_index("ix_series_canonical_title", table_name="series")
    op.drop_table("series")
