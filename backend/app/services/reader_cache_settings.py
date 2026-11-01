"""Persistent budget for disposable reader images, independent of reader presentation."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock

from sqlalchemy.exc import SQLAlchemyError

from app.domain.setting import Setting
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.setting import setting_to_row
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.models.setting import SettingRow
from app.services.online_image_cache import OnlineImageCache

READER_DISK_CACHE_KEY = "reader.disk_cache_mb"
MIN_READER_DISK_CACHE_MB = 128
MAX_READER_DISK_CACHE_MB = 8192
DEFAULT_READER_DISK_CACHE_MB = 1024
_MIB = 1024 * 1024


class ReaderCacheSettingsError(RuntimeError):
    """The cache or its persisted configuration could not be safely inspected or changed."""


@dataclass(frozen=True, slots=True)
class ReaderCacheSettings:
    limit_mb: int
    used_bytes: int
    entries: int
    file_bytes: int


def _validate_limit(limit_mb: int) -> None:
    if (
        type(limit_mb) is not int
        or not MIN_READER_DISK_CACHE_MB <= limit_mb <= MAX_READER_DISK_CACHE_MB
    ):
        raise ValueError("Reader disk cache must be an integer from 128 through 8192 MiB")


class ReaderCacheSettingsService:
    def __init__(self, database: SQLiteDatabase, cache: OnlineImageCache) -> None:
        self._database = database
        self._cache = cache
        self._lock = RLock()

    @staticmethod
    def _decode(row: SettingRow | None) -> int:
        if row is None:
            return DEFAULT_READER_DISK_CACHE_MB
        try:
            if row.schema_version != 1 or len(row.value_json) > 5:
                raise ValueError("Unsupported reader cache setting")
            value: object = json.loads(row.value_json)
            if type(value) is not int:
                raise ValueError("Invalid reader cache setting")
            _validate_limit(value)
            return value
        except (TypeError, ValueError, RecursionError) as error:
            raise ReaderCacheSettingsError(
                "La configuración de caché guardada no es válida."
            ) from error

    def _limit(self) -> int:
        with self._database.read_session() as session:
            return self._decode(session.get(SettingRow, READER_DISK_CACHE_KEY))

    def restore(self) -> None:
        with self._lock:
            try:
                with self._cache.changing_limit(self._limit() * _MIB):
                    pass
            except (OSError, sqlite3.Error, RuntimeError, SQLAlchemyError) as error:
                raise ReaderCacheSettingsError(
                    "No se pudo aplicar la configuración de caché; sus archivos se conservan."
                ) from error

    def get(self) -> ReaderCacheSettings:
        with self._lock:
            try:
                limit = self._limit()
                current = self._cache.status()
                if current.max_bytes != limit * _MIB:
                    self.restore()
                    current = self._cache.status()
                return ReaderCacheSettings(
                    limit_mb=limit,
                    used_bytes=current.used_bytes,
                    entries=current.entries,
                    file_bytes=current.file_bytes,
                )
            except (OSError, sqlite3.Error, RuntimeError, SQLAlchemyError) as error:
                raise ReaderCacheSettingsError(
                    "No se pudo consultar la caché de forma segura; sus archivos se conservan."
                ) from error

    def put(self, limit_mb: int) -> ReaderCacheSettings:
        _validate_limit(limit_mb)
        with self._lock:
            try:
                self._limit()
                # The cache lock spans the settings commit; failures restore its previous RAM limit.
                with self._cache.changing_limit(limit_mb * _MIB):
                    with self._database.write_session() as session:
                        row = session.get(SettingRow, READER_DISK_CACHE_KEY)
                        self._decode(row)
                        now = datetime.now(UTC)
                        if row is None:
                            session.add(
                                setting_to_row(
                                    Setting.new_local(
                                        READER_DISK_CACHE_KEY,
                                        str(limit_mb),
                                        schema_version=1,
                                        updated_at=now,
                                    )
                                )
                            )
                        else:
                            row.value_json = str(limit_mb)
                            row.updated_at_us = datetime_to_epoch_us(now)
                return self.get()
            except (OSError, sqlite3.Error, RuntimeError, SQLAlchemyError) as error:
                raise ReaderCacheSettingsError(
                    "No se pudo guardar el límite de caché. La configuración anterior se conserva."
                ) from error
