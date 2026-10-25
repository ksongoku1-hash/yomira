import asyncio
import base64
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.connectors.chapter import ConnectorChapter
from app.connectors.errors import SourceConnectorError
from app.connectors.fmd2 import (
    FMD2_API_ORIGIN,
    FMD2_PUBLIC_SOURCES,
    FMD2_SOURCE_ID,
    Fmd2Connector,
)
from app.connectors.search import ConnectorSearchQuery
from app.domain.source import SourceHealthStatus
from app.settings import SettingsValidationError, load_settings


def _fmd_reference(url: str) -> str:
    return "fmd2:v1:" + (FMD2_SOURCE_ID + "\n" + url).encode().hex().upper()


def _chapter_reference() -> str:
    body = json.dumps(
        [
            "/en/romance/lore-olympus/list?title_no=1320",
            "/en/romance/lore-olympus/episode-1/viewer?episode_no=1",
            1,
        ],
        separators=(",", ":"),
    ).encode()
    return "yomira-fmd2:v1:" + base64.urlsafe_b64encode(body).decode().rstrip("=")


def _connector(tmp_path: Path, handler: httpx.AsyncBaseTransport) -> Fmd2Connector:
    client = httpx.AsyncClient(
        base_url=FMD2_API_ORIGIN,
        headers={"Authorization": "Bearer " + "a" * 64, "Accept": "application/json"},
        transport=handler,
        follow_redirects=False,
        trust_env=False,
    )
    return Fmd2Connector(tmp_path / "fmd.exe", tmp_path / "staging", api_client=client)


def test_fmd2_online_plan_does_not_create_a_download(tmp_path: Path) -> None:
    sid = uuid4()
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            assert request.url.path == "/v1/reader"
            assert json.loads(request.content)["request_id"] == str(sid)
            return httpx.Response(200, json={"session_id": str(sid), "page_count": 10_000})
        return httpx.Response(200, json={"status": "closed"})

    connector = _connector(tmp_path, httpx.MockTransport(handler))

    async def exercise() -> None:
        pages = await connector.open_online_pages(sid, _chapter_reference())
        assert len(pages) == 10_000
        request = connector.resolve_page(pages[-1])
        assert request.url == f"{FMD2_API_ORIGIN}/v1/reader/{sid}/pages/10000"
        assert request.secret_reference is not None
        await connector.close_online_pages(sid)
        await connector.close()

    asyncio.run(exercise())
    assert calls == [("POST", "/v1/reader"), ("DELETE", f"/v1/reader/{sid}")]
    assert not (tmp_path / "staging").exists()


def test_novelcool_online_resume_prepares_its_catalog_and_reuses_it(tmp_path: Path) -> None:
    website = "424747b0135c4240a902eb9cfcb11db9"
    manga = "fmd2:v1:" + (website + "\n/novel/The-Boxer.html").encode().hex()
    chapter = "fmd2:v1:" + (website + "\n/chapter-16-id").encode().hex()
    reference = "yomira-fmd2:v3:" + base64.urlsafe_b64encode(
        json.dumps(
            [website, "/novel/The-Boxer.html", "/chapter-16-id"], separators=(",", ":")
        ).encode()
    ).decode().rstrip("=")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.method == "GET":
            assert request.url.path == f"/v1/manga/{manga}/chapters"
            return httpx.Response(
                200,
                json={
                    "chapters": [{"chapter_ref": chapter, "title": "Capítulo 16", "ordinal": 16}]
                },
            )
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "closed"})
        assert request.url.path == "/v1/reader"
        return httpx.Response(
            200, json={"session_id": json.loads(request.content)["request_id"], "page_count": 2}
        )

    connector = _connector(tmp_path, httpx.MockTransport(handler))

    async def exercise() -> None:
        try:
            for _ in range(2):
                sid = uuid4()
                assert len(await connector.open_online_pages(sid, reference)) == 2
                await connector.close_online_pages(sid)
        finally:
            await connector.close()

    asyncio.run(exercise())
    assert calls.count(f"/v1/manga/{manga}/chapters") == 1
    assert calls.count("/v1/reader") == 2
    assert not (tmp_path / "staging").exists()


@pytest.mark.parametrize("count", [True, 0, -1, 10_001, "3"])
def test_fmd2_rejects_invalid_online_plans(tmp_path: Path, count: object) -> None:
    sid = uuid4()
    connector = _connector(
        tmp_path,
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"session_id": str(sid), "page_count": count},
            )
        ),
    )

    async def exercise() -> None:
        try:
            with pytest.raises(SourceConnectorError):
                await connector.open_online_pages(sid, _chapter_reference())
        finally:
            await connector.close()

    asyncio.run(exercise())


def test_fmd2_search_chapters_and_durable_staged_pages(tmp_path: Path) -> None:
    manga_ref = _fmd_reference("/en/romance/lore-olympus/list?title_no=1320")
    chapter_ref = _fmd_reference("/en/romance/lore-olympus/episode-1/viewer?episode_no=1")
    job_id = uuid4()
    download_requests: list[dict[str, object]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/search":
            payload = json.loads(request.content)
            assert payload["source_id"] == "*"
            return httpx.Response(
                200,
                json={
                    "api_version": "v1",
                    "data": [
                        {
                            "source_id": FMD2_SOURCE_ID,
                            "manga_ref": manga_ref,
                            "remote_url": "https://www.webtoons.com/en/romance/lore-olympus/list?title_no=1320",
                            "title": "Lore Olympus",
                        }
                    ],
                },
            )
        if request.url.path.endswith("/chapters"):
            return httpx.Response(
                200,
                json={
                    "chapters": [{"chapter_ref": chapter_ref, "title": "Episode 1", "ordinal": 1}]
                },
            )
        if request.url.path.startswith("/v1/manga/"):
            return httpx.Response(
                200,
                json={
                    "manga": {
                        "manga_ref": manga_ref,
                        "source_id": FMD2_SOURCE_ID,
                        "title": "Lore Olympus",
                        "cover_url": (
                            "https://swebtoon-phinf.pstatic.net/test.jpg?type=crop540_540"
                        ),
                        "summary": "A public series.",
                        "authors": "Rachel Smythe",
                        "artists": "Rachel Smythe",
                        "genres": "Romance, Drama",
                        "status": "ONGOING",
                    }
                },
            )
        if request.url.path == "/v1/downloads":
            download_requests.append(json.loads(request.content))
            return httpx.Response(202, json={"job_id": str(job_id), "status": "queued"})
        if request.url.path == f"/v1/downloads/{job_id}":
            return httpx.Response(
                200,
                json={
                    "job_id": str(job_id),
                    "status": "completed",
                    "completed_pages": 2,
                    "total_pages": 2,
                    "files": ["002.png", "001.png"],
                },
            )
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    async def exercise() -> None:
        connector = _connector(tmp_path, httpx.MockTransport(handle))
        results = await connector.search(ConnectorSearchQuery("Lore Olympus"))
        assert len(results) == 1
        assert results[0].remote_id == manga_ref.lower()
        raw = await connector.get_series(manga_ref)
        metadata = connector.normalize_metadata(raw)
        assert metadata.title == "Lore Olympus"
        assert metadata.cover_url == "https://swebtoon-phinf.pstatic.net/test.jpg"
        assert metadata.author == "Rachel Smythe"
        chapters = await connector.get_chapters(manga_ref)
        assert len(chapters) == 1
        assert type(chapters[0]) is ConnectorChapter
        assert chapters[0].display_label == "Episode 1"
        pages = await connector.acquire_pages(
            job_id,
            chapters[0].remote_id or "",
            chapters[0].display_label,
        )
        assert [item.remote_reference.rsplit(":", 1)[-1] for item in pages] == [
            "001.png",
            "002.png",
        ]
        assert download_requests == [
            {
                "request_id": str(job_id),
                "manga_ref": manga_ref.lower(),
                "chapter_ref": chapter_ref.lower(),
                "chapter_title": "Episode 1",
            }
        ]
        resolved = connector.resolve_page(pages[0])
        assert resolved.url == (f"{FMD2_API_ORIGIN}/v1/downloads/{job_id}/files/001.png")
        await connector.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("source_id", list(FMD2_PUBLIC_SOURCES))
def test_fmd2_search_accepts_only_allowlisted_public_source_origins(
    tmp_path: Path, source_id: str
) -> None:
    source_name, source_origin, _language = FMD2_PUBLIC_SOURCES[source_id]
    remote_path = (
        "/en/action/dragon-ball/list" if source_id == FMD2_SOURCE_ID else "/manga/dragon-ball"
    )
    manga_ref = "fmd2:v1:" + (source_id + "\n" + remote_path).encode().hex()

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/search":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "source_id": source_id,
                            "manga_ref": manga_ref,
                            "remote_url": f"{source_origin}{remote_path}",
                            "title": "Dragon Ball",
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    async def exercise() -> None:
        connector = _connector(tmp_path, httpx.MockTransport(handle))
        results = await connector.search(ConnectorSearchQuery("Dragon Ball"))
        assert len(results) == 1
        assert results[0].remote_id == manga_ref
        assert results[0].remote_url == f"{source_origin}{remote_path}"
        assert results[0].source_label == source_name
        await connector.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("source_id", "cover_url", "expected"),
    [
        (
            "9185eb6c49324a849c7d7925a41ef3a3",
            "https://zonatmo.org/storage/covers/dragon-ball.jpg?v=2",
            "https://zonatmo.org/storage/covers/dragon-ball.jpg?v=2",
        ),
        (
            "9185eb6c49324a849c7d7925a41ef3a3",
            "https://attacker.example/cover.jpg",
            None,
        ),
        (
            "9185eb6c49324a849c7d7925a41ef3a3",
            "https://[invalid/cover.jpg",
            None,
        ),
        (
            "7103ae6839ea46ec80cdfc2c4b37c803",
            "https://api.asurascans.com/covers/series.jpg",
            "https://api.asurascans.com/covers/series.jpg",
        ),
        (
            "7103ae6839ea46ec80cdfc2c4b37c803",
            "https://cdn.asurascans.com/covers/series.webp",
            "https://cdn.asurascans.com/covers/series.webp",
        ),
        (
            "97dcd409f781467eb2da4a8634e5a1d9",
            "https://r2.capibaratraductor.com/tenants/test/mangas/cover.png",
            "https://r2.capibaratraductor.com/tenants/test/mangas/cover.png",
        ),
        (
            "424747b0135c4240a902eb9cfcb11db9",
            "https://img.novelcool.com/logo/naruto.jpg",
            "https://img.novelcool.com/logo/naruto.jpg",
        ),
        (
            "03ae42ac9141443fa0711bd401c0203d",
            "https://cdn1.intomanga.com/i/m/naruto.jpg",
            "https://cdn1.intomanga.com/i/m/naruto.jpg",
        ),
        (
            "K042a85566244b7e836679491ce67ot0",
            "https://img1mw.xyz/manhwas/solo/cover.webp",
            "https://img1mw.xyz/manhwas/solo/cover.webp",
        ),
    ],
)
def test_fmd2_normalizes_public_covers_by_source_origin(
    tmp_path: Path, source_id: str, cover_url: str, expected: str | None
) -> None:
    connector = _connector(tmp_path, httpx.MockTransport(lambda _request: httpx.Response(404)))
    metadata = connector.normalize_metadata(
        {"source_id": source_id, "title": "Fixture", "cover_url": cover_url}
    )
    assert metadata.cover_url == expected
    asyncio.run(connector.close())


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0", "completed"),
        ("1", "ongoing"),
        ("2", "hiatus"),
        ("3", "cancelled"),
        ("unknown", "unknown"),
    ],
)
def test_fmd2_normalizes_publication_status(tmp_path: Path, raw: str, expected: str) -> None:
    connector = _connector(tmp_path, httpx.MockTransport(lambda _request: httpx.Response(404)))
    assert connector.normalize_metadata({"title": "Fixture", "status": raw}).status == expected
    asyncio.run(connector.close())


def test_fmd2_search_restarts_provider_after_process_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "fmd.exe"
    executable.touch()

    class FakeProcess:
        def __init__(self, poll_results: list[int | None]) -> None:
            self.pid = 1
            self._poll_results = poll_results
            self.returncode: int | None = None

        def poll(self) -> int | None:
            if self._poll_results:
                self.returncode = self._poll_results.pop(0)
            return self.returncode

        def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = 0

    spawned: list[FakeProcess] = []

    def spawn(*_args: object, **_kwargs: object) -> FakeProcess:
        process = FakeProcess([None, 1] if not spawned else [None])
        spawned.append(process)
        return process

    monkeypatch.setattr("app.fmd2_bridge.subprocess.Popen", spawn)
    closed_jobs: list[int] = []

    class FakeJob:
        def __init__(self, pid: int) -> None:
            assert pid == 1

        def close(self) -> None:
            closed_jobs.append(1)

    monkeypatch.setattr("app.fmd2_bridge.WindowsChildJob", FakeJob)
    original_async_client = httpx.AsyncClient

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sources":
            return httpx.Response(
                200,
                json={
                    "api_version": "v1",
                    "source_policy": "public-allowlist-v1",
                    "data": [
                        {
                            "source_id": source_id,
                            "name": name,
                            "base_url": origin,
                            "access": "public",
                            "catalog_ready": True,
                        }
                        for source_id, (name, origin, _language) in FMD2_PUBLIC_SOURCES.items()
                    ],
                },
            )
        if request.url.path == "/v1/search":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/v1/shutdown":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    def mock_client(
        *,
        base_url: str,
        headers: dict[str, str],
        timeout: httpx.Timeout,
        follow_redirects: bool,
        trust_env: bool,
        limits: httpx.Limits,
    ) -> httpx.AsyncClient:
        return original_async_client(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=follow_redirects,
            trust_env=trust_env,
            limits=limits,
            transport=httpx.MockTransport(handle),
        )

    monkeypatch.setattr("app.fmd2_bridge.httpx.AsyncClient", mock_client)

    async def exercise() -> None:
        connector = Fmd2Connector(executable, tmp_path / "staging")
        await connector.start()
        try:
            results = await connector.search(ConnectorSearchQuery("Dragon Ball"))
            assert results == ()
            assert len(spawned) == 2
        finally:
            await connector.close()
        if os.name == "nt":
            assert len(closed_jobs) == 2

    asyncio.run(exercise())


def test_fmd2_cancellation_reaches_provider(tmp_path: Path) -> None:
    job_id = uuid4()
    chapter_ref = _chapter_reference()
    saw_running_status = asyncio.Event()
    deleted: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/downloads":
            return httpx.Response(202, json={"job_id": str(job_id), "status": "queued"})
        if request.url.path == f"/v1/downloads/{job_id}" and request.method == "GET":
            saw_running_status.set()
            return httpx.Response(200, json={"job_id": str(job_id), "status": "running"})
        if request.url.path == f"/v1/downloads/{job_id}" and request.method == "DELETE":
            deleted.append(request.method)
            return httpx.Response(
                200,
                json={"job_id": str(job_id), "status": "cancellation_requested"},
            )
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    async def exercise() -> None:
        connector = _connector(tmp_path, httpx.MockTransport(handle))
        acquisition = asyncio.create_task(connector.acquire_pages(job_id, chapter_ref, "Episode 1"))
        await asyncio.wait_for(saw_running_status.wait(), timeout=2)
        acquisition.cancel()
        with pytest.raises(asyncio.CancelledError):
            await acquisition
        assert deleted == ["DELETE"]
        await connector.close()

    asyncio.run(exercise())


def test_fmd2_executable_setting_requires_an_absolute_path(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fmd.exe"
    configured = load_settings(
        {
            "YOMIRA_FMD2_EXECUTABLE": str(executable),
        }
    )
    assert configured.fmd2_executable == executable
    with pytest.raises(SettingsValidationError):
        load_settings(
            {
                "YOMIRA_FMD2_EXECUTABLE": "fmd.exe",
            }
        )


@pytest.mark.parametrize("source_id", list(FMD2_PUBLIC_SOURCES))
def test_compact_chapters_roundtrip_without_reencoding_source_refs(
    tmp_path: Path, source_id: str
) -> None:
    manga_url = "/en/action/1-second/list?title_no=4322"
    chapter_url = (
        "/en/action/1-second/ep-2-cpr-cardiopulmonary-resuscitation/"
        "viewer?title_no=4322&episode_no=3"
    )
    manga_ref = "fmd2:v1:" + (source_id + "\n" + manga_url).encode().hex()
    chapter_ref = "fmd2:v1:" + (source_id + "\n" + chapter_url).encode().hex()
    job_id = uuid4()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chapters"):
            return httpx.Response(
                200,
                json={"chapters": [{"chapter_ref": chapter_ref, "title": "Ep. 2", "ordinal": 3}]},
            )
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["manga_ref"] == manga_ref and body["chapter_ref"] == chapter_ref
            return httpx.Response(202, json={"job_id": str(job_id)})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "files": ["001.png"],
                "completed_pages": 1,
                "total_pages": 1,
            },
        )

    async def exercise() -> None:
        connector = _connector(tmp_path, httpx.MockTransport(respond))
        try:
            chapter = (await connector.get_chapters(manga_ref))[0]
            assert chapter.remote_id and chapter.remote_id.startswith("yomira-fmd2:v3:")
            assert len(chapter.remote_id) <= 512
            assert (await connector.get_chapter(chapter.remote_id)).chapter_major == 3
            assert len(await connector.acquire_pages(job_id, chapter.remote_id, "Ep. 2")) == 1
            legacy = "yomira-fmd2:v1:" + base64.urlsafe_b64encode(
                json.dumps([manga_ref, chapter_ref, 3]).encode()
            ).decode().rstrip("=")
            assert (await connector.get_chapter(legacy)).chapter_major == 3
        finally:
            await connector.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("completed", [True, False])
def test_raw_staging_is_counted_and_only_completed_jobs_release_it(
    tmp_path: Path, completed: bool
) -> None:
    job_id = uuid4()
    directory = tmp_path / "staging" / str(job_id)
    directory.mkdir(parents=True)
    page = directory / "001.png"
    page.write_bytes(b"raw-page")
    connector = _connector(
        tmp_path,
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"job_id": str(job_id), "status": "completed" if completed else "running"}
            )
        ),
    )
    try:
        assert connector.existing_stage_bytes(job_id) == 8
        if completed:
            asyncio.run(connector.finalize_download(job_id))
            assert not directory.exists()
        else:
            with pytest.raises(ValueError):
                asyncio.run(connector.finalize_download(job_id))
            assert page.read_bytes() == b"raw-page"
    finally:
        asyncio.run(connector.close())


def test_stalled_acquisition_requests_cancellation_and_remains_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    deleted: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"job_id": str(job_id), "status": "running"})

    monkeypatch.setattr("app.fmd2_bridge._STALLED_ACQUISITION_SECONDS", 0)
    connector = _connector(tmp_path, httpx.MockTransport(respond))
    try:
        with pytest.raises(SourceConnectorError) as error:
            asyncio.run(connector.acquire_pages(job_id, _chapter_reference(), "Episode 1"))
        assert error.value.retryable
        assert deleted == [f"/v1/downloads/{job_id}"]
    finally:
        asyncio.run(connector.close())


@pytest.mark.parametrize("entry_kind", ["unexpected_file", "directory", "over_budget"])
def test_raw_staging_rejects_unexpected_entries_and_excess_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_kind: str
) -> None:
    job_id = uuid4()
    directory = tmp_path / "staging" / str(job_id)
    directory.mkdir(parents=True)
    page = directory / "001.png"
    page.write_bytes(b"raw-page")
    if entry_kind == "unexpected_file":
        (directory / "notes.txt").write_text("preserve", encoding="utf-8")
    elif entry_kind == "directory":
        (directory / "nested").mkdir()
    else:
        monkeypatch.setattr("app.fmd2_bridge._MAX_STAGE_BYTES", 7)
    connector = _connector(tmp_path, httpx.MockTransport(lambda _request: httpx.Response(200)))
    try:
        with pytest.raises(ValueError):
            connector.existing_stage_bytes(job_id)
        assert page.read_bytes() == b"raw-page"
    finally:
        asyncio.run(connector.close())


@pytest.mark.parametrize("ready", [True, False])
@pytest.mark.parametrize("source_count", [len(FMD2_PUBLIC_SOURCES) - 1, len(FMD2_PUBLIC_SOURCES)])
def test_health_reports_missing_or_unavailable_source_catalogs(
    tmp_path: Path, ready: bool, source_count: int
) -> None:
    data = [
        {
            "source_id": source_id,
            "name": name,
            "base_url": origin,
            "access": "public",
            "catalog_ready": ready,
        }
        for source_id, (name, origin, _language) in list(FMD2_PUBLIC_SOURCES.items())[:source_count]
    ]
    connector = _connector(
        tmp_path,
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"api_version": "v1", "source_policy": "public-allowlist-v1", "data": data},
            )
        ),
    )
    try:
        expected = (
            SourceHealthStatus.HEALTHY
            if ready and source_count == len(FMD2_PUBLIC_SOURCES)
            else SourceHealthStatus.DEGRADED
        )
        assert asyncio.run(connector.health_check()) == expected
        assert asyncio.run(connector.connection_available()) is True
    finally:
        asyncio.run(connector.close())
