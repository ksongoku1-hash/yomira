import sqlalchemy as sa
from alembic import op

revision = "0001_schema_history"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schema_history",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("revision_id", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("resulting_revision", sa.String(length=32), nullable=False),
        sa.Column("applied_at_us", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "direction IN ('upgrade', 'downgrade')",
            name="direction",
        ),
        sqlite_autoincrement=True,
    )
    op.execute(
        "CREATE TRIGGER schema_history_reject_update "
        "BEFORE UPDATE ON schema_history "
        "BEGIN SELECT RAISE(ABORT, 'schema_history is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER schema_history_reject_delete "
        "BEFORE DELETE ON schema_history "
        "BEGIN SELECT RAISE(ABORT, 'schema_history is append-only'); END"
    )


def downgrade() -> None:
    # Keep the append-only audit trail even if a development database is downgraded.
    pass
