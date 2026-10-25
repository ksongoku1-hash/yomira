import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from test_mangadex_reader import CHAPTER
from test_online_reader import _memory_harness  # pyright: ignore[reportPrivateUsage]

from app.connectors.errors import SourceConnectorError
from app.connectors.http_client import ConnectorHttpSession
from app.connectors.mangadex import MANGADEX_NETWORK_ORIGINS, MangaDexConnector
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.source import SourceRow


def _payload(digest: str = "a" * 32) -> dict[str, object]:
    return {
        "result": "ok",
        "baseUrl": "https://untrusted.example",
        "chapter": {
            "hash": digest,
            "data": ["1-original.png", "2-original.png"],
            "dataSaver": ["1-small.jpg", "2-small.jpg"],
        },
    }


def test_page_plan_calls_are_shared_bounded_by_time_and_keep_original_quality() -> None:
    calls = 0
    now = [0.0]

    async def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return httpx.Response(200, json=_payload())

    async def run() -> None:
        http = ConnectorHttpSession(test_transport=httpx.MockTransport(respond))
        connector = MangaDexConnector(
            http.scoped_client(MANGADEX_NETWORK_ORIGINS), clock=lambda: now[0]
        )
        try:
            online = await asyncio.gather(
                *(connector.open_online_pages(uuid4(), CHAPTER) for _ in range(8))
            )
            assert calls == 1 and all(plan == online[0] for plan in online)
            originals = await asyncio.gather(*(connector.get_pages(CHAPTER) for _ in range(8)))
            assert calls == 2
            assert "/data-saver/" in online[0][0].remote_reference
            assert "/data/" in originals[0][0].remote_reference
            assert all(
                plan[0].remote_reference.startswith("https://uploads.mangadex.org/")
                for plan in (*online, *originals)
            )
            now[0] = 299.99
            await connector.open_online_pages(uuid4(), CHAPTER)
            assert calls == 2
            now[0] = 300.0
            await connector.open_online_pages(uuid4(), CHAPTER)
            await connector.get_pages(CHAPTER)
            assert calls == 4
            await connector.invalidate_page_plan(CHAPTER)
            await connector.open_online_pages(uuid4(), CHAPTER)
            await connector.get_pages(CHAPTER)
            assert calls == 6
        finally:
            await http.close()

    asyncio.run(run())


@pytest.mark.parametrize("bound", ["entries", "pages"])
def test_page_plans_have_entry_and_total_page_limits(
    monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    calls = 0
    references = [str(uuid4()) for _ in range(3)]

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_payload())

    async def run() -> None:
        http = ConnectorHttpSession(test_transport=httpx.MockTransport(respond))
        connector = MangaDexConnector(http.scoped_client(MANGADEX_NETWORK_ORIGINS))
        try:
            if bound == "entries":
                monkeypatch.setattr("app.connectors.mangadex._PAGE_PLAN_CACHE_ENTRIES", 2)
                for reference in (
                    references[0],
                    references[1],
                    references[0],
                    references[2],
                    references[0],
                    references[1],
                ):
                    await connector.get_pages(reference)
                assert calls == 4
                assert len(connector._page_plans) == 2  # pyright: ignore[reportPrivateUsage]
            else:
                monkeypatch.setattr("app.connectors.mangadex._MAX_CHAPTERS", 3)
                for reference in references:
                    await connector.get_pages(reference)
                assert sum(len(entry[1]) for entry in connector._page_plans.values()) <= 3  # pyright: ignore[reportPrivateUsage]
                await connector.get_pages(references[0])
                assert calls == 4
        finally:
            await http.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["files", "hash", "source"])
@pytest.mark.parametrize("saver", [False, True])
def test_failed_or_untrusted_page_plans_are_not_cached(failure: str, saver: bool) -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            bad: object = {"result": "error"}
            if failure != "source":
                bad = {
                    "result": "ok",
                    "chapter": {
                        "hash": "../private" if failure == "hash" else "a" * 32,
                        "data": ["../private.jpg"],
                        "dataSaver": ["../private.jpg"],
                    },
                }
            return httpx.Response(200, json=bad)
        return httpx.Response(200, json=_payload())

    async def run() -> None:
        http = ConnectorHttpSession(test_transport=httpx.MockTransport(respond))
        connector = MangaDexConnector(http.scoped_client(MANGADEX_NETWORK_ORIGINS))
        try:

            async def pages():
                return (
                    await connector.open_online_pages(uuid4(), CHAPTER)
                    if saver
                    else await connector.get_pages(CHAPTER)
                )

            with pytest.raises(SourceConnectorError):
                await pages()
            assert len(await pages()) == 2 and calls == 2
            await pages()
            assert calls == 2
        finally:
            await http.close()

    asyncio.run(run())


def test_original_downloads_do_not_require_the_lightweight_variant() -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "result": "ok",
                "chapter": {
                    "hash": "a" * 32,
                    "data": ["original.png"],
                },
            },
        )

    async def run() -> None:
        http = ConnectorHttpSession(test_transport=httpx.MockTransport(respond))
        connector = MangaDexConnector(http.scoped_client(MANGADEX_NETWORK_ORIGINS))
        try:
            assert len(await connector.get_pages(CHAPTER)) == 1
            with pytest.raises(SourceConnectorError):
                await connector.open_online_pages(uuid4(), CHAPTER)
            assert len(await connector.get_pages(CHAPTER)) == 1 and calls == 2
        finally:
            await http.close()

    asyncio.run(run())


def test_manual_invalidation_waits_for_the_old_request_before_removing_it() -> None:
    async def run() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def respond(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            return httpx.Response(200, json=_payload(("a" if calls == 1 else "b") * 32))

        http = ConnectorHttpSession(test_transport=httpx.MockTransport(respond))
        connector = MangaDexConnector(http.scoped_client(MANGADEX_NETWORK_ORIGINS))
        try:
            old = asyncio.create_task(connector.get_pages(CHAPTER))
            await asyncio.wait_for(entered.wait(), 2)
            invalidate = asyncio.create_task(connector.invalidate_page_plan(CHAPTER))
            await asyncio.sleep(0)
            assert not invalidate.done()
            release.set()
            await asyncio.gather(old, invalidate)
            fresh = await connector.get_pages(CHAPTER)
            assert "b" * 32 in fresh[0].remote_reference and calls == 2
        finally:
            release.set()
            await http.close()

    asyncio.run(run())


def test_reader_manual_renewal_bypasses_the_page_plan_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        h, online, _runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
        calls = 0

        def respond(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=_payload(("a" if calls == 1 else "b") * 32))

        http = ConnectorHttpSession(test_transport=httpx.MockTransport(respond))
        connector = MangaDexConnector(http.scoped_client(MANGADEX_NETWORK_ORIGINS))

        def resolve(_id: str, _version: str) -> MangaDexConnector:
            return connector

        monkeypatch.setattr(h.source_pages, "resolve", resolve)
        with h.database.write_session() as session:
            source = session.scalar(select(SourceRow))
            chapter_source = session.scalar(select(ChapterSourceRow))
            assert source is not None and chapter_source is not None
            source.connector_id = connector.metadata.connector_id
            source.connector_version = connector.metadata.connector_version
            chapter_source.remote_id = CHAPTER
        try:
            await online.open(h.chapter_id)
            await online.pause(h.chapter_id)
            await online.open(h.chapter_id)
            assert calls == 1
            await connector.get_pages(CHAPTER)
            assert calls == 2
            await online.open(h.chapter_id, restart=True)
            assert calls == 3
            await connector.get_pages(CHAPTER)
            assert calls == 4
            assert (
                "b" * 32
                in (await connector.open_online_pages(uuid4(), CHAPTER))[0].remote_reference
            )
        finally:
            await online.close()
            await http.close()
            await h.close()

    asyncio.run(run())
