from alembic import op

revision = "0022_cbr_asset_type"
down_revision = "0021_download_job_archives"
branch_labels = None
depends_on = None

_COVER_ASSET_TRIGGER_NAMES = (
    "tr_series_cover_asset_type_insert",
    "tr_series_cover_asset_type_update",
    "tr_asset_referenced_cover_type_update",
    "tr_metadata_override_cover_asset_type_insert",
    "tr_metadata_override_cover_asset_type_update",
    "tr_asset_referenced_metadata_cover_type_update",
)

_COVER_ASSET_TRIGGER_SQL = (
    "CREATE TRIGGER tr_series_cover_asset_type_insert "
    "BEFORE INSERT ON series "
    "WHEN NEW.cover_asset_id IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM assets "
    "WHERE asset_id = NEW.cover_asset_id AND asset_type = 'COVER') "
    "BEGIN SELECT RAISE(ABORT, 'series cover must reference a COVER asset'); END",
    "CREATE TRIGGER tr_series_cover_asset_type_update "
    "BEFORE UPDATE OF cover_asset_id ON series "
    "WHEN NEW.cover_asset_id IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM assets "
    "WHERE asset_id = NEW.cover_asset_id AND asset_type = 'COVER') "
    "BEGIN SELECT RAISE(ABORT, 'series cover must reference a COVER asset'); END",
    "CREATE TRIGGER tr_asset_referenced_cover_type_update "
    "BEFORE UPDATE OF asset_type ON assets "
    "WHEN NEW.asset_type <> 'COVER' "
    "AND EXISTS (SELECT 1 FROM series WHERE cover_asset_id = OLD.asset_id) "
    "BEGIN SELECT RAISE(ABORT, 'referenced cover asset must remain COVER'); END",
    "CREATE TRIGGER tr_metadata_override_cover_asset_type_insert "
    "BEFORE INSERT ON metadata_overrides "
    "WHEN NEW.field_name = 'cover' AND NEW.cover_asset_id IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM assets "
    "WHERE asset_id = NEW.cover_asset_id AND asset_type = 'COVER') "
    "BEGIN SELECT RAISE(ABORT, 'metadata cover must reference a COVER asset'); END",
    "CREATE TRIGGER tr_metadata_override_cover_asset_type_update "
    "BEFORE UPDATE OF field_name, cover_asset_id ON metadata_overrides "
    "WHEN NEW.field_name = 'cover' AND NEW.cover_asset_id IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM assets "
    "WHERE asset_id = NEW.cover_asset_id AND asset_type = 'COVER') "
    "BEGIN SELECT RAISE(ABORT, 'metadata cover must reference a COVER asset'); END",
    "CREATE TRIGGER tr_asset_referenced_metadata_cover_type_update "
    "BEFORE UPDATE OF asset_type ON assets "
    "WHEN NEW.asset_type <> 'COVER' "
    "AND EXISTS (SELECT 1 FROM metadata_overrides "
    "WHERE cover_asset_id = OLD.asset_id) "
    "BEGIN SELECT RAISE(ABORT, 'referenced metadata cover must remain COVER'); END",
)


def _drop_cover_asset_triggers() -> None:
    for name in _COVER_ASSET_TRIGGER_NAMES:
        op.execute(f"DROP TRIGGER {name}")


def _create_cover_asset_triggers() -> None:
    for statement in _COVER_ASSET_TRIGGER_SQL:
        op.execute(statement)


def upgrade() -> None:
    # SQLite cannot rename/rebuild assets while other-table triggers still
    # compile against it. Drop and restore the schema guards around the rebuild.
    _drop_cover_asset_triggers()
    with op.batch_alter_table("assets", recreate="always") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_assets_ck_assets_asset_type_supported"),
            type_="check",
        )
        batch_op.create_check_constraint(
            "asset_type_supported",
            "asset_type IN ('COVER', 'THUMBNAIL', 'CBZ', 'CBR', "
            "'PAGE_CACHE', 'TEMP', 'EXPORT_COPY')",
        )
    _create_cover_asset_triggers()


def downgrade() -> None:
    connection = op.get_bind()
    cbr_assets = connection.exec_driver_sql(
        "SELECT asset_id FROM assets WHERE asset_type = 'CBR' LIMIT 1"
    ).first()
    if cbr_assets is not None:
        raise RuntimeError("CBR assets must be removed before downgrading this migration.")
    existing_links = connection.exec_driver_sql(
        "SELECT 1 FROM chapter_cbz_assets LIMIT 1"
    ).first()
    if existing_links is not None:
        raise RuntimeError(
            "Asset downgrade is blocked while durable chapter reader links exist."
        )

    _drop_cover_asset_triggers()
    with op.batch_alter_table("assets", recreate="always") as batch_op:
        batch_op.drop_constraint(op.f("ck_assets_asset_type_supported"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_assets_ck_assets_asset_type_supported"),
            "asset_type IN ('COVER', 'THUMBNAIL', 'CBZ', 'PAGE_CACHE', 'TEMP', 'EXPORT_COPY')",
        )
    _create_cover_asset_triggers()
