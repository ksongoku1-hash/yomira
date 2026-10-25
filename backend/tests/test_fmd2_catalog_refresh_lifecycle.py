import asyncio
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI

from app import lifecycle
from app.connectors.http_client import ConnectorHttpSession
from app.factory import create_app
from app.fmd2_bridge import Fmd2Connector
from app.runtime import RuntimeState
from app.services.fmd2_catalog_refresh import Fmd2CatalogRefreshService
from app.settings import Settings
from app.single_instance import WindowsMutexGuard


class NoopInstanceGuard(WindowsMutexGuard):
    def acquire(self) -> None:
        pass

    def release(self) -> None:
        pass


async def native_start(_bridge: Fmd2Connector) -> None:
    pass


def configured_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(lifecycle, "create_instance_guard", NoopInstanceGuard)
    return create_app(
        Settings(
            app_env="test",
            data_root=tmp_path / "data",
            library_root=tmp_path / "library",
            fmd2_executable=tmp_path / "native" / "fmd.exe",
        )
    )


def test_missing_native_engine_does_not_start_catalog_network_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = configured_app(tmp_path, monkeypatch)

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            runtime = cast(RuntimeState, app.state.runtime)
            assert runtime.ready
            assert runtime.fmd2_catalog_refresh_service is None

    asyncio.run(exercise())


def test_catalog_network_does_not_delay_readiness_and_closes_before_native_and_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = configured_app(tmp_path, monkeypatch)
    runtime = cast(RuntimeState, app.state.runtime)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    never_release = asyncio.Event()
    close_order: list[str] = []

    async def block_directory(request: httpx.Request) -> httpx.Response:
        assert request.url.host in {"inventariooculto.com", "ragnarokscanlation.org"}
        started.set()
        try:
            await never_release.wait()
        finally:
            cancelled.set()
        return httpx.Response(200, text="unreachable")

    session = ConnectorHttpSession(test_transport=httpx.MockTransport(block_directory))
    runtime.connector_http_session = session
    original_close = Fmd2Connector.close

    async def native_close(bridge: Fmd2Connector) -> None:
        assert cancelled.is_set()
        assert not session.is_closed
        close_order.append("native")
        await original_close(bridge)

    monkeypatch.setattr(Fmd2Connector, "start", native_start)
    monkeypatch.setattr(Fmd2Connector, "close", native_close)

    async def exercise() -> None:
        async with asyncio.timeout(10):
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(started.wait(), timeout=2)
                assert runtime.ready and runtime.fmd2_catalog_refresh_service is not None
                assert not session.is_closed
            assert runtime.fmd2_catalog_refresh_service is None
            assert session.is_closed
            assert close_order == ["native"]

    asyncio.run(exercise())


def test_refresh_start_failure_keeps_library_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnavailableRefresh(Fmd2CatalogRefreshService):
        async def start(self) -> None:
            raise OSError("QA: refresh unavailable")

    app = configured_app(tmp_path, monkeypatch)
    monkeypatch.setattr(Fmd2Connector, "start", native_start)
    monkeypatch.setattr(lifecycle, "Fmd2CatalogRefreshService", UnavailableRefresh)

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            assert cast(RuntimeState, app.state.runtime).ready
        assert cast(RuntimeState, app.state.runtime).fmd2_catalog_refresh_service is None

    asyncio.run(exercise())


def test_refresh_close_failure_still_releases_native_pool_and_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenClose(Fmd2CatalogRefreshService):
        async def start(self) -> None:
            pass

        async def close(self) -> None:
            raise RuntimeError("QA: close failed")

    app = configured_app(tmp_path, monkeypatch)
    runtime = cast(RuntimeState, app.state.runtime)
    session = ConnectorHttpSession(
        test_transport=httpx.MockTransport(lambda _: httpx.Response(500))
    )
    runtime.connector_http_session = session
    monkeypatch.setattr(Fmd2Connector, "start", native_start)
    monkeypatch.setattr(lifecycle, "Fmd2CatalogRefreshService", BrokenClose)

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="An HTTP session failed to close"):
            async with app.router.lifespan_context(app):
                assert runtime.ready
        assert session.is_closed
        assert runtime.fmd2_connector is None
        assert runtime.database is None
        assert not runtime.ready

    asyncio.run(exercise())
