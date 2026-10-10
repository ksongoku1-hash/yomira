import sqlalchemy as sa
from alembic import op

revision = "0004_series_sources"
down_revision = "0003_sources_health"
branch_labels = None
depends_on = None

# Migration revisions are immutable snapshots and do not import current ORM code.
SERIES_SOURCE_ID_UUID4_CHECK = (
    "typeof(series_source_id) = 'text' "
    "AND length(series_source_id) = 36 "
    "AND length(replace(series_source_id, '-', '')) = 32 "
    "AND substr(series_source_id, 9, 1) = '-' "
    "AND substr(series_source_id, 14, 1) = '-' "
    "AND substr(series_source_id, 19, 1) = '-' "
    "AND substr(series_source_id, 24, 1) = '-' "
    "AND substr(series_source_id, 15, 1) = '4' "
    "AND substr(series_source_id, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND series_source_id = lower(series_source_id) "
    "AND series_source_id NOT GLOB '*[^0-9a-f-]*'"
)


def upgrade() -> None:
    op.create_table(
        "series_sources",
        sa.Column("series_source_id", sa.String(length=36), nullable=False),
        sa.Column("series_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("remote_id", sa.Text(), nullable=True),
        sa.Column("remote_url", sa.Text(), nullable=True),
        sa.Column("remote_title", sa.Text(), nullable=True),
        sa.Column("last_checked_at_us", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            SERIES_SOURCE_ID_UUID4_CHECK,
            name="ck_series_sources_series_source_id_uuid4",
        ),
        sa.CheckConstraint(
            "remote_id IS NULL OR length(remote_id) > 0",
            name="ck_series_sources_remote_id_nonempty_when_present",
        ),
        sa.CheckConstraint(
            "remote_url IS NULL OR length(remote_url) > 0",
            name="ck_series_sources_remote_url_nonempty_when_present",
        ),
        sa.CheckConstraint(
            "(remote_id IS NOT NULL AND length(remote_id) > 0) "
            "OR (remote_url IS NOT NULL AND length(remote_url) > 0)",
            name="ck_series_sources_remote_reference_required",
        ),
        sa.CheckConstraint(
            "last_checked_at_us IS NULL OR typeof(last_checked_at_us) = 'integer'",
            name="ck_series_sources_last_checked_integer",
        ),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["series.series_id"],
            name="fk_series_sources_series_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.source_id"],
            name="fk_series_sources_source_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("series_source_id", name="pk_series_sources"),
    )
    op.create_index("ix_series_sources_series_id", "series_sources", ["series_id"])
    op.create_index("ix_series_sources_source_id", "series_sources", ["source_id"])
    op.create_index(
        "uq_series_sources_source_remote_id",
        "series_sources",
        ["source_id", "remote_id"],
        unique=True,
        sqlite_where=sa.text("remote_id IS NOT NULL"),
    )
    op.create_index(
        "uq_series_sources_source_remote_url",
        "series_sources",
        ["source_id", "remote_url"],
        unique=True,
        sqlite_where=sa.text("remote_url IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_series_sources_source_remote_url", table_name="series_sources")
    op.drop_index("uq_series_sources_source_remote_id", table_name="series_sources")
    op.drop_index("ix_series_sources_source_id", table_name="series_sources")
    op.drop_index("ix_series_sources_series_id", table_name="series_sources")
    op.drop_table("series_sources")
