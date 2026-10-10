from alembic import op

revision = "0012_cover_asset_type"
down_revision = "0011_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    invalid_cover = connection.exec_driver_sql(
        "SELECT series.series_id FROM series "
        "LEFT JOIN assets ON assets.asset_id = series.cover_asset_id "
        "WHERE series.cover_asset_id IS NOT NULL "
        "AND (assets.asset_id IS NULL OR assets.asset_type <> 'COVER') LIMIT 1"
    ).first()
    if invalid_cover is not None:
        raise RuntimeError("A series cover reference does not point to a COVER asset.")

    op.execute(
        "CREATE TRIGGER tr_series_cover_asset_type_insert "
        "BEFORE INSERT ON series "
        "WHEN NEW.cover_asset_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM assets "
        "WHERE asset_id = NEW.cover_asset_id AND asset_type = 'COVER') "
        "BEGIN SELECT RAISE(ABORT, 'series cover must reference a COVER asset'); END"
    )
    op.execute(
        "CREATE TRIGGER tr_series_cover_asset_type_update "
        "BEFORE UPDATE OF cover_asset_id ON series "
        "WHEN NEW.cover_asset_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM assets "
        "WHERE asset_id = NEW.cover_asset_id AND asset_type = 'COVER') "
        "BEGIN SELECT RAISE(ABORT, 'series cover must reference a COVER asset'); END"
    )
    op.execute(
        "CREATE TRIGGER tr_asset_referenced_cover_type_update "
        "BEFORE UPDATE OF asset_type ON assets "
        "WHEN NEW.asset_type <> 'COVER' "
        "AND EXISTS (SELECT 1 FROM series WHERE cover_asset_id = OLD.asset_id) "
        "BEGIN SELECT RAISE(ABORT, 'referenced cover asset must remain COVER'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER tr_asset_referenced_cover_type_update")
    op.execute("DROP TRIGGER tr_series_cover_asset_type_update")
    op.execute("DROP TRIGGER tr_series_cover_asset_type_insert")
