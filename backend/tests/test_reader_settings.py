from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from support.sqlite import open_test_database

from app.domain.setting import Setting
from app.factory import create_app
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.setting import setting_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.setting import SettingRow
from app.runtime import RuntimeState
from app.services.reader_settings import (
    ReaderSettings,
    ReaderSettingsService,
    ReaderSettingsValueError,
)
from app.settings import Settings


def _database(tmp_path: Path) -> SQLiteDatabase:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    return database


def _rows(database: SQLiteDatabase) -> dict[str, SettingRow]:
    with database.read_session() as session:
        return {
            row.setting_key: row
            for row in session.scalars(select(SettingRow)).all()
        }


def _ready_app(tmp_path: Path) -> tuple[FastAPI, SQLiteDatabase]:
    app = create_app(Settings(app_env="test", data_root=tmp_path))
    database = _database(tmp_path)
    runtime = app.state.runtime
    assert isinstance(runtime, RuntimeState)
    runtime.database = database
    runtime.reader_settings_service = ReaderSettingsService(database)
    return app, database


async def _get(app: FastAPI) -> Response:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get("/api/v1/settings/reader")


async def _put(app: FastAPI, body: object) -> Response:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.put("/api/v1/settings/reader", json=body)


def test_reader_settings_use_adr_defaults_without_persisting_them(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        settings = ReaderSettingsService(database).get()

        assert settings == ReaderSettings(
            cache_limit_mb=500,
            preload_ahead=2,
            mode="single",
            rtl=False,
            e_reader=False,
        )
        assert _rows(database) == {}
    finally:
        database.close()


def test_reader_settings_put_is_atomic_preserves_unknown_keys_and_repairs_invalid_rows(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    try:
        with database.write_session() as session:
            session.add(
                setting_to_row(
                    Setting.new_local("future.setting", '{"kept":true}')
                )
            )
            session.add(
                setting_to_row(
                    Setting.new_local("reader.cache_limit_mb", "501")
                )
            )

        service = ReaderSettingsService(database)
        with pytest.raises(ReaderSettingsValueError):
            service.get()

        saved = service.put(
            ReaderSettings(
                cache_limit_mb=128,
                preload_ahead=1,
                mode="vertical",
                rtl=True,
                e_reader=True,
            )
        )

        assert saved == ReaderSettings(128, 1, "vertical", True, True)
        rows = _rows(database)
        assert set(rows) == {
            "future.setting",
            "reader.cache_limit_mb",
            "reader.preload_ahead",
            "reader.mode",
            "reader.rtl",
            "reader.e_reader",
        }
        assert rows["future.setting"].value_json == '{"kept":true}'
        for key, expected in {
            "reader.cache_limit_mb": 128,
            "reader.preload_ahead": 1,
            "reader.mode": "vertical",
            "reader.rtl": True,
            "reader.e_reader": True,
        }.items():
            assert rows[key].schema_version == 1
            assert json.loads(rows[key].value_json) == expected

        with database.write_session() as session:
            session.execute(
                text(
                    "CREATE TRIGGER fail_reader_rtl_update "
                    "BEFORE UPDATE ON settings WHEN NEW.setting_key = 'reader.rtl' "
                    "BEGIN SELECT RAISE(ABORT, 'injected transaction failure'); END"
                )
            )
        previous = {
            key: (row.value_json, row.schema_version, row.updated_at_us)
            for key, row in rows.items()
            if key.startswith("reader.")
        }
        with pytest.raises(IntegrityError):
            service.put(ReaderSettings(64, 0, "double", False))

        after_failure = _rows(database)
        assert {
            key: (
                after_failure[key].value_json,
                after_failure[key].schema_version,
                after_failure[key].updated_at_us,
            )
            for key in previous
        } == previous
    finally:
        database.close()


def test_invalid_persisted_value_fails_closed_and_logs_no_value(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = _database(tmp_path)
    secret_like_value = "DO_NOT_LOG_501"
    try:
        with database.write_session() as session:
            session.add(
                setting_to_row(
                    Setting.new_local(
                        "reader.cache_limit_mb",
                        json.dumps(secret_like_value),
                    )
                )
            )

        caplog.set_level(logging.WARNING, logger="yomira.settings")
        with pytest.raises(ReaderSettingsValueError):
            ReaderSettingsService(database).get()

        assert secret_like_value not in caplog.text
        record = caplog.records[-1]
        record_data = cast(dict[str, object], record.__dict__)
        assert record_data["setting_key"] == "reader.cache_limit_mb"
        assert record_data["issue"] == "invalid_value"
        assert record_data["trace_id"]
        assert secret_like_value not in str(record_data)
        assert _rows(database)["reader.cache_limit_mb"].value_json == json.dumps(
            secret_like_value
        )
    finally:
        database.close()


def test_unsupported_reader_setting_version_is_not_treated_as_default(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    try:
        with database.write_session() as session:
            session.add(
                setting_to_row(
                    Setting.new_local(
                        "reader.mode",
                        '"vertical"',
                        schema_version=2,
                    )
                )
            )

        with pytest.raises(ReaderSettingsValueError) as caught:
            ReaderSettingsService(database).get()

        assert caught.value.setting_key == "reader.mode"
        assert caught.value.issue == "unsupported_schema_version"
        row = _rows(database)["reader.mode"]
        assert row.schema_version == 2
        assert row.value_json == '"vertical"'
    finally:
        database.close()


def test_reader_settings_api_get_and_put_use_stable_envelope(tmp_path: Path) -> None:
    app, database = _ready_app(tmp_path)
    try:
        initial = asyncio.run(_get(app))
        changed = asyncio.run(
            _put(
                app,
                {
                    "cache_limit_mb": 256,
                    "preload_ahead": 1,
                    "mode": "double",
                    "rtl": True,
                    "e_reader": True,
                },
            )
        )
        reread = asyncio.run(_get(app))

        assert initial.status_code == 200
        assert initial.json()["data"] == {
            "cache_limit_mb": 500,
            "preload_ahead": 2,
            "mode": "single",
            "rtl": False,
            "e_reader": False,
        }
        assert changed.status_code == 200
        assert changed.json()["data"] == {
            "cache_limit_mb": 256,
            "preload_ahead": 1,
            "mode": "double",
            "rtl": True,
            "e_reader": True,
        }
        assert reread.json() == changed.json()
        assert initial.headers["x-trace-id"]
        assert changed.headers["x-trace-id"]
    finally:
        database.close()


@pytest.mark.parametrize(
    "body",
    [
        {
            "cache_limit_mb": 15,
            "preload_ahead": 2,
            "mode": "single",
            "rtl": False,
            "e_reader": False,
        },
        {
            "cache_limit_mb": 501,
            "preload_ahead": 2,
            "mode": "single",
            "rtl": False,
            "e_reader": False,
        },
        {
            "cache_limit_mb": True,
            "preload_ahead": 2,
            "mode": "single",
            "rtl": False,
            "e_reader": False,
        },
        {
            "cache_limit_mb": 500,
            "preload_ahead": 3,
            "mode": "single",
            "rtl": False,
            "e_reader": False,
        },
        {
            "cache_limit_mb": 500,
            "preload_ahead": 1,
            "mode": "page",
            "rtl": False,
            "e_reader": False,
        },
        {
            "cache_limit_mb": 500,
            "preload_ahead": 1,
            "mode": "single",
            "rtl": 0,
            "e_reader": False,
        },
        {
            "cache_limit_mb": 500,
            "preload_ahead": 1,
            "mode": "single",
            "rtl": False,
            "e_reader": 0,
        },
        {"cache_limit_mb": 500, "preload_ahead": 1, "mode": "single", "rtl": False},
        {
            "cache_limit_mb": 500,
            "preload_ahead": 1,
            "mode": "single",
            "rtl": False,
            "e_reader": False,
            "extra": 1,
        },
    ],
    ids=[
        "cache-underflow",
        "cache-overflow",
        "bool-is-not-int",
        "window-overflow",
        "bad-mode",
        "rtl-not-bool",
        "e-reader-not-bool",
        "missing-e-reader",
        "extra-field",
    ],
)
def test_reader_settings_api_rejects_invalid_put_without_writing(
    tmp_path: Path,
    body: object,
) -> None:
    app, database = _ready_app(tmp_path)
    try:
        response = asyncio.run(_put(app, body))

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "API_VALIDATION_FAILED"
        assert response.headers["x-trace-id"]
        assert _rows(database) == {}
    finally:
        database.close()


def test_reader_settings_api_reports_invalid_persisted_value_without_echoing_it(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    dangerous_value = "DO_NOT_ECHO_777"
    try:
        with database.write_session() as session:
            session.add(
                setting_to_row(
                    Setting.new_local(
                        "reader.preload_ahead",
                        json.dumps(dangerous_value),
                    )
                )
            )

        response = asyncio.run(_get(app))

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "SYS_INVALID_CONFIG"
        assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]
        assert response.json()["error"]["details"] == [
            {"field": "reader.preload_ahead", "issue": "invalid_value"}
        ]
        assert dangerous_value not in response.text
        assert _rows(database)["reader.preload_ahead"].value_json == json.dumps(
            dangerous_value
        )
    finally:
        database.close()
