import logging
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from app.errors import ErrorCode
from app.persistence.base import PersistenceBase
from app.persistence.database import SQLiteDatabase
from app.persistence.windows_paths import windows_path_key
from app.tracing import activate_trace_id, create_trace_id, current_trace_id, reset_trace_id

logger = logging.getLogger("yomira.migrations")
PRE_MIGRATION_BACKUP_RETENTION = 3
REQUIRED_SCHEMA_TRIGGERS = frozenset(
    {
        "tr_series_cover_asset_type_insert",
        "tr_series_cover_asset_type_update",
        "tr_asset_referenced_cover_type_update",
        "tr_metadata_override_cover_asset_type_insert",
        "tr_metadata_override_cover_asset_type_update",
        "tr_asset_referenced_metadata_cover_type_update",
    }
)


def _trace_id_for_log() -> str:
    return current_trace_id() or create_trace_id()


class DatabaseBackupError(RuntimeError):
    code = ErrorCode.DB_BACKUP_FAILED
    retryable = False

    def __init__(self) -> None:
        super().__init__("A verified pre-migration backup could not be created.")


class DatabaseIntegrityError(RuntimeError):
    code = ErrorCode.DB_INTEGRITY_FAILED
    retryable = False

    def __init__(self) -> None:
        super().__init__("SQLite integrity or schema validation failed.")


class DatabaseMigrationError(RuntimeError):
    code = ErrorCode.DB_MIGRATION_FAILED
    retryable = False

    def __init__(self, backup_path: Path | None = None, restored: bool = False) -> None:
        super().__init__("The database migration did not complete safely.")
        self.backup_path = backup_path
        self.restored = restored


@dataclass(frozen=True, slots=True)
class MigrationReport:
    previous_revision: str | None
    current_revision: str
    backup_path: Path | None
    changed: bool


class MigrationRunner:
    def __init__(
        self,
        database: SQLiteDatabase,
        backup_directory: Path,
        backup_retention: int = PRE_MIGRATION_BACKUP_RETENTION,
    ) -> None:
        if backup_retention < 1:
            raise ValueError("backup_retention must be at least one")
        self.database = database
        expected_backup_directory = database.data_root_identity.canonical_path / "backups"
        self._backup_directory_matches_root = (
            backup_directory.is_absolute()
            and windows_path_key(backup_directory)
            == windows_path_key(expected_backup_directory)
        )
        self.backup_directory = expected_backup_directory
        self.backup_retention = backup_retention
        self.config_path = Path(__file__).parents[3] / "alembic.ini"

    def _assert_root_identity_current(self) -> None:
        self.database.assert_data_root_identity_current()

    @staticmethod
    def _has_reparse_attribute(path_info: os.stat_result) -> bool:
        file_attributes = getattr(path_info, "st_file_attributes", 0)
        return stat.S_ISLNK(path_info.st_mode) or bool(file_attributes & 0x400)

    def _assert_backup_directory(self, *, create: bool) -> None:
        self._assert_root_identity_current()
        root_info = self.database.data_root_identity.canonical_path.lstat()
        if self._has_reparse_attribute(root_info) or not stat.S_ISDIR(root_info.st_mode):
            raise OSError("The canonical data root is not a plain directory.")
        try:
            backup_info = self.backup_directory.lstat()
        except FileNotFoundError:
            if not create:
                raise
            self._assert_root_identity_current()
            self.backup_directory.mkdir(parents=False, exist_ok=True)
            self._assert_root_identity_current()
            backup_info = self.backup_directory.lstat()
        if self._has_reparse_attribute(backup_info) or not stat.S_ISDIR(
            backup_info.st_mode
        ):
            raise OSError("The migration backup directory is not a plain directory.")

    def _assert_regular_file(self, path: Path) -> None:
        self._assert_root_identity_current()
        file_info = path.lstat()
        if self._has_reparse_attribute(file_info) or not stat.S_ISREG(file_info.st_mode):
            raise OSError("A migration database file is not a plain file.")

    def _connect_read_only(self, path: Path) -> sqlite3.Connection:
        self._assert_regular_file(path)
        self._assert_root_identity_current()
        return sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            timeout=5.0,
            uri=True,
        )

    def _connect_existing_writable(self, path: Path) -> sqlite3.Connection:
        self._assert_regular_file(path)
        self._assert_root_identity_current()
        return sqlite3.connect(str(path), timeout=5.0)

    def _create_exclusive_file(self, path: Path) -> None:
        self._assert_backup_directory(create=False)
        self._assert_root_identity_current()
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_BINARY", 0)
        descriptor = os.open(str(path), flags, 0o600)
        os.close(descriptor)

    def _cleanup_partial_backup(self, path: Path) -> None:
        try:
            self._assert_backup_directory(create=False)
            self._assert_root_identity_current()
            try:
                path.lstat()
            except FileNotFoundError:
                return
            self._assert_regular_file(path)
            self._assert_root_identity_current()
            path.unlink()
        except Exception:
            logger.warning(
                "database.backup_partial_cleanup_deferred",
                extra={
                    "stage": "pre_migration",
                    "evidence_preserved": True,
                    "trace_id": _trace_id_for_log(),
                },
            )

    def upgrade_to_head(self) -> MigrationReport:
        trace_token = None
        if current_trace_id() is None:
            trace_token = activate_trace_id(create_trace_id())
        try:
            with self.database.exclusive_connection() as connection:
                return self._upgrade_with_connection(connection)
        finally:
            if trace_token is not None:
                reset_trace_id(trace_token)

    def _upgrade_with_connection(self, connection: Connection) -> MigrationReport:
        backup_path: Path | None = None
        try:
            self._verify_integrity(connection)
            config = self._new_config(connection)
            script_directory = ScriptDirectory.from_config(config)
            heads = script_directory.get_heads()
            if len(heads) != 1:
                raise DatabaseMigrationError()
            head = heads[0]
            current = MigrationContext.configure(connection).get_current_revision()
            if current is not None:
                try:
                    script_directory.get_revision(current)
                except ResolutionError:
                    raise DatabaseMigrationError() from None
            self._verify_history_agreement(connection, current)
            connection.commit()

            if current == head:
                self._verify_schema(connection, head)
                connection.commit()
                return MigrationReport(current, head, None, False)

            backup_path = self._create_verified_backup()
            try:
                self._apply_migrations_with_foreign_key_check(connection, config, head)
                self._verify_integrity(connection)
                resulting_revision = MigrationContext.configure(connection).get_current_revision()
                if resulting_revision != head:
                    raise DatabaseMigrationError(backup_path)
                self._verify_schema(connection, head)
                self._verify_history_agreement(connection, head)
                connection.commit()
            except Exception:
                self._restore_verified_backup(connection, backup_path, current)
                logger.exception(
                    "database.migration_failed",
                    extra={
                        "error_code": ErrorCode.DB_MIGRATION_FAILED.value,
                        "retryable": False,
                        "stage": "apply_or_verify",
                        "restored": True,
                        "trace_id": _trace_id_for_log(),
                    },
                )
                raise DatabaseMigrationError(backup_path, restored=True) from None

            self._retain_pre_migration_backups()
            logger.info(
                "database.migration_succeeded",
                extra={
                    "previous_revision": current,
                    "current_revision": head,
                    "stage": "verified",
                    "trace_id": _trace_id_for_log(),
                },
            )
            return MigrationReport(current, head, backup_path, True)
        except DatabaseBackupError:
            raise
        except DatabaseIntegrityError:
            logger.exception(
                "database.integrity_failed",
                extra={
                    "error_code": ErrorCode.DB_INTEGRITY_FAILED.value,
                    "retryable": False,
                    "stage": "preflight",
                    "trace_id": _trace_id_for_log(),
                },
            )
            raise
        except DatabaseMigrationError:
            if backup_path is None:
                logger.exception(
                    "database.migration_preflight_failed",
                    extra={
                        "error_code": ErrorCode.DB_MIGRATION_FAILED.value,
                        "retryable": False,
                        "stage": "preflight",
                        "trace_id": _trace_id_for_log(),
                    },
                )
            raise
        except Exception:
            if backup_path is not None:
                self._restore_verified_backup(connection, backup_path, None)
            logger.exception(
                "database.migration_failed",
                extra={
                    "error_code": ErrorCode.DB_MIGRATION_FAILED.value,
                    "retryable": False,
                    "stage": "preflight",
                    "restored": backup_path is not None,
                    "trace_id": _trace_id_for_log(),
                },
            )
            raise DatabaseMigrationError(backup_path, restored=backup_path is not None) from None

    def _new_config(self, connection: Connection) -> Config:
        config = Config(str(self.config_path))
        config.attributes["connection"] = connection
        return config

    @staticmethod
    def _apply_migrations(config: Config, head: str) -> None:
        command.upgrade(config, head)

    def _apply_migrations_with_foreign_key_check(
        self,
        connection: Connection,
        config: Config,
        head: str,
    ) -> None:
        connection.commit()
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
            connection.commit()
            raise DatabaseIntegrityError()
        connection.commit()

        # SQLite cannot toggle foreign_keys inside a transaction. Migrations run
        # under the exclusive connection and are checked before enforcement returns.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
            connection.commit()
            raise DatabaseIntegrityError()
        connection.commit()

        try:
            self._apply_migrations(config, head)
            connection.commit()
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
            connection.commit()
            if violations:
                raise DatabaseIntegrityError()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.rollback()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
            foreign_keys_enabled = connection.exec_driver_sql(
                "PRAGMA foreign_keys"
            ).scalar_one()
            connection.commit()
            if foreign_keys_enabled != 1:
                raise DatabaseIntegrityError()

    @staticmethod
    def _verify_integrity(connection: Connection) -> None:
        result = connection.exec_driver_sql("PRAGMA integrity_check").scalars().all()
        if result != ["ok"]:
            raise DatabaseIntegrityError()
        foreign_key_errors = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        if foreign_key_errors:
            raise DatabaseIntegrityError()

    def _verify_history_agreement(
        self,
        connection: Connection,
        current_revision: str | None,
    ) -> None:
        tables = set(inspect(connection).get_table_names())
        if "schema_history" not in tables:
            if current_revision is not None:
                raise DatabaseMigrationError()
            return

        latest_revision = connection.exec_driver_sql(
            "SELECT resulting_revision FROM schema_history ORDER BY event_id DESC LIMIT 1"
        ).scalar_one_or_none()
        expected_revision = current_revision if current_revision is not None else "base"
        if latest_revision != expected_revision:
            raise DatabaseMigrationError()

    def _verify_schema(self, connection: Connection, head: str) -> None:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        required_tables = {"alembic_version", "schema_history"}
        required_tables.update(PersistenceBase.metadata.tables)
        if not required_tables.issubset(tables):
            raise DatabaseIntegrityError()

        triggers = set(
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).scalars()
        )
        if not REQUIRED_SCHEMA_TRIGGERS.issubset(triggers):
            raise DatabaseIntegrityError()

        version_rows = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalars().all()
        if version_rows != [head]:
            raise DatabaseMigrationError()

    def _create_verified_backup(self) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        filename = (
            f"{self.database.path.stem}.pre-migration.{timestamp}.{uuid4().hex}.sqlite3"
        )
        final_path = self.backup_directory / filename
        partial_path = final_path.with_suffix(final_path.suffix + ".part")
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            if not self._backup_directory_matches_root:
                raise OSError("The backup directory must be data_root/backups.")
            self._assert_backup_directory(create=True)
            source = self._connect_read_only(self.database.path)
            self._create_exclusive_file(partial_path)
            destination = self._connect_existing_writable(partial_path)
            self._assert_root_identity_current()
            source.backup(destination, pages=128, sleep=0.05)
            self._assert_root_identity_current()
            destination.close()
            destination = None
            source.close()
            source = None
            self._verify_path_integrity(partial_path)
            self._assert_regular_file(partial_path)
            self._assert_root_identity_current()
            with partial_path.open("r+b") as backup_file:
                self._assert_root_identity_current()
                backup_file.flush()
                self._assert_root_identity_current()
                os.fsync(backup_file.fileno())
            self._assert_backup_directory(create=False)
            self._assert_root_identity_current()
            if final_path.exists():
                raise FileExistsError("A migration backup with this name already exists.")
            self._assert_root_identity_current()
            os.rename(partial_path, final_path)
            self._verify_path_integrity(final_path)
            return final_path
        except Exception:
            logger.exception(
                "database.backup_failed",
                extra={
                    "error_code": ErrorCode.DB_BACKUP_FAILED.value,
                    "retryable": False,
                    "stage": "pre_migration",
                    "trace_id": _trace_id_for_log(),
                },
            )
            raise DatabaseBackupError() from None
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
            self._cleanup_partial_backup(partial_path)

    def _verify_path_integrity(self, path: Path) -> None:
        try:
            connection = self._connect_read_only(path)
            try:
                result = connection.execute("PRAGMA integrity_check").fetchall()
            finally:
                connection.close()
        except (FileNotFoundError, sqlite3.Error):
            raise DatabaseIntegrityError() from None
        if result != [("ok",)]:
            raise DatabaseIntegrityError()

    def _restore_verified_backup(
        self,
        connection: Connection,
        backup_path: Path,
        previous_revision: str | None,
    ) -> None:
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            self._assert_root_identity_current()
            connection.rollback()
            self._assert_root_identity_current()
            connection.invalidate()
            self._assert_root_identity_current()
            self._verify_path_integrity(backup_path)
            source = self._connect_read_only(backup_path)
            destination = self._connect_existing_writable(self.database.path)
            self._assert_root_identity_current()
            source.backup(destination, pages=128, sleep=0.05)
            self._assert_root_identity_current()
            destination.commit()
            destination.close()
            destination = None
            source.close()
            source = None
            self._verify_path_integrity(self.database.path)
            self._verify_restored_revision(previous_revision)
        except Exception:
            logger.exception(
                "database.migration_restore_failed",
                extra={
                    "error_code": ErrorCode.DB_MIGRATION_FAILED.value,
                    "retryable": False,
                    "stage": "restore",
                    "backup_preserved": True,
                    "trace_id": _trace_id_for_log(),
                },
            )
            raise DatabaseMigrationError(backup_path, restored=False) from None
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()

    def _verify_restored_revision(self, previous_revision: str | None) -> None:
        connection = self._connect_read_only(self.database.path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "alembic_version" in tables:
                versions = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchall()
                if versions != [(previous_revision,)]:
                    raise DatabaseIntegrityError()
            elif previous_revision is not None:
                raise DatabaseIntegrityError()

            if "schema_history" in tables:
                row = connection.execute(
                    "SELECT resulting_revision FROM schema_history "
                    "ORDER BY event_id DESC LIMIT 1"
                ).fetchone()
                expected_revision = previous_revision or "base"
                if row is None or row[0] != expected_revision:
                    raise DatabaseIntegrityError()
        finally:
            connection.close()

    def _retain_pre_migration_backups(self) -> None:
        pattern = f"{self.database.path.stem}.pre-migration.*.sqlite3"
        try:
            self._assert_backup_directory(create=False)
            self._assert_root_identity_current()
            backups = sorted(
                self.backup_directory.glob(pattern),
                key=self._backup_modification_time,
            )
        except OSError:
            logger.warning(
                "database.backup_retention_deferred",
                extra={"stage": "retention", "trace_id": _trace_id_for_log()},
            )
            return

        for outdated in backups[: max(0, len(backups) - self.backup_retention)]:
            try:
                self._assert_regular_file(outdated)
                self._assert_root_identity_current()
                outdated.unlink()
            except OSError:
                logger.warning(
                    "database.backup_retention_deferred",
                    extra={"stage": "retention", "trace_id": _trace_id_for_log()},
                )

    def _backup_modification_time(self, path: Path) -> int:
        self._assert_regular_file(path)
        self._assert_root_identity_current()
        return path.stat().st_mtime_ns
