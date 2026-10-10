import sqlalchemy as sa
from alembic import op

revision = "0020_home_recent_views"
down_revision = "0019_usb_export_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_series_created_recent",
        "series",
        [sa.text("created_at_us DESC"), sa.text("series_id ASC")],
    )
    op.create_index(
        "ix_reader_progress_saved_recent",
        "reader_progress",
        [
            sa.text("updated_at_us DESC"),
            sa.text("series_id ASC"),
            sa.text("chapter_id ASC"),
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_reader_progress_saved_recent", table_name="reader_progress")
    op.drop_index("ix_series_created_recent", table_name="series")
