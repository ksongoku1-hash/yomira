import asyncio
from pathlib import Path
from typing import cast

import httpx
import pytest
from test_fmd2_connector import _connector, _fmd_reference  # pyright: ignore[reportPrivateUsage]

from app.connectors.errors import SourceConnectorError
from app.connectors.search import ConnectorSearchQuery
from app.errors import ErrorCode
from app.fmd2_bridge import FMD2_SOURCE_ID


@pytest.mark.parametrize("order", ["search-first", "catalog-first", "two-searches"])
def test_search_and_catalog_requests_share_the_engine_gate(tmp_path: Path, order: str) -> None:
    async def exercise() -> None:
        busy = False
        requests: list[str] = []
        reference = _fmd_reference("/en/fixture/list")

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal busy
            requests.append(request.url.path)
            if busy:
                return httpx.Response(503, json={"error": {"code": "catalog_busy"}})
            busy = True
            try:
                await asyncio.sleep(0)
                return httpx.Response(
                    200, json={"data": []} if request.url.path == "/v1/search"
                    else _catalog(reference),
                )
            finally:
                busy = False

        connector = _connector(tmp_path, httpx.MockTransport(handler))
        try:
            search = connector.search(ConnectorSearchQuery("Fixture"))
            other = connector.search(ConnectorSearchQuery("Another")) if order == "two-searches" \
                else connector.get_series(reference)
            results = await asyncio.gather(
                *(other, search) if order == "catalog-first" else (search, other)
            )
            assert len(results) == 2 and len(requests) == 2
        finally:
            await connector.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("catalog", [False, True])
@pytest.mark.parametrize("remains_busy", [False, True])
def test_local_catalog_busy_is_retried_with_a_fixed_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remains_busy: bool, catalog: bool
) -> None:
    monkeypatch.setattr("app.fmd2_bridge._CATALOG_BUSY_DELAYS", (0.0, 0.0))
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if remains_busy or attempts < 3:
            return httpx.Response(503, json={"error": {"code": "catalog_busy"}})
        return httpx.Response(200, json=_catalog(_fmd_reference("/en/fixture/list"))
                              if catalog else {"data": []})

    async def exercise() -> None:
        connector = _connector(tmp_path, httpx.MockTransport(handler))
        try:
            operation = connector.get_series(_fmd_reference("/en/fixture/list")) if catalog \
                else connector.search(ConnectorSearchQuery("Fixture"))
            if remains_busy:
                with pytest.raises(SourceConnectorError) as caught:
                    await operation
                assert caught.value.code is ErrorCode.SRC_CONNECTOR_FAILED
                assert caught.value.retryable is True
            else:
                result = await operation
                if catalog:
                    assert cast(dict[str, object], result)["title"] == "Fixture"
                else:
                    assert result == ()
            assert attempts == 3
        finally:
            await connector.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["not-ready", "upstream", "html", "oversized", "malformed"])
def test_catalog_retries_only_the_exact_local_busy_response(tmp_path: Path, failure: str) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if failure == "html":
            return httpx.Response(503, text="<html>catalog_busy</html>")
        if failure == "oversized":
            return httpx.Response(503, json={"error": {"code": "catalog_busy"}, "data": "x" * 2048})
        if failure == "malformed":
            return httpx.Response(503, content=b"{", headers={"content-type": "application/json"})
        code = "catalog_not_ready" if failure == "not-ready" else "catalog_busy"
        return httpx.Response(
            503 if failure == "not-ready" else 502,
            json={"error": {"code": code}},
        )

    async def exercise() -> None:
        connector = _connector(tmp_path, httpx.MockTransport(handler))
        try:
            with pytest.raises(SourceConnectorError) as caught:
                await connector.search(ConnectorSearchQuery("Fixture"))
            assert caught.value.code is ErrorCode.SRC_CONNECTOR_FAILED
            assert attempts == 1
        finally:
            await connector.close()

    asyncio.run(exercise())


def test_canceling_busy_wait_releases_the_catalog_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.fmd2_bridge._CATALOG_BUSY_DELAYS", (60.0,))

    async def exercise() -> None:
        entered = asyncio.Event()
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                return httpx.Response(503, json={"error": {"code": "catalog_busy"}})
            return httpx.Response(200, json={"data": []})

        connector = _connector(tmp_path, httpx.MockTransport(handler))
        try:
            task = asyncio.create_task(connector.search(ConnectorSearchQuery("Fixture")))
            await entered.wait()
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await connector.search(ConnectorSearchQuery("Another")) == ()
            assert attempts == 2
        finally:
            await connector.close()

    asyncio.run(exercise())


def test_busy_response_never_repeats_reader_session_creation(tmp_path: Path) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"error": {"code": "catalog_busy"}})

    async def exercise() -> None:
        from uuid import uuid4

        from test_fmd2_connector import _chapter_reference  # pyright: ignore[reportPrivateUsage]

        connector = _connector(tmp_path, httpx.MockTransport(handler))
        try:
            with pytest.raises(SourceConnectorError):
                await connector.open_online_pages(uuid4(), _chapter_reference())
            assert attempts == 1
        finally:
            await connector.close()

    asyncio.run(exercise())


def _catalog(reference: str, *, invalid: bool = False) -> dict[str, object]:
    return {
        "manga": {"manga_ref": reference.lower(), "source_id": FMD2_SOURCE_ID, "title": "Fixture"},
        "chapters": [
            {
                "chapter_ref": _fmd_reference("/en/fixture/1").lower(),
                "title": "1",
                "ordinal": 0 if invalid else 1,
            }
        ],
    }


def test_catalog_is_shared_between_concurrent_metadata_chapters_and_reader_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        reference = _fmd_reference("/en/fixture/list")
        requests: list[str] = []
        clock = 10.0

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            await asyncio.sleep(0)
            return httpx.Response(200, json=_catalog(reference))

        monkeypatch.setattr("app.fmd2_bridge.time.monotonic", lambda: clock)
        connector = _connector(tmp_path, httpx.MockTransport(handler))
        try:
            raw, chapters = await asyncio.gather(
                connector.get_series(reference), connector.get_chapters(reference)
            )
            assert cast(dict[str, object], raw)["title"] == "Fixture" and len(chapters) == 1
            assert len(requests) == 1
            await connector.get_series(reference)
            await connector.get_chapters(reference)
            assert len(requests) == 1
            clock = 70.0
            await connector.get_chapters(reference)
            assert len(requests) == 2
            await connector.invalidate_catalog(reference)
            await connector.get_chapters(reference)
            assert len(requests) == 3
        finally:
            await connector.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["chapter", "metadata"])
def test_catalog_cache_is_bounded_and_never_retains_invalid_chapters(
    tmp_path: Path, failure: str
) -> None:
    async def exercise() -> None:
        requests: list[str] = []
        invalid = True

        def handler(request: httpx.Request) -> httpx.Response:
            reference = request.url.path.removeprefix("/v1/manga/").removesuffix("/chapters")
            requests.append(reference)
            payload = _catalog(reference, invalid=invalid and failure == "chapter")
            if invalid and failure == "metadata":
                payload["manga"] = {"manga_ref": reference.lower(), "source_id": FMD2_SOURCE_ID}
            return httpx.Response(200, json=payload)

        connector = _connector(tmp_path, httpx.MockTransport(handler))
        reference = _fmd_reference("/en/fixture/0")
        try:
            for _ in range(2):
                with pytest.raises(SourceConnectorError):
                    await connector.get_chapters(reference)
            assert len(requests) == 2
            invalid = False
            for index in range(17):
                await connector.get_series(_fmd_reference(f"/en/fixture/{index}"))
            assert len(requests) == 19
            await connector.get_series(_fmd_reference("/en/fixture/16"))
            assert len(requests) == 19
            await connector.get_series(reference)
            assert len(requests) == 20
        finally:
            await connector.close()

    asyncio.run(exercise())
