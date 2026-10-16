from __future__ import annotations

import asyncio
import io
import os
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import func, select
from support.sqlite import open_test_database, reopen_test_database

from app.connectors.fmd2 import FMD2_METADATA
from app.connectors.mock import MockConnector
from app.connectors.pages import ConnectorPageAvailability, ConnectorPageReference, PageFetchRequest
from app.connectors.registry import ConnectorRegistration, ConnectorRegistry
from app.connectors.sandbox import ConnectorHostContext
from app.factory import create_app
from app.persistence.atomic_file_publisher import ADR_0087_RETRY_POLICY, AtomicFilePublisher
from app.persistence.database import SQLiteDatabase
from app.persistence.disk_budget import DiskBudgetManager
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.reader_progress import ReaderProgressRow
from app.persistence.models.reader_session import ReaderSessionPageRow, ReaderSessionRow
from app.runtime import RuntimeState
from app.secrets.vault import WindowsDpapiSecretVault
from app.services.download_transport import (
    DownloadTransport,
    DownloadTransportPolicy,
    NetworkOrigin,
)
from app.services.image_validation import ImageValidationPolicy
from app.services.jobs import DownloadJobService
from app.services.reader_asset import (
    ReaderAssetConflict,
    ReaderAssetError,
    ReaderAssetLimits,
    ReaderAssetNotFound,
    ReaderAssetService,
)
from app.services.reader_progress import ReaderProgressPageOutOfRange, ReaderProgressService
from app.services.reader_session import HybridReaderChapterLookup, ReaderSessionService
from app.services.source_pages import SourcePageService
from app.settings import Settings


class _Images(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body


class _Progressive(MockConnector):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.started: set[UUID] = set()
        self.generations: dict[UUID, UUID] = {}

    async def acquire_pages(
        self,
        job_id: UUID,
        remote_chapter_reference: str,
        chapter_title: str,
    ) -> tuple[ConnectorPageReference, ...]:
        self.started.add(job_id)
        await self.release.wait()
        return (ConnectorPageReference("online:1"), ConnectorPageReference("online:2"))

    async def get_ready_pages(
        self,
        job_id: UUID,
        previous: ConnectorPageAvailability | None = None,
    ) -> ConnectorPageAvailability:
        generation = self.generations.setdefault(job_id, uuid4())
        if job_id not in self.started:
            return ConnectorPageAvailability(generation, 0, (), False)
        pages = ((1, ConnectorPageReference("online:1")),)
        if self.release.is_set():
            pages += ((2, ConnectorPageReference("online:2")),)
        return ConnectorPageAvailability(generation, 2, pages, self.release.is_set())

    def resolve_page(self, page_reference: ConnectorPageReference) -> PageFetchRequest:
        ordinal = int(page_reference.remote_reference.rsplit(":", 1)[1])
        return PageFetchRequest(url=f"https://pages.test/{ordinal}")


@dataclass
class _Harness:
    database: SQLiteDatabase
    provider: _Progressive
    service: ReaderSessionService
    source_pages: SourcePageService
    transport: DownloadTransport
    client: httpx.AsyncClient
    app: FastAPI
    chapter_id: UUID
    body: bytes

    async def finish(self) -> None:
        self.provider.release.set()
        await _wait(self.service, self.chapter_id, complete=True)

    async def close(self) -> None:
        await self.service.close()
        await self.client.aclose()
        self.database.close()


def test_retiring_the_disk_cache_preserves_canonical_progress(tmp_path: Path) -> None:
    async def exercise() -> None:
        h = await _harness(tmp_path)
        try:
            await h.service.open(h.chapter_id)
            await h.finish()
            runtime = cast(RuntimeState, h.app.state.runtime)
            assert runtime.reader_progress_service is not None
            runtime.reader_progress_service.save(h.chapter_id, 1)
            sid = h.service.status(h.chapter_id).session_id
            await h.service.close()
            await h.service.retire_cache()
            assert not (tmp_path / "cache/reader" / str(sid)).exists()
            assert not list(tmp_path.rglob("*.page"))
            with h.database.read_session() as session:
                progress = session.scalar(select(ReaderProgressRow))
                assert progress is not None and progress.page_index == 1
                assert session.scalar(select(func.count()).select_from(ReaderSessionPageRow)) == 0
        finally:
            await h.close()
    asyncio.run(exercise())


def _new_service(harness: _Harness, database: SQLiteDatabase) -> ReaderSessionService:
    return ReaderSessionService(
        database,
        source_pages=harness.source_pages,
        transport=harness.transport,
        disk_budget=DiskBudgetManager(database),
        publisher=AtomicFilePublisher(
            data_root_identity=database.data_root_identity,
            retry_policy=ADR_0087_RETRY_POLICY,
            sleep=time.sleep,
            clock=time.monotonic,
            storage_gate=database.storage_gate,
        ),
        image_policy=ImageValidationPolicy(1024, 100, 100, 10_000),
    )


async def _harness(tmp_path: Path, *, invalid_image: bool = False) -> _Harness:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    provider = _Progressive()
    registry = ConnectorRegistry(
        context=ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(tmp_path)),
        registrations=(
            ConnectorRegistration(
                metadata=provider.metadata,
                factory=lambda _c: provider,
                network_origins=frozenset({"https://pages.test"}),
            ),
        ),
    )
    chapters = await provider.get_chapters("mock-series-1")
    metadata = provider.normalize_metadata(await provider.get_series("mock-series-1"))
    registered = DownloadJobService(database).register_reader_chapter(
        connector=provider.metadata,
        remote_series_id="mock-series-1",
        series_metadata=metadata,
        chapters=chapters,
        selected_remote_id="mock-chapter-1",
    )
    buffer = io.BytesIO()
    Image.new("RGB", (4, 6), "white").save(buffer, format="PNG")
    body = b"<html>not an image</html>" if invalid_image else buffer.getvalue()
    handler = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            stream=_Images(body),
            headers={"Content-Type": "image/png"},
        )
    )
    client = httpx.AsyncClient(transport=handler, trust_env=False)
    budget = DiskBudgetManager(database)
    transport = DownloadTransport(
        client,
        DownloadTransportPolicy(
            allowed_origins=frozenset({NetworkOrigin.from_url("https://pages.test")}),
            max_bytes=1024,
            chunk_size=64,
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            write_timeout_seconds=1,
            pool_timeout_seconds=1,
            max_redirects=0,
            max_retry_after_seconds=1,
            global_concurrency=1,
            per_host_concurrency=1,
        ),
        data_root=tmp_path,
        data_root_identity=database.data_root_identity,
        storage_gate=database.storage_gate,
        disk_budget_manager=budget,
        test_transport=handler,
    )
    service = ReaderSessionService(
        database,
        source_pages=SourcePageService(registry),
        transport=transport,
        disk_budget=budget,
        publisher=AtomicFilePublisher(
            data_root_identity=database.data_root_identity,
            retry_policy=ADR_0087_RETRY_POLICY,
            sleep=time.sleep,
            clock=time.monotonic,
            storage_gate=database.storage_gate,
        ),
        image_policy=ImageValidationPolicy(1024, 100, 100, 10_000),
    )
    local = ReaderAssetService(
        database,
        data_root_identity=database.data_root_identity,
        limits=ReaderAssetLimits(2_147_483_648, 67_108_864, 65_536),
    )
    lookup = HybridReaderChapterLookup(local, service)
    app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = app.state.runtime
    assert isinstance(runtime, RuntimeState)
    runtime.database = database
    runtime.reader_asset_service = local
    runtime.reader_session_service = service
    runtime.reader_progress_service = ReaderProgressService(
        database, lookup, page_available=lookup.page_available
    )
    return _Harness(
        database,
        provider,
        service,
        SourcePageService(registry),
        transport,
        client,
        app,
        registered.chapter_id,
        body,
    )


async def _wait(service: ReaderSessionService, chapter_id: UUID, *, complete: bool = False) -> None:
    for _ in range(100):
        current = service.status(chapter_id)
        assert current.state != "failed", current
        if current.state == "complete" if complete else bool(current.ready_pages):
            return
        await asyncio.sleep(0.03)
    raise AssertionError("Reader cache did not become available")


def test_first_page_is_served_by_existing_reader_api_before_completion(tmp_path: Path) -> None:
    async def exercise() -> None:
        h = await _harness(tmp_path)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=h.app), base_url="http://testserver"
            ) as client:
                url = f"/api/v1/reader/{h.chapter_id}"
                first = await client.post(url + "/session", json={})
                repeated = await client.post(url + "/session", json={})
                assert first.status_code == repeated.status_code == 200
                assert first.json()["data"]["session_id"] == repeated.json()["data"]["session_id"]
                await _wait(h.service, h.chapter_id)
                state = (await client.get(url + "/session")).json()["data"]
                assert state["state"] == "acquiring" and state["ready_pages"] == [1]
                assert "remote_reference" not in state and "generation" not in state
                metadata = await client.get(url)
                assert metadata.status_code == 200 and metadata.json()["data"]["page_count"] == 2
                assert (await client.get(url + "/pages/0")).status_code == 404
                page = await client.get(url + "/pages/1")
                assert page.status_code == 200 and page.content == h.body
                assert page.headers["content-type"] == "image/png"
                assert (await client.get(url + "/pages/2")).status_code == 404
                saved = await client.put(url + "/progress", json={"page_index": 0})
                assert saved.status_code == 200 and saved.json()["data"]["chapter_id"] == str(
                    h.chapter_id
                )
                assert (
                    await client.put(url + "/progress", json={"page_index": 1})
                ).status_code == 422
                await h.finish()
                assert (await client.get(url + "/pages/2")).content == h.body
            with h.database.read_session() as session:
                assert session.scalar(select(func.count()).select_from(ReaderSessionRow)) == 1
                assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
            assert not (tmp_path / "library").exists()
        finally:
            await h.close()

    asyncio.run(exercise())


def test_invalid_image_never_becomes_a_reader_page(tmp_path: Path) -> None:
    async def exercise() -> None:
        h = await _harness(tmp_path, invalid_image=True)
        try:
            await h.service.open(h.chapter_id)
            for _ in range(100):
                if h.service.status(h.chapter_id).state == "failed":
                    break
                await asyncio.sleep(0.03)
            state = h.service.status(h.chapter_id)
            assert state.state == "failed" and state.ready_pages == ()
            with pytest.raises(ReaderAssetNotFound):
                h.service.open_page(h.chapter_id, 1)
            assert not list((tmp_path / "temp").rglob("*.part"))
        finally:
            await h.close()

    asyncio.run(exercise())


def test_stream_pin_prevents_cleanup_and_release_allows_eviction(tmp_path: Path) -> None:
    async def exercise() -> None:
        h = await _harness(tmp_path)
        try:
            await h.service.open(h.chapter_id)
            await h.finish()
            limit = 500 * 1024 * 1024
            stream = h.service.open_page(h.chapter_id, 1)
            with pytest.raises(ReaderAssetError):
                h.service.trim(needed_bytes=limit)
            assert b"".join(stream.iter_chunks()) == h.body
            stream.close()
            assert h.service.trim(needed_bytes=limit) == limit
            with pytest.raises(ReaderAssetNotFound):
                h.service.status(h.chapter_id)
            assert not list((tmp_path / "cache/reader").rglob("*.page"))
        finally:
            await h.close()

    asyncio.run(exercise())


def test_reopened_cache_recovers_publication_and_keeps_reading_progress(tmp_path: Path) -> None:
    async def exercise() -> None:
        h = await _harness(tmp_path)
        try:
            await h.service.open(h.chapter_id)
            await h.finish()
            lookup = HybridReaderChapterLookup(h.app.state.runtime.reader_asset_service, h.service)
            progress = ReaderProgressService(
                h.database, lookup, page_available=lookup.page_available
            )
            saved = progress.save(h.chapter_id, 1)
            state = h.service.status(h.chapter_id)
            with h.database.write_session() as session:
                page = session.get(ReaderSessionPageRow, (str(state.session_id), 1))
                row = session.get(ReaderSessionRow, str(state.session_id))
                assert page is not None and row is not None
                page.state, row.state = "publishing", "acquiring"
                row.connector_id = FMD2_METADATA.connector_id
                row.connector_version = FMD2_METADATA.connector_version
                row.native_released = False
            await h.service.close()
            h.database.close()
            h.database = reopen_test_database(h.database)
            h.service = _new_service(h, h.database)
            h.service.recover()
            assert h.service.status(h.chapter_id).state == "interrupted"
            assert h.service.status(h.chapter_id).ready_pages == (1, 2)
            assert (await h.service.open(h.chapter_id)).session_id == state.session_id
            with pytest.raises(ReaderAssetConflict, match="unsettled"):
                await h.service.open(h.chapter_id, restart=True)
            stream = h.service.open_page(h.chapter_id, 1)
            assert b"".join(stream.iter_chunks()) == h.body
            local = ReaderAssetService(
                h.database,
                data_root_identity=h.database.data_root_identity,
                limits=ReaderAssetLimits(2_147_483_648, 67_108_864, 65_536),
            )
            reopened = ReaderProgressService(
                h.database, HybridReaderChapterLookup(local, h.service)
            )
            assert reopened.get(h.chapter_id) == saved
        finally:
            await h.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["corrupt", "unknown", "hardlink"])
def test_changed_or_unowned_cache_is_preserved_and_rejected(tmp_path: Path, change: str) -> None:
    async def exercise() -> None:
        h = await _harness(tmp_path)
        try:
            await h.service.open(h.chapter_id)
            await h.finish()
            sid = h.service.status(h.chapter_id).session_id
            path = tmp_path / "cache/reader" / str(sid) / "00001.page"
            if change == "corrupt":
                path.write_bytes(b"changed")
                with pytest.raises(ReaderAssetConflict):
                    h.service.open_page(h.chapter_id, 1)
                assert path.read_bytes() == b"changed"
            elif change == "unknown":
                note = path.parent / "preserve.txt"
                note.write_text("preserve", encoding="utf-8")
                with pytest.raises(ReaderAssetConflict):
                    h.service.trim(needed_bytes=500 * 1024 * 1024)
                assert note.read_text() == "preserve" and path.exists()
            else:
                other = tmp_path / "preserve.png"
                os.link(path, other)
                with pytest.raises(ReaderAssetConflict):
                    h.service.open_page(h.chapter_id, 1)
                assert other.read_bytes() == h.body and path.exists()
        finally:
            await h.close()

    asyncio.run(exercise())


def test_pause_and_restart_keep_canonical_progress_without_mixing_attempts(tmp_path: Path) -> None:
    async def exercise() -> None:
        h = await _harness(tmp_path)
        try:
            before = await h.service.open(h.chapter_id)
            await _wait(h.service, h.chapter_id)
            progress = h.app.state.runtime.reader_progress_service
            assert isinstance(progress, ReaderProgressService)
            saved = progress.save(h.chapter_id, 0)
            with pytest.raises(ReaderProgressPageOutOfRange):
                progress.save(h.chapter_id, 1)
            paused = await h.service.pause(h.chapter_id)
            assert paused.state == "interrupted" and paused.ready_pages == (1,)
            h.provider.release.set()
            restarted = await h.service.open(h.chapter_id, restart=True)
            assert restarted.session_id != before.session_id
            await h.finish()
            assert progress.get(h.chapter_id) == saved
        finally:
            await h.close()

    asyncio.run(exercise())


def test_pause_waits_for_atomic_publication_before_recovering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    publish = AtomicFilePublisher.publish_no_replace

    def held_publication(
        publisher: AtomicFilePublisher, source: Path, destination: Path, *, job_id: UUID
    ) -> object:
        entered.set()
        assert release.wait(5), "Publication was never released"
        return publish(publisher, source, destination, job_id=job_id)

    monkeypatch.setattr(AtomicFilePublisher, "publish_no_replace", held_publication)

    async def exercise() -> None:
        h = await _harness(tmp_path)
        pause: asyncio.Task[object] | None = None
        try:
            await h.service.open(h.chapter_id)
            assert await asyncio.to_thread(entered.wait, 3)
            pause = asyncio.create_task(h.service.pause(h.chapter_id))
            await asyncio.sleep(0.05)
            assert not pause.done(), "Cleanup raced the outstanding atomic publication"
            release.set()
            await asyncio.wait_for(pause, 3)
            assert h.service.status(h.chapter_id).ready_pages == ()
            h.service.recover()
            assert h.service.status(h.chapter_id).ready_pages == (1,)
            assert b"".join(h.service.open_page(h.chapter_id, 1).iter_chunks()) == h.body
        finally:
            release.set()
            if pause is not None:
                await asyncio.gather(pause, return_exceptions=True)
            await h.close()

    asyncio.run(exercise())


def test_deferred_native_cleanup_is_retried_before_restarting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import reader_session

    failures = 1

    async def available(_provider: _Progressive) -> bool:
        return True

    async def cleanup(_provider: _Progressive, _sid: UUID, *, restored: bool = False) -> None:
        nonlocal failures
        if failures:
            failures -= 1
            raise OSError("Synthetic transient file lock")

    monkeypatch.setattr(reader_session, "Fmd2Connector", _Progressive)
    monkeypatch.setattr(_Progressive, "connection_available", available, raising=False)
    monkeypatch.setattr(_Progressive, "release_reader_staging", cleanup, raising=False)

    async def exercise() -> None:
        h = await _harness(tmp_path, invalid_image=True)
        try:
            old = await h.service.open(h.chapter_id)
            for _ in range(100):
                if h.service.status(h.chapter_id).state == "failed":
                    break
                await asyncio.sleep(0.03)
            # Pause joins the finalizer, including its deliberately failed cleanup.
            await h.service.pause(h.chapter_id)
            with h.database.read_session() as session:
                row = session.get(ReaderSessionRow, str(old.session_id))
                assert row is not None and not row.native_released
            new = await h.service.open(h.chapter_id, restart=True)
            assert old.session_id != new.session_id
            with h.database.read_session() as session:
                row = session.get(ReaderSessionRow, str(old.session_id))
                assert row is not None and row.native_released and row.state == "evicted"
        finally:
            await h.close()

    asyncio.run(exercise())
