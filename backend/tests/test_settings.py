import os
from contextlib import nullcontext
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.settings import Settings, SettingsValidationError, load_settings


def test_settings_have_safe_bounded_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    settings = Settings()

    assert settings.app_env == "dev"
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000
    assert settings.log_level == "INFO"
    assert settings.log_max_bytes == 10 * 1024 * 1024
    assert settings.log_file_count == 5
    assert settings.data_root.is_absolute()
    assert settings.data_root.name == "YomiraData"
    assert settings.library_root.name == "Mangas de Yomira"


def test_production_adopts_existing_alpha_data_when_yomiradata_database_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    legacy_root = tmp_path / "Yomira" / "AlphaData"
    legacy_database = legacy_root / "database" / "yomira.sqlite3"
    legacy_database.parent.mkdir(parents=True)
    legacy_database.write_bytes(b"existing database marker")

    settings = load_settings({"YOMIRA_APP_ENV": "prod"})

    assert settings.data_root == legacy_root


def test_production_keeps_existing_yomiradata_database_when_both_roots_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    primary_root = tmp_path / "Yomira" / "YomiraData"
    legacy_root = tmp_path / "Yomira" / "AlphaData"
    for root in (primary_root, legacy_root):
        database = root / "database" / "yomira.sqlite3"
        database.parent.mkdir(parents=True)
        database.write_bytes(b"existing database marker")

    settings = load_settings({"YOMIRA_APP_ENV": "prod"})

    assert settings.data_root == primary_root


def test_default_library_root_uses_profile_documents_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_profile = tmp_path / "profile"
    monkeypatch.setenv("USERPROFILE", str(user_profile))
    if os.name == "nt":
        import winreg

        def unavailable(*_args: object) -> object:
            raise FileNotFoundError

        monkeypatch.setattr(winreg, "OpenKey", unavailable)

    assert Settings().library_root == user_profile / "Documents" / "Mangas de Yomira"


@pytest.mark.skipif(os.name != "nt", reason="Windows Documents redirection")
def test_default_library_root_uses_redirected_documents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import winreg

    documents = tmp_path / "OneDrive" / "Documents"
    monkeypatch.setattr(winreg, "OpenKey", lambda *_args: nullcontext(object()))
    monkeypatch.setattr(winreg, "QueryValueEx", lambda *_args: (str(documents), winreg.REG_SZ))
    assert Settings().library_root == documents / "Mangas de Yomira"


def test_library_root_can_be_overridden_by_environment(tmp_path: Path) -> None:
    settings = load_settings({"YOMIRA_LIBRARY_ROOT": str(tmp_path / "Mangas")})

    assert settings.library_root == tmp_path / "Mangas"


def test_environment_loader_rejects_relative_library_root() -> None:
    with pytest.raises(SettingsValidationError) as error:
        load_settings({"YOMIRA_LIBRARY_ROOT": "relative-mangas"})

    assert error.value.fields == ("library_root",)


def test_settings_reject_relative_data_root() -> None:
    with pytest.raises(ValidationError):
        Settings(data_root=Path("relative-data"))


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_settings_accept_loopback_addresses(host: str) -> None:
    assert Settings(api_host=host).api_host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "localhost"])
def test_settings_reject_non_loopback_or_non_ip_hosts(host: str) -> None:
    with pytest.raises(ValidationError):
        Settings(api_host=host)


@pytest.mark.parametrize("port", [0, 65536])
def test_settings_reject_ports_outside_tcp_range(port: int) -> None:
    with pytest.raises(ValidationError):
        Settings(api_port=port)


def test_settings_reject_unknown_environment_names() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"app_env": "staging"})


def test_environment_loader_reads_only_supported_prefixed_values(
    tmp_path: Path,
) -> None:
    settings = load_settings(
        {
            "YOMIRA_APP_ENV": "test",
            "YOMIRA_API_HOST": "::1",
            "YOMIRA_API_PORT": "9123",
            "YOMIRA_LOG_LEVEL": "WARNING",
            "YOMIRA_DATA_ROOT": str(tmp_path),
            "UNRELATED_SETTING": "ignored",
        }
    )

    assert settings.app_env == "test"
    assert settings.api_host == "::1"
    assert settings.api_port == 9123
    assert settings.log_level == "WARNING"
    assert settings.log_max_bytes == 10 * 1024 * 1024
    assert settings.log_file_count == 5
    assert settings.data_root == tmp_path


def test_environment_loader_accepts_bounded_log_rotation_settings(
    tmp_path: Path,
) -> None:
    settings = load_settings(
        {
            "YOMIRA_DATA_ROOT": str(tmp_path),
            "YOMIRA_LOG_MAX_BYTES": str(2 * 1024 * 1024),
            "YOMIRA_LOG_FILE_COUNT": "3",
        }
    )

    assert settings.log_max_bytes == 2 * 1024 * 1024
    assert settings.log_file_count == 3


@pytest.mark.parametrize(
    ("environment_name", "value"),
    [
        ("YOMIRA_LOG_MAX_BYTES", "0"),
        ("YOMIRA_LOG_MAX_BYTES", str(26 * 1024 * 1024)),
        ("YOMIRA_LOG_FILE_COUNT", "0"),
        ("YOMIRA_LOG_FILE_COUNT", "1"),
        ("YOMIRA_LOG_FILE_COUNT", "11"),
    ],
)
def test_environment_loader_rejects_unbounded_log_rotation_settings(
    environment_name: str,
    value: str,
) -> None:
    with pytest.raises(SettingsValidationError) as error:
        load_settings({environment_name: value})

    assert error.value.fields in {("log_file_count",), ("log_max_bytes",)}


def test_environment_loader_fails_without_exposing_invalid_values() -> None:
    with pytest.raises(SettingsValidationError) as error:
        load_settings({"YOMIRA_API_HOST": "0.0.0.0"})

    assert error.value.fields == ("api_host",)
    assert str(error.value) == "Invalid Yomira runtime settings."


def test_environment_loader_rejects_relative_data_root() -> None:
    with pytest.raises(SettingsValidationError) as error:
        load_settings({"YOMIRA_DATA_ROOT": "relative-data"})

    assert error.value.fields == ("data_root",)
