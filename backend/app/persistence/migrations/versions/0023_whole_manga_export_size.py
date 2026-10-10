"""Allow a verified whole-manga export up to 8 GiB without losing its history."""

from alembic import op

revision = "0023_whole_manga_export_size"
down_revision = "0022_cbr_asset_type"
branch_labels = None
depends_on = None


def _set_limit(limit: int) -> None:
    with op.batch_alter_table("usb_export_jobs", recreate="always") as batch:
        batch.drop_constraint(
            op.f("ck_usb_export_jobs_expected_size_bounded_integer"), type_="check",
        )
        batch.create_check_constraint(
            op.f("ck_usb_export_jobs_expected_size_bounded_integer"),
            "typeof(expected_size_bytes) = 'integer' "
            f"AND expected_size_bytes BETWEEN 1 AND {limit}",
        )


def upgrade() -> None:
    _set_limit(8_589_934_592)


def downgrade() -> None:
    if op.get_bind().exec_driver_sql(
        "SELECT 1 FROM usb_export_jobs WHERE expected_size_bytes > 2147483648 LIMIT 1"
    ).first() is not None:
        raise RuntimeError("Large export evidence must be retained; downgrade would discard it.")
    _set_limit(2_147_483_648)
