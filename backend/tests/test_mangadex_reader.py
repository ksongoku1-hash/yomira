import asyncio
from uuid import uuid4

import httpx
import pytest

from app.connectors.errors import SourceConnectorError
from app.connectors.http_client import ConnectorHttpSession
from app.connectors.mangadex import MANGADEX_NETWORK_ORIGINS, MangaDexConnector
from app.connectors.pages import ConnectorPageReference

MANGA = str(uuid4())
CHAPTER = str(uuid4())


def chapter(**changes: object) -> dict[str, object]:
    return {
        "id": CHAPTER,
        "type": "chapter",
        "attributes": {
            "chapter": "21",
            "title": "El Divertido",
            "volume": None,
            "translatedLanguage": "es-la",
            "pages": 2,
            "externalUrl": None,
            **changes,
        },
        "relationships": [{"type": "manga", "id": MANGA}],
    }


def test_catalog_and_page_plans_reuse_core_online_and_download_contracts() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/feed"):
            assert request.url.params.get_list("translatedLanguage[]") == ["es", "es-la"]
            payload: object = {
                "result": "ok",
                "response": "collection",
                "total": 1,
                "offset": 0,
                "limit": 100,
                "data": [chapter()],
            }
        elif request.url.path.startswith("/chapter/"):
            payload = {"result": "ok", "response": "entity", "data": chapter()}
        else:
            payload = {
                "result": "ok",
                "baseUrl": "http://127.0.0.1:9999",
                "chapter": {
                    "hash": "a" * 32,
                    "data": ["1-original.png", "2-original.png"],
                    "dataSaver": ["1-small.jpg", "2-small.jpg"],
                },
            }
        return httpx.Response(200, json=payload)

    async def run() -> None:
        http = ConnectorHttpSession(test_transport=httpx.MockTransport(respond))
        try:
            connector = MangaDexConnector(http.scoped_client(MANGADEX_NETWORK_ORIGINS))
            catalog = await connector.get_chapters(MANGA)
            assert len(catalog) == 1 and catalog[0].remote_id == CHAPTER
            assert catalog[0].language == "es-la"
            assert catalog[0] == await connector.get_chapter(CHAPTER)
            online = await connector.open_online_pages(uuid4(), CHAPTER)
            original = await connector.acquire_pages(uuid4(), CHAPTER, "21")
            assert len(online) == len(original) == 2
            assert connector.resolve_page(online[0]).url.endswith("/1-small.jpg")
            assert "/data-saver/" in online[0].remote_reference
            assert "/data/" in original[0].remote_reference
            assert all(
                p.remote_reference.startswith("https://uploads.mangadex.org/")
                for p in (*online, *original)
            )
            await connector.close_online_pages(uuid4())
            assert all(
                "cookie" not in r.headers and "authorization" not in r.headers for r in requests
            )
        finally:
            await http.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "bad",
    [
        "../secret.jpg",
        "https://evil.example/a.jpg",
        "a%2f.png",
        "a.jpg?token=private",
        "a.jpg#fragment",
        "a\\b.jpg",
    ],
)
def test_page_plan_and_resolution_reject_untrusted_paths(bad: str) -> None:
    async def run() -> None:
        http = ConnectorHttpSession(
            test_transport=httpx.MockTransport(
                lambda _r: httpx.Response(
                    200,
                    json={
                        "result": "ok",
                        "chapter": {
                            "hash": "a" * 32,
                            "data": [bad],
                            "dataSaver": [bad],
                        },
                    },
                )
            )
        )
        try:
            connector = MangaDexConnector(http.scoped_client(MANGADEX_NETWORK_ORIGINS))
            with pytest.raises(SourceConnectorError):
                await connector.get_pages(CHAPTER)
            with pytest.raises(SourceConnectorError):
                connector.resolve_page(ConnectorPageReference(bad))
        finally:
            await http.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "attrs",
    [
        {"translatedLanguage": "en"},
        {"pages": 0},
        {"externalUrl": "https://external.example/chapter"},
    ],
)
def test_unreadable_or_other_language_chapters_are_not_offered(attrs: dict[str, object]) -> None:
    async def run() -> None:
        http = ConnectorHttpSession(
            test_transport=httpx.MockTransport(
                lambda _r: httpx.Response(
                    200,
                    json={
                        "result": "ok",
                        "response": "collection",
                        "total": 1,
                        "offset": 0,
                        "limit": 100,
                        "data": [chapter(**attrs)],
                    },
                )
            )
        )
        try:
            connector = MangaDexConnector(http.scoped_client(MANGADEX_NETWORK_ORIGINS))
            assert await connector.get_chapters(MANGA) == ()
        finally:
            await http.close()

    asyncio.run(run())


def test_catalog_paginates_and_rejects_wrong_series() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        items = [chapter() for _ in range(100 if offset == 0 else 1)]
        for item in items:
            item["id"] = str(uuid4())
        return httpx.Response(
            200,
            json={
                "result": "ok",
                "response": "collection",
                "total": 101,
                "offset": offset,
                "limit": 100,
                "data": items,
            },
        )

    async def run() -> None:
        http = ConnectorHttpSession(test_transport=httpx.MockTransport(respond))
        try:
            connector = MangaDexConnector(http.scoped_client(MANGADEX_NETWORK_ORIGINS))
            assert len(await connector.get_chapters(MANGA)) == 101
            with pytest.raises(SourceConnectorError):
                await connector.get_chapters(str(uuid4()))
        finally:
            await http.close()

    asyncio.run(run())
