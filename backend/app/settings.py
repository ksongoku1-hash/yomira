import ipaddress
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


def _local_app_data_directory() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data)

    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        return Path(user_profile) / "AppData" / "Local"

    return Path.home() / "AppData" / "Local"


def _default_data_root() -> Path:
    return _local_app_data_directory() / "Yomira" / "YomiraData"


def _legacy_alpha_data_root() -> Path:
    return _local_app_data_directory() / "Yomira" / "AlphaData"


def _documents_directory() -> Path:
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                directory, _ = winreg.QueryValueEx(key, "Personal")
            if isinstance(directory, str):
                path = Path(os.path.expandvars(directory))
                if path.is_absolute():
                    return path
        except OSError:
            pass
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        return Path(user_profile) / "Documents"
    return Path.home() / "Documents"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    app_env: Literal["dev", "test", "prod"] = "dev"
    fmd2_executable: Path | None = None
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=32 * 1024, le=25 * 1024 * 1024)
    log_file_count: int = Field(default=5, ge=2, le=10)
    data_root: Path = Field(default_factory=_default_data_root)
    library_root: Path = Field(default_factory=lambda: _documents_directory() / "Mangas de Yomira")

    @model_validator(mode="before")
    @classmethod
    def isolate_test_library_root(cls, value: object) -> object:
        if type(value) is not dict:
            return value
        values = cast(dict[str, object], dict(cast(dict[object, object], value)))
        if values.get("app_env") == "test" and "library_root" not in values:
            data_root = values.get("data_root")
            if not isinstance(data_root, (Path, str)):
                data_root = _default_data_root()
            values["library_root"] = Path(data_root) / "library"
        return values

    @field_validator("api_host")
    @classmethod
    def require_loopback_address(cls, value: str) -> str:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise ValueError("API host must be a loopback IP address.") from error

        if not address.is_loopback:
            raise ValueError("API host must be a loopback IP address.")
        return address.compressed

    @field_validator("data_root")
    @classmethod
    def require_absolute_data_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Data root must be an absolute path.")
        return value

    @field_validator("library_root")
    @classmethod
    def require_absolute_library_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Library root must be an absolute path.")
        return value

    @field_validator("fmd2_executable")
    @classmethod
    def require_absolute_fmd2_executable(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("FMD2 executable path must be absolute.")
        return value


class SettingsValidationError(ValueError):
    def __init__(self, fields: tuple[str, ...]) -> None:
        super().__init__("Invalid Yomira runtime settings.")
        self.fields = fields


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    source = os.environ if environ is None else environ
    environment_fields = {
        "YOMIRA_APP_ENV": "app_env",
        "YOMIRA_FMD2_EXECUTABLE": "fmd2_executable",
        "YOMIRA_API_HOST": "api_host",
        "YOMIRA_API_PORT": "api_port",
        "YOMIRA_LOG_LEVEL": "log_level",
        "YOMIRA_LOG_MAX_BYTES": "log_max_bytes",
        "YOMIRA_LOG_FILE_COUNT": "log_file_count",
        "YOMIRA_DATA_ROOT": "data_root",
        "YOMIRA_LIBRARY_ROOT": "library_root",
    }
    values = {
        model_field: source[environment_name]
        for environment_name, model_field in environment_fields.items()
        if environment_name in source
    }
    if values.get("app_env") == "prod" and "data_root" not in values:
        current_database = _default_data_root() / "database" / "yomira.sqlite3"
        legacy_root = _legacy_alpha_data_root()
        legacy_database = legacy_root / "database" / "yomira.sqlite3"
        if not current_database.is_file() and legacy_database.is_file():
            values["data_root"] = str(legacy_root)

    try:
        return Settings.model_validate(values)
    except ValidationError as error:
        invalid_fields = tuple(
            sorted(
                {
                    str(issue["loc"][0])
                    for issue in error.errors(include_url=False)
                    if issue.get("loc")
                }
            )
        )
        raise SettingsValidationError(invalid_fields) from error
