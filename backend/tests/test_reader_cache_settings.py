import asyncio
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session
from support.sqlite import open_test_database

from app.domain.setting import Setting
from app.persistence.database import SQLiteDatabase
from app.persistence.disk_budget import DiskBudgetManager
from app.persistence.mappers.setting import setting_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.setting import SettingRow
from app.routes.reader_cache import router
from app.services.online_image_cache import OnlineImageCache
from app.services.reader_cache_settings import (
    READER_DISK_CACHE_KEY,
    ReaderCacheSettingsError,
    ReaderCacheSettingsService,
)


def _service(tmp_path: Path) -> tuple[SQLiteDatabase, OnlineImageCache, ReaderCacheSettingsService]:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    cache = OnlineImageCache(database, DiskBudgetManager(database))
    return database, cache, ReaderCacheSettingsService(database, cache)


def test_defaults_and_persisted_budget_survive_reopening_the_library(tmp_path: Path) -> None:
    database, cache, service = _service(tmp_path)
    try:
        assert service.get().limit_mb == 1024
        assert not (tmp_path / "cache").exists()
        assert service.put(2048).limit_mb == 2048
        assert cache.status().max_bytes == 2048 * 1024**2
    finally:
        database.close()
    database = open_test_database(tmp_path)
    try:
        cache = OnlineImageCache(database, DiskBudgetManager(database))
        restored = ReaderCacheSettingsService(database, cache)
        restored.restore()
        assert restored.get().limit_mb == 2048
        assert cache.status().max_bytes == 2048 * 1024**2
    finally:
        database.close()


def test_failed_settings_commit_restores_the_runtime_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, cache, service = _service(tmp_path)

    @contextmanager
    def unavailable() -> Generator[Session]:
        raise RuntimeError("Write blocked")
        yield  # pragma: no cover

    try:
        monkeypatch.setattr(database, "write_session", unavailable)
        with pytest.raises(ReaderCacheSettingsError):
            service.put(2048)
        assert cache.status().max_bytes == 1024**3
        assert service.get().limit_mb == 1024
    finally:
        database.close()


@pytest.mark.parametrize("value", ["true", '"1024"', "127", "8193", "1024.0"])
def test_invalid_saved_value_is_not_overwritten(tmp_path: Path, value: str) -> None:
    database, _cache, service = _service(tmp_path)
    try:
        with database.write_session() as session:
            session.add(
                setting_to_row(
                    Setting.new_local(
                        READER_DISK_CACHE_KEY,
                        value,
                        schema_version=1,
                        updated_at=datetime.now(UTC),
                    )
                )
            )
        with pytest.raises(ReaderCacheSettingsError):
            service.get()
        with pytest.raises(ReaderCacheSettingsError):
            service.put(1024)
        with database.read_session() as session:
            row = session.get(SettingRow, READER_DISK_CACHE_KEY)
            assert row is not None and row.value_json == value
        assert not (tmp_path / "cache").exists()
    finally:
        database.close()


def test_concurrent_configuration_writes_keep_database_and_runtime_in_agreement(
    tmp_path: Path,
) -> None:
    database, cache, service = _service(tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(service.put, [128, 256, 512, 2048]))
        assert [result.limit_mb for result in results] == [128, 256, 512, 2048]
        assert cache.status().max_bytes == service.get().limit_mb * 1024**2
    finally:
        database.close()


def test_api_contract_and_strict_limits(tmp_path: Path) -> None:
    async def run() -> None:
        database, _cache, service = _service(tmp_path)
        app = FastAPI()
        app.include_router(router)
        app.state.runtime = SimpleNamespace(reader_cache_settings_service=service)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.get("/api/v1/settings/reader-cache")
                assert response.status_code == 200
                assert response.json() == {
                    "data": {"limit_mb": 1024, "used_bytes": 0, "entries": 0, "file_bytes": 0},
                    "meta": {},
                }
                response = await client.put(
                    "/api/v1/settings/reader-cache", json={"limit_mb": 8192}
                )
                assert response.status_code == 200 and response.json()["data"]["limit_mb"] == 8192
                for invalid in [127, 8193, True, 128.5, "1024"]:
                    response = await client.put(
                        "/api/v1/settings/reader-cache", json={"limit_mb": invalid}
                    )
                    assert response.status_code == 422
                response = await client.put(
                    "/api/v1/settings/reader-cache", json={"limit_mb": 1024, "clear": True}
                )
                assert response.status_code == 422
        finally:
            database.close()

    asyncio.run(run())
