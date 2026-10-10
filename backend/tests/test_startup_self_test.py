import logging
from pathlib import Path
from typing import NamedTuple

import pytest
from support.sqlite import sqlite_test_context

from app.errors import ErrorCode
from app.persistence.migrations.runner import MigrationRunner
from app.services.startup_self_test import (
    StartupSelfTestError,
    run_startup_self_tests,
)
from app.settings import Settings


def _ready_database(tmp_path: Path):
    context = sqlite_test_context(tmp_path)
    database = context.open()
    migration = MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    return database, migration.current_revision, database.data_root_identity


class _DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


def test_startup_self_test_checks_runtime_and_accepts_zero_free_space(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, revision, identity = _ready_database(tmp_path)

    def zero_free_space(_path: Path) -> _DiskUsage:
        return _DiskUsage(total=10, used=10, free=0)

    monkeypatch.setattr("app.services.startup_self_test.disk_usage", zero_free_space)
    try:
        report = run_startup_self_tests(
            Settings(app_env="test", data_root=tmp_path),
            database=database,
            data_root_identity=identity,
            expected_schema_revision=revision,
        )
        assert report.checks == (
            "configuration",
            "database_accessible",
            "schema_compatible",
            "filesystem_accessible",
            "temp_writable_deletable",
            "library_readable",
            "disk_space",
        )
        assert report.disk_free_bytes == 0
        assert list((tmp_path / "temp").iterdir()) == []
        assert (tmp_path / "library").is_dir()
    finally:
        database.shutdown()


def test_startup_self_test_rejects_an_incompatible_schema_without_path_details(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database, revision, identity = _ready_database(tmp_path)
    caplog.set_level(logging.ERROR, logger="yomira.startup_self_test")
    try:
        with pytest.raises(StartupSelfTestError) as raised:
            run_startup_self_tests(
                Settings(app_env="test", data_root=tmp_path),
                database=database,
                data_root_identity=identity,
                expected_schema_revision=f"{revision}-unexpected",
            )
        assert raised.value.check_id == "schema_compatible"
        assert raised.value.code is ErrorCode.SYS_STARTUP_FAILED
        assert str(tmp_path) not in str(raised.value)
        failure_record = next(
            record for record in caplog.records if record.name == "yomira.startup_self_test"
        )
        assert failure_record.__dict__["stage"] == "schema_compatible"
        assert failure_record.__dict__["failed_check"] == "schema_compatible"
        assert failure_record.__dict__["error_code"] == ErrorCode.SYS_STARTUP_FAILED.value
    finally:
        database.shutdown()


def test_startup_self_test_rejects_invalid_configuration_with_catalog_code(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database, revision, identity = _ready_database(tmp_path)
    caplog.set_level(logging.ERROR, logger="yomira.startup_self_test")
    invalid_settings = Settings.model_construct(
        app_env="test",
        api_host="0.0.0.0",
        api_port=8000,
        log_level="INFO",
        log_max_bytes=10 * 1024 * 1024,
        log_file_count=5,
        data_root=tmp_path,
    )
    try:
        with pytest.raises(StartupSelfTestError) as raised:
            run_startup_self_tests(
                invalid_settings,
                database=database,
                data_root_identity=identity,
                expected_schema_revision=revision,
            )
        assert raised.value.check_id == "configuration"
        assert raised.value.code is ErrorCode.SYS_INVALID_CONFIG
        failure_record = next(
            record for record in caplog.records if record.name == "yomira.startup_self_test"
        )
        assert failure_record.__dict__["invalid_fields"] == ["api_host"]
    finally:
        database.shutdown()


def test_startup_self_test_fails_when_library_is_not_a_directory(
    tmp_path: Path,
) -> None:
    database, revision, identity = _ready_database(tmp_path)
    (tmp_path / "library").write_text("preserve", encoding="utf-8")
    try:
        with pytest.raises(StartupSelfTestError) as raised:
            run_startup_self_tests(
                Settings(app_env="test", data_root=tmp_path),
                database=database,
                data_root_identity=identity,
                expected_schema_revision=revision,
            )
        assert raised.value.check_id == "library_readable"
        assert (tmp_path / "library").read_text(encoding="utf-8") == "preserve"
    finally:
        database.shutdown()


def test_startup_self_test_retries_cleanup_after_temp_delete_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, revision, identity = _ready_database(tmp_path)
    original_unlink = Path.unlink
    first_delete_failed = False

    def fail_first_probe_delete(path: Path, *, missing_ok: bool = False) -> None:
        nonlocal first_delete_failed
        if path.name.startswith(".yomira-startup-check-") and not first_delete_failed:
            first_delete_failed = True
            raise PermissionError("private path detail")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_first_probe_delete)
    try:
        with pytest.raises(StartupSelfTestError) as raised:
            run_startup_self_tests(
                Settings(app_env="test", data_root=tmp_path),
                database=database,
                data_root_identity=identity,
                expected_schema_revision=revision,
            )
        assert raised.value.check_id == "temp_writable_deletable"
        assert first_delete_failed is True
        assert list((tmp_path / "temp").iterdir()) == []
    finally:
        database.shutdown()


def test_startup_self_test_reports_unavailable_disk_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, revision, identity = _ready_database(tmp_path)

    def unavailable(_path: Path) -> object:
        raise OSError("private path and system detail")

    monkeypatch.setattr("app.services.startup_self_test.disk_usage", unavailable)
    try:
        with pytest.raises(StartupSelfTestError) as raised:
            run_startup_self_tests(
                Settings(app_env="test", data_root=tmp_path),
                database=database,
                data_root_identity=identity,
                expected_schema_revision=revision,
            )
        assert raised.value.check_id == "disk_space"
        assert str(tmp_path) not in str(raised.value)
        assert "private path" not in str(raised.value)
    finally:
        database.shutdown()
