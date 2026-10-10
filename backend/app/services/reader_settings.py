from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, NoReturn, cast

from sqlalchemy import select

from app.domain.setting import Setting
from app.errors import ErrorCode
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.setting import setting_from_row, setting_to_row
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.models.setting import SettingRow
from app.tracing import create_trace_id, current_trace_id

logger = logging.getLogger("yomira.settings")

ReaderMode = Literal["single", "vertical", "double"]
SETTING_SCHEMA_VERSION = 1

_SETTING_KEYS = (
    "reader.cache_limit_mb",
    "reader.preload_ahead",
    "reader.mode",
    "reader.rtl",
    "reader.e_reader",
)


@dataclass(frozen=True, slots=True)
class ReaderSettings:
    cache_limit_mb: int = 500
    preload_ahead: int = 2
    mode: ReaderMode = "single"
    rtl: bool = False
    e_reader: bool = False

    def __post_init__(self) -> None:
        if type(self.cache_limit_mb) is not int or not 16 <= self.cache_limit_mb <= 500:
            raise ValueError("cache_limit_mb must be an integer from 16 through 500")
        if type(self.preload_ahead) is not int or not 0 <= self.preload_ahead <= 2:
            raise ValueError("preload_ahead must be an integer from 0 through 2")
        if type(self.mode) is not str or self.mode not in {"single", "vertical", "double"}:
            raise ValueError("mode must be single, vertical, or double")
        if type(self.rtl) is not bool:
            raise ValueError("rtl must be a boolean")
        if type(self.e_reader) is not bool:
            raise ValueError("e_reader must be a boolean")


class ReaderSettingsValueError(ValueError):
    """A stored reader preference is incompatible with the current typed contract."""

    def __init__(self, setting_key: str, issue: str) -> None:
        self.setting_key = setting_key
        self.issue = issue
        super().__init__("A persisted reader setting is invalid.")


class ReaderSettingsService:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def get(self) -> ReaderSettings:
        with self._database.read_session() as session:
            rows = {
                row.setting_key: row
                for row in session.scalars(
                    select(SettingRow).where(SettingRow.setting_key.in_(_SETTING_KEYS))
                ).all()
            }

        values: dict[str, object] = {
            "cache_limit_mb": 500,
            "preload_ahead": 2,
            "mode": "single",
            "rtl": False,
            "e_reader": False,
        }
        for setting_key, field_name in (
            ("reader.cache_limit_mb", "cache_limit_mb"),
            ("reader.preload_ahead", "preload_ahead"),
            ("reader.mode", "mode"),
            ("reader.rtl", "rtl"),
            ("reader.e_reader", "e_reader"),
        ):
            row = rows.get(setting_key)
            if row is not None:
                values[field_name] = self._decode_row(row)

        return ReaderSettings(
            cache_limit_mb=cast(int, values["cache_limit_mb"]),
            preload_ahead=cast(int, values["preload_ahead"]),
            mode=cast(ReaderMode, values["mode"]),
            rtl=cast(bool, values["rtl"]),
            e_reader=cast(bool, values["e_reader"]),
        )

    def put(self, settings: ReaderSettings) -> ReaderSettings:
        if type(settings) is not ReaderSettings:
            raise TypeError("settings must be a ReaderSettings value")

        values: tuple[tuple[str, object], ...] = (
            ("reader.cache_limit_mb", settings.cache_limit_mb),
            ("reader.preload_ahead", settings.preload_ahead),
            ("reader.mode", settings.mode),
            ("reader.rtl", settings.rtl),
            ("reader.e_reader", settings.e_reader),
        )
        now_us = datetime_to_epoch_us(datetime.now(UTC))

        with self._database.write_session() as session:
            rows = {
                row.setting_key: row
                for row in session.scalars(
                    select(SettingRow).where(SettingRow.setting_key.in_(_SETTING_KEYS))
                ).all()
            }
            for setting_key, value in values:
                value_json = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
                row = rows.get(setting_key)
                if row is None:
                    session.add(
                        setting_to_row(
                            Setting.new_local(
                                setting_key,
                                value_json,
                                schema_version=SETTING_SCHEMA_VERSION,
                                updated_at=datetime.now(UTC),
                            )
                        )
                    )
                else:
                    row.value_json = value_json
                    row.schema_version = SETTING_SCHEMA_VERSION
                    row.updated_at_us = now_us

        return settings

    def _decode_row(self, row: SettingRow) -> object:
        setting_key = row.setting_key
        try:
            setting = setting_from_row(row)
        except (OverflowError, TypeError, ValueError, RecursionError):
            self._raise_invalid(setting_key, "invalid_record")

        if setting.schema_version != SETTING_SCHEMA_VERSION:
            self._raise_invalid(setting_key, "unsupported_schema_version")
        if len(setting.value_json) > 32:
            self._raise_invalid(setting_key, "invalid_value")
        try:
            value: object = json.loads(setting.value_json)
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
            self._raise_invalid(setting_key, "invalid_json")

        valid = False
        if setting_key == "reader.cache_limit_mb":
            valid = type(value) is int and 16 <= value <= 500
        elif setting_key == "reader.preload_ahead":
            valid = type(value) is int and 0 <= value <= 2
        elif setting_key == "reader.mode":
            valid = type(value) is str and value in {"single", "vertical", "double"}
        elif setting_key == "reader.rtl":
            valid = type(value) is bool
        elif setting_key == "reader.e_reader":
            valid = type(value) is bool
        if not valid:
            self._raise_invalid(setting_key, "invalid_value")
        return value

    @staticmethod
    def _raise_invalid(setting_key: str, issue: str) -> NoReturn:
        trace_id = current_trace_id() or create_trace_id()
        logger.error(
            "settings.reader.persisted_value_invalid",
            extra={
                "error_code": ErrorCode.SYS_INVALID_CONFIG.value,
                "retryable": False,
                "setting_key": setting_key,
                "issue": issue,
                "trace_id": trace_id,
            },
        )
        raise ReaderSettingsValueError(setting_key, issue)
