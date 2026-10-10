import sqlalchemy as sa
from alembic import op

revision = "0011_settings"
down_revision = "0010_reader_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("setting_key", sa.Text(), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_us", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "typeof(setting_key) = 'text' AND length(setting_key) > 0",
            name="setting_key_nonempty",
        ),
        sa.CheckConstraint(
            "typeof(value_json) = 'text' AND json_valid(value_json) = 1",
            name="value_json_valid",
        ),
        sa.CheckConstraint(
            "typeof(schema_version) = 'integer' AND schema_version > 0",
            name="schema_version_positive_integer",
        ),
        sa.CheckConstraint(
            "typeof(updated_at_us) = 'integer'",
            name="updated_at_integer",
        ),
        sa.PrimaryKeyConstraint("setting_key", name="pk_settings"),
    )


def downgrade() -> None:
    op.drop_table("settings")
