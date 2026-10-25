from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_online_reader import _memory_harness  # pyright: ignore[reportPrivateUsage]
from test_reader_session import _Harness  # pyright: ignore[reportPrivateUsage]

from app.connectors.http_client import ConnectorHttpSession
from app.connectors.mangadex import MANGADEX_NETWORK_ORIGINS, MangaDexConnector
from app.connectors.pages import PageFetchRequest
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.reader_progress import ReaderProgressRow
from app.persistence.models.source import SourceRow
from app.services.download_transport import MemoryPageResult, NetworkOrigin
from app.services.online_reader import OnlineReaderSessionService
from app.services.reader_asset import ReaderAssetConflict, ReaderAssetError, ReaderAssetNotFound


async def _fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[_Harness, OnlineReaderSessionService, ConnectorHttpSession]:
    h, online, _runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
    reference = str(uuid4())
    http = ConnectorHttpSession(
        test_transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "result": "ok",
                    "chapter": {
                        "hash": "a" * 32,
                        "data": [f"{n}-original.png" for n in range(1, 7)],
                        "dataSaver": [f"{n}-small.jpg" for n in range(1, 7)],
                    },
                },
            )
        )
    )
    connector = MangaDexConnector(http.scoped_client(MANGADEX_NETWORK_ORIGINS))

    def resolve(_id: str, _version: str) -> MangaDexConnector:
        return connector

    monkeypatch.setattr(h.source_pages, "resolve", resolve)

    def origins(_id: str) -> frozenset[NetworkOrigin]:
        return frozenset({NetworkOrigin.from_url("https://uploads.mangadex.org")})

    monkeypatch.setattr(h.source_pages, "download_origins", origins)
    with h.database.write_session() as session:
        source = session.scalar(select(SourceRow))
        chapter = session.scalar(select(ChapterSourceRow))
        assert source is not None and chapter is not None
        source.connector_id, source.connector_version = (
            connector.metadata.connector_id,
            connector.metadata.connector_version,
        )
        chapter.remote_id = reference
    return h, online, http


async def _read(online: OnlineReaderSessionService, chapter: UUID, number: int) -> bytes:
    stream = await online.open_page(chapter, number)
    return b"".join(stream.iter_chunks())


def test_mangadex_has_two_image_slots_without_blocking_cached_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        h, online, http = await _fixture(tmp_path, monkeypatch)
        entered = {number: asyncio.Event() for number in (2, 3, 4)}
        release = asyncio.Event()
        active, maximum = 0, 0

        async def fetch(
            _request: PageFetchRequest,
            *,
            session_id: UUID,
            ordinal: int,
            allowed_origins: frozenset[NetworkOrigin],
        ) -> MemoryPageResult:
            nonlocal active, maximum
            active += 1
            maximum = max(active, maximum)
            try:
                if ordinal in entered:
                    entered[ordinal].set()
                    await release.wait()
                return MemoryPageResult(h.body, "image/png")
            finally:
                active -= 1

        monkeypatch.setattr(h.transport, "fetch_memory", fetch)
        tasks: list[asyncio.Task[bytes]] = []
        try:
            await online.open(h.chapter_id)
            assert await _read(online, h.chapter_id, 1) == h.body
            tasks = [
                asyncio.create_task(_read(online, h.chapter_id, number)) for number in (2, 3, 4)
            ]
            await asyncio.wait_for(asyncio.gather(entered[2].wait(), entered[3].wait()), 2)
            assert not entered[4].is_set() and maximum == 2
            assert await asyncio.wait_for(_read(online, h.chapter_id, 1), 1) == h.body
            release.set()
            assert all(body == h.body for body in await asyncio.gather(*tasks))
            assert maximum == 2 and entered[4].is_set()
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await online.close()
            await http.close()
            await h.close()

    asyncio.run(run())


def test_duplicate_direct_requests_share_a_fetch_and_survive_one_cancelled_waiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        h, online, http = await _fixture(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def fetch(
            _request: PageFetchRequest,
            *,
            session_id: UUID,
            ordinal: int,
            allowed_origins: frozenset[NetworkOrigin],
        ) -> MemoryPageResult:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return MemoryPageResult(h.body, "image/png")

        monkeypatch.setattr(h.transport, "fetch_memory", fetch)
        tasks: list[asyncio.Task[bytes]] = []
        try:
            await online.open(h.chapter_id)
            tasks = [asyncio.create_task(_read(online, h.chapter_id, 2)) for _ in range(2)]
            await asyncio.wait_for(entered.wait(), 2)
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            release.set()
            assert await tasks[1] == h.body and calls == 1
            assert online.status(h.chapter_id).ready_pages == (2,)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await online.close()
            await http.close()
            await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("action", ["renew", "pause"])
def test_late_images_cannot_enter_a_renewed_or_paused_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    async def run() -> None:
        h, online, http = await _fixture(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(
            _request: PageFetchRequest,
            *,
            session_id: UUID,
            ordinal: int,
            allowed_origins: frozenset[NetworkOrigin],
        ) -> MemoryPageResult:
            entered.set()
            await release.wait()
            return MemoryPageResult(h.body, "image/png")

        monkeypatch.setattr(h.transport, "fetch_memory", fetch)
        pending: asyncio.Task[bytes] | None = None
        try:
            initial = await online.open(h.chapter_id)
            pending = asyncio.create_task(_read(online, h.chapter_id, 1))
            await asyncio.wait_for(entered.wait(), 2)
            if action == "renew":
                renewed = await asyncio.wait_for(online.open(h.chapter_id, restart=True), 2)
                assert initial.session_id != renewed.session_id
            else:
                await asyncio.wait_for(online.pause(h.chapter_id), 2)
            release.set()
            with pytest.raises((ReaderAssetConflict, ReaderAssetNotFound)):
                await pending
            assert online.status(h.chapter_id).ready_pages == ()
            assert not online._chapters[h.chapter_id].cache  # pyright: ignore[reportPrivateUsage]
        finally:
            release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await online.close()
            await http.close()
            await h.close()

    asyncio.run(run())


def test_closing_waits_for_direct_io_without_publishing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        h, online, http = await _fixture(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(
            _request: PageFetchRequest,
            *,
            session_id: UUID,
            ordinal: int,
            allowed_origins: frozenset[NetworkOrigin],
        ) -> MemoryPageResult:
            entered.set()
            await release.wait()
            return MemoryPageResult(h.body, "image/png")

        monkeypatch.setattr(h.transport, "fetch_memory", fetch)
        try:
            await online.open(h.chapter_id)
            pending = asyncio.create_task(_read(online, h.chapter_id, 1))
            await asyncio.wait_for(entered.wait(), 2)
            closing = asyncio.create_task(online.close())
            await asyncio.sleep(0)
            assert not closing.done()
            release.set()
            with pytest.raises(ReaderAssetError):
                await pending
            await asyncio.wait_for(closing, 2)
        finally:
            release.set()
            await online.close()
            await http.close()
            await h.close()

    asyncio.run(run())


def test_open_endpoint_returns_chapter_and_progress_without_marking_pages_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        h, online, _runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=h.app), base_url="http://testserver"
            ) as client:
                route = f"/api/v1/reader/{h.chapter_id}"
                opened = await client.post(route + "/open", json={"restart": False})
                assert opened.status_code == 200
                assert set(opened.json()["data"]) == {"chapter", "progress"}
                assert opened.json()["data"]["chapter"]["chapter_id"] == str(h.chapter_id)
                assert opened.json()["data"]["progress"] is None
                with h.database.read_session() as session:
                    assert session.scalar(select(func.count()).select_from(ReaderProgressRow)) == 0
                    assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
                assert (await client.get(route + "/pages/3")).status_code == 200
                assert (
                    await client.put(route + "/progress", json={"page_index": 2})
                ).status_code == 200
                reopened = await client.post(route + "/open", json={"restart": False})
                assert reopened.json()["data"]["progress"]["page_index"] == 2
                assert set((await client.get(route + "/session")).json()["data"]) == {
                    "session_id",
                    "chapter_id",
                    "state",
                    "page_count",
                    "ready_pages",
                    "error_code",
                }
                assert (
                    await client.post(f"/api/v1/reader/{uuid4()}/open", json={})
                ).status_code == 404
        finally:
            await online.close()
            await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("already_open", [False, True])
def test_background_preparation_refuses_native_sources_without_changing_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_open: bool
) -> None:
    async def run() -> None:
        h, online, _runtime, closed = await _memory_harness(tmp_path, monkeypatch)
        try:
            initial = await online.open(h.chapter_id) if already_open else None
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=h.app), base_url="http://testserver"
            ) as client:
                response = await client.post(
                    f"/api/v1/reader/{h.chapter_id}/open", json={"prepare": True}
                )
                assert response.status_code == 409 and closed == []
                if initial is not None:
                    assert online.status(h.chapter_id) == initial
                else:
                    with pytest.raises(ReaderAssetNotFound):
                        online.status(h.chapter_id)
        finally:
            await online.close()
            await h.close()

    asyncio.run(run())


def test_background_preparation_can_open_mangadex_but_never_renew_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        h, online, http = await _fixture(tmp_path, monkeypatch)
        try:
            initial = await online.open(h.chapter_id, prepare=True)
            with pytest.raises(ReaderAssetConflict):
                await online.open(h.chapter_id, prepare=True, restart=True)
            assert online.status(h.chapter_id) == initial
            assert initial.ready_pages == ()
        finally:
            await online.close()
            await http.close()
            await h.close()

    asyncio.run(run())
