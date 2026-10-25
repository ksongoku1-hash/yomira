import asyncio
from pathlib import Path

import httpx
from test_mangadex_reader import CHAPTER, MANGA, chapter

from app.connectors.builtins import create_connector_registry
from app.connectors.http_client import ConnectorHttpSession
from app.connectors.mangadex import MANGADEX_NETWORK_ORIGINS, MangaDexConnector
from app.factory import create_app
from app.settings import Settings


def test_catalog_requests_are_shared_expire_and_refresh_explicitly() -> None:
    calls: list[str] = []
    now = [0.0]

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        await asyncio.sleep(0)
        if request.url.path.endswith("/feed"):
            return httpx.Response(
                200,
                json={
                    "result": "ok",
                    "response": "collection",
                    "total": 1,
                    "offset": 0,
                    "limit": 100,
                    "data": [chapter()],
                },
            )
        return httpx.Response(
            200,
            json={
                "result": "ok",
                "response": "entity",
                "data": {
                    "id": MANGA,
                    "type": "manga",
                    "attributes": {"title": {"en": "Example"}},
                    "relationships": [],
                },
            },
        )

    async def run() -> None:
        http = ConnectorHttpSession(test_transport=httpx.MockTransport(respond))
        connector = MangaDexConnector(
            http.scoped_client(MANGADEX_NETWORK_ORIGINS), clock=lambda: now[0]
        )
        try:
            catalogs = await asyncio.gather(*(connector.get_chapters(MANGA) for _ in range(8)))
            assert all(result[0].remote_id == CHAPTER for result in catalogs)
            assert len(calls) == 1
            await asyncio.gather(*(connector.get_series(MANGA) for _ in range(8)))
            assert len(calls) == 2
            await connector.get_series(MANGA)
            await connector.get_chapters(MANGA)
            assert len(calls) == 2
            now[0] = 300.0
            await connector.get_chapters(MANGA)
            await connector.get_series(MANGA)
            assert len(calls) == 4
            await connector.invalidate_catalog(MANGA)
            await connector.get_chapters(MANGA)
            await connector.get_series(MANGA)
            assert len(calls) == 6
        finally:
            await http.close()

    asyncio.run(run())


def test_refresh_button_invalidates_the_mangadex_catalog(tmp_path: Path) -> None:
    requests = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={
                "result": "ok",
                "response": "collection",
                "total": 1,
                "offset": 0,
                "limit": 100,
                "data": [chapter()],
            },
        )

    async def run() -> None:
        settings = Settings(app_env="test", data_root=tmp_path)
        app = create_app(settings)
        http = ConnectorHttpSession(test_transport=httpx.MockTransport(respond))
        assert app.state.runtime.secret_vault is not None
        app.state.runtime.connector_registry = create_connector_registry(
            settings,
            app.state.runtime.secret_vault,
            http_client_factory=http.scoped_client,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                route = "/api/v1/connectors/yomira.metadata.mangadex/series/" + MANGA + "/chapters"
                for _ in range(2):
                    assert (await client.get(route)).status_code == 200
                assert requests == 1
                assert (await client.get(route, params={"refresh": True})).status_code == 200
                assert requests == 2
        finally:
            await http.close()

    asyncio.run(run())
