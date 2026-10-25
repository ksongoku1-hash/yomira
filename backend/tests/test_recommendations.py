import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.connectors.builtins import create_connector_registry
from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.http_client import ConnectorHttpSession
from app.connectors.mangadex import MANGADEX_NETWORK_ORIGINS, MangaDexConnector
from app.errors import ErrorCode
from app.factory import create_app
from app.fmd2_bridge import FMD2_API_ORIGIN, FMD2_PUBLIC_SOURCES, Fmd2Connector
from app.runtime import RuntimeState
from app.services.recommendations import RecommendationsService
from app.settings import Settings

MANGA = str(uuid4())
COVER = str(uuid4())
NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)


def payload() -> dict[str, object]:
    return {
        "result": "ok",
        "response": "collection",
        "limit": 24,
        "offset": 0,
        "total": 1,
        "data": [
            {
                "id": MANGA,
                "type": "manga",
                "attributes": {
                    "title": {"ko-ro": "Na Honjaman Level-Up"},
                    "altTitles": [{"ja": "俺だけレベルアップな件"}, {"en": "Solo Leveling"}],
                    "tags": [{"attributes": {"name": {"en": "Action"}}}],
                    "description": {},
                    "year": 2018,
                    "status": "completed",
                    "originalLanguage": "ko",
                },
                "relationships": [
                    {"id": COVER, "type": "cover_art", "attributes": {"fileName": COVER + ".jpg"}}
                ],
            }
        ],
    }


def test_one_daily_request_is_shared_persisted_and_prefers_a_searchable_title(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET" and request.url.host == "api.mangadex.org"
        assert request.url.params["order[followedCount]"] == "desc"
        assert request.url.params.get_list("availableTranslatedLanguage[]") == ["es", "es-la"]
        assert request.url.params.get_list("contentRating[]") == ["safe"]
        assert request.url.params["limit"] == "24"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(200, json=payload())

    async def run() -> None:
        session = ConnectorHttpSession(test_transport=httpx.MockTransport(respond))
        try:
            connector = MangaDexConnector(session.scoped_client(MANGADEX_NETWORK_ORIGINS))
            path = tmp_path / "cache/feed.json"
            service = RecommendationsService(path, clock=lambda: NOW)
            responses = await asyncio.gather(*(service.get(connector) for _ in range(12)))
            assert len(requests) == 1
            assert all(feed.data[0].title == "Solo Leveling" for feed in responses)
            assert responses[0].data[0].aliases[0] == "Na Honjaman Level-Up"
            assert responses[0].meta.status == "ready"
            restored = RecommendationsService(path, clock=lambda: NOW + timedelta(hours=23))
            assert (await restored.get(connector)).data == responses[0].data
            assert len(requests) == 1
            expired = RecommendationsService(path, clock=lambda: NOW + timedelta(days=1, seconds=1))
            await expired.get(connector)
            assert len(requests) == 2
        finally:
            await session.close()

    asyncio.run(run())


def test_public_inventory_and_feed_need_no_library_or_progress_access(tmp_path: Path) -> None:
    async def run() -> None:
        target = create_app(Settings(app_env="test", data_root=tmp_path))
        runtime = cast(RuntimeState, target.state.runtime)
        session = ConnectorHttpSession(
            test_transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload()))
        )
        bridge = Fmd2Connector(
            tmp_path / "fmd.exe",
            tmp_path / "staging",
            api_client=httpx.AsyncClient(
                base_url=FMD2_API_ORIGIN,
                transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
            ),
        )
        assert runtime.secret_vault is not None
        runtime.connector_registry = create_connector_registry(
            Settings(app_env="test"),
            runtime.secret_vault,
            http_client_factory=session.scoped_client,
            fmd2_connector=bridge,
        )
        runtime.recommendations_service = RecommendationsService(
            tmp_path / "feed.json", clock=lambda: NOW
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=target), base_url="http://127.0.0.1"
            ) as client:
                sources = await client.get("/api/v1/connectors/public-sources")
                assert (
                    sources.status_code == 200
                    and len(sources.json()["data"]) == len(FMD2_PUBLIC_SOURCES) + 1
                )
                assert {item["name"] for item in sources.json()["data"]} >= {
                    "LeerCapitulo",
                    "MangaOni",
                    "MangaDex",
                }
                feed = await client.get("/api/v1/recommendations")
                assert (
                    feed.status_code == 200 and feed.json()["data"][0]["title"] == "Solo Leveling"
                )
                assert runtime.database is None and runtime.reader_progress_service is None
        finally:
            await session.close()
            await bridge.close()

    asyncio.run(run())


def test_stale_cache_survives_an_outage_and_cooldown_prevents_repeated_requests(
    tmp_path: Path,
) -> None:
    now = [NOW]
    calls = [0]

    def respond(_request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return httpx.Response(200, json=payload()) if calls[0] == 1 else httpx.Response(429)

    async def run() -> None:
        session = ConnectorHttpSession(test_transport=httpx.MockTransport(respond))
        try:
            connector = MangaDexConnector(session.scoped_client(MANGADEX_NETWORK_ORIGINS))
            service = RecommendationsService(tmp_path / "feed.json", clock=lambda: now[0])
            await service.get(connector)
            now[0] += timedelta(days=2)
            stale = await service.get(connector)
            assert stale.meta.status == "stale" and stale.data[0].title == "Solo Leveling"
            assert stale.meta.retry_at == now[0] + timedelta(minutes=15)
            for _ in range(10):
                assert (await service.get(connector)).meta.status == "stale"
            assert calls[0] == 2
            now[0] += timedelta(minutes=16)
            await service.get(connector)
            assert calls[0] == 3
            now[0] += timedelta(days=8)
            unavailable = await service.get(connector)
            assert unavailable.meta.status == "unavailable" and not unavailable.data
        finally:
            await session.close()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["oversized", "invalid", "future", "old", "naive"])
def test_untrusted_disk_cache_is_rejected(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "feed.json"
    content = '{"provider":"MangaDex","data":[],"updated_at":"2026-10-21T12:00:00Z"}'
    content = {
        "oversized": "x" * 200_000,
        "invalid": "not-json",
        "future": content.replace("2026-10-21", "2027-10-21"),
        "old": content.replace("2026-10-21", "2025-10-21"),
        "naive": content.replace("Z", ""),
    }[kind]
    path.write_text(content, encoding="utf-8")
    connector = AsyncMock(spec=MangaDexConnector)
    connector.popular.side_effect = SourceConnectorError(
        code=ErrorCode.SRC_CONNECTOR_FAILED,
        operation=SourceConnectorOperation.SEARCH,
        retryable=True,
    )
    service = RecommendationsService(path, clock=lambda: NOW)
    response = asyncio.run(service.get(connector))
    assert response.meta.status == "unavailable" and not response.data


@pytest.mark.parametrize("kind", ["duplicates", "oversized", "wrong_limit", "wrong_offset"])
def test_provider_ranking_rejects_invalid_collections(kind: str) -> None:
    data = payload()
    if kind == "duplicates":
        data["data"] = data["data"] * 2  # type: ignore[operator]
        data["total"] = 2
    elif kind == "oversized":
        data["data"] = data["data"] * 25  # type: ignore[operator]
        data["total"] = 25
    else:
        data["limit" if kind == "wrong_limit" else "offset"] = 999

    async def run() -> None:
        session = ConnectorHttpSession(
            test_transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=data))
        )
        try:
            connector = MangaDexConnector(session.scoped_client(MANGADEX_NETWORK_ORIGINS))
            with pytest.raises(SourceConnectorError):
                await connector.popular()
        finally:
            await session.close()

    asyncio.run(run())
