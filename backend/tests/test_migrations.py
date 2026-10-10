import logging
import os
import sqlite3
import subprocess
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import IntegrityError
from support.sqlite import open_test_database, sqlite_test_context

from app.errors import ErrorCode
from app.persistence.database import DatabaseInitializationError, SQLiteDatabase
from app.persistence.migrations.runner import (
    DatabaseBackupError,
    DatabaseMigrationError,
    MigrationRunner,
)


def test_upgrade_creates_verified_backup_and_append_only_history(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    backup_directory = tmp_path / "backups"
    runner = MigrationRunner(database, backup_directory)
    try:
        report = runner.upgrade_to_head()

        assert report.previous_revision is None
        assert report.current_revision == "0023_whole_manga_export_size"
        assert report.changed is True
        assert report.backup_path is not None
        assert report.backup_path.is_file()
        with closing(sqlite3.connect(report.backup_path)) as backup:
            assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert (
                backup.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'alembic_version'"
                ).fetchone()
                is None
            )

        with database.read_session() as session:
            history = session.execute(
                text(
                    "SELECT revision_id, direction, resulting_revision "
                    "FROM schema_history ORDER BY event_id"
                )
            ).all()
            assert [tuple(row) for row in history[:-6]] == [
                ("0001_schema_history", "upgrade", "0001_schema_history"),
                ("0002_series", "upgrade", "0002_series"),
                ("0003_sources_health", "upgrade", "0003_sources_health"),
                ("0004_series_sources", "upgrade", "0004_series_sources"),
                ("0005_chapters", "upgrade", "0005_chapters"),
                ("0006_chapter_sources", "upgrade", "0006_chapter_sources"),
                ("0007_assets", "upgrade", "0007_assets"),
                ("0008_download_jobs", "upgrade", "0008_download_jobs"),
                ("0009_download_pages", "upgrade", "0009_download_pages"),
                ("0010_reader_progress", "upgrade", "0010_reader_progress"),
                ("0011_settings", "upgrade", "0011_settings"),
                ("0012_cover_asset_type", "upgrade", "0012_cover_asset_type"),
                ("0013_metadata_authority", "upgrade", "0013_metadata_authority"),
                ("0014_job_dispatch_fairness", "upgrade", "0014_job_dispatch_fairness"),
                ("0015_job_cancellation_intent", "upgrade", "0015_job_cancellation_intent"),
                ("0016_temporary_path_key", "upgrade", "0016_temporary_path_key"),
                ("0017_job_recovery_checkpoint", "upgrade", "0017_job_recovery_checkpoint"),
            ]
            assert history[-6][0] == "0018_chapter_cbz_assets"
            assert history[-5][0] == "0019_usb_export_history"
            assert history[-4][0] == "0020_home_recent_views"
            assert history[-3][0] == "0021_download_job_archives"
            assert history[-2][0] == "0022_cbr_asset_type"
            assert history[-1][0] == "0023_whole_manga_export_size"
            assert history[-1][1] == "upgrade"
            assert history[-1][2] == history[-1][0]
            assert len(history) == 23

        with database.write_session() as session:
            with pytest.raises(IntegrityError, match="append-only"):
                session.execute(text("UPDATE schema_history SET direction = 'downgrade'"))
        with database.write_session() as session:
            with pytest.raises(IntegrityError, match="append-only"):
                session.execute(text("DELETE FROM schema_history"))

        second_report = runner.upgrade_to_head()
        assert second_report.changed is False
        assert second_report.backup_path is None
        assert len(list(backup_directory.glob("*.sqlite3"))) == 1
    finally:
        database.close()


def _database_at_revision(
    tmp_path: Path, revision: str = "0015_job_cancellation_intent"
) -> tuple[SQLiteDatabase, MigrationRunner]:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    with database.exclusive_connection() as connection:
        config = Config(str(runner.config_path))
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
        connection.commit()
    return database, runner


def _create_directory_reparse_point(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except NotImplementedError, OSError:
        if os.name != "nt":
            pytest.skip("directory reparse points are unavailable")

    environment = os.environ.copy()
    environment["YOMIRA_TEST_LINK"] = str(link)
    environment["YOMIRA_TEST_TARGET"] = str(target)
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; try { New-Item -ItemType Junction "
                "-Path $env:YOMIRA_TEST_LINK -Target $env:YOMIRA_TEST_TARGET "
                "| Out-Null } catch { exit 1 }",
            ],
            capture_output=True,
            check=False,
            env=environment,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        pytest.skip(f"directory junctions are unavailable: {type(error).__name__}")
    if result.returncode != 0 or not link.exists():
        pytest.skip("directory symlinks and junctions are unavailable")


def _create_file_reparse_point(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"file symlinks are unavailable: {type(error).__name__}")


def _assert_database_path_preflight_failure(
    database_path: Path,
    data_root_identity: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="yomira.database")
    with pytest.raises(DatabaseInitializationError) as raised:
        SQLiteDatabase(database_path, data_root_identity=data_root_identity)
    assert raised.value.code is ErrorCode.DB_CONNECTION_FAILED
    assert any(
        getattr(record, "stage", None) == "path_preflight"
        for record in caplog.records
        if record.name == "yomira.database"
    )


def test_sqlite_database_rejects_reparse_point_in_managed_parent_before_open(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = sqlite_test_context(tmp_path, create_database_directory=False)
    managed_parent = tmp_path / "database"
    outside_parent = tmp_path / "outside"
    outside_parent.mkdir()
    sentinel = outside_parent / "preserve.txt"
    sentinel.write_bytes(b"outside-content")
    _create_directory_reparse_point(managed_parent, outside_parent)
    database_path = context.database_path

    _assert_database_path_preflight_failure(
        database_path,
        context.data_root_identity,
        caplog,
    )

    assert sentinel.read_bytes() == b"outside-content"
    assert not (outside_parent / "yomira.sqlite3").exists()


def test_sqlite_database_rejects_reparse_point_at_database_file_before_open(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = sqlite_test_context(tmp_path)
    outside_parent = tmp_path / "outside"
    outside_parent.mkdir()
    database_path = context.database_path
    outside_database = outside_parent / "yomira.sqlite3"
    outside_database.write_bytes(b"preserve-external-database-bytes")
    _create_file_reparse_point(database_path, outside_database)

    _assert_database_path_preflight_failure(
        database_path,
        context.data_root_identity,
        caplog,
    )

    assert outside_database.read_bytes() == b"preserve-external-database-bytes"
    assert database_path.is_symlink()
    assert not Path(f"{database_path}-wal").exists()
    assert not Path(f"{database_path}-shm").exists()


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm", "-journal"])
def test_sqlite_database_rejects_reparse_point_at_existing_sidecar_before_open(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    sidecar_suffix: str,
) -> None:
    context = sqlite_test_context(tmp_path)
    database_path = context.database_path
    initialized_database = context.open()
    initialized_database.close()
    sidecar_path = Path(f"{database_path}{sidecar_suffix}")
    outside_sidecar = tmp_path / f"external{sidecar_suffix}"
    outside_sidecar.write_bytes(b"preserve-external-sidecar-bytes")
    _create_file_reparse_point(sidecar_path, outside_sidecar)

    _assert_database_path_preflight_failure(
        database_path,
        context.data_root_identity,
        caplog,
    )

    assert outside_sidecar.read_bytes() == b"preserve-external-sidecar-bytes"
    assert sidecar_path.is_symlink()


@pytest.mark.parametrize(
    "compile_option",
    [None, "TEMP_STORE=0", "TEMP_STORE=1", "TEMP_STORE=2"],
    ids=["compile-option-absent", "temp-store-0", "temp-store-1", "temp-store-2"],
)
def test_migration_0016_accepts_disk_backed_temp_store_compile_options(
    tmp_path: Path,
    compile_option: str | None,
) -> None:
    database, runner = _database_at_revision(tmp_path)
    with database.engine.connect() as connection:
        driver_connection = connection.connection.driver_connection
        assert isinstance(driver_connection, sqlite3.Connection)
        original_length_limit = driver_connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    statements: list[str] = []

    def supply_compile_options(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> tuple[str, object]:
        normalized = " ".join(statement.upper().split())
        statements.append(normalized)
        if normalized == "PRAGMA COMPILE_OPTIONS":
            value = "'BASELINE_OPTION'" if compile_option is None else f"'{compile_option}'"
            return f"SELECT {value}", parameters
        return statement, parameters

    event.listen(database.engine, "before_cursor_execute", supply_compile_options, retval=True)
    try:
        report = runner.upgrade_to_head()

        assert report.changed is True
        assert report.current_revision == "0023_whole_manga_export_size"
        assert driver_connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == original_length_limit
        normalized_statements = [" ".join(statement.split()) for statement in statements]
        assert "PRAGMA TEMP_STORE=FILE" in normalized_statements
        assert "PRAGMA TEMP.CACHE_SIZE=-2048" in normalized_statements
        assert "PRAGMA COMPILE_OPTIONS" in normalized_statements
        assert "PRAGMA TEMP_STORE" in normalized_statements
        assert "PRAGMA TEMP.CACHE_SIZE" in normalized_statements
        assert normalized_statements.index("PRAGMA TEMP_STORE=FILE") < normalized_statements.index(
            "PRAGMA TEMP_STORE"
        )
        assert normalized_statements.index(
            "PRAGMA TEMP.CACHE_SIZE=-2048"
        ) < normalized_statements.index("PRAGMA TEMP.CACHE_SIZE")
        scan_or_temp_table_indexes = [
            index
            for index, statement in enumerate(normalized_statements)
            if "FROM DOWNLOAD_PAGES" in statement or "CREATE TEMP" in statement
        ]
        assert scan_or_temp_table_indexes
        verified_configuration_indexes = [
            normalized_statements.index("PRAGMA TEMP_STORE=FILE"),
            normalized_statements.index("PRAGMA TEMP_STORE"),
            normalized_statements.index("PRAGMA COMPILE_OPTIONS"),
            normalized_statements.index("PRAGMA TEMP.CACHE_SIZE=-2048"),
            normalized_statements.index("PRAGMA TEMP.CACHE_SIZE"),
        ]
        assert max(verified_configuration_indexes) < min(scan_or_temp_table_indexes)
    finally:
        event.remove(database.engine, "before_cursor_execute", supply_compile_options)
        database.close()


def test_migration_0016_rejects_temp_store_3_before_scan_temp_table_or_ddl(
    tmp_path: Path,
) -> None:
    database, runner = _database_at_revision(tmp_path)
    with database.engine.connect() as connection:
        driver_connection = connection.connection.driver_connection
        assert isinstance(driver_connection, sqlite3.Connection)
        original_length_limit = driver_connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    statements: list[str] = []
    observed_length_limits: list[int] = []

    def force_temp_store_three(
        connection: Any,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> tuple[str, object]:
        driver_connection = connection.connection.driver_connection
        assert isinstance(driver_connection, sqlite3.Connection)
        observed_length_limits.append(driver_connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH))
        normalized = " ".join(statement.upper().split())
        statements.append(normalized)
        if normalized == "PRAGMA COMPILE_OPTIONS":
            return "SELECT 'TEMP_STORE=3'", parameters
        return statement, parameters

    event.listen(database.engine, "before_cursor_execute", force_temp_store_three, retval=True)
    try:
        with pytest.raises(DatabaseMigrationError) as raised:
            runner.upgrade_to_head()

        assert raised.value.code is ErrorCode.DB_MIGRATION_FAILED
        assert raised.value.restored is True
        assert observed_length_limits
        assert all(limit == original_length_limit for limit in observed_length_limits)
        assert "PRAGMA COMPILE_OPTIONS" in statements
        assert not any("CREATE TEMP" in statement for statement in statements)
        assert not any("FROM DOWNLOAD_PAGES" in statement for statement in statements)
        assert not any(
            statement.startswith(("ALTER TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX"))
            for statement in statements
        )
        with database.read_session() as session:
            revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert revision == "0015_job_cancellation_intent"
            assert "temporary_path_key" not in {
                column["name"]
                for column in inspect(session.connection()).get_columns("download_pages")
            }
    finally:
        event.remove(database.engine, "before_cursor_execute", force_temp_store_three)
        database.close()


def test_migration_0016_blocks_a_different_unicode_database_version_before_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, runner = _database_at_revision(tmp_path)
    statements: list[str] = []

    def record_statements(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> tuple[str, object]:
        statements.append(" ".join(statement.upper().split()))
        return statement, parameters

    event.listen(database.engine, "before_cursor_execute", record_statements, retval=True)
    monkeypatch.setattr(unicodedata, "unidata_version", "15.1.0")
    try:
        with pytest.raises(DatabaseMigrationError) as raised:
            runner.upgrade_to_head()

        assert raised.value.code is ErrorCode.DB_MIGRATION_FAILED
        assert raised.value.restored is True
        assert not any("FROM DOWNLOAD_PAGES" in statement for statement in statements)
        assert not any("CREATE TEMP" in statement for statement in statements)
        assert not any(
            statement.startswith(("ALTER TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX"))
            for statement in statements
        )
    finally:
        event.remove(database.engine, "before_cursor_execute", record_statements)
        database.close()


@pytest.mark.parametrize(
    ("pragma_name", "observed_value"),
    [("PRAGMA TEMP_STORE", "SELECT 2"), ("PRAGMA TEMP.CACHE_SIZE", "SELECT -1024")],
    ids=["temp-store-readback-not-file", "temp-cache-size-readback-mismatch"],
)
def test_migration_0016_aborts_if_temp_configuration_cannot_be_verified(
    tmp_path: Path,
    pragma_name: str,
    observed_value: str,
) -> None:
    database, runner = _database_at_revision(tmp_path)
    statements: list[str] = []

    def override_configuration_readback(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> tuple[str, object]:
        normalized = " ".join(statement.upper().split())
        statements.append(normalized)
        if normalized == pragma_name:
            return observed_value, parameters
        return statement, parameters

    event.listen(
        database.engine,
        "before_cursor_execute",
        override_configuration_readback,
        retval=True,
    )
    try:
        with pytest.raises(DatabaseMigrationError) as raised:
            runner.upgrade_to_head()

        assert raised.value.code is ErrorCode.DB_MIGRATION_FAILED
        assert raised.value.restored is True
        assert pragma_name in statements
        assert not any("CREATE TEMP" in statement for statement in statements)
        assert not any("FROM DOWNLOAD_PAGES" in statement for statement in statements)
        assert not any(
            statement.startswith(("ALTER TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX"))
            for statement in statements
        )
    finally:
        event.remove(
            database.engine,
            "before_cursor_execute",
            override_configuration_readback,
        )
        database.close()


def test_backup_failure_stops_before_any_migration(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    occupied_path = tmp_path / "not-a-directory"
    occupied_path.write_text("preserve this file", encoding="utf-8")
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, occupied_path / "backups")
    caplog.set_level(logging.ERROR, logger="yomira.migrations")
    try:
        with pytest.raises(DatabaseBackupError) as raised:
            runner.upgrade_to_head()

        assert raised.value.code == ErrorCode.DB_BACKUP_FAILED
        backup_record = next(
            record
            for record in caplog.records
            if record.name == "yomira.migrations" and record.msg == "database.backup_failed"
        )
        assert isinstance(getattr(backup_record, "trace_id", None), str)
        assert getattr(backup_record, "trace_id", "").strip()
        with database.read_session() as session:
            table_names = (
                session.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
                .scalars()
                .all()
            )
        assert "alembic_version" not in table_names
        assert "schema_history" not in table_names
        assert "series" not in table_names
        assert "sources" not in table_names
        assert "series_sources" not in table_names
        assert "chapters" not in table_names
        assert "chapter_sources" not in table_names
        assert "assets" not in table_names
        assert occupied_path.read_text(encoding="utf-8") == "preserve this file"
    finally:
        database.close()


def test_failed_migration_restores_pre_migration_database(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = open_test_database(tmp_path)
    backup_directory = tmp_path / "backups"
    with database.write_session() as session:
        session.execute(text("CREATE TABLE preserved_content (value TEXT NOT NULL)"))
        session.execute(text("INSERT INTO preserved_content(value) VALUES ('keep this row')"))

    runner = MigrationRunner(database, backup_directory)
    caplog.set_level(logging.ERROR, logger="yomira.migrations")

    def fail_after_committing_partial_ddl(config: Config, _head: str) -> None:
        connection = config.attributes["connection"]
        connection.exec_driver_sql("CREATE TABLE partial_migration (value INTEGER)")
        connection.commit()
        raise RuntimeError("injected migration failure after partial commit")

    monkeypatch.setattr(runner, "_apply_migrations", fail_after_committing_partial_ddl)
    try:
        with pytest.raises(DatabaseMigrationError) as raised:
            runner.upgrade_to_head()

        assert raised.value.code == ErrorCode.DB_MIGRATION_FAILED
        assert raised.value.retryable is False
        assert raised.value.restored is True
        assert raised.value.backup_path is not None
        assert raised.value.backup_path.is_file()

        with database.read_session() as session:
            saved_value = session.execute(text("SELECT value FROM preserved_content")).scalar_one()
            table_names = (
                session.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
                .scalars()
                .all()
            )
        assert saved_value == "keep this row"
        assert "partial_migration" not in table_names
        assert "alembic_version" not in table_names
        assert any(
            record.msg == "database.migration_failed"
            and record.__dict__.get("stage") == "apply_or_verify"
            for record in caplog.records
        )
        assert not any(
            record.msg == "database.migration_preflight_failed" for record in caplog.records
        )
    finally:
        database.close()


def test_backup_retention_keeps_three_automatic_and_all_manual_copies(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    runner = MigrationRunner(database, backup_directory)
    try:
        automatic_backups = [
            backup_directory / f"yomira.pre-migration.20260925T00000{index}Z.sqlite3"
            for index in range(4)
        ]
        for index, path in enumerate(automatic_backups):
            path.write_bytes(f"backup-{index}".encode("ascii"))
            timestamp_ns = index * 1_000_000_000
            os.utime(path, ns=(timestamp_ns, timestamp_ns))
        manual_backup = backup_directory / "manual-before-release.sqlite3"
        manual_backup.write_bytes(b"manual copy")

        report = runner.upgrade_to_head()

        assert automatic_backups[0].exists() is False
        assert automatic_backups[1].exists() is False
        assert all(path.exists() for path in automatic_backups[2:])
        assert report.backup_path is not None and report.backup_path.is_file()
        retained_automatic_backups = list(backup_directory.glob("yomira.pre-migration.*.sqlite3"))
        assert len(retained_automatic_backups) == 3
        assert manual_backup.read_bytes() == b"manual copy"
    finally:
        database.close()
