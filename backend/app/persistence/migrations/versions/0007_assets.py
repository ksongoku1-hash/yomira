import sqlalchemy as sa
from alembic import op

revision = "0007_assets"
down_revision = "0006_chapter_sources"
branch_labels = None
depends_on = None

# Migration revisions are immutable snapshots and do not import current ORM code.
ASSET_ID_UUID4_CHECK = (
    "typeof(asset_id) = 'text' "
    "AND length(asset_id) = 36 "
    "AND length(replace(asset_id, '-', '')) = 32 "
    "AND substr(asset_id, 9, 1) = '-' "
    "AND substr(asset_id, 14, 1) = '-' "
    "AND substr(asset_id, 19, 1) = '-' "
    "AND substr(asset_id, 24, 1) = '-' "
    "AND substr(asset_id, 15, 1) = '4' "
    "AND substr(asset_id, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND asset_id = lower(asset_id) "
    "AND asset_id NOT GLOB '*[^0-9a-f-]*'"
)
ASSET_RELATIVE_PATH_CHECK = (
    "length(relative_path) > 0 "
    "AND substr(relative_path, 1, 1) <> '/' "
    "AND substr(relative_path, -1, 1) <> '/' "
    "AND instr(relative_path, char(92)) = 0 "
    "AND instr(relative_path, ':') = 0 "
    "AND instr(relative_path, '//') = 0 "
    "AND instr('/' || relative_path || '/', '/../') = 0 "
    "AND instr('/' || relative_path || '/', '/./') = 0 "
    "AND instr(relative_path, char(0)) = 0 "
    "AND relative_path NOT GLOB '*[<>:\"|?*]*'"
)
ASSET_SHA256_CHECK = (
    "typeof(sha256) = 'text' "
    "AND length(sha256) = 64 "
    "AND sha256 = lower(sha256) "
    "AND sha256 NOT GLOB '*[^0-9a-f]*'"
)


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("asset_type", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at_us", sa.BigInteger(), nullable=False),
        sa.Column("verified_at_us", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(ASSET_ID_UUID4_CHECK, name="ck_assets_asset_id_uuid4"),
        sa.CheckConstraint(
            "asset_type IN ('COVER', 'THUMBNAIL', 'CBZ', 'PAGE_CACHE', 'TEMP', 'EXPORT_COPY')",
            name="ck_assets_asset_type_supported",
        ),
        sa.CheckConstraint(ASSET_RELATIVE_PATH_CHECK, name="ck_assets_relative_path_safe"),
        sa.CheckConstraint(ASSET_SHA256_CHECK, name="ck_assets_sha256_valid"),
        sa.CheckConstraint(
            "typeof(size_bytes) = 'integer' AND size_bytes >= 0",
            name="ck_assets_size_bytes_nonnegative_integer",
        ),
        sa.CheckConstraint(
            "typeof(created_at_us) = 'integer' "
            "AND (verified_at_us IS NULL OR typeof(verified_at_us) = 'integer')",
            name="ck_assets_timestamps_integer",
        ),
        sa.PrimaryKeyConstraint("asset_id", name="pk_assets"),
    )
    op.create_index("ix_assets_sha256", "assets", ["sha256"])

    connection = op.get_bind()
    with op.batch_alter_table("series", recreate="always") as batch_op:
        batch_op.create_foreign_key(
            "fk_series_cover_asset_id_assets",
            "assets",
            ["cover_asset_id"],
            ["asset_id"],
            ondelete="RESTRICT",
        )

    foreign_key_errors = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if foreign_key_errors:
        raise RuntimeError("Foreign key check failed while rebuilding series for assets.")


def downgrade() -> None:
    with op.batch_alter_table("series", recreate="always") as batch_op:
        batch_op.drop_constraint("fk_series_cover_asset_id_assets", type_="foreignkey")

    op.drop_index("ix_assets_sha256", table_name="assets")
    op.drop_table("assets")
