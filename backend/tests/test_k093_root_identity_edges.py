from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from sqlalchemy.engine import Connection
from support.sqlite import open_test_database

from app import lifecycle
from app.persistence import windows_paths
from app.persistence.database import DatabaseInitializationError
from app.persistence.migrations import runner as migration_runner_module
from app.persistence.migrations.runner import (
    DatabaseIntegrityError,
    DatabaseMigrationError,
    MigrationRunner,
)
from app.persistence.windows_paths import DataRootIdentityError
from app.runtime import RuntimeState
from app.settings import Settings


def test_initialize_runtime_rejects_remote_root_before_creating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The local sandbox path is classified as a UNC volume by deterministic
    # Win32 adapters, so the test never contacts or writes to a network share.
    remote_root = tmp_path / "uncreated-remote-root"
    assert not remote_root.exists()

    calls: dict[str, list[Any]] = {
        "volume_path": [],
        "drive_type": [],
        "file_id_info": [],
    }

    def get_volume_path_name(path: Path) -> str:
        calls["volume_path"].append(path)
        return "\\\\test-server\\share\\"

    def get_drive_type(volume_root: str) -> int:
        calls["drive_type"].append(volume_root)
        return 4  # DRIVE_REMOTE

    def get_file_id_info(path: Path) -> tuple[int, bytes]:
        calls["file_id_info"].append(path)
        return 0x1234, bytes(range(16))

    monkeypatch.setattr(windows_paths, "_get_volume_path_name", get_volume_path_name)
    monkeypatch.setattr(windows_paths, "_get_drive_type", get_drive_type)
    monkeypatch.setattr(windows_paths, "_get_file_id_info", get_file_id_info)

    target_app = FastAPI()
    target_app.state.settings = Settings(app_env="test", data_root=remote_root)
    target_app.state.runtime = RuntimeState()

    with pytest.raises(DatabaseInitializationError):
        asyncio.run(lifecycle.initialize_runtime(target_app))

    assert calls["volume_path"]
    assert calls["drive_type"] == ["\\\\test-server\\share\\"]
    assert calls["file_id_info"] == []
    assert not remote_root.exists()


def test_migration_runner_does_not_restore_after_root_identity_invalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = open_test_database(tmp_path)
    identity = database.data_root_identity
    root_changed = False
    identity_invalidated = False

    original_file_id_info = cast(
        Callable[[Path], tuple[int, bytes]],
        windows_paths.__dict__["_get_file_id_info"],
    )

    def get_changed_file_id_info(path: Path) -> tuple[int, bytes]:
        serial, file_id = original_file_id_info(path)
        return (serial + 1, file_id) if root_changed else (serial, file_id)

    monkeypatch.setattr(windows_paths, "_get_file_id_info", get_changed_file_id_info)

    post_invalidation_connections: list[str] = []
    original_sqlite_connect = sqlite3.connect

    def observe_sqlite_connect(
        database_path: str | Path,
        *args: Any,
        **kwargs: Any,
    ) -> sqlite3.Connection:
        if identity_invalidated:
            post_invalidation_connections.append(str(database_path))
        return original_sqlite_connect(database_path, *args, **kwargs)

    monkeypatch.setattr(
        migration_runner_module.sqlite3,
        "connect",
        observe_sqlite_connect,
    )

    runner = MigrationRunner(database, tmp_path / "backups")

    def fail_after_root_identity_change(_config: Any, _head: str) -> None:
        nonlocal root_changed, identity_invalidated
        root_changed = True
        with pytest.raises(DataRootIdentityError):
            identity.assert_current()
        identity_invalidated = True
        raise RuntimeError("injected migration failure after root identity change")

    monkeypatch.setattr(runner, "_apply_migrations", fail_after_root_identity_change)

    try:
        with pytest.raises(
            (DatabaseMigrationError, DatabaseInitializationError, DataRootIdentityError)
        ):
            runner.upgrade_to_head()

        assert identity_invalidated
        assert post_invalidation_connections == []
    finally:
        database.close()


def test_migration_restore_checks_identity_before_touching_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = open_test_database(tmp_path)
    root_changed = False
    original_file_id_info = cast(
        Callable[[Path], tuple[int, bytes]],
        windows_paths.__dict__["_get_file_id_info"],
    )

    def get_changed_file_id_info(path: Path) -> tuple[int, bytes]:
        serial, file_id = original_file_id_info(path)
        return (serial + 1, file_id) if root_changed else (serial, file_id)

    monkeypatch.setattr(windows_paths, "_get_file_id_info", get_changed_file_id_info)
    connection_operations: list[str] = []

    class ConnectionSpy:
        def rollback(self) -> None:
            connection_operations.append("rollback")

        def invalidate(self) -> None:
            connection_operations.append("invalidate")

    runner = MigrationRunner(database, tmp_path / "backups")
    restore_backup_name = "_restore_verified_backup"
    restore_verified_backup = cast(
        Callable[[Connection, Path, str | None], None],
        getattr(runner, restore_backup_name),
    )
    root_changed = True
    caplog.set_level("ERROR", logger="yomira.migrations")
    try:
        with pytest.raises(DatabaseMigrationError):
            restore_verified_backup(
                cast(Connection, ConnectionSpy()),
                tmp_path / "missing-backup.sqlite3",
                None,
            )

        assert connection_operations == []
        restore_record = next(
            record
            for record in caplog.records
            if record.name == "yomira.migrations"
            and record.msg == "database.migration_restore_failed"
        )
        assert isinstance(getattr(restore_record, "trace_id", None), str)
        assert getattr(restore_record, "trace_id", "").strip()
    finally:
        database.close()


def test_missing_backup_integrity_check_does_not_create_a_file(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    missing_backup = tmp_path / "backups" / "missing.sqlite3"
    verify_path_integrity_name = "_verify_path_integrity"
    verify_path_integrity = cast(
        Callable[[Path], None],
        getattr(runner, verify_path_integrity_name),
    )
    try:
        with pytest.raises(DatabaseIntegrityError):
            verify_path_integrity(missing_backup)

        assert not missing_backup.exists()
    finally:
        database.close()
