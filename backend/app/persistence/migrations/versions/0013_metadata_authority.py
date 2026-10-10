import sqlalchemy as sa
from alembic import op

revision = "0013_metadata_authority"
down_revision = "0012_cover_asset_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("series", sa.Column("author", sa.Text(), nullable=True))
    op.execute(
        "ALTER TABLE series ADD COLUMN genres_json TEXT CHECK ("
        "genres_json IS NULL OR (typeof(genres_json) = 'text' AND "
        "CASE WHEN json_valid(genres_json) = 1 "
        "THEN json_type(genres_json) = 'array' ELSE 0 END))"
    )

    op.add_column("series_sources", sa.Column("remote_cover_url", sa.Text(), nullable=True))
    op.add_column("series_sources", sa.Column("remote_author", sa.Text(), nullable=True))
    op.add_column("series_sources", sa.Column("remote_description", sa.Text(), nullable=True))
    op.execute(
        "ALTER TABLE series_sources ADD COLUMN remote_genres_json TEXT CHECK ("
        "remote_genres_json IS NULL OR (typeof(remote_genres_json) = 'text' AND "
        "CASE WHEN json_valid(remote_genres_json) = 1 "
        "THEN json_type(remote_genres_json) = 'array' ELSE 0 END))"
    )
    op.add_column("series_sources", sa.Column("remote_status", sa.Text(), nullable=True))
    op.add_column("series_sources", sa.Column("remote_language", sa.Text(), nullable=True))

    op.create_table(
        "metadata_overrides",
        sa.Column("series_id", sa.String(length=36), nullable=False),
        sa.Column("field_name", sa.String(length=32), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=True),
        sa.Column("cover_asset_id", sa.String(length=36), nullable=True),
        sa.Column("created_at_us", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_us", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "field_name IN ('title', 'cover', 'author', 'description', "
            "'genres', 'status', 'language')",
            name="field_name_supported",
        ),
        sa.CheckConstraint(
            "typeof(created_at_us) = 'integer' AND typeof(updated_at_us) = 'integer' "
            "AND updated_at_us >= created_at_us",
            name="timestamps_ordered_integer",
        ),
        sa.CheckConstraint(
            "(field_name = 'cover' AND value_json IS NULL) OR "
            "(field_name <> 'cover' AND cover_asset_id IS NULL AND value_json IS NOT NULL "
            "AND json_valid(value_json) = 1 AND CASE WHEN field_name = 'genres' "
            "THEN json_type(value_json) IN ('array', 'null') "
            "ELSE json_type(value_json) IN ('text', 'null') END)",
            name="value_matches_field",
        ),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["series.series_id"],
            name="fk_metadata_overrides_series_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cover_asset_id"],
            ["assets.asset_id"],
            name="fk_metadata_overrides_cover_asset_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("series_id", "field_name", name="pk_metadata_overrides"),
    )
    op.create_index(
        "ix_metadata_overrides_field_name",
        "metadata_overrides",
        ["field_name"],
        unique=False,
    )

    op.execute(
        "CREATE TRIGGER tr_metadata_override_cover_asset_type_insert "
        "BEFORE INSERT ON metadata_overrides "
        "WHEN NEW.field_name = 'cover' AND NEW.cover_asset_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM assets "
        "WHERE asset_id = NEW.cover_asset_id AND asset_type = 'COVER') "
        "BEGIN SELECT RAISE(ABORT, 'metadata cover must reference a COVER asset'); END"
    )
    op.execute(
        "CREATE TRIGGER tr_metadata_override_cover_asset_type_update "
        "BEFORE UPDATE OF field_name, cover_asset_id ON metadata_overrides "
        "WHEN NEW.field_name = 'cover' AND NEW.cover_asset_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM assets "
        "WHERE asset_id = NEW.cover_asset_id AND asset_type = 'COVER') "
        "BEGIN SELECT RAISE(ABORT, 'metadata cover must reference a COVER asset'); END"
    )
    op.execute(
        "CREATE TRIGGER tr_asset_referenced_metadata_cover_type_update "
        "BEFORE UPDATE OF asset_type ON assets "
        "WHEN NEW.asset_type <> 'COVER' "
        "AND EXISTS (SELECT 1 FROM metadata_overrides "
        "WHERE cover_asset_id = OLD.asset_id) "
        "BEGIN SELECT RAISE(ABORT, 'referenced metadata cover must remain COVER'); END"
    )


def downgrade() -> None:
    connection = op.get_bind()
    populated_values = connection.exec_driver_sql(
        "SELECT 1 FROM series WHERE author IS NOT NULL OR genres_json IS NOT NULL "
        "UNION ALL SELECT 1 FROM series_sources WHERE remote_cover_url IS NOT NULL "
        "OR remote_author IS NOT NULL OR remote_description IS NOT NULL "
        "OR remote_genres_json IS NOT NULL OR remote_status IS NOT NULL "
        "OR remote_language IS NOT NULL LIMIT 1"
    ).first()
    override_exists = connection.exec_driver_sql(
        "SELECT 1 FROM metadata_overrides LIMIT 1"
    ).first()
    if populated_values is not None or override_exists is not None:
        raise RuntimeError(
            "Metadata authority downgrade is blocked while K056 values are stored."
        )

    op.execute("DROP TRIGGER tr_asset_referenced_metadata_cover_type_update")
    op.execute("DROP TRIGGER tr_metadata_override_cover_asset_type_update")
    op.execute("DROP TRIGGER tr_metadata_override_cover_asset_type_insert")
    op.drop_index("ix_metadata_overrides_field_name", table_name="metadata_overrides")
    op.drop_table("metadata_overrides")
    op.drop_column("series_sources", "remote_language")
    op.drop_column("series_sources", "remote_status")
    op.drop_column("series_sources", "remote_genres_json")
    op.drop_column("series_sources", "remote_description")
    op.drop_column("series_sources", "remote_author")
    op.drop_column("series_sources", "remote_cover_url")
    op.drop_column("series", "genres_json")
    op.drop_column("series", "author")
