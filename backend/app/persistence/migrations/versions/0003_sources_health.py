import sqlalchemy as sa
from alembic import op

revision = "0003_sources_health"
down_revision = "0002_series"
branch_labels = None
depends_on = None

# Migration revisions are immutable snapshots and do not import current ORM code.
SOURCE_ID_UUID4_CHECK = (
    "typeof(source_id) = 'text' "
    "AND length(source_id) = 36 "
    "AND length(replace(source_id, '-', '')) = 32 "
    "AND substr(source_id, 9, 1) = '-' "
    "AND substr(source_id, 14, 1) = '-' "
    "AND substr(source_id, 19, 1) = '-' "
    "AND substr(source_id, 24, 1) = '-' "
    "AND substr(source_id, 15, 1) = '4' "
    "AND substr(source_id, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND source_id = lower(source_id) "
    "AND source_id NOT GLOB '*[^0-9a-f-]*'"
)


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("connector_id", sa.Text(), nullable=False),
        sa.Column("connector_version", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("health_status", sa.Text(), nullable=False),
        sa.Column("created_at_us", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_us", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(SOURCE_ID_UUID4_CHECK, name="ck_sources_source_id_uuid4"),
        sa.CheckConstraint(
            "length(connector_id) > 0",
            name="ck_sources_connector_id_nonempty",
        ),
        sa.CheckConstraint(
            "length(connector_version) > 0",
            name="ck_sources_connector_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(display_name) > 0",
            name="ck_sources_display_name_nonempty",
        ),
        sa.CheckConstraint("enabled IN (0, 1)", name="ck_sources_enabled_boolean"),
        sa.CheckConstraint(
            "health_status IN ('HEALTHY', 'DEGRADED', 'BROKEN', 'DISABLED', 'INCOMPATIBLE')",
            name="ck_sources_health_status_supported",
        ),
        sa.CheckConstraint(
            "typeof(created_at_us) = 'integer' AND typeof(updated_at_us) = 'integer'",
            name="ck_sources_timestamps_integer",
        ),
        sa.CheckConstraint(
            "updated_at_us >= created_at_us",
            name="ck_sources_timestamps_ordered",
        ),
        sa.PrimaryKeyConstraint("source_id", name="pk_sources"),
    )
    op.create_index("ix_sources_connector_id", "sources", ["connector_id"])
    op.create_index("ix_sources_enabled", "sources", ["enabled"])
    op.create_index("ix_sources_health_status", "sources", ["health_status"])

    op.create_table(
        "connector_health",
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("last_success_at_us", sa.BigInteger(), nullable=True),
        sa.Column("last_failure_at_us", sa.BigInteger(), nullable=True),
        sa.Column("circuit_state", sa.Text(), nullable=False),
        sa.Column("updated_at_us", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "circuit_state IN ('CLOSED', 'OPEN', 'HALF_OPEN')",
            name="ck_connector_health_circuit_state_supported",
        ),
        sa.CheckConstraint(
            "last_success_at_us IS NULL OR typeof(last_success_at_us) = 'integer'",
            name="ck_connector_health_last_success_integer",
        ),
        sa.CheckConstraint(
            "last_failure_at_us IS NULL OR typeof(last_failure_at_us) = 'integer'",
            name="ck_connector_health_last_failure_integer",
        ),
        sa.CheckConstraint(
            "typeof(updated_at_us) = 'integer'",
            name="ck_connector_health_updated_at_integer",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.source_id"],
            name="fk_connector_health_source_id_sources",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("source_id", name="pk_connector_health"),
    )
    op.create_index(
        "ix_connector_health_updated_at_us",
        "connector_health",
        ["updated_at_us"],
    )


def downgrade() -> None:
    op.drop_index("ix_connector_health_updated_at_us", table_name="connector_health")
    op.drop_table("connector_health")
    op.drop_index("ix_sources_health_status", table_name="sources")
    op.drop_index("ix_sources_enabled", table_name="sources")
    op.drop_index("ix_sources_connector_id", table_name="sources")
    op.drop_table("sources")
