import asyncio
import base64
import hashlib
import json
import threading
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from starlette.requests import Request
from support.cbz import index_test_job
from test_download_job_api import _ready_app  # pyright: ignore[reportPrivateUsage]
from test_local_import import _comic, _image  # pyright: ignore[reportPrivateUsage]
from test_local_import import _fixture as import_fixture  # pyright: ignore[reportPrivateUsage]
from test_reader_asset import _fixture as reader_fixture  # pyright: ignore[reportPrivateUsage]

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.mock import MockConnector
from app.factory import create_app
from app.fmd2_bridge import (
    FMD2_METADATA,
    FMD2_SOURCE_ID,
    Fmd2Connector,
    _pack_chapter,  # pyright: ignore[reportPrivateUsage]
    _pack_reference,  # pyright: ignore[reportPrivateUsage]
)
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.series_source import SeriesSourceRow
from app.routes.reader import get_reader_page
from app.services.jobs import DownloadJobService, DownloadJobSummary
from app.services.reader_asset import ReaderPageStream
from app.services.reader_progress import ReaderProgressService
from app.settings import Settings


@pytest.mark.parametrize("version", (1, 2))
def test_saved_chapter_keeps_identity_after_catalog_reordering(
    tmp_path: Path, version: int
) -> None:
    _, database = _ready_app(tmp_path)
    connector = MockConnector()
    template = asyncio.run(connector.get_chapters("mock-series-1"))[0]
    metadata = connector.normalize_metadata(asyncio.run(connector.get_series("mock-series-1")))
    manga_url, chapter_url = "/en/action/a/list?title_no=1", "/en/action/a/viewer?episode_no=1"
    manga, page = _pack_reference(manga_url), _pack_reference(chapter_url)
    source = FMD2_SOURCE_ID
    payload = [manga, page, 1] if version == 1 else [source, manga_url, chapter_url, 1]
    old_reference = f"yomira-fmd2:v{version}:" + base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).decode().rstrip("=")
    service = DownloadJobService(database)

    def select_chapter(reference: str) -> DownloadJobSummary:
        chapter = replace(template, remote_id=reference, display_label="Chapter 1")
        return service.create_selected_jobs(
            connector=FMD2_METADATA,
            remote_series_id=manga,
            series_metadata=metadata,
            chapters=(chapter,),
            selected_remote_ids=(reference,),
        )[0]

    try:
        first = select_chapter(old_reference)
        current = _pack_chapter(manga, page, 2, SourceConnectorOperation.GET_CHAPTERS)
        assert current == _pack_chapter(manga, page, 100, SourceConnectorOperation.GET_CHAPTERS)
        assert service.chapter_availability(FMD2_METADATA.connector_id, manga)[
            "busy_remote_ids"
        ] == [current]
        repeated = select_chapter(current)
        assert not repeated.created and repeated.job.job_id == first.job.job_id
        index_test_job(database, first.job.job_id)
        assert service.chapter_availability(FMD2_METADATA.connector_id, manga)[
            "ready_remote_ids"
        ] == [current]
        assert select_chapter(current).job.job_id == first.job.job_id
    finally:
        database.close()


def test_missing_download_can_be_queued_again_without_another_chapter(tmp_path: Path) -> None:
    fixture = reader_fixture(tmp_path)
    connector = MockConnector()
    try:
        with fixture.database.write_session() as session:
            session.add(
                SeriesSourceRow(
                    series_source_id=str(uuid4()),
                    series_id=str(fixture.series.series_id),
                    source_id=str(fixture.source.source_id),
                    remote_id="reader-series",
                )
            )
        fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id, chapter_id=fixture.chapter.chapter_id, result=fixture.result
        )
        fixture.cbz_path.unlink()
        jobs = DownloadJobService(fixture.database)
        assert (
            jobs.chapter_availability("reader-fixture", "reader-series")["ready_remote_ids"] == []
        )
        chapter = replace(
            asyncio.run(connector.get_chapters("mock-series-1"))[0], remote_id="chapter-1"
        )
        selected = jobs.create_selected_jobs(
            connector=replace(connector.metadata, connector_id="reader-fixture"),
            remote_series_id="reader-series",
            series_metadata=connector.normalize_metadata(
                asyncio.run(connector.get_series("mock-series-1"))
            ),
            chapters=(chapter,),
            selected_remote_ids=("chapter-1",),
        )[0]
        assert selected.created and selected.job.status.value == "QUEUED"
        assert selected.chapter_id == fixture.chapter.chapter_id
        index_test_job(fixture.database, selected.job.job_id)
        assert jobs.chapter_availability("reader-fixture", "reader-series")["ready_remote_ids"] == [
            "chapter-1"
        ]
    finally:
        fixture.database.close()


@pytest.mark.parametrize("kind", ("cbz", "epub", "images"))
def test_scanner_rebuilds_missing_import_without_losing_chapter_identity(
    tmp_path: Path, kind: str
) -> None:
    database, root, reader, _covers, http, factory = import_fixture(tmp_path)
    importer = factory()
    original = _comic(root, kind, _image("red"), _image("blue"))

    async def run() -> None:
        await importer.scan_once()
        await importer.scan_once()
        with database.read_session() as session:
            before = tuple(session.scalars(select(ChapterRow.chapter_id)))
        assert len(before) == 1
        progress_service = ReaderProgressService(database, reader)
        saved = progress_service.save(UUID(before[0]), 1)
        generated = tuple((root / "chapters").rglob("*.cbz"))
        assert len(generated) == 1
        generated[0].unlink()
        await importer.scan_once()
        with database.read_session() as session:
            assert tuple(session.scalars(select(ChapterRow.chapter_id))) == before
            assert session.scalar(select(func.count()).select_from(ChapterSourceRow)) == 1
        restored = tuple((root / "chapters").rglob("*.cbz"))
        assert len(restored) == 1 and restored[0] != generated[0]
        assert original.exists()
        assert progress_service.get(UUID(before[0])) == saved
        stream = reader.open_page(UUID(before[0]), 1)
        try:
            assert b"".join(stream.iter_chunks())
        finally:
            stream.close()
            await http.close()

    try:
        asyncio.run(run())
    finally:
        database.close()


@pytest.mark.parametrize(
    "headers",
    (
        {"Host": "foreign.example"},
        {"Origin": "https://foreign.example"},
        {"Origin": "null"},
        {"Origin": "http://127.0.0.1:9999"},
        {"Sec-Fetch-Site": "cross-site"},
    ),
)
def test_external_requests_cannot_cancel_downloads(tmp_path: Path, headers: dict[str, str]) -> None:
    app, database = _ready_app(tmp_path)
    connector = MockConnector()
    chapters = asyncio.run(connector.get_chapters("mock-series-1"))
    job = app.state.runtime.download_job_service.create_selected_jobs(
        connector=connector.metadata,
        remote_series_id="mock-series-1",
        series_metadata=connector.normalize_metadata(
            asyncio.run(connector.get_series("mock-series-1"))
        ),
        chapters=chapters,
        selected_remote_ids=(chapters[0].remote_id,),
    )[0].job

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(f"/api/v1/downloads/{job.job_id}/cancel", headers=headers)
            assert response.status_code == 403
            assert response.headers["x-trace-id"] == response.json()["error"]["trace_id"]
            assert (
                await client.post(
                    f"/api/v1/downloads/{job.job_id}/cancel",
                    headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
                )
            ).status_code == 200

    try:
        asyncio.run(run())
    finally:
        database.close()


def test_reader_verification_leaves_other_requests_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = reader_fixture(tmp_path)
    fixture.service.index_verified_cbz(
        job_id=fixture.job.job_id, chapter_id=fixture.chapter.chapter_id, result=fixture.result
    )
    app = create_app(Settings(app_env="test", data_root=tmp_path))
    app.state.runtime.reader_asset_service = fixture.service
    original = fixture.service.open_page

    started, release = threading.Event(), threading.Event()

    def slow_storage(chapter_id: UUID, page: int) -> ReaderPageStream:
        started.set()
        assert release.wait(3)
        return original(chapter_id, page)

    monkeypatch.setattr(fixture.service, "open_page", slow_storage)

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            page = asyncio.create_task(
                client.get(f"/api/v1/reader/{fixture.chapter.chapter_id}/pages/1")
            )
            try:
                assert await asyncio.to_thread(started.wait, 2)
                assert not page.done()
                response = await asyncio.wait_for(client.get("/api/v1/not-a-route"), 1)
                assert response.status_code == 404
                assert not page.done()
            finally:
                release.set()
            assert (await page).status_code == 200

    try:
        asyncio.run(run())
    finally:
        fixture.database.close()


def test_archive_hash_cache_is_reused_and_invalidated_by_a_file_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = reader_fixture(tmp_path)
    fixture.service.index_verified_cbz(
        job_id=fixture.job.job_id, chapter_id=fixture.chapter.chapter_id, result=fixture.result
    )
    fixture.service._verified_files.clear()  # pyright: ignore[reportPrivateUsage]
    hashes: list[int] = []
    real_hash = hashlib.sha256

    def track_hash(data: bytes = b""):
        if not data:
            hashes.append(1)
        return real_hash(data)

    monkeypatch.setattr("app.services.reader_asset.hashlib.sha256", track_hash)
    try:
        for _ in range(2):
            stream = fixture.service.open_page(fixture.chapter.chapter_id, 1)
            stream.close()
        assert len(hashes) == 3  # One archive hash and two page hashes.
        hashes.clear()
        fixture.cbz_path.touch()
        stream = fixture.service.open_page(fixture.chapter.chapter_id, 1)
        stream.close()
        assert len(hashes) == 2
    finally:
        fixture.database.close()


def test_uncertain_native_attempt_blocks_the_next_download_until_cancelled(tmp_path: Path) -> None:
    first, second = uuid4(), uuid4()
    posts: list[str] = []
    settled = False

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(json.loads(request.content)["request_id"])
            return httpx.Response(202, json={"job_id": posts[-1]})
        if request.method == "DELETE":
            if not settled:
                raise httpx.ReadTimeout("fixture", request=request)
            return httpx.Response(200, json={"status": "cancellation_requested"})
        if request.url.path.endswith(str(first)):
            if not settled:
                raise httpx.ReadTimeout("fixture", request=request)
            return httpx.Response(200, json={"status": "cancelled"})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "files": ["001.png"],
                "completed_pages": 1,
                "total_pages": 1,
            },
        )

    async def run() -> None:
        nonlocal settled
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:18873", transport=httpx.MockTransport(respond)
        ) as client:
            connector = Fmd2Connector(
                tmp_path / "unused.exe", tmp_path / "staging", api_client=client
            )
            ref = _pack_chapter(
                _pack_reference("/en/action/a/list"),
                _pack_reference("/en/action/a/viewer"),
                1,
                SourceConnectorOperation.GET_CHAPTERS,
            )
            for job_id in (first, second):
                with pytest.raises(SourceConnectorError):
                    await connector.acquire_pages(job_id, ref, "Chapter 1")
            assert posts == [str(first)]
            settled = True
            assert len(await connector.acquire_pages(second, ref, "Chapter 1")) == 1
            assert posts == [str(first), str(second)]

    asyncio.run(run())


def test_cancelled_reader_request_closes_the_eventual_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = reader_fixture(tmp_path)
    fixture.service.index_verified_cbz(
        job_id=fixture.job.job_id, chapter_id=fixture.chapter.chapter_id, result=fixture.result
    )
    app = create_app(Settings(app_env="test", data_root=tmp_path))
    app.state.runtime.reader_asset_service = fixture.service
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    original_open, original_close = fixture.service.open_page, ReaderPageStream.close

    def slow_storage(chapter_id: UUID, page: int) -> ReaderPageStream:
        started.set()
        assert release.wait(3)
        return original_open(chapter_id, page)

    def close(stream: ReaderPageStream) -> None:
        original_close(stream)
        closed.set()

    monkeypatch.setattr(fixture.service, "open_page", slow_storage)
    monkeypatch.setattr(ReaderPageStream, "close", close)

    async def run() -> None:
        request = Request({"type": "http", "app": app})
        response = asyncio.create_task(get_reader_page(fixture.chapter.chapter_id, 1, request))
        assert await asyncio.to_thread(started.wait, 2)
        response.cancel()
        with pytest.raises(asyncio.CancelledError):
            await response
        release.set()
        assert await asyncio.to_thread(closed.wait, 2)

    try:
        asyncio.run(run())
    finally:
        release.set()
        fixture.database.close()
