"""Retain verified EPUB export evidence for books up to 64 GiB."""

from alembic import op

revision = "0025_epub_export_size"
down_revision = "0024_reader_sessions"
branch_labels = None
depends_on = None


def _set_limit(limit: int) -> None:
    with op.batch_alter_table("usb_export_jobs", recreate="always") as batch:
        batch.drop_constraint(
            op.f("ck_usb_export_jobs_expected_size_bounded_integer"), type_="check"
        )
        batch.create_check_constraint(
            op.f("ck_usb_export_jobs_expected_size_bounded_integer"),
            "typeof(expected_size_bytes) = 'integer' "
            f"AND expected_size_bytes BETWEEN 1 AND {limit}",
        )


def upgrade() -> None:
    _set_limit(64 * 1024**3)


def downgrade() -> None:
    if (
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM usb_export_jobs WHERE expected_size_bytes > 8589934592 LIMIT 1"
        )
        .first()
        is not None
    ):
        raise RuntimeError("Large EPUB export evidence must be retained before downgrade.")
    _set_limit(8 * 1024**3)
