from collections.abc import Collection, Mapping
from time import time_ns
from typing import Any

from alembic import context
from alembic.runtime.migration import MigrationContext, MigrationInfo
from sqlalchemy.engine import Connection

# pyright: reportUnusedImport=false
import app.persistence.models  # noqa: F401 - imports mapped tables into shared metadata.
from app.persistence.base import PersistenceBase

config = context.config
target_metadata = PersistenceBase.metadata


def _record_revision_application(
    ctx: MigrationContext,
    step: MigrationInfo,
    heads: Collection[str],
    run_args: Mapping[str, Any],
) -> None:
    _ = heads, run_args
    connection = ctx.connection
    if connection is None:
        raise RuntimeError("Schema history requires an online SQLite connection.")

    if step.is_stamp:
        raise RuntimeError("Alembic stamp is not allowed for Yomira databases.")
    if step.up_revision_id is None or len(step.destination_revision_ids) > 1:
        raise RuntimeError("Yomira migrations must have one linear revision head.")

    resulting_revision = (
        step.destination_revision_ids[0] if step.destination_revision_ids else "base"
    )
    direction = "upgrade" if step.is_upgrade else "downgrade"
    connection.exec_driver_sql(
        "INSERT INTO schema_history "
        "(revision_id, direction, resulting_revision, applied_at_us) "
        "VALUES (?, ?, ?, ?)",
        (
            step.up_revision_id,
            direction,
            resulting_revision,
            time_ns() // 1_000,
        ),
    )


def _include_migration_object(
    object_: Any,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: Any | None,
) -> bool:
    _ = object_
    if type_ == "table" and name == "schema_history" and reflected and compare_to is None:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        version_table=config.get_main_option("version_table"),
        compare_type=True,
        include_object=_include_migration_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if not isinstance(connection, Connection):
        raise RuntimeError(
            "Online migrations require the application-managed SQLite connection."
        )

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        transaction_per_migration=True,
        version_table=config.get_main_option("version_table"),
        compare_type=True,
        include_object=_include_migration_object,
        on_version_apply=_record_revision_application,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
