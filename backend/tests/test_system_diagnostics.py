from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from PIL import Image
from sqlalchemy import event
from support.sqlite import open_test_database

from app.connectors.mock import MockConnector
from app.connectors.registry import ConnectorRegistration, ConnectorRegistry
from app.connectors.sandbox import ConnectorHostContext
from app.factory import create_app
from app.persistence.database import SQLiteDatabase
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.windows_paths import DataRootIdentity
from app.runtime import RuntimeState
from app.settings import Settings


@pytest.fixture
def diagnostic_app(tmp_path: Path) -> Generator[tuple[FastAPI, SQLiteDatabase]]:
    data_root = tmp_path / "YomiraData"
    data_root.mkdir()
    (data_root / "library").mkdir()
    database = open_test_database(data_root)
    MigrationRunner(database, data_root / "backups").upgrade_to_head()

    target_app = create_app(Settings(app_env="test", data_root=data_root))
    runtime = cast(RuntimeState, target_app.state.runtime)
    runtime.database = database
    runtime.data_root_identity = database.data_root_identity
    runtime.library_root_identity = DataRootIdentity.capture(data_root / "library")
    assert runtime.secret_vault is not None
    mock = MockConnector()
    runtime.connector_registry = ConnectorRegistry(
        context=ConnectorHostContext(secret_vault=runtime.secret_vault),
        registrations=[
            ConnectorRegistration(metadata=mock.metadata, factory=lambda _context: MockConnector())
        ],
    )

    try:
        yield target_app, database
    finally:
        database.close()


async def _get(
    path: str,
    target_app: FastAPI,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    transport = ASGITransport(app=target_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path, headers=headers)


def _request(
    path: str,
    target_app: FastAPI,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    return asyncio.run(_get(path, target_app, headers=headers))


def _filesystem_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_diagnostics_reports_all_checks_and_schema_without_filesystem_writes(
    diagnostic_app: tuple[FastAPI, SQLiteDatabase],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target_app, database = diagnostic_app
    with database.read_session() as session:
        expected_revision = (
            session.connection()
            .exec_driver_sql("SELECT version_num FROM alembic_version")
            .scalar_one()
        )
    before_files = _filesystem_snapshot(tmp_path)
    sql_statements: list[str] = []

    def capture_sql(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _many: Any,
    ) -> None:
        sql_statements.append(statement.strip().lower())

    event.listen(database.engine, "before_cursor_execute", capture_sql)

    async def forbidden_health_check() -> object:
        raise AssertionError("Diagnostics must not perform connector health checks.")

    monkeypatch.setattr(MockConnector, "health_check", forbidden_health_check)
    response = _request("/api/v1/system/diagnostics", target_app)

    assert response.status_code == 200
    payload = response.json()
    assert payload["app_version"]
    assert payload["api_version"] == 1
    assert payload["schema_version"] == expected_revision
    assert [check["id"] for check in payload["checks"]] == [
        "backend",
        "database",
        "library",
        "disk",
        "image_processor",
        "cbz_builder",
        "connectors",
        "lan",
    ]
    for check in payload["checks"]:
        assert set(check) == {"id", "status", "code", "message", "trace_id"}
        assert check["status"] in {"ok", "warning", "error", "unknown"}
        assert check["code"] is None or isinstance(check["code"], str)
        assert isinstance(check["message"], str) and check["message"]
    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["backend"]["status"] == "ok"
    assert checks["database"]["status"] == "ok"
    assert checks["library"]["status"] == "ok"
    assert checks["image_processor"]["status"] == "ok"
    assert checks["cbz_builder"]["status"] == "unknown"
    assert checks["cbz_builder"]["code"] is None
    assert checks["connectors"]["status"] == "ok"
    assert checks["lan"]["status"] == "ok"
    assert response.headers["x-trace-id"]
    assert _filesystem_snapshot(tmp_path) == before_files
    assert not any(
        statement.startswith(("insert", "update", "delete", "replace", "create", "drop", "alter"))
        for statement in sql_statements
    )


def test_diagnostics_sanitizes_database_probe_failures_and_preserves_trace(
    diagnostic_app: tuple[FastAPI, SQLiteDatabase],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    target_app, database = diagnostic_app
    private_detail = f"{tmp_path} secret=cookie-value"

    @contextmanager
    def failing_read_session() -> Generator[object]:
        raise RuntimeError(private_detail)
        yield  # pragma: no cover

    monkeypatch.setattr(database, "read_session", failing_read_session)
    trace_id = "bbf649e8-3d3e-45b2-bd59-7f8157a7a946"
    response = _request(
        "/api/v1/system/diagnostics",
        target_app,
        headers={"X-Trace-ID": trace_id},
    )

    assert response.status_code == 200
    database_check = next(check for check in response.json()["checks"] if check["id"] == "database")
    assert database_check["status"] == "error"
    assert database_check["code"] == "DB_CONNECTION_FAILED"
    assert database_check["trace_id"] == trace_id
    assert private_detail not in response.text
    assert str(tmp_path) not in response.text
    assert "cookie-value" not in response.text
    assert response.headers["x-trace-id"] == trace_id
    assert private_detail not in caplog.text
    assert "cookie-value" not in caplog.text


def test_diagnostics_image_probe_uses_in_memory_webp_round_trip() -> None:
    from app.services.thumbnails import ThumbnailService, ThumbnailVariant

    service = ThumbnailService(cache_max_bytes=1024, cache_max_entries=1)
    with Image.new("RGB", (1, 1), color=(12, 34, 56)) as image:
        result = service.generate(image, "0" * 64, ThumbnailVariant.SMALL)
    with Image.open(BytesIO(result.content)) as decoded:
        decoded.load()
        assert decoded.format == "WEBP"
        assert decoded.size == (1, 1)
