import sqlalchemy as sa
from alembic import op

revision = "0018_chapter_cbz_assets"
down_revision = "0017_job_recovery_checkpoint"
branch_labels = None
depends_on = None

_UUID_CHECK = (
    "typeof({column}) = 'text' "
    "AND length({column}) = 36 "
    "AND length(replace({column}, '-', '')) = 32 "
    "AND substr({column}, 9, 1) = '-' "
    "AND substr({column}, 14, 1) = '-' "
    "AND substr({column}, 19, 1) = '-' "
    "AND substr({column}, 24, 1) = '-' "
    "AND substr({column}, 15, 1) = '4' "
    "AND substr({column}, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND {column} = lower({column}) "
    "AND {column} NOT GLOB '*[^0-9a-f-]*'"
)


def upgrade() -> None:
    op.create_table(
        "chapter_cbz_assets",
        sa.Column("chapter_id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("download_job_id", sa.String(length=36), nullable=False),
        sa.Column("page_count", sa.BigInteger(), nullable=False),
        sa.Column("indexed_at_us", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            _UUID_CHECK.format(column="chapter_id"),
            name="chapter_id_uuid4",
        ),
        sa.CheckConstraint(
            _UUID_CHECK.format(column="asset_id"),
            name="asset_id_uuid4",
        ),
        sa.CheckConstraint(
            _UUID_CHECK.format(column="download_job_id"),
            name="download_job_id_uuid4",
        ),
        sa.CheckConstraint(
            "typeof(page_count) = 'integer' AND page_count > 0",
            name="page_count_positive_integer",
        ),
        sa.CheckConstraint(
            "typeof(indexed_at_us) = 'integer'",
            name="indexed_at_integer",
        ),
        sa.ForeignKeyConstraint(
            ["chapter_id"],
            ["chapters.chapter_id"],
            name="fk_chapter_cbz_assets_chapter",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.asset_id"],
            name="fk_chapter_cbz_assets_asset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["download_job_id"],
            ["download_jobs.job_id"],
            name="fk_chapter_cbz_assets_download_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("chapter_id", name="pk_chapter_cbz_assets"),
        sa.UniqueConstraint("asset_id", name="uq_chapter_cbz_assets_asset_id"),
        sa.UniqueConstraint(
            "download_job_id",
            name="uq_chapter_cbz_assets_download_job_id",
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    existing_links = connection.exec_driver_sql(
        "SELECT 1 FROM chapter_cbz_assets LIMIT 1"
    ).first()
    if existing_links is not None:
        raise RuntimeError(
            "Chapter CBZ asset downgrade is blocked while durable library links exist."
        )
    op.drop_table("chapter_cbz_assets")
