from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from test_reader_session import _Harness, _harness  # pyright: ignore[reportPrivateUsage]

from app.connectors.pages import ConnectorPageReference
from app.connectors.registry import RegisteredSourceConnector
from app.fmd2_bridge import FMD2_METADATA
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.reader_session import ReaderSessionRow
from app.runtime import RuntimeState
from app.services.image_validation import ImageValidationPolicy
from app.services.online_reader import OnlineReaderSessionService
from app.services.reader_asset import ReaderAssetError, ReaderAssetNotFound
from app.services.reader_progress import ReaderProgressService
from app.services.reader_session import HybridReaderChapterLookup


@pytest.mark.parametrize("ambiguous", [False, True])
def test_novelcool_legacy_resume_uses_an_exact_current_label_without_replacing_the_chapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ambiguous: bool
) -> None:
    async def exercise() -> None:
        h, online, _runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
        website, manga = "424747b0135c4240a902eb9cfcb11db9", "/novel/The-Boxer.html"

        def reference(path: str) -> str:
            return "yomira-fmd2:v3:" + base64.urlsafe_b64encode(
                json.dumps([website, manga, path]).encode()
            ).decode().rstrip("=")

        legacy, fresh = reference("/go/rds/?data=expired"), reference("/stable16")
        template = (await h.provider.get_chapters("mock-series-1"))[0]
        remote = replace(template, remote_id=fresh, display_label="Capítulo 16", language="es")
        calls: list[str] = []

        async def catalog(_manga: str):
            return [remote, remote] if ambiguous else [remote]

        async def plan(_sid: UUID, ref: str):
            calls.append(ref)
            return (ConnectorPageReference("online:1"),)

        monkeypatch.setattr(type(h.provider), "metadata", property(lambda _self: FMD2_METADATA))
        def resolve(
            _connector_id: str, _connector_version: str
        ) -> RegisteredSourceConnector:
            return h.provider

        monkeypatch.setattr(h.source_pages, "resolve", resolve)
        monkeypatch.setattr(h.provider, "get_chapters", catalog)
        monkeypatch.setattr(h.provider, "open_online_pages", plan)
        try:
            with h.database.write_session() as session:
                chapter = session.get(ChapterRow, str(h.chapter_id))
                source = session.scalar(select(ChapterSourceRow))
                assert chapter is not None and source is not None
                chapter.display_label, chapter.language = "Capítulo 16", "es"
                source.remote_id = legacy
            if ambiguous:
                with pytest.raises(ReaderAssetNotFound):
                    await online.open(h.chapter_id)
                assert calls == []
            else:
                opened = await online.open(h.chapter_id)
                assert opened.chapter_id == h.chapter_id and opened.page_count == 1
                assert calls == [fresh]
            with h.database.read_session() as session:
                assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
                source = session.scalar(select(ChapterSourceRow))
                assert source is not None and source.remote_id == legacy
        finally:
            await online.close()
            await h.close()

    asyncio.run(exercise())


class _FaultStream(httpx.AsyncByteStream):
    def __init__(self, *, interrupted: bool) -> None:
        self.interrupted = interrupted
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"<html>not an image</html>"
        if self.interrupted:
            raise httpx.ReadError("Fixture connection interrupted")

    async def aclose(self) -> None:
        self.closed = True


class InspectableOnlineReader(OnlineReaderSessionService):
    def cache_snapshot(self) -> tuple[int, tuple[int, ...]]:
        assert self._current is not None
        return self._current.cached_bytes, tuple(self._current.cache)


async def _memory_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, invalid: bool = False
) -> tuple[_Harness, InspectableOnlineReader, RuntimeState, list[UUID]]:
    harness = await _harness(tmp_path, invalid_image=invalid)
    closed: list[UUID] = []

    async def plan(_sid: UUID, _reference: str) -> tuple[ConnectorPageReference, ...]:
        return tuple(ConnectorPageReference(f"online:{i}") for i in range(1, 10_001))

    async def release(sid: UUID) -> None:
        closed.append(sid)

    monkeypatch.setattr(harness.provider, "open_online_pages", plan, raising=False)
    monkeypatch.setattr(harness.provider, "close_online_pages", release, raising=False)
    service = InspectableOnlineReader(
        harness.database,
        source_pages=harness.source_pages,
        transport=harness.transport,
        image_policy=ImageValidationPolicy(1024, 100, 100, 10_000),
    )
    runtime = cast(RuntimeState, harness.app.state.runtime)
    runtime.reader_session_service = service
    assert runtime.reader_asset_service is not None
    lookup = HybridReaderChapterLookup(runtime.reader_asset_service, service)
    runtime.reader_progress_service = ReaderProgressService(
        harness.database,
        lookup,
        page_available=lookup.page_available,
    )
    return harness, service, runtime, closed


async def _plan_and_pages_never_use_disk_or_download_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, service, runtime, closed = await _memory_harness(tmp_path, monkeypatch)
    calls: list[str] = []
    original_send = harness.client.send

    async def send(request: httpx.Request, **kwargs: Any) -> httpx.Response:
        calls.append(request.url.path)
        return await original_send(request, **kwargs)

    monkeypatch.setattr(harness.client, "send", send)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Online reading must not call a disk writer or reservation")

    monkeypatch.setattr(harness.transport, "download", forbidden)
    monkeypatch.setattr(harness.transport, "_preflight_job_temporary_budget", forbidden)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=harness.app),
            base_url="http://testserver",
        ) as client:
            route = f"/api/v1/reader/{harness.chapter_id}"
            opened = await client.post(route + "/session", json={})
            assert opened.status_code == 200 and opened.json()["data"]["page_count"] == 10_000
            assert calls == []
            assert (await client.get(route)).status_code == 200
            assert (
                await client.put(route + "/progress", json={"page_index": 9999})
            ).status_code == 422
            for ordinal in (1, 10_000):
                response = await client.get(route + f"/pages/{ordinal}")
                assert response.content == harness.body and response.status_code == 200
                assert "no-store" in response.headers["cache-control"]
            assert calls == ["/1", "/10000"]
            assert (
                await client.put(route + "/progress", json={"page_index": 9999})
            ).status_code == 200
            assert (await client.delete(route + "/session")).status_code == 200
            assert closed == [service.status(harness.chapter_id).session_id]
            assert (
                await client.post(route + "/session", json={"restart": True})
            ).status_code == 200
            assert (await client.get(route + "/progress")).json()["data"]["page_index"] == 9999
        with harness.database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
            assert session.scalar(select(func.count()).select_from(ReaderSessionRow)) == 0
        assert not (tmp_path / "cache/reader").exists()
        assert not list(tmp_path.rglob("*.part")) and not list(tmp_path.rglob("*.page"))
        assert not list(tmp_path.rglob("*.cbz"))
        assert runtime.reader_progress_service is not None
    finally:
        await service.close()
        await harness.close()


async def _memory_image_capacity_and_repeated_close_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, service, _runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
    try:
        await service.open(harness.chapter_id)
        one = await service.open_page(harness.chapter_id, 1)
        two = await service.open_page(harness.chapter_id, 2)
        with pytest.raises(ReaderAssetError):
            await service.open_page(harness.chapter_id, 3)
        await asyncio.to_thread(one.close)
        one.close()
        three = await service.open_page(harness.chapter_id, 3)
        two.close()
        three.close()
        assert service.status(harness.chapter_id).ready_pages == (1, 2, 3)
    finally:
        await service.close()
        await harness.close()


async def _invalid_online_image_does_not_enable_progress_or_create_a_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, service, runtime, _closed = await _memory_harness(tmp_path, monkeypatch, invalid=True)
    try:
        await service.open(harness.chapter_id)
        for _ in range(3):
            with pytest.raises(ReaderAssetError):
                await service.open_page(harness.chapter_id, 1)
        assert service.status(harness.chapter_id).ready_pages == ()
        assert runtime.reader_progress_service is not None
        from app.services.reader_progress import ReaderProgressPageOutOfRange

        with pytest.raises(ReaderProgressPageOutOfRange):
            runtime.reader_progress_service.save(harness.chapter_id, 0)
        assert not list(tmp_path.rglob("*.part"))
    finally:
        await service.close()
        await harness.close()


def test_online_plan_and_pages_never_use_disk_or_download_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_plan_and_pages_never_use_disk_or_download_jobs(tmp_path, monkeypatch))


def test_memory_image_capacity_and_repeated_close_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_memory_image_capacity_and_repeated_close_are_bounded(tmp_path, monkeypatch))


def test_invalid_online_image_does_not_enable_progress_or_create_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _invalid_online_image_does_not_enable_progress_or_create_a_file(tmp_path, monkeypatch)
    )


def test_reopening_a_lost_provider_plan_preserves_progress_and_revalidates_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        harness, service, _runtime, closed = await _memory_harness(tmp_path, monkeypatch)
        active: UUID | None = None
        original_resolve = harness.provider.resolve_page

        async def plan(sid: UUID, _reference: str) -> tuple[ConnectorPageReference, ...]:
            nonlocal active
            active = sid
            return tuple(ConnectorPageReference(f"{sid}:{i}") for i in range(1, 4))

        def resolve(reference: ConnectorPageReference):
            sid, ordinal = reference.remote_reference.rsplit(":", 1)
            if active is None or sid != str(active):
                raise RuntimeError("The provider no longer has this plan")
            return original_resolve(ConnectorPageReference(f"online:{ordinal}"))

        monkeypatch.setattr(harness.provider, "open_online_pages", plan)
        monkeypatch.setattr(harness.provider, "resolve_page", resolve)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=harness.app), base_url="http://testserver"
            ) as client:
                route = f"/api/v1/reader/{harness.chapter_id}"
                first = await client.post(route + "/session", json={"restart": True})
                assert first.status_code == 200
                assert (await client.get(route + "/pages/1")).content == harness.body
                assert (
                    await client.put(route + "/progress", json={"page_index": 0})
                ).status_code == 200
                active = None
                assert (await client.get(route + "/pages/2")).status_code == 503
                assert (await client.get(route + "/progress")).json()["data"]["page_index"] == 0
                reopened = await client.post(route + "/session", json={"restart": True})
                assert reopened.status_code == 200
                assert reopened.json()["data"]["session_id"] != first.json()["data"]["session_id"]
                assert reopened.json()["data"]["ready_pages"] == []
                assert len(closed) == 1
                assert (await client.get(route + "/progress")).json()["data"]["page_index"] == 0
                assert (
                    await client.put(route + "/progress", json={"page_index": 1})
                ).status_code == 422
                page = await client.get(route + "/pages/2")
                assert page.status_code == 200 and page.content == harness.body
                assert "no-store" in page.headers["cache-control"]
                assert (
                    await client.put(route + "/progress", json={"page_index": 1})
                ).status_code == 200
            with harness.database.read_session() as session:
                assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
            assert not list(tmp_path.rglob("*.part"))
            assert not list(tmp_path.rglob("*.page"))
            assert not list(tmp_path.rglob("*.cbz"))
        finally:
            await service.close()
            await harness.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["403", "429", "503", "timeout", "body", "image"])
def test_online_page_failure_preserves_progress_and_releases_capacity_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def exercise() -> None:
        harness, service, _runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
        original_send = harness.client.send
        failed_streams: list[_FaultStream] = []
        delays: list[float] = []
        failing = True
        now = [0.0]
        monkeypatch.setattr(harness.transport, "_clock", lambda: now[0])

        async def sleep(delay: float) -> None:
            delays.append(delay)

        async def send(request: httpx.Request, **kwargs: Any) -> httpx.Response:
            if not failing or request.url.path != "/2":
                return await original_send(request, **kwargs)
            if failure == "timeout":
                raise httpx.ReadTimeout("Fixture timed out", request=request)
            stream = _FaultStream(interrupted=failure == "body")
            failed_streams.append(stream)
            return httpx.Response(
                int(failure) if failure.isdigit() else 200,
                request=request,
                stream=stream,
                headers={"Content-Type": "image/png", "Retry-After": "900"},
            )

        monkeypatch.setattr(harness.client, "send", send)
        monkeypatch.setattr(harness.transport, "_sleep", sleep)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=harness.app), base_url="http://testserver"
            ) as client:
                route = f"/api/v1/reader/{harness.chapter_id}"
                opened = (await client.post(route + "/session", json={})).json()["data"]
                assert (await client.get(route + "/pages/1")).content == harness.body
                assert (
                    await client.put(route + "/progress", json={"page_index": 0})
                ).status_code == 200
                saved = (await client.get(route + "/progress")).json()["data"]
                for _ in range(3):
                    assert (await client.get(route + "/pages/2")).status_code == 503
                    assert service.status(harness.chapter_id).ready_pages == (1,)
                    assert (
                        await client.put(route + "/progress", json={"page_index": 1})
                    ).status_code == 422
                    assert (await client.get(route + "/progress")).json()["data"] == saved
                assert all(stream.closed for stream in failed_streams)
                if failure == "429":
                    assert delays == [] and len(failed_streams) == 1
                    assert (await client.get(route + "/pages/1")).content == harness.body
                    now[0] = 900.0
                elif failure in {"503", "timeout", "body"}:
                    assert len(delays) == 9
                else:
                    assert delays == []
                failing = False
                retried = await client.get(route + "/pages/2")
                assert retried.status_code == 200 and retried.content == harness.body
                assert service.status(harness.chapter_id).session_id == UUID(opened["session_id"])
                assert (
                    await client.put(route + "/progress", json={"page_index": 1})
                ).status_code == 200
            with harness.database.read_session() as session:
                assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
            assert not (tmp_path / "cache/reader").exists()
            assert not any(tmp_path.rglob("*.part"))
            assert not any(tmp_path.rglob("*.page"))
            assert not any(tmp_path.rglob("*.cbz"))
        finally:
            await service.close()
            await harness.close()

    asyncio.run(exercise())


def test_cancelled_online_page_releases_streaming_capacity_without_advancing_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        harness, service, runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
        original_send = harness.client.send
        waiting = asyncio.Event()

        async def send(request: httpx.Request, **kwargs: Any) -> httpx.Response:
            if request.url.path == "/2":
                waiting.set()
                await asyncio.Event().wait()
            return await original_send(request, **kwargs)

        monkeypatch.setattr(harness.client, "send", send)
        pending: asyncio.Task[Any] | None = None
        try:
            await service.open(harness.chapter_id)
            first = await service.open_page(harness.chapter_id, 1)
            first.close()
            assert runtime.reader_progress_service is not None
            saved = runtime.reader_progress_service.save(harness.chapter_id, 0)
            pending = asyncio.create_task(service.open_page(harness.chapter_id, 2))
            await asyncio.wait_for(waiting.wait(), timeout=2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, timeout=2)
            assert service.status(harness.chapter_id).ready_pages == (1,)
            assert runtime.reader_progress_service.get(harness.chapter_id) == saved
            monkeypatch.setattr(harness.client, "send", original_send)
            second = await service.open_page(harness.chapter_id, 2)
            try:
                third = await service.open_page(harness.chapter_id, 3)
                third.close()
            finally:
                second.close()
            assert service.status(harness.chapter_id).ready_pages == (1, 2, 3)
            with harness.database.read_session() as session:
                assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
            assert not (tmp_path / "cache/reader").exists()
            assert not any(tmp_path.rglob("*.part"))
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await service.close()
            await harness.close()

    asyncio.run(exercise())


def test_reopening_reuses_verified_images_but_explicit_retry_and_pause_refresh_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        h, service, _runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
        calls: list[str] = []
        original = h.client.send

        async def send(request: httpx.Request, **kwargs: Any) -> httpx.Response:
            calls.append(request.url.path)
            return await original(request, **kwargs)

        monkeypatch.setattr(h.client, "send", send)
        try:
            first = await service.open(h.chapter_id)
            page = await service.open_page(h.chapter_id, 3)
            assert b"".join(page.iter_chunks()) == h.body
            reopened = await service.open(h.chapter_id)
            assert reopened.session_id == first.session_id
            page = await service.open_page(h.chapter_id, 3)
            assert b"".join(page.iter_chunks()) == h.body
            assert calls == ["/3"]
            refreshed = await service.open(h.chapter_id, restart=True)
            assert refreshed.session_id != first.session_id and refreshed.ready_pages == ()
            page = await service.open_page(h.chapter_id, 3)
            page.close()
            assert calls == ["/3", "/3"]
            await service.pause(h.chapter_id)
            resumed = await service.open(h.chapter_id)
            assert resumed.state == "complete" and resumed.session_id != refreshed.session_id
            page = await service.open_page(h.chapter_id, 3)
            page.close()
            assert calls == ["/3", "/3", "/3"]
        finally:
            await service.close()
            await h.close()

    asyncio.run(exercise())


def test_verified_image_cache_evicts_the_least_recently_used_page_within_its_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        h, service, _runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
        monkeypatch.setattr("app.services.online_reader.ONLINE_IMAGE_CACHE_BYTES", len(h.body) * 2)
        calls: list[str] = []
        original = h.client.send

        async def send(request: httpx.Request, **kwargs: Any) -> httpx.Response:
            calls.append(request.url.path)
            return await original(request, **kwargs)

        monkeypatch.setattr(h.client, "send", send)
        try:
            await service.open(h.chapter_id)
            for number in [1, 2, 1, 3, 2]:
                page = await service.open_page(h.chapter_id, number)
                page.close()
            assert calls == ["/1", "/2", "/3", "/2"]
            cached_bytes, cached_pages = service.cache_snapshot()
            assert cached_bytes == len(h.body) * 2
            assert cached_pages == (3, 2)
        finally:
            await service.close()
            await h.close()

    asyncio.run(exercise())


def test_cached_current_page_does_not_wait_for_a_slow_neighbour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        h, service, _runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
        original = h.client.send
        waiting, resume = asyncio.Event(), asyncio.Event()

        async def send(request: httpx.Request, **kwargs: Any) -> httpx.Response:
            if request.url.path == "/2":
                waiting.set()
                await resume.wait()
            return await original(request, **kwargs)

        monkeypatch.setattr(h.client, "send", send)
        pending = None
        try:
            await service.open(h.chapter_id)
            first = await service.open_page(h.chapter_id, 1)
            first.close()
            pending = asyncio.create_task(service.open_page(h.chapter_id, 2))
            await asyncio.wait_for(waiting.wait(), 2)
            reopened = await asyncio.wait_for(service.open(h.chapter_id), 1)
            assert reopened.state == "complete"
            cached = await asyncio.wait_for(service.open_page(h.chapter_id, 1), 1)
            assert b"".join(cached.iter_chunks()) == h.body
            assert not pending.done()
        finally:
            resume.set()
            if pending is not None:
                page = await pending
                page.close()
            await service.close()
            await h.close()

    asyncio.run(exercise())
