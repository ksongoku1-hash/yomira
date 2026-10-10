import logging
import unicodedata
from typing import NoReturn

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from app.errors import ErrorCode
from app.tracing import create_trace_id, current_trace_id

revision = "0016_temporary_path_key"
down_revision = "0015_job_cancellation_intent"
branch_labels = None
depends_on = None

logger = logging.getLogger("yomira.migrations")
REQUIRED_UNICODE_VERSION = "16.0.0"
MAX_TEMPORARY_PATH_UTF8_BYTES = 4_096
MAX_TEMPORARY_PATH_UTF16_UNITS = 259
PREFLIGHT_TABLE = "_k093_temporary_path_key_preflight"


class TemporaryPathMigrationError(RuntimeError):
    """A safe, identity-only diagnosis for a rejected temporary-path migration."""

    def __init__(
        self,
        code: ErrorCode,
        stage: str,
        identity: tuple[str, int] | None = None,
        conflicting_identity: tuple[str, int] | None = None,
        *,
        trace_id: str,
    ) -> None:
        self.code = code
        self.retryable = False
        self.stage = stage
        self.identity = identity
        self.conflicting_identity = conflicting_identity
        self.trace_id = trace_id
        super().__init__("The temporary-path identity migration was rejected safely.")


def _abort_preflight(
    code: ErrorCode,
    stage: str,
    *,
    identity: tuple[str, int] | None = None,
    conflicting_identity: tuple[str, int] | None = None,
    byte_length: int | None = None,
    utf16_code_units: int | None = None,
) -> NoReturn:
    trace_id = current_trace_id() or create_trace_id()
    extra: dict[str, object] = {
        "error_code": code.value,
        "retryable": False,
        "component": "download" if code.value.startswith("DL_") else "database",
        "stage": stage,
        "trace_id": trace_id,
    }
    if identity is not None:
        extra["job_id"], extra["ordinal"] = identity
    if conflicting_identity is not None:
        extra["conflicting_job_id"], extra["conflicting_ordinal"] = conflicting_identity
    if byte_length is not None:
        extra["byte_length"] = byte_length
    if utf16_code_units is not None:
        extra["utf16_code_units"] = utf16_code_units
    logger.error("database.temporary_path_migration_rejected", extra=extra)
    raise TemporaryPathMigrationError(
        code,
        stage,
        identity=identity,
        conflicting_identity=conflicting_identity,
        trace_id=trace_id,
    ) from None


def _configure_temporary_database(connection: Connection) -> None:
    try:
        connection.exec_driver_sql("PRAGMA temp_store=FILE")
        temp_store = connection.exec_driver_sql("PRAGMA temp_store").scalar_one()
    except Exception:
        _abort_preflight(ErrorCode.DB_MIGRATION_FAILED, "temporary_path_temp_store")
    if temp_store != 1:
        _abort_preflight(ErrorCode.DB_MIGRATION_FAILED, "temporary_path_temp_store")

    try:
        result = connection.exec_driver_sql("PRAGMA compile_options")
        try:
            compile_options = {str(option).upper() for option in result.scalars()}
        finally:
            result.close()
    except Exception:
        _abort_preflight(
            ErrorCode.DB_MIGRATION_FAILED,
            "temporary_path_compile_options",
        )
    if "TEMP_STORE=3" in compile_options:
        _abort_preflight(
            ErrorCode.DB_MIGRATION_FAILED,
            "temporary_path_compile_options",
        )

    # This is a TEMP-only page-cache suggestion, not a process-wide memory limit.
    try:
        connection.exec_driver_sql("PRAGMA temp.cache_size=-2048")
        temp_cache_size = connection.exec_driver_sql(
            "PRAGMA temp.cache_size"
        ).scalar_one()
    except Exception:
        _abort_preflight(ErrorCode.DB_MIGRATION_FAILED, "temporary_path_cache_size")
    if temp_cache_size != -2_048:
        _abort_preflight(ErrorCode.DB_MIGRATION_FAILED, "temporary_path_cache_size")

    # octet_length() reports bytes in the database encoding; Yomira databases use UTF-8.
    try:
        encoding = connection.exec_driver_sql("PRAGMA encoding").scalar_one()
    except Exception:
        _abort_preflight(ErrorCode.DB_MIGRATION_FAILED, "temporary_path_encoding")
    if not isinstance(encoding, str) or encoding.upper().replace("-", "") != "UTF8":
        _abort_preflight(ErrorCode.DB_MIGRATION_FAILED, "temporary_path_encoding")


def _begin_migration_transaction(connection: Connection) -> None:
    """Make permanent DDL/backfill atomic even with sqlite3 legacy transaction mode."""
    try:
        raw_connection = connection.connection.driver_connection
    except Exception:
        _abort_preflight(ErrorCode.DB_MIGRATION_FAILED, "temporary_path_transaction")
    if raw_connection is None:
        _abort_preflight(ErrorCode.DB_MIGRATION_FAILED, "temporary_path_transaction")
    if raw_connection.in_transaction:
        # The active driver's transaction mode cannot be proven to be IMMEDIATE.
        _abort_preflight(ErrorCode.DB_MIGRATION_FAILED, "temporary_path_transaction")
    try:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    except Exception:
        _abort_preflight(ErrorCode.DB_MIGRATION_FAILED, "temporary_path_transaction")
    if not raw_connection.in_transaction:
        _abort_preflight(ErrorCode.DB_MIGRATION_FAILED, "temporary_path_transaction")


def _create_preflight_table(connection: Connection) -> None:
    try:
        connection.exec_driver_sql(f"DROP TABLE IF EXISTS temp.{PREFLIGHT_TABLE}")
        connection.exec_driver_sql(
            f"CREATE TEMP TABLE {PREFLIGHT_TABLE} ("
            "job_id TEXT NOT NULL, "
            "ordinal INTEGER NOT NULL, "
            "temporary_path_key TEXT NOT NULL, "
            "PRIMARY KEY (job_id, ordinal)"
            ") WITHOUT ROWID"
        )
        connection.exec_driver_sql(
            f"CREATE UNIQUE INDEX temp.ux_{PREFLIGHT_TABLE} "
            f"ON {PREFLIGHT_TABLE} (temporary_path_key)"
        )
    except Exception:
        _abort_preflight(ErrorCode.DB_MIGRATION_FAILED, "temporary_path_temp_table")


def _stage_existing_keys(connection: Connection) -> int:
    try:
        # Keep this first scan to bounded identities and SQLite's stored byte metadata.
        scan = connection.exec_driver_sql(
            "SELECT job_id, ordinal, octet_length(temporary_path) "
            "FROM download_pages "
            "WHERE temporary_path IS NOT NULL "
            "ORDER BY job_id, ordinal"
        )
    except Exception:
        _abort_preflight(ErrorCode.DB_MIGRATION_FAILED, "temporary_path_scan")

    staged_count = 0
    current_identity: tuple[str, int] | None = None
    try:
        while True:
            row = scan.fetchone()
            if row is None:
                break

            job_id, ordinal, byte_length = row
            if not isinstance(job_id, str) or type(ordinal) is not int:
                _abort_preflight(ErrorCode.DB_MIGRATION_FAILED, "temporary_path_scan")
            current_identity = (job_id, ordinal)
            if type(byte_length) is not int or byte_length < 0:
                _abort_preflight(
                    ErrorCode.DB_MIGRATION_FAILED,
                    "temporary_path_byte_length",
                    identity=current_identity,
                )
            if byte_length > MAX_TEMPORARY_PATH_UTF8_BYTES:
                _abort_preflight(
                    ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
                    "temporary_path_byte_length",
                    identity=current_identity,
                    byte_length=byte_length,
                )

            try:
                path_row = connection.exec_driver_sql(
                    "SELECT temporary_path FROM download_pages "
                    "WHERE job_id = ? AND ordinal = ?",
                    current_identity,
                ).first()
            except Exception:
                _abort_preflight(
                    ErrorCode.DB_MIGRATION_FAILED,
                    "temporary_path_read",
                    identity=current_identity,
                )
            if path_row is None or not isinstance(path_row[0], str):
                _abort_preflight(
                    ErrorCode.DB_MIGRATION_FAILED,
                    "temporary_path_read",
                    identity=current_identity,
                )
            temporary_path = path_row[0]

            try:
                actual_byte_length = len(temporary_path.encode("utf-8"))
                utf16_code_units = len(temporary_path.encode("utf-16-le")) // 2
            except UnicodeEncodeError:
                _abort_preflight(
                    ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
                    "temporary_path_encoding",
                    identity=current_identity,
                )
            if actual_byte_length != byte_length:
                _abort_preflight(
                    ErrorCode.DB_MIGRATION_FAILED,
                    "temporary_path_encoding",
                    identity=current_identity,
                    byte_length=actual_byte_length,
                )
            if utf16_code_units > MAX_TEMPORARY_PATH_UTF16_UNITS:
                _abort_preflight(
                    ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
                    "temporary_path_utf16_length",
                    identity=current_identity,
                    utf16_code_units=utf16_code_units,
                )

            key = temporary_path.casefold()
            try:
                connection.exec_driver_sql(
                    f"INSERT INTO temp.{PREFLIGHT_TABLE} "
                    "(job_id, ordinal, temporary_path_key) VALUES (?, ?, ?)",
                    (job_id, ordinal, key),
                )
            except IntegrityError:
                try:
                    existing = connection.exec_driver_sql(
                        f"SELECT job_id, ordinal FROM temp.{PREFLIGHT_TABLE} "
                        "WHERE temporary_path_key = ? LIMIT 1",
                        (key,),
                    ).first()
                except Exception:
                    _abort_preflight(
                        ErrorCode.DB_MIGRATION_FAILED,
                        "temporary_path_collision_lookup",
                        identity=current_identity,
                    )
                if existing is not None:
                    _abort_preflight(
                        ErrorCode.DL_TEMPORARY_PATH_CONFLICT,
                        "temporary_path_key_collision",
                        identity=current_identity,
                        conflicting_identity=(existing[0], existing[1]),
                    )
                _abort_preflight(
                    ErrorCode.DB_MIGRATION_FAILED,
                    "temporary_path_temp_index",
                    identity=current_identity,
                )
            except Exception:
                _abort_preflight(
                    ErrorCode.DB_MIGRATION_FAILED,
                    "temporary_path_temp_index",
                    identity=current_identity,
                )
            staged_count += 1
    except TemporaryPathMigrationError:
        raise
    except Exception:
        _abort_preflight(
            ErrorCode.DB_MIGRATION_FAILED,
            "temporary_path_scan",
            identity=current_identity,
        )
    finally:
        scan.close()
    return staged_count


def upgrade() -> None:
    connection = op.get_bind()
    _configure_temporary_database(connection)
    if unicodedata.unidata_version != REQUIRED_UNICODE_VERSION:
        _abort_preflight(
            ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
            "temporary_path_unicode_version",
        )
    _begin_migration_transaction(connection)

    try:
        _create_preflight_table(connection)
        staged_count = _stage_existing_keys(connection)
    except Exception:
        try:
            connection.exec_driver_sql(f"DROP TABLE IF EXISTS temp.{PREFLIGHT_TABLE}")
        except Exception:
            pass
        raise

    # No persistent schema or row is changed until the complete preflight succeeds.
    op.add_column(
        "download_pages",
        sa.Column("temporary_path_key", sa.Text(), nullable=True),
    )
    connection.exec_driver_sql(
        "UPDATE download_pages SET temporary_path_key = ("
        "SELECT temporary_path_key FROM temp._k093_temporary_path_key_preflight "
        "WHERE _k093_temporary_path_key_preflight.job_id = download_pages.job_id "
        "AND _k093_temporary_path_key_preflight.ordinal = download_pages.ordinal) "
        "WHERE temporary_path IS NOT NULL"
    )
    backfilled_count = connection.exec_driver_sql(
        "SELECT count(*) FROM download_pages WHERE temporary_path_key IS NOT NULL"
    ).scalar_one()
    if backfilled_count != staged_count:
        _abort_preflight(
            ErrorCode.DB_MIGRATION_FAILED,
            "temporary_path_key_backfill",
        )

    op.create_index(
        "ux_download_pages_temporary_path_key",
        "download_pages",
        ["temporary_path_key"],
        unique=True,
    )
    connection.exec_driver_sql(f"DROP TABLE temp.{PREFLIGHT_TABLE}")


def downgrade() -> None:
    op.drop_index("ux_download_pages_temporary_path_key", table_name="download_pages")
    op.drop_column("download_pages", "temporary_path_key")
