from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from test_online_reader import _memory_harness  # pyright: ignore[reportPrivateUsage]
from test_reader_session import _Harness  # pyright: ignore[reportPrivateUsage]

from app.connectors.pages import ConnectorPageReference, PageFetchRequest
from app.persistence.disk_budget import DiskBudgetManager
from app.persistence.models.download_job import DownloadJobRow
from app.services.jobs import DownloadJobService
from app.services.online_image_cache import OnlineImageCache
from app.services.online_reader import OnlineReaderSessionService
from app.services.reader_asset import ReaderAssetNotFound


@dataclass
class _SingleNativeReader:
    active: UUID | None = None
    opened: list[tuple[UUID, str]] = field(default_factory=lambda: list[tuple[UUID, str]]())
    closed: list[UUID] = field(default_factory=lambda: list[UUID]())
    fetched: list[tuple[str, int]] = field(default_factory=lambda: list[tuple[str, int]]())
    page_count: int = 6
    failure: str | None = None

    async def plan(self, session_id: UUID, reference: str) -> tuple[ConnectorPageReference, ...]:
        assert self.active is None, "The native engine allows only one open chapter"
        self.active = session_id
        self.opened.append((session_id, reference))
        if self.failure == "failed":
            raise RuntimeError("Source unavailable")
        if self.failure == "cancelled":
            raise asyncio.CancelledError
        return tuple(
            ConnectorPageReference(f"{session_id}|{reference}|{n}")
            for n in range(1, self.page_count + 1)
        )

    async def release(self, session_id: UUID) -> None:
        assert self.active in (None, session_id)
        self.active = None
        self.closed.append(session_id)

    def resolve(self, page: ConnectorPageReference) -> PageFetchRequest:
        session, reference, ordinal = page.remote_reference.split("|")
        assert self.active == UUID(session), "Pages must use their own active native session"
        self.fetched.append((reference, int(ordinal)))
        return PageFetchRequest(url=f"https://pages.test/{reference}/{ordinal}")


async def _add_chapter(harness: _Harness, number: int) -> UUID:
    template = (await harness.provider.get_chapters("mock-series-1"))[0]
    chapter = replace(
        template,
        remote_id=f"mock-chapter-{number}",
        display_label=f"Capítulo {number}",
        chapter_raw_label=str(number),
        chapter_major=number,
        chapter_sort_key=(number, 0),
    )
    registered = DownloadJobService(harness.database).register_reader_chapter(
        connector=harness.provider.metadata,
        remote_series_id="mock-series-1",
        series_metadata=harness.provider.normalize_metadata(
            await harness.provider.get_series("mock-series-1")
        ),
        chapters=(chapter,),
        selected_remote_id=f"mock-chapter-{number}",
    )
    return registered.chapter_id


async def _fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[_Harness, OnlineReaderSessionService, _SingleNativeReader, UUID]:
    h, online, _runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
    native = _SingleNativeReader()
    monkeypatch.setattr(h.provider, "open_online_pages", native.plan)
    monkeypatch.setattr(h.provider, "close_online_pages", native.release)
    monkeypatch.setattr(h.provider, "resolve_page", native.resolve)
    return h, online, native, await _add_chapter(h, 2)


async def _page(
    online: OnlineReaderSessionService, chapter: UUID, number: int, body: bytes
) -> None:
    stream = await online.open_page(chapter, number)
    assert b"".join(stream.iter_chunks()) == body


def test_two_open_chapters_keep_their_pages_and_progress_when_the_native_reader_switches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        h, online, native, second = await _fixture(tmp_path, monkeypatch)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=h.app), base_url="http://testserver"
            ) as client:

                async def request(
                    method: str, chapter: UUID, suffix: str, *, json: dict[str, int] | None = None
                ) -> httpx.Response:
                    response = await client.request(
                        method, f"/api/v1/reader/{chapter}{suffix}", json=json
                    )
                    response.raise_for_status()
                    return response

                first_session = (await request("POST", h.chapter_id, "/session", json={})).json()[
                    "data"
                ]["session_id"]
                await request("GET", h.chapter_id, "/pages/1")
                await request("PUT", h.chapter_id, "/progress", json={"page_index": 0})
                second_session = (await request("POST", second, "/session", json={})).json()[
                    "data"
                ]["session_id"]
                await request("GET", second, "/pages/2")
                await request("PUT", second, "/progress", json={"page_index": 1})
                assert (await request("GET", h.chapter_id, "")).json()["data"]["page_count"] == 6
                assert (await request("GET", h.chapter_id, "/progress")).json()["data"][
                    "page_index"
                ] == 0
                images = await asyncio.gather(
                    request("GET", h.chapter_id, "/pages/6"), request("GET", second, "/pages/5")
                )
                assert all(image.content == h.body for image in images)
                assert native.fetched == [
                    ("mock-chapter-1", 1),
                    ("mock-chapter-2", 2),
                    ("mock-chapter-1", 6),
                    ("mock-chapter-2", 5),
                ]
                assert str(online.status(h.chapter_id).session_id) == first_session
                assert str(online.status(second).session_id) == second_session
                await request("PUT", h.chapter_id, "/progress", json={"page_index": 5})
                await request("PUT", second, "/progress", json={"page_index": 4})
                assert (await request("GET", h.chapter_id, "/progress")).json()["data"][
                    "page_index"
                ] == 5
                assert (await request("GET", second, "/progress")).json()["data"]["page_index"] == 4
                assert (
                    await client.put(
                        f"/api/v1/reader/{h.chapter_id}/progress", json={"page_index": 3}
                    )
                ).status_code == 422
            with h.database.read_session() as session:
                assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
        finally:
            await online.close()
            assert native.active is None
            await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("disk", [False, True])
def test_cached_pages_from_another_chapter_do_not_switch_or_redownload_from_the_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disk: bool,
) -> None:
    async def run() -> None:
        h, online, native, second = await _fixture(tmp_path, monkeypatch)
        if disk:
            monkeypatch.setattr(
                online, "_disk_cache", OnlineImageCache(h.database, DiskBudgetManager(h.database))
            )
            monkeypatch.setattr("app.services.online_reader.ONLINE_IMAGE_CACHE_BYTES", len(h.body))
        try:
            await online.open(h.chapter_id)
            await _page(online, h.chapter_id, 1, h.body)
            await online.open(second)
            await _page(online, second, 2, h.body)
            if disk:
                assert not online._chapters[h.chapter_id].cache  # pyright: ignore[reportPrivateUsage]
            await _page(online, h.chapter_id, 1, h.body)
            assert len(native.opened) == 2 and len(native.fetched) == 2
            assert native.active == online.status(second).session_id
            assert online.get_chapter(h.chapter_id) is not None
        finally:
            await online.close()
            await h.close()

    asyncio.run(run())


def test_memory_and_retained_chapters_have_shared_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        h, online, native, second = await _fixture(tmp_path, monkeypatch)
        third = await _add_chapter(h, 3)
        monkeypatch.setattr("app.services.online_reader.ONLINE_IMAGE_CACHE_BYTES", len(h.body) * 2)
        monkeypatch.setattr("app.services.online_reader.ONLINE_CHAPTER_LIMIT", 2)
        try:
            for chapter in (h.chapter_id, second):
                await online.open(chapter)
                await _page(online, chapter, 1, h.body)
            await _page(online, second, 2, h.body)
            assert sum(item.cached_bytes for item in online._chapters.values()) <= len(h.body) * 2  # pyright: ignore[reportPrivateUsage]
            await online.open(third)
            assert len(online._chapters) == 2  # pyright: ignore[reportPrivateUsage]
            with pytest.raises(ReaderAssetNotFound):
                online.status(h.chapter_id)
            assert online.get_chapter(second) is not None
            await _page(online, second, 1, h.body)
            assert len(native.opened) == 3
        finally:
            await online.close()
            await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("action", ["pause", "renew"])
def test_pausing_or_renewing_one_chapter_preserves_the_other(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    async def run() -> None:
        h, online, native, second = await _fixture(tmp_path, monkeypatch)
        try:
            first = await online.open(h.chapter_id)
            await _page(online, h.chapter_id, 1, h.body)
            other = await online.open(second)
            await _page(online, second, 2, h.body)
            if action == "pause":
                await online.pause(h.chapter_id)
                assert native.active == other.session_id
            else:
                await online.open(h.chapter_id, restart=True)
            assert online.status(second).ready_pages == (2,)
            await _page(online, second, 2, h.body)
            if action == "pause":
                assert len(native.opened) == 2
                assert online.get_chapter(h.chapter_id) is None
            resumed = await online.open(h.chapter_id)
            assert resumed.session_id != first.session_id
            assert online.status(second).session_id == other.session_id
            await _page(online, second, 3, h.body)
        finally:
            await online.close()
            await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["changed", "failed", "cancelled"])
def test_reactivating_a_broken_chapter_does_not_lose_another_chapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    async def run() -> None:
        h, online, native, second = await _fixture(tmp_path, monkeypatch)
        try:
            await online.open(h.chapter_id)
            await _page(online, h.chapter_id, 1, h.body)
            await online.open(second)
            await _page(online, second, 2, h.body)
            native.failure = failure
            if failure == "changed":
                native.page_count = 3
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=h.app), base_url="http://testserver"
            ) as client:
                if failure == "cancelled":
                    with pytest.raises(asyncio.CancelledError):
                        await online.open_page(h.chapter_id, 5)
                else:
                    response = await client.get(f"/api/v1/reader/{h.chapter_id}/pages/5")
                    assert response.status_code == (409 if failure == "changed" else 503)
            assert online.status(h.chapter_id).state == "interrupted"
            assert online.status(h.chapter_id).ready_pages == (1,)
            assert online.status(second).state == "complete"
            native.failure, native.page_count = None, 6
            await _page(online, second, 3, h.body)
            await online.open(h.chapter_id)
            await _page(online, h.chapter_id, 5, h.body)
        finally:
            await online.close()
            await h.close()

    asyncio.run(run())
