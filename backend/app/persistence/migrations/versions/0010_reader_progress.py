import sqlalchemy as sa
from alembic import op

revision = "0010_reader_progress"
down_revision = "0009_download_pages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ux_chapters_series_chapter_id",
        "chapters",
        ["series_id", "chapter_id"],
        unique=True,
    )
    op.create_table(
        "reader_progress",
        sa.Column("series_id", sa.String(length=36), nullable=False),
        sa.Column("chapter_id", sa.String(length=36), nullable=False),
        sa.Column("page_index", sa.BigInteger(), nullable=False),
        sa.Column("percentage_basis_points", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_us", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "typeof(page_index) = 'integer' AND page_index >= 0",
            name="page_index_nonnegative_integer",
        ),
        sa.CheckConstraint(
            "typeof(percentage_basis_points) = 'integer' "
            "AND percentage_basis_points BETWEEN 0 AND 10000",
            name="percentage_basis_points_range",
        ),
        sa.CheckConstraint(
            "typeof(updated_at_us) = 'integer'",
            name="updated_at_integer",
        ),
        sa.ForeignKeyConstraint(
            ["series_id", "chapter_id"],
            ["chapters.series_id", "chapters.chapter_id"],
            name="fk_reader_progress_series_chapter",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("series_id", "chapter_id", name="pk_reader_progress"),
    )
    op.create_index(
        "ix_reader_progress_chapter_id",
        "reader_progress",
        ["chapter_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_reader_progress_chapter_id", table_name="reader_progress")
    op.drop_table("reader_progress")
    op.drop_index("ux_chapters_series_chapter_id", table_name="chapters")
