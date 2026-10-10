import logging
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from shutil import disk_usage
from typing import Final

from pydantic import ValidationError

from app.errors import ErrorCode
from app.persistence.database import SQLiteDatabase
from app.persistence.windows_paths import DataRootIdentity
from app.settings import Settings

logger = logging.getLogger("yomira.startup_self_test")
_TEMP_TEST_CONTENT: Final[bytes] = b"yomira-startup-self-test-v1"


class StartupSelfTestError(RuntimeError):
    code: ErrorCode
    retryable = False

    def __init__(self, check_id: str, code: ErrorCode) -> None:
        super().__init__("A required startup self-test failed.")
        self.check_id = check_id
        self.code = code


@dataclass(frozen=True, slots=True)
class StartupSelfTestReport:
    checks: tuple[str, ...]
    disk_free_bytes: int


def _report_failure(
    check_id: str,
    error: BaseException,
    *,
    code: ErrorCode = ErrorCode.SYS_STARTUP_FAILED,
    invalid_fields: tuple[str, ...] = (),
) -> StartupSelfTestError:
    extra: dict[str, object] = {
        "error_code": code.value,
        "retryable": False,
        "stage": check_id,
        "failed_check": check_id,
        "cause_type": type(error).__name__,
    }
    if invalid_fields:
        extra["invalid_fields"] = list(invalid_fields)
    logger.error("startup.self_test_failed", extra=extra)
    return StartupSelfTestError(check_id, code)


def _run_check(check_id: str, check: Callable[[], None]) -> None:
    try:
        check()
    except StartupSelfTestError:
        raise
    except Exception as error:
        code = (
            ErrorCode.SYS_INVALID_CONFIG
            if check_id == "configuration"
            else ErrorCode.SYS_STARTUP_FAILED
        )
        invalid_fields: tuple[str, ...] = ()
        if check_id == "configuration" and isinstance(error, ValidationError):
            invalid_fields = tuple(
                sorted(
                    {
                        str(issue["loc"][0])
                        for issue in error.errors(include_url=False)
                        if issue.get("loc") and str(issue["loc"][0]) in Settings.model_fields
                    }
                )
            )
        raise _report_failure(
            check_id,
            error,
            code=code,
            invalid_fields=invalid_fields,
        ) from None


def _assert_plain_directory(path: Path, identity: DataRootIdentity) -> None:
    identity.assert_current()
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir()
        identity.assert_current()
        info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    if stat.S_ISLNK(info.st_mode) or bool(attributes & 0x400) or not stat.S_ISDIR(info.st_mode):
        raise OSError("A required data directory is not a plain directory.")
    identity.assert_current()


def _check_database_accessible(database: SQLiteDatabase) -> None:
    if database.is_closed:
        raise OSError("The database is closed.")
    with database.read_session() as session:
        result = session.connection().exec_driver_sql("SELECT 1").scalar_one()
    if result != 1:
        raise OSError("The database probe returned an unexpected result.")


def _check_schema_compatible(
    database: SQLiteDatabase,
    expected_schema_revision: str,
) -> None:
    if database.is_closed:
        raise OSError("The database is closed.")
    with database.read_session() as session:
        connection = session.connection()
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one_or_none()
        history_revision = connection.exec_driver_sql(
            "SELECT resulting_revision FROM schema_history ORDER BY event_id DESC LIMIT 1"
        ).scalar_one_or_none()
    if revision != expected_schema_revision or history_revision != expected_schema_revision:
        raise ValueError("The active schema revision is not compatible.")


def _check_filesystem_accessible(
    data_root_identity: DataRootIdentity,
) -> None:
    root = data_root_identity.canonical_path
    _assert_plain_directory(root, data_root_identity)
    with os.scandir(root) as entries:
        next(entries, None)
    data_root_identity.assert_current()


def _check_temp_writable_deletable(
    data_root_identity: DataRootIdentity,
) -> None:
    temp_directory = data_root_identity.canonical_path / "temp"
    _assert_plain_directory(temp_directory, data_root_identity)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".yomira-startup-check-",
        suffix=".tmp",
        dir=temp_directory,
    )
    temporary_path = Path(temporary_name)
    try:
        data_root_identity.assert_current()
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(_TEMP_TEST_CONTENT)
            temporary_file.flush()
        with temporary_path.open("rb") as temporary_file:
            if temporary_file.read(len(_TEMP_TEST_CONTENT) + 1) != _TEMP_TEST_CONTENT:
                raise OSError("The temporary-file probe could not be read back.")
        data_root_identity.assert_current()
        temporary_path.unlink()
        data_root_identity.assert_current()
        try:
            temporary_path.lstat()
        except FileNotFoundError:
            return
        raise OSError("The temporary-file probe remained after deletion.")
    finally:
        try:
            temporary_path.lstat()
        except FileNotFoundError:
            pass
        else:
            temporary_path.unlink()


def _check_library_readable(
    data_root_identity: DataRootIdentity,
    library_root_identity: DataRootIdentity | None = None,
) -> None:
    library_directory = (
        library_root_identity.canonical_path
        if library_root_identity is not None
        else data_root_identity.canonical_path / "library"
    )
    identity = library_root_identity or data_root_identity
    _assert_plain_directory(library_directory, identity)
    with os.scandir(library_directory) as entries:
        next(entries, None)
    identity.assert_current()


def _check_disk_space(data_root_identity: DataRootIdentity) -> int:
    data_root_identity.assert_current()
    usage = disk_usage(data_root_identity.canonical_path)
    values = (usage.total, usage.used, usage.free)
    if any(type(value) is not int or value < 0 for value in values):
        raise OSError("The filesystem returned invalid disk-usage values.")
    data_root_identity.assert_current()
    return usage.free


def run_startup_self_tests(
    settings: Settings,
    *,
    database: SQLiteDatabase,
    data_root_identity: DataRootIdentity,
    library_root_identity: DataRootIdentity | None = None,
    expected_schema_revision: str,
) -> StartupSelfTestReport:
    passed: list[str] = []

    def check_configuration() -> None:
        Settings.model_validate(settings.model_dump())

    _run_check("configuration", check_configuration)
    passed.append("configuration")

    _run_check("database_accessible", lambda: _check_database_accessible(database))
    passed.append("database_accessible")

    _run_check(
        "schema_compatible",
        lambda: _check_schema_compatible(database, expected_schema_revision),
    )
    passed.append("schema_compatible")

    _run_check(
        "filesystem_accessible",
        lambda: _check_filesystem_accessible(data_root_identity),
    )
    passed.append("filesystem_accessible")

    _run_check(
        "temp_writable_deletable",
        lambda: _check_temp_writable_deletable(data_root_identity),
    )
    passed.append("temp_writable_deletable")

    _run_check(
        "library_readable",
        lambda: _check_library_readable(data_root_identity, library_root_identity),
    )
    passed.append("library_readable")

    disk_free_bytes = 0

    def check_disk_space() -> None:
        nonlocal disk_free_bytes
        disk_free_bytes = _check_disk_space(data_root_identity)

    _run_check("disk_space", check_disk_space)
    passed.append("disk_space")
    return StartupSelfTestReport(tuple(passed), disk_free_bytes)
