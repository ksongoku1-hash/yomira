import json
import logging
import sqlite3
import threading
from pathlib import Path
from time import perf_counter
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.pool import QueuePool
from support.sqlite import (
    open_test_database,
    reopen_test_database,
    sqlite_test_context,
)

from app.errors import ErrorCode
from app.logging_config import JsonLogFormatter
from app.persistence import database as database_module
from app.persistence.database import (
    DatabaseBusyError,
    DatabaseInitializationError,
    SQLiteDatabase,
    SQLiteOptions,
)


def test_file_database_enables_required_pragmas_and_bounded_pool(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    try:
        with database.read_session() as session:
            connection = session.connection()
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 2
            assert connection.exec_driver_sql("PRAGMA cache_size").scalar_one() == -2048

        assert database.options.pool_size == 4
        pool = cast(QueuePool, database.engine.pool)
        assert pool.size() == 4
    finally:
        database.close()


def test_recovery_integrity_check_is_read_only_while_storage_gate_is_closed(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    try:
        database.storage_gate.close("SQLITE_FULL")
        assert database.verify_recovery_integrity() is True
        assert database.storage_gate.closed is True
    finally:
        database.close()


def test_missing_database_parent_fails_without_creating_directories(tmp_path: Path) -> None:
    context = sqlite_test_context(
        tmp_path / "not-created",
        create_database_directory=False,
    )
    missing_parent = context.data_root / "database"

    with pytest.raises(DatabaseInitializationError) as raised:
        context.open()

    assert raised.value.code == ErrorCode.DB_CONNECTION_FAILED
    assert raised.value.retryable is True
    assert not missing_parent.exists()


def test_missing_data_root_identity_logs_trace_without_filesystem_io(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_path = tmp_path / "database" / "yomira.sqlite3"
    caplog.set_level(logging.ERROR, logger="yomira.database")

    with pytest.raises(DatabaseInitializationError) as raised:
        SQLiteDatabase(database_path)

    assert raised.value.code is ErrorCode.DB_CONNECTION_FAILED
    assert not database_path.parent.exists()
    record = next(
        record
        for record in caplog.records
        if record.name == "yomira.database" and record.msg == "database.open_failed"
    )
    event = json.loads(JsonLogFormatter().format(record))
    assert event["error_code"] == ErrorCode.DB_CONNECTION_FAILED.value
    assert event["stage"] == "root_identity"
    assert isinstance(event["trace_id"], str) and event["trace_id"].strip()


def test_write_session_commits_and_rolls_back_as_one_unit(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    try:
        with database.write_session() as session:
            session.execute(text("CREATE TABLE sample (value INTEGER NOT NULL)"))

        with pytest.raises(RuntimeError, match="injected transaction failure"):
            with database.write_session() as session:
                session.execute(text("INSERT INTO sample(value) VALUES (1)"))
                raise RuntimeError("injected transaction failure")

        with database.read_session() as session:
            assert session.execute(text("SELECT count(*) FROM sample")).scalar_one() == 0

        with database.write_session() as session:
            session.execute(text("INSERT INTO sample(value) VALUES (2)"))

        with database.read_session() as session:
            assert session.execute(text("SELECT value FROM sample")).scalar_one() == 2
    finally:
        database.close()


def test_sqlite_full_rolls_back_and_closes_global_mutation_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = open_test_database(tmp_path)
    try:
        gate = getattr(database, "storage_gate", None)
        close_observations: list[tuple[str, bool] | str] = []

        def remove_listener() -> None:
            return None

        if gate is not None:

            def try_permission_after_close(reason: str) -> None:
                try:
                    with gate.mutation():
                        close_observations.append("unexpected-permit")
                except RuntimeError:
                    close_observations.append((reason, gate.closed))

            remove_listener = gate.subscribe(try_permission_after_close)

        with database.write_session() as session:
            session.execute(
                text("CREATE TABLE disk_full_probe (id INTEGER PRIMARY KEY, payload BLOB NOT NULL)")
            )
            session.execute(
                text("INSERT INTO disk_full_probe(payload) VALUES (:payload)"),
                {"payload": b"preexisting"},
            )

        with database.exclusive_connection() as connection:
            current_pages = connection.exec_driver_sql("PRAGMA page_count").scalar_one()
            limited_pages = connection.exec_driver_sql(
                f"PRAGMA max_page_count={current_pages}"
            ).scalar_one()
            assert limited_pages == current_pages

        with pytest.raises(OperationalError) as raised:
            with database.write_session() as session:
                session.execute(
                    text("INSERT INTO disk_full_probe(payload) VALUES (:payload)"),
                    {"payload": b"must-roll-back"},
                )
                session.execute(
                    text("INSERT INTO disk_full_probe(payload) VALUES (:payload)"),
                    {"payload": b"x" * (1024 * 1024)},
                )

        sqlite_errorcode = getattr(raised.value.orig, "sqlite_errorcode", None)
        assert type(sqlite_errorcode) is int
        assert sqlite_errorcode & 0xFF == sqlite3.SQLITE_FULL

        with database.read_session() as session:
            rows = session.execute(
                text("SELECT payload FROM disk_full_probe ORDER BY id")
            ).scalars().all()
        assert rows == [b"preexisting"]

        assert gate is not None, "SQLITE_FULL must synchronously close the global storage gate"
        assert gate.closed is True
        assert close_observations == [("SQLITE_FULL", True)]
        remove_listener()

        with pytest.raises(RuntimeError, match="storage gate is closed"):
            with database.write_session() as session:
                session.execute(
                    text("INSERT INTO disk_full_probe(payload) VALUES (:payload)"),
                    {"payload": b"must-not-commit"},
                )

        with database.read_session() as session:
            rows = session.execute(
                text("SELECT payload FROM disk_full_probe ORDER BY id")
            ).scalars().all()
        assert rows == [b"preexisting"]

        def maintenance_must_not_run() -> None:
            pytest.fail("shutdown maintenance must not mutate SQLite behind the closed gate")

        monkeypatch.setattr(database, "_run_shutdown_maintenance", maintenance_must_not_run)
        report = database.shutdown()
        assert report.status == "failed"
        assert database.is_closed
    finally:
        database.close()


def test_writer_queued_before_sqlite_full_is_rejected_after_writer_lock_releases(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    waiter_reached_lock = threading.Event()
    waiter_result: list[BaseException | str] = []

    class ObservedLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def acquire(self, timeout: float = -1) -> bool:
            if threading.current_thread().name == "queued-sqlite-writer":
                waiter_reached_lock.set()
            return self._lock.acquire(timeout=timeout)

        def release(self) -> None:
            self._lock.release()

    try:
        with database.write_session() as session:
            session.execute(
                text(
                    "CREATE TABLE queued_writer_probe "
                    "(id INTEGER PRIMARY KEY, payload BLOB NOT NULL)"
                )
            )
            session.execute(
                text("INSERT INTO queued_writer_probe(payload) VALUES (:payload)"),
                {"payload": b"preexisting"},
            )

        with database.exclusive_connection() as connection:
            current_pages = connection.exec_driver_sql("PRAGMA page_count").scalar_one()
            connection.exec_driver_sql(f"PRAGMA max_page_count={current_pages}")

        database._write_lock = ObservedLock()  # type: ignore[assignment]

        def queued_writer() -> None:
            try:
                with database.write_session() as session:
                    session.execute(
                        text("INSERT INTO queued_writer_probe(payload) VALUES (:payload)"),
                        {"payload": b"queued-but-must-not-commit"},
                    )
                waiter_result.append("committed")
            except BaseException as exception:
                waiter_result.append(exception)

        writer = threading.Thread(target=queued_writer, name="queued-sqlite-writer")
        with pytest.raises(OperationalError) as raised:
            with database.write_session() as session:
                writer.start()
                assert waiter_reached_lock.wait(timeout=2)
                session.execute(
                    text("INSERT INTO queued_writer_probe(payload) VALUES (:payload)"),
                    {"payload": b"x" * (1024 * 1024)},
                )

        assert getattr(raised.value.orig, "sqlite_errorcode", 0) & 0xFF == sqlite3.SQLITE_FULL
        writer.join(timeout=2)
        assert not writer.is_alive()
        assert len(waiter_result) == 1
        assert isinstance(waiter_result[0], RuntimeError)
        assert "storage gate is closed" in str(waiter_result[0])

        with database.read_session() as session:
            rows = session.execute(
                text("SELECT payload FROM queued_writer_probe ORDER BY id")
            ).scalars().all()
        assert rows == [b"preexisting"]
    finally:
        database.close()


def test_foreign_key_violation_is_rejected_by_sqlite(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    try:
        with database.write_session() as session:
            session.execute(text("CREATE TABLE parent (id TEXT PRIMARY KEY)"))
            session.execute(
                text(
                    "CREATE TABLE child (parent_id TEXT NOT NULL REFERENCES parent(id) "
                    "ON DELETE RESTRICT)"
                )
            )

        with pytest.raises(IntegrityError):
            with database.write_session() as session:
                session.execute(text("INSERT INTO child(parent_id) VALUES ('missing')"))
    finally:
        database.close()


def test_read_session_rejects_writes(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    try:
        with database.write_session() as session:
            session.execute(text("CREATE TABLE sample (value INTEGER NOT NULL)"))

        with pytest.raises(OperationalError):
            with database.read_session() as session:
                session.execute(text("INSERT INTO sample(value) VALUES (1)"))

        with database.read_session() as session:
            assert session.execute(text("SELECT count(*) FROM sample")).scalar_one() == 0
    finally:
        database.close()


def test_database_lock_wait_is_bounded_and_typed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = sqlite_test_context(tmp_path)
    path = context.database_path
    database = context.open(options=SQLiteOptions(busy_timeout_ms=80))
    blocker = sqlite3.connect(path, timeout=0, isolation_level=None)
    caplog.set_level(logging.WARNING, logger="yomira.database")
    try:
        with database.write_session() as session:
            session.execute(text("CREATE TABLE sample (value INTEGER NOT NULL)"))

        blocker.execute("BEGIN EXCLUSIVE")
        started = perf_counter()
        with pytest.raises(DatabaseBusyError) as raised:
            with database.write_session() as session:
                session.execute(text("INSERT INTO sample(value) VALUES (1)"))
        elapsed = perf_counter() - started

        assert elapsed >= 0.05
        assert raised.value.retryable is True
        timeout_record = next(
            record
            for record in caplog.records
            if record.name == "yomira.database" and record.msg == "database.busy_timeout"
        )
        assert timeout_record.__dict__["error_code"] == "DB_BUSY_TIMEOUT"
        assert timeout_record.__dict__["stage"] == "sqlite_lock"
        assert timeout_record.__dict__["retryable"] is True
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()
        database.close()


def test_pool_does_not_grow_past_its_configured_capacity(tmp_path: Path) -> None:
    database = open_test_database(
        tmp_path,
        options=SQLiteOptions(pool_size=1, pool_timeout_seconds=0.08),
    )
    try:
        with database.read_session() as held_session:
            held_session.execute(text("SELECT 1"))
            started = perf_counter()
            with pytest.raises(DatabaseBusyError):
                with database.read_session() as queued_session:
                    queued_session.execute(text("SELECT 1"))
            assert perf_counter() - started >= 0.05
    finally:
        database.close()


def test_sqlite_resource_options_have_hard_upper_bounds() -> None:
    with pytest.raises(ValueError):
        SQLiteOptions(pool_size=5)

    with pytest.raises(ValueError):
        SQLiteOptions(busy_timeout_ms=5_001)

    with pytest.raises(ValueError):
        SQLiteOptions(cache_size_kib=2_049)


def test_close_keeps_engine_open_until_active_sessions_finish(tmp_path: Path) -> None:
    database = open_test_database(
        tmp_path,
        options=SQLiteOptions(shutdown_timeout_seconds=0.08),
    )
    active_session = database.read_session()
    session = active_session.__enter__()
    session.execute(text("SELECT 1"))
    try:
        with pytest.raises(DatabaseBusyError) as raised:
            database.close()
        assert raised.value.retryable is True
    finally:
        active_session.__exit__(None, None, None)

    database.close()
    with pytest.raises(DatabaseInitializationError):
        with database.read_session():
            pass


def test_exclusive_connection_blocks_normal_database_sessions(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    try:
        with database.exclusive_connection() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            with pytest.raises(DatabaseBusyError) as read_error:
                with database.read_session():
                    pass
            with pytest.raises(DatabaseBusyError) as write_error:
                with database.write_session():
                    pass

            assert read_error.value.code == ErrorCode.DB_BUSY_TIMEOUT
            assert write_error.value.code == ErrorCode.DB_BUSY_TIMEOUT
    finally:
        database.close()


def _make_fragmented_database(database: SQLiteDatabase) -> tuple[int, int]:
    with database.write_session() as session:
        session.execute(
            text("CREATE TABLE volatile_state (id INTEGER PRIMARY KEY, payload BLOB NOT NULL)")
        )
        session.execute(
            text("INSERT INTO volatile_state(id, payload) VALUES (:id, zeroblob(8192))"),
            [{"id": record_id} for record_id in range(1, 257)],
        )
        session.execute(text("DELETE FROM volatile_state WHERE id <= 192"))

    with database.exclusive_connection() as connection:
        page_count = int(connection.exec_driver_sql("PRAGMA page_count").scalar_one())
        freelist_count = int(connection.exec_driver_sql("PRAGMA freelist_count").scalar_one())

    return page_count, freelist_count


def _mock_available_disk_space(
    monkeypatch: pytest.MonkeyPatch,
    available_bytes: int,
) -> None:
    def available_disk_bytes(_path: Path) -> int:
        return available_bytes

    monkeypatch.setattr(database_module, "_available_disk_bytes", available_disk_bytes)


def test_shutdown_vacuums_fragmented_database_and_checks_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = sqlite_test_context(tmp_path)
    path = context.database_path
    database = context.open()
    page_count, freelist_count = _make_fragmented_database(database)
    assert freelist_count * 4 >= page_count
    _mock_available_disk_space(monkeypatch, 1_000_000_000)
    caplog.set_level(logging.INFO, logger="yomira.database")

    report = database.shutdown()

    assert report.status == "vacuumed"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert int(connection.execute("PRAGMA freelist_count").fetchone()[0]) == 0
    assert any(
        record.name == "yomira.database" and record.msg == "database.maintenance_succeeded"
        for record in caplog.records
    )


def test_shutdown_skips_vacuum_when_fragmentation_is_below_threshold(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)

    report = database.shutdown()

    assert report.status == "skipped_low_fragmentation"


def test_shutdown_waits_for_active_sessions_before_starting_maintenance(
    tmp_path: Path,
) -> None:
    database = open_test_database(
        tmp_path,
        options=SQLiteOptions(shutdown_timeout_seconds=0.08),
    )
    active_session = database.read_session()
    session = active_session.__enter__()
    session.execute(text("SELECT 1"))
    try:
        with pytest.raises(DatabaseBusyError) as raised:
            database.shutdown()
        assert raised.value.code == ErrorCode.DB_BUSY_TIMEOUT
        assert database.is_closed is False
    finally:
        active_session.__exit__(None, None, None)

    assert database.shutdown().status == "skipped_low_fragmentation"
    assert database.is_closed is True


def test_shutdown_skips_vacuum_when_disk_reserve_is_insufficient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = sqlite_test_context(tmp_path)
    path = context.database_path
    database = context.open()
    page_count, freelist_count = _make_fragmented_database(database)
    assert freelist_count * 4 >= page_count
    _mock_available_disk_space(monkeypatch, 0)

    report = database.shutdown()

    assert report.status == "skipped_insufficient_space"
    with sqlite3.connect(path) as connection:
        assert int(connection.execute("PRAGMA freelist_count").fetchone()[0]) == freelist_count
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_shutdown_traces_locked_vacuum_and_preserves_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = sqlite_test_context(tmp_path)
    path = context.database_path
    database = context.open(options=SQLiteOptions(busy_timeout_ms=50))
    _make_fragmented_database(database)
    _mock_available_disk_space(monkeypatch, 1_000_000_000)
    caplog.set_level(logging.ERROR, logger="yomira.database")
    blocker = sqlite3.connect(path, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        report = database.shutdown()
    finally:
        blocker.rollback()
        blocker.close()

    assert report.status == "failed"
    maintenance_error = next(
        record
        for record in caplog.records
        if record.name == "yomira.database" and record.msg == "database.maintenance_failed"
    )
    assert maintenance_error.__dict__["error_code"] == "DB_MAINTENANCE_FAILED"
    assert maintenance_error.__dict__["retryable"] is True
    assert maintenance_error.__dict__["stage"] in {"pragma_optimize", "vacuum"}

    reopened = reopen_test_database(database)
    try:
        with reopened.read_session() as session:
            assert session.execute(text("SELECT count(*) FROM volatile_state")).scalar_one() == 64
    finally:
        reopened.close()
