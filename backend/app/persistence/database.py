import logging
import os
import sqlite3
import stat
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from shutil import disk_usage
from threading import Condition, Lock
from time import monotonic
from typing import Literal, NoReturn
from uuid import UUID

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL, Connection, ExceptionContext
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

from app.errors import ErrorCode
from app.persistence.storage_gate import SharedStorageGate
from app.persistence.windows_paths import (
    DataRootIdentity,
    is_absolute_windows_path,
    windows_path_key,
)
from app.tracing import create_trace_id, current_trace_id

logger = logging.getLogger("yomira.database")
MINIMUM_SQLITE_VERSION = (3, 46, 0)
VACUUM_FRAGMENTATION_NUMERATOR = 1
VACUUM_FRAGMENTATION_DENOMINATOR = 4
VACUUM_DISK_RESERVE_BYTES = 64 * 1024 * 1024

MaintenanceStatus = Literal[
    "already_closed",
    "failed",
    "skipped_insufficient_space",
    "skipped_low_fragmentation",
    "vacuumed",
]


def _available_disk_bytes(path: Path) -> int:
    return disk_usage(path).free


@dataclass(frozen=True, slots=True)
class SQLiteOptions:
    busy_timeout_ms: int = 5_000
    cache_size_kib: int = 2_048
    pool_size: int = 4
    pool_timeout_seconds: float = 5.0
    shutdown_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not 1 <= self.busy_timeout_ms <= 5_000:
            raise ValueError("busy_timeout_ms must be between 1 and 5000")
        if not 1 <= self.cache_size_kib <= 2_048:
            raise ValueError("cache_size_kib must be between 1 and 2048")
        if not 1 <= self.pool_size <= 4:
            raise ValueError("pool_size must be between 1 and 4")
        if not 0 < self.pool_timeout_seconds <= 5.0:
            raise ValueError("pool_timeout_seconds must not exceed 5 seconds")
        if not 0 < self.shutdown_timeout_seconds <= 5.0:
            raise ValueError("shutdown_timeout_seconds must not exceed 5 seconds")


@dataclass(frozen=True, slots=True)
class DatabaseMaintenanceReport:
    status: MaintenanceStatus
    page_count: int | None = None
    freelist_page_count: int | None = None
    database_size_bytes: int | None = None
    available_disk_bytes: int | None = None


class DatabaseInitializationError(RuntimeError):
    code = ErrorCode.DB_CONNECTION_FAILED
    retryable = True

    def __init__(self) -> None:
        super().__init__("SQLite could not be opened with the required configuration.")


class DatabaseBusyError(TimeoutError):
    code = ErrorCode.DB_BUSY_TIMEOUT
    retryable = True

    def __init__(self) -> None:
        super().__init__("The database or its bounded pool remained busy until the timeout.")


def _is_sqlite_busy(exception: OperationalError) -> bool:
    result_code = getattr(exception.orig, "sqlite_errorcode", None)
    if not isinstance(result_code, int):
        return False
    base_code = result_code & 0xFF
    return base_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


class SQLiteDatabase:
    def __init__(
        self,
        path: Path,
        options: SQLiteOptions | None = None,
        *,
        data_root_identity: DataRootIdentity | None = None,
        storage_gate: SharedStorageGate | None = None,
    ) -> None:
        self.options = options if options is not None else SQLiteOptions()
        if not isinstance(data_root_identity, DataRootIdentity):
            self._log_open_failure("root_identity")
            raise DatabaseInitializationError()
        if not self._path_matches_identity(path, data_root_identity):
            self._log_open_failure("root_identity")
            raise DatabaseInitializationError()

        # The argument has passed a purely lexical check. From this point on,
        # use only a path derived from the shared, captured root identity.
        self.data_root_identity = data_root_identity
        self.path = data_root_identity.canonical_path / "database" / "yomira.sqlite3"
        if storage_gate is not None and type(storage_gate) is not SharedStorageGate:
            raise TypeError("storage_gate must be a SharedStorageGate or None")
        self.storage_gate = storage_gate if storage_gate is not None else SharedStorageGate()
        self._write_lock = Lock()
        self._session_condition = Condition()
        self._active_sessions = 0
        self._closing = False
        self._maintenance = False
        self._closed = False
        self._journal_mode_ready = False
        self._identity_failure_logged = False
        self._path_failure_logged = False
        self._pool_invalidated = False

        self._assert_runtime_paths()
        if sqlite3.sqlite_version_info < MINIMUM_SQLITE_VERSION:
            self._log_open_failure("sqlite_version")
            raise DatabaseInitializationError()

        database_url = URL.create(
            drivername="sqlite+pysqlite",
            database=str(self.path),
        )
        self.engine: Engine = create_engine(
            database_url,
            connect_args={
                "check_same_thread": False,
                "timeout": self.options.busy_timeout_ms / 1_000,
            },
            pool_size=self.options.pool_size,
            max_overflow=0,
            pool_timeout=self.options.pool_timeout_seconds,
        )
        event.listen(self.engine, "do_connect", self._before_connect)
        event.listen(self.engine, "connect", self._configure_connection)
        event.listen(self.engine, "checkout", self._reset_read_only_mode)
        event.listen(self.engine, "before_cursor_execute", self._before_cursor_execute)
        event.listen(self.engine, "handle_error", self._handle_engine_error)

        try:
            with self.engine.connect() as connection:
                journal_mode = connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one()
                if not isinstance(journal_mode, str) or journal_mode.lower() != "wal":
                    raise DatabaseInitializationError()
                self._verify_connection(connection)
                self._journal_mode_ready = True
        except DatabaseInitializationError:
            self.engine.dispose()
            raise
        except SQLAlchemyError:
            self.engine.dispose()
            self._log_open_failure("open")
            raise DatabaseInitializationError() from None

    @property
    def is_closed(self) -> bool:
        with self._session_condition:
            return self._closed

    @staticmethod
    def _path_matches_identity(
        path: object,
        data_root_identity: DataRootIdentity,
    ) -> bool:
        try:
            if not isinstance(path, Path) or not is_absolute_windows_path(path):
                return False
            expected_path = data_root_identity.canonical_path / "database" / "yomira.sqlite3"
            return windows_path_key(path) == windows_path_key(expected_path)
        except OSError, TypeError, ValueError:
            return False

    @staticmethod
    def _has_reparse_attribute(path_info: os.stat_result) -> bool:
        file_attributes = getattr(path_info, "st_file_attributes", 0)
        return stat.S_ISLNK(path_info.st_mode) or bool(file_attributes & 0x400)

    def _invalidate_pool(self) -> None:
        if self._pool_invalidated:
            return
        self._pool_invalidated = True
        engine = getattr(self, "engine", None)
        if engine is not None:
            engine.dispose()

    def _raise_path_preflight(self, error: BaseException | None = None) -> NoReturn:
        if not self._path_failure_logged:
            self._log_open_failure("path_preflight")
            self._path_failure_logged = True
        self._invalidate_pool()
        if error is None:
            raise DatabaseInitializationError() from None
        raise DatabaseInitializationError() from error

    def _assert_root_identity_current(self) -> None:
        try:
            self.data_root_identity.assert_current()
        except Exception:
            if not self._identity_failure_logged:
                self._log_open_failure("root_identity")
                self._identity_failure_logged = True
            self._invalidate_pool()
            raise DatabaseInitializationError() from None

    def assert_data_root_identity_current(self) -> None:
        """Fail closed when the shared data root no longer matches its capture."""
        self._assert_root_identity_current()

    def _preflight_database_paths(self) -> None:
        try:
            database_directory = self.path.parent
            directory_info = database_directory.lstat()
            if self._has_reparse_attribute(directory_info) or not stat.S_ISDIR(
                directory_info.st_mode
            ):
                raise OSError("The SQLite database directory is not a plain directory.")

            for candidate in (
                self.path,
                self.path.with_name(self.path.name + "-wal"),
                self.path.with_name(self.path.name + "-shm"),
                self.path.with_name(self.path.name + "-journal"),
            ):
                try:
                    candidate_info = candidate.lstat()
                except FileNotFoundError:
                    continue
                if self._has_reparse_attribute(candidate_info) or not stat.S_ISREG(
                    candidate_info.st_mode
                ):
                    raise OSError("A SQLite database or sidecar path is not a plain file.")
        except Exception as error:
            self._raise_path_preflight(error)

    def _assert_runtime_paths(self) -> None:
        self._assert_root_identity_current()
        self._preflight_database_paths()
        # A path preflight failure disposes pooled handles. If the filesystem
        # is repaired later, the next verified operation may reconnect safely.
        self._pool_invalidated = False

    def _before_connect(
        self,
        _dialect: object,
        _connection_record: object,
        _connection_args: list[object],
        _connection_parameters: dict[str, object],
    ) -> None:
        self._assert_runtime_paths()

    def _before_cursor_execute(
        self,
        _connection: Connection,
        _cursor: object,
        _statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        self._assert_runtime_paths()

    def _handle_engine_error(self, context: ExceptionContext) -> None:
        exception = context.original_exception
        sqlite_errorcode = getattr(exception, "sqlite_errorcode", None)
        if type(sqlite_errorcode) is int and sqlite_errorcode & 0xFF == sqlite3.SQLITE_FULL:
            closed_now = self.storage_gate.close("SQLITE_FULL")
            logger.critical(
                "database.sqlite_full_observed",
                extra={
                    "error_code": ErrorCode.SYS_DISK_BUDGET_EXCEEDED.value,
                    "retryable": False,
                    "stage": "sqlite_full",
                    "sqlite_error_code": sqlite_errorcode,
                    "storage_gate_closed_now": closed_now,
                    "trace_id": current_trace_id() or create_trace_id(),
                },
            )

    def _configure_connection(self, dbapi_connection: object, _connection_record: object) -> None:
        if not isinstance(dbapi_connection, sqlite3.Connection):
            raise DatabaseInitializationError()

        dbapi_connection.create_function("yomira_casefold", 1, str.casefold, deterministic=True)
        cursor = dbapi_connection.cursor()
        try:
            configured_values = (
                ("PRAGMA foreign_keys=ON", "PRAGMA foreign_keys", 1),
                (
                    f"PRAGMA busy_timeout={self.options.busy_timeout_ms}",
                    "PRAGMA busy_timeout",
                    self.options.busy_timeout_ms,
                ),
                ("PRAGMA synchronous=FULL", "PRAGMA synchronous", 2),
                ("PRAGMA query_only=OFF", "PRAGMA query_only", 0),
                (
                    f"PRAGMA cache_size=-{self.options.cache_size_kib}",
                    "PRAGMA cache_size",
                    -self.options.cache_size_kib,
                ),
            )
            for configuration, verification, expected in configured_values:
                self._assert_runtime_paths()
                cursor.execute(configuration)
                self._assert_runtime_paths()
                row = cursor.execute(verification).fetchone()
                if row is None or row[0] != expected:
                    raise DatabaseInitializationError()

            if self._journal_mode_ready:
                self._assert_runtime_paths()
                row = cursor.execute("PRAGMA journal_mode").fetchone()
                if row is None or not isinstance(row[0], str) or row[0].lower() != "wal":
                    raise DatabaseInitializationError()
        finally:
            cursor.close()

    def _reset_read_only_mode(
        self,
        dbapi_connection: object,
        _connection_record: object,
        _connection_proxy: object,
    ) -> None:
        if not isinstance(dbapi_connection, sqlite3.Connection):
            raise DatabaseInitializationError()
        self._assert_runtime_paths()
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA query_only=OFF")
        finally:
            cursor.close()

    def _verify_connection(self, connection: Connection) -> None:
        expected_values = {
            "PRAGMA foreign_keys": 1,
            "PRAGMA busy_timeout": self.options.busy_timeout_ms,
            "PRAGMA synchronous": 2,
            "PRAGMA cache_size": -self.options.cache_size_kib,
        }
        for pragma, expected in expected_values.items():
            actual = connection.exec_driver_sql(pragma).scalar_one()
            if actual != expected:
                raise DatabaseInitializationError()

    @staticmethod
    def _log_open_failure(stage: str) -> None:
        logger.error(
            "database.open_failed",
            extra={
                "error_code": ErrorCode.DB_CONNECTION_FAILED.value,
                "retryable": True,
                "stage": stage,
                "trace_id": current_trace_id() or create_trace_id(),
            },
        )

    def _begin_session(self) -> None:
        self._assert_runtime_paths()
        with self._session_condition:
            if self._closed:
                raise DatabaseInitializationError()
            if self._closing or self._maintenance:
                raise self._busy_error("session_start")
            self._active_sessions += 1

    def _end_session(self) -> None:
        with self._session_condition:
            self._active_sessions -= 1
            self._session_condition.notify_all()

    @staticmethod
    def _busy_error(stage: str) -> DatabaseBusyError:
        logger.warning(
            "database.busy_timeout",
            extra={
                "error_code": ErrorCode.DB_BUSY_TIMEOUT.value,
                "retryable": True,
                "stage": stage,
            },
        )
        return DatabaseBusyError()

    def _translate_busy(self, exception: Exception) -> NoReturn:
        if isinstance(exception, PoolTimeoutError):
            raise self._busy_error("connection_pool") from None
        if isinstance(exception, OperationalError) and _is_sqlite_busy(exception):
            raise self._busy_error("sqlite_lock") from None
        raise exception

    @contextmanager
    def read_session(self) -> Generator[Session]:
        self._begin_session()
        try:
            try:
                with Session(self.engine, expire_on_commit=False) as session:
                    connection = session.connection()
                    connection.exec_driver_sql("PRAGMA query_only=ON")
                    try:
                        yield session
                    finally:
                        connection.exec_driver_sql("PRAGMA query_only=OFF")
            except (OperationalError, PoolTimeoutError) as exception:
                self._translate_busy(exception)
        finally:
            self._end_session()

    @contextmanager
    def write_session(self) -> Generator[Session]:
        self._begin_session()
        acquired = False
        try:
            acquired = self._write_lock.acquire(timeout=self.options.pool_timeout_seconds)
            if not acquired:
                raise self._busy_error("writer_queue")
            try:
                with Session(self.engine, expire_on_commit=False) as session:
                    # A queued writer must not carry admission across the lock wait:
                    # SQLITE_FULL may close the shared gate while it is queued.
                    with self.storage_gate.mutation():
                        with session.begin():
                            yield session
                            session.flush()
                            # Refresh stale small-library estimates as bulk imports grow tables.
                            session.execute(text("PRAGMA optimize=0x10002"))
            except (OperationalError, PoolTimeoutError) as exception:
                self._translate_busy(exception)
        finally:
            if acquired:
                self._write_lock.release()
            self._end_session()

    @contextmanager
    def quiesced_recovery_write_session(
        self,
        *,
        job_id: UUID,
        quiescence_ack: object,
    ) -> Generator[Session]:
        """Allow only a RecoveryCoordinator CAS after its worker's quiescence ack."""
        from app.services.job_recovery import WorkerQuiescenceAck

        if type(job_id) is not UUID:
            raise TypeError("job_id must be a UUID")
        if type(quiescence_ack) is not WorkerQuiescenceAck:
            raise TypeError("quiescence_ack must be a WorkerQuiescenceAck")
        if quiescence_ack.job_id != job_id:
            raise ValueError("quiescence_ack belongs to a different job")
        if not self.storage_gate.closed:
            raise ValueError("quiesced recovery writes require a closed shared gate")

        self._begin_session()
        acquired = False
        try:
            acquired = self._write_lock.acquire(timeout=self.options.pool_timeout_seconds)
            if not acquired:
                raise self._busy_error("recovery_writer_queue")
            try:
                with Session(self.engine, expire_on_commit=False) as session:
                    # This exceptional permit is private to the recovery CAS. The
                    # caller validates the ack and owns the per-job mutex first.
                    with self.storage_gate.quiesced_recovery_mutation(
                        job_id=job_id,
                        quiescence_ack=quiescence_ack,
                    ):
                        with session.begin():
                            yield session
            except (OperationalError, PoolTimeoutError) as exception:
                self._translate_busy(exception)
        finally:
            if acquired:
                self._write_lock.release()
            self._end_session()

    @contextmanager
    def exclusive_connection(self) -> Generator[Connection]:
        """Drain sessions and yield one connection while normal DB access is blocked."""
        self._assert_runtime_paths()
        deadline = monotonic() + self.options.shutdown_timeout_seconds
        with self._session_condition:
            if self._closed:
                raise DatabaseInitializationError()
            if self._closing or self._maintenance:
                raise self._busy_error("exclusive_operation")
            self._maintenance = True
            self._active_sessions += 1
            while self._active_sessions > 1:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self._active_sessions -= 1
                    self._maintenance = False
                    self._session_condition.notify_all()
                    raise self._busy_error("exclusive_drain")
                self._session_condition.wait(timeout=remaining)

        acquired = self._write_lock.acquire(timeout=self.options.pool_timeout_seconds)
        if not acquired:
            self._end_exclusive_operation()
            raise self._busy_error("exclusive_writer_lock")

        try:
            try:
                self._assert_runtime_paths()
                with self.engine.connect() as connection:
                    yield connection
            except (OperationalError, PoolTimeoutError) as exception:
                self._translate_busy(exception)
        finally:
            self._write_lock.release()
            self._end_exclusive_operation()

    def verify_recovery_integrity(self) -> bool:
        """Run read-only SQLite checks before shared storage admission reopens."""
        try:
            with self.exclusive_connection() as connection:
                connection.exec_driver_sql("PRAGMA query_only=ON")
                try:
                    quick_check = tuple(
                        row[0] for row in connection.exec_driver_sql("PRAGMA quick_check").all()
                    )
                    foreign_key_errors = connection.exec_driver_sql(
                        "PRAGMA foreign_key_check"
                    ).all()
                finally:
                    connection.exec_driver_sql("PRAGMA query_only=OFF")
            valid = quick_check == ("ok",) and not foreign_key_errors
            logger.log(
                logging.INFO if valid else logging.ERROR,
                "database.recovery_integrity_checked",
                extra={
                    "quick_check_passed": quick_check == ("ok",),
                    "foreign_key_violation_count": len(foreign_key_errors),
                    "admission_safe": valid,
                    **(
                        {
                            "error_code": ErrorCode.DB_INTEGRITY_FAILED.value,
                            "retryable": False,
                            "stage": "recovery_integrity_check",
                            "trace_id": current_trace_id() or create_trace_id(),
                        }
                        if not valid
                        else {}
                    ),
                },
            )
            return valid
        except Exception as error:
            logger.exception(
                "database.recovery_integrity_check_failed",
                extra={
                    "error_code": ErrorCode.DB_CONNECTION_FAILED.value,
                    "retryable": True,
                    "stage": "recovery_integrity_check",
                    "cause_type": type(error).__name__,
                    "trace_id": current_trace_id() or create_trace_id(),
                },
            )
            return False

    def _end_exclusive_operation(self) -> None:
        with self._session_condition:
            self._active_sessions -= 1
            self._maintenance = False
            self._session_condition.notify_all()

    def close(self) -> None:
        deadline = monotonic() + self.options.shutdown_timeout_seconds
        with self._session_condition:
            if self._closed:
                return
            self._closing = True
            while self._active_sessions:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise self._busy_error("shutdown_drain")
                self._session_condition.wait(timeout=remaining)

        self.engine.dispose()
        with self._session_condition:
            self._closed = True
            self._session_condition.notify_all()

    def shutdown(self) -> DatabaseMaintenanceReport:
        """Run bounded maintenance, then close the database pool permanently."""
        deadline = monotonic() + self.options.shutdown_timeout_seconds
        with self._session_condition:
            if self._closed:
                return DatabaseMaintenanceReport(status="already_closed")
            self._closing = True
            while self._active_sessions:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise self._busy_error("shutdown_drain")
                self._session_condition.wait(timeout=remaining)
            self._maintenance = True

        acquired = self._write_lock.acquire(timeout=self.options.pool_timeout_seconds)
        try:
            if not acquired:
                report = self._log_maintenance_failure("shutdown_writer_lock", RuntimeError)
            elif self.storage_gate.closed:
                report = self._log_maintenance_failure(
                    "storage_gate_closed",
                    RuntimeError,
                )
            else:
                report = self._run_shutdown_maintenance()
        except DatabaseInitializationError:
            self.engine.dispose()
            with self._session_condition:
                self._closed = True
                self._maintenance = False
                self._session_condition.notify_all()
            raise
        finally:
            if acquired:
                self._write_lock.release()

        self.engine.dispose()
        with self._session_condition:
            self._closed = True
            self._maintenance = False
            self._session_condition.notify_all()
        return report

    def _execute_maintenance_query(
        self,
        connection: sqlite3.Connection,
        statement: str,
    ) -> sqlite3.Cursor:
        self._assert_runtime_paths()
        return connection.execute(statement)

    def _run_shutdown_maintenance(self) -> DatabaseMaintenanceReport:
        logger.info("database.maintenance_started")
        connection: sqlite3.Connection | None = None
        stage = "open_connection"
        failure_type: str | None = None
        page_count: int | None = None
        freelist_page_count: int | None = None
        database_size_bytes: int | None = None
        available_disk_bytes: int | None = None
        identity_error: DatabaseInitializationError | None = None
        report = DatabaseMaintenanceReport(status="failed")

        try:
            self._assert_runtime_paths()
            connection = sqlite3.connect(
                str(self.path),
                timeout=self.options.busy_timeout_ms / 1_000,
                isolation_level=None,
            )
            stage = "configure_connection"
            self._execute_maintenance_query(
                connection,
                f"PRAGMA busy_timeout={self.options.busy_timeout_ms}",
            )
            self._execute_maintenance_query(connection, "PRAGMA foreign_keys=ON")
            self._execute_maintenance_query(connection, "PRAGMA synchronous=FULL")
            self._execute_maintenance_query(
                connection,
                f"PRAGMA cache_size=-{self.options.cache_size_kib}",
            )
            journal_mode = self._execute_maintenance_query(
                connection,
                "PRAGMA journal_mode",
            ).fetchone()
            if journal_mode is None or str(journal_mode[0]).lower() != "wal":
                raise sqlite3.DatabaseError("Required journal mode is unavailable.")

            stage = "pragma_optimize"
            self._execute_maintenance_query(connection, "PRAGMA optimize")

            stage = "maintenance_preflight"
            page_count_row = self._execute_maintenance_query(
                connection,
                "PRAGMA page_count",
            ).fetchone()
            freelist_row = self._execute_maintenance_query(
                connection,
                "PRAGMA freelist_count",
            ).fetchone()
            if page_count_row is None or freelist_row is None:
                raise sqlite3.DatabaseError("SQLite maintenance metadata is unavailable.")
            page_count = int(page_count_row[0])
            freelist_page_count = int(freelist_row[0])
            database_size_bytes = self._database_footprint_bytes()
            self._assert_runtime_paths()
            available_disk_bytes = _available_disk_bytes(self.path.parent)

            if (
                page_count <= 0
                or freelist_page_count * VACUUM_FRAGMENTATION_DENOMINATOR
                < page_count * VACUUM_FRAGMENTATION_NUMERATOR
            ):
                report = DatabaseMaintenanceReport(
                    status="skipped_low_fragmentation",
                    page_count=page_count,
                    freelist_page_count=freelist_page_count,
                    database_size_bytes=database_size_bytes,
                    available_disk_bytes=available_disk_bytes,
                )
            elif available_disk_bytes < (2 * database_size_bytes + VACUUM_DISK_RESERVE_BYTES):
                report = DatabaseMaintenanceReport(
                    status="skipped_insufficient_space",
                    page_count=page_count,
                    freelist_page_count=freelist_page_count,
                    database_size_bytes=database_size_bytes,
                    available_disk_bytes=available_disk_bytes,
                )
            else:
                stage = "integrity_precheck"
                integrity_row = self._execute_maintenance_query(
                    connection,
                    "PRAGMA integrity_check",
                ).fetchone()
                if integrity_row is None or integrity_row[0] != "ok":
                    raise sqlite3.DatabaseError("SQLite integrity precheck did not pass.")
                if connection.in_transaction:
                    raise sqlite3.DatabaseError("VACUUM requires an idle SQLite connection.")

                stage = "vacuum"
                self._execute_maintenance_query(connection, "VACUUM")
                if connection.in_transaction:
                    raise sqlite3.DatabaseError("VACUUM left an active SQLite transaction.")

                stage = "integrity_check"
                integrity_row = self._execute_maintenance_query(
                    connection,
                    "PRAGMA integrity_check",
                ).fetchone()
                if integrity_row is None or integrity_row[0] != "ok":
                    raise sqlite3.DatabaseError("SQLite integrity check after VACUUM did not pass.")
                page_count_row = self._execute_maintenance_query(
                    connection,
                    "PRAGMA page_count",
                ).fetchone()
                freelist_row = self._execute_maintenance_query(
                    connection,
                    "PRAGMA freelist_count",
                ).fetchone()
                if page_count_row is None or freelist_row is None:
                    raise sqlite3.DatabaseError("SQLite maintenance result is unavailable.")
                page_count = int(page_count_row[0])
                freelist_page_count = int(freelist_row[0])
                report = DatabaseMaintenanceReport(
                    status="vacuumed",
                    page_count=page_count,
                    freelist_page_count=freelist_page_count,
                    database_size_bytes=database_size_bytes,
                    available_disk_bytes=available_disk_bytes,
                )
        except DatabaseInitializationError as error:
            identity_error = error
        except Exception as error:
            failure_type = type(error).__name__
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception as error:
                    failure_type = type(error).__name__
                    stage = "connection_close"

        if identity_error is not None:
            raise identity_error

        if failure_type is not None:
            return self._log_maintenance_failure(
                stage,
                failure_type,
                page_count=page_count,
                freelist_page_count=freelist_page_count,
                database_size_bytes=database_size_bytes,
                available_disk_bytes=available_disk_bytes,
            )

        if report.status in {"skipped_insufficient_space", "skipped_low_fragmentation"}:
            logger.info(
                "database.vacuum_skipped",
                extra={
                    "reason": report.status,
                    "page_count": report.page_count,
                    "freelist_page_count": report.freelist_page_count,
                    "database_size_bytes": report.database_size_bytes,
                    "available_disk_bytes": report.available_disk_bytes,
                },
            )
        else:
            logger.info(
                "database.maintenance_succeeded",
                extra={
                    "result": report.status,
                    "page_count": report.page_count,
                    "freelist_page_count": report.freelist_page_count,
                },
            )
        return report

    def _database_footprint_bytes(self) -> int:
        sidecar_suffixes = ("", "-wal", "-shm", "-journal")
        size_bytes = 0
        self._assert_runtime_paths()
        for suffix in sidecar_suffixes:
            sidecar_path = self.path.with_name(self.path.name + suffix)
            self._assert_root_identity_current()
            try:
                sidecar_info = sidecar_path.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                self._raise_path_preflight(error)
            if self._has_reparse_attribute(sidecar_info) or not stat.S_ISREG(sidecar_info.st_mode):
                self._raise_path_preflight()
            size_bytes += sidecar_info.st_size
        return size_bytes

    @staticmethod
    def _log_maintenance_failure(
        stage: str,
        error_type: type[BaseException] | str,
        *,
        page_count: int | None = None,
        freelist_page_count: int | None = None,
        database_size_bytes: int | None = None,
        available_disk_bytes: int | None = None,
    ) -> DatabaseMaintenanceReport:
        exception_type = error_type if isinstance(error_type, str) else error_type.__name__
        logger.error(
            "database.maintenance_failed",
            extra={
                "error_code": ErrorCode.DB_MAINTENANCE_FAILED.value,
                "retryable": True,
                "stage": stage,
                "exception_type": exception_type,
                "page_count": page_count,
                "freelist_page_count": freelist_page_count,
                "database_size_bytes": database_size_bytes,
                "available_disk_bytes": available_disk_bytes,
            },
        )
        return DatabaseMaintenanceReport(
            status="failed",
            page_count=page_count,
            freelist_page_count=freelist_page_count,
            database_size_bytes=database_size_bytes,
            available_disk_bytes=available_disk_bytes,
        )
