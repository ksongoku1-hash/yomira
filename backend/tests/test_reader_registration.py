"""Reader registration shares chapter identity without queuing an acquisition."""

import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from support.cbz import index_test_job
from support.sqlite import open_test_database, reopen_test_database

from app.connectors.errors import SourceConnectorOperation
from app.connectors.metadata import ConnectorCapability
from app.connectors.mock import MockConnector
from app.connectors.registry import ConnectorRegistration, ConnectorRegistry
from app.connectors.sandbox import ConnectorHostContext
from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.factory import create_app
from app.fmd2_bridge import (
    FMD2_METADATA,
    _pack_chapter,  # pyright: ignore[reportPrivateUsage]
    _pack_reference,  # pyright: ignore[reportPrivateUsage]
)
from app.persistence.database import SQLiteDatabase
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.series import SeriesRow
from app.persistence.models.series_source import SeriesSourceRow
from app.persistence.models.source import SourceRow
from app.runtime import RuntimeState
from app.services.jobs import DownloadJobService, InconsistentConnectorSelection
from app.services.reader_progress import ReaderProgressChapterUnavailable, ReaderProgressService
from app.settings import Settings

PREFIX = "/api/v1/connectors/yomira.mock/series/mock-series-1"


def _app(tmp_path: Path) -> tuple[FastAPI, SQLiteDatabase]:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    app = create_app(Settings(app_env="test", data_root=tmp_path))
    runtime = app.state.runtime
    assert isinstance(runtime, RuntimeState)
    runtime.database = database
    runtime.download_job_service = DownloadJobService(database)
    return app, database


async def _post(app: FastAPI, path: str, body: object) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    ) as client:
        return await client.post(path, json=body)


def test_registration_reopens_and_download_uses_the_same_chapter(tmp_path: Path) -> None:
    app, database = _app(tmp_path)
    try:
        body = {"chapter_remote_id": "mock-chapter-1"}
        first = asyncio.run(_post(app, PREFIX + "/reader-chapter", body))
        repeated = asyncio.run(_post(app, PREFIX + "/reader-chapter", body))
        assert first.status_code == repeated.status_code == 200
        assert first.json() == repeated.json()
        data = first.json()["data"]
        assert set(data) == {"series_id", "chapter_id", "display_label"}
        assert UUID(data["chapter_id"]).version == 4
        assert first.headers["x-trace-id"]
        with database.read_session() as session:
            for model in (SeriesRow, SeriesSourceRow, SourceRow, ChapterRow, ChapterSourceRow):
                assert session.scalar(select(func.count()).select_from(model)) == 1
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
        assert not (tmp_path / "temp").exists()
        assert not (tmp_path / "library").exists()

        database.close()
        database = reopen_test_database(database)
        app.state.runtime.download_job_service = DownloadJobService(database)
        reopened = asyncio.run(_post(app, PREFIX + "/reader-chapter", body))
        assert reopened.json() == first.json()
        created = asyncio.run(
            _post(app, PREFIX + "/download-jobs", {"chapter_remote_ids": ["mock-chapter-1"]})
        )
        assert created.status_code == 200
        job = created.json()["data"][0]
        assert job["chapter_id"] == data["chapter_id"]
        assert job["series_id"] == data["series_id"]
        index_test_job(database, UUID(job["job_id"]))
        reader = app.state.runtime.download_job_service._reader  # pyright: ignore[reportPrivateUsage]
        progress = ReaderProgressService(database, reader)
        saved = progress.save(UUID(data["chapter_id"]), 0)
        assert saved.chapter_id == UUID(data["chapter_id"])
        assert progress.get(saved.chapter_id) == saved
        ready = asyncio.run(_post(app, PREFIX + "/reader-chapter", body))
        assert ready.json() == first.json()
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(ChapterRow)) == 1
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 1
    finally:
        database.close()


@pytest.mark.parametrize(
    "body,code",
    [
        ({"chapter_remote_id": "missing"}, 404),
        ({"chapter_remote_id": ""}, 422),
        ({"chapter_remote_id": "x" * 513}, 422),
        ({"chapter_remote_id": 1}, 422),
        ({"chapter_remote_id": "mock-chapter-1", "page_count": 3}, 422),
    ],
)
def test_rejected_registration_writes_nothing(tmp_path: Path, body: object, code: int) -> None:
    app, database = _app(tmp_path)
    try:
        response = asyncio.run(_post(app, PREFIX + "/reader-chapter", body))
        assert response.status_code == code
        assert response.json()["error"]["trace_id"]
        with database.read_session() as session:
            for model in (SourceRow, SeriesRow, ChapterRow, ChapterSourceRow, DownloadJobRow):
                assert session.scalar(select(func.count()).select_from(model)) == 0
    finally:
        database.close()


def test_reading_and_downloading_concurrently_share_one_identity(tmp_path: Path) -> None:
    _unused, database = _app(tmp_path)
    connector = MockConnector()
    chapters = asyncio.run(connector.get_chapters("mock-series-1"))
    metadata = connector.normalize_metadata(asyncio.run(connector.get_series("mock-series-1")))
    service = DownloadJobService(database)

    def register() -> UUID:
        return service.register_reader_chapter(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=metadata,
            chapters=chapters,
            selected_remote_id="mock-chapter-1",
        ).chapter_id

    def download() -> UUID:
        return service.create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=metadata,
            chapters=chapters,
            selected_remote_ids=("mock-chapter-1",),
        )[0].chapter_id

    def request(index: int) -> UUID:
        return register() if index % 2 else download()

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(request, range(12)))
        assert len(set(results)) == 1
        with database.read_session() as session:
            for model in (ChapterRow, ChapterSourceRow, DownloadJobRow):
                assert session.scalar(select(func.count()).select_from(model)) == 1
    finally:
        database.close()


def test_local_chapter_mapping_survives_missing_file_without_losing_identity(
    tmp_path: Path,
) -> None:
    _unused, database = _app(tmp_path)
    connector = MockConnector()
    service = DownloadJobService(database)
    chapter = asyncio.run(connector.get_chapters("mock-series-1"))[0]
    metadata = ConnectorSeriesMetadata(title="Example", language="es")
    try:
        local = service.create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="local-source",
            series_metadata=metadata,
            chapters=(chapter,),
            selected_remote_ids=("mock-chapter-1",),
        )[0]
        index_test_job(database, local.job.job_id)
        other_connector = replace(connector.metadata, connector_id="yomira.other")
        remote = replace(chapter, remote_id="other-chapter")
        registered = service.register_reader_chapter(
            connector=other_connector,
            remote_series_id="remote-source",
            series_metadata=metadata,
            chapters=(remote,),
            selected_remote_id="other-chapter",
        )
        assert registered.chapter_id == local.chapter_id
        path = tmp_path / "library/chapters" / f"{local.chapter_id}.cbz"
        path.rename(path.with_suffix(".preserved"))
        reopened = service.register_reader_chapter(
            connector=other_connector,
            remote_series_id="remote-source",
            series_metadata=metadata,
            chapters=(remote,),
            selected_remote_id="other-chapter",
        )
        assert reopened == registered
        later = service.create_selected_jobs(
            connector=other_connector,
            remote_series_id="remote-source",
            series_metadata=metadata,
            chapters=(remote,),
            selected_remote_ids=("other-chapter",),
        )[0]
        assert later.chapter_id == local.chapter_id
        assert later.job.job_id != local.job.job_id
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(ChapterRow)) == 1
            assert session.scalar(select(func.count()).select_from(ChapterSourceRow)) == 2
    finally:
        database.close()


def test_registration_does_not_make_an_unacquired_chapter_readable(tmp_path: Path) -> None:
    app, database = _app(tmp_path)
    try:
        response = asyncio.run(
            _post(app, PREFIX + "/reader-chapter", {"chapter_remote_id": "mock-chapter-1"})
        )
        assert response.status_code == 200
        chapter_id = UUID(response.json()["data"]["chapter_id"])
        service = app.state.runtime.download_job_service
        progress = ReaderProgressService(database, service._reader)  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(ReaderProgressChapterUnavailable):
            progress.save(chapter_id, 0)
    finally:
        database.close()


def test_registration_preserves_source_lineage_and_rolls_back_other_series(tmp_path: Path) -> None:
    _unused, database = _app(tmp_path)
    service = DownloadJobService(database)
    connector = MockConnector()
    chapters = asyncio.run(connector.get_chapters("mock-series-1"))
    try:
        first = service.register_reader_chapter(
            connector=connector.metadata,
            remote_series_id="first",
            series_metadata=ConnectorSeriesMetadata(title="First"),
            chapters=chapters,
            selected_remote_id="mock-chapter-1",
        )
        with pytest.raises(InconsistentConnectorSelection):
            service.register_reader_chapter(
                connector=connector.metadata,
                remote_series_id="second",
                series_metadata=ConnectorSeriesMetadata(title="Second"),
                chapters=chapters,
                selected_remote_id="mock-chapter-1",
            )
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(SeriesRow)) == 1
            chapter = session.get(ChapterRow, str(first.chapter_id))
            assert chapter is not None and chapter.series_id == str(first.series_id)
    finally:
        database.close()


def test_registration_rejects_naive_time_before_writing(tmp_path: Path) -> None:
    _unused, database = _app(tmp_path)
    connector = MockConnector()
    try:
        with pytest.raises(ValueError, match="timezone-aware"):
            DownloadJobService(database).register_reader_chapter(
                connector=connector.metadata,
                remote_series_id="mock-series-1",
                series_metadata=ConnectorSeriesMetadata(title="Example"),
                chapters=asyncio.run(connector.get_chapters("mock-series-1")),
                selected_remote_id="mock-chapter-1",
                now=datetime(2026, 10, 11),
            )
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(SourceRow)) == 0
    finally:
        database.close()


@pytest.mark.parametrize("version", (1, 2))
def test_reader_registration_keeps_old_fmd_identity_after_reordering(
    tmp_path: Path, version: int
) -> None:
    _unused, database = _app(tmp_path)
    service = DownloadJobService(database)
    template = asyncio.run(MockConnector().get_chapters("mock-series-1"))[0]
    source = "18f636ec7fdf47fabe95d940ad0b548f"
    manga_url, page_url = (
        "/en/action/fixture/list?title_no=1",
        "/en/action/fixture/viewer?episode_no=1",
    )
    manga, page = _pack_reference(manga_url), _pack_reference(page_url)
    payload = [manga, page, 1] if version == 1 else [source, manga_url, page_url, 1]
    old = f"yomira-fmd2:v{version}:" + base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).decode().rstrip("=")
    current = _pack_chapter(manga, page, 100, SourceConnectorOperation.GET_CHAPTERS)
    try:
        first = service.register_reader_chapter(
            connector=FMD2_METADATA,
            remote_series_id=manga,
            series_metadata=ConnectorSeriesMetadata(title="Example"),
            chapters=(replace(template, remote_id=old),),
            selected_remote_id=old,
        )
        repeated = service.register_reader_chapter(
            connector=FMD2_METADATA,
            remote_series_id=manga,
            series_metadata=ConnectorSeriesMetadata(title="Example"),
            chapters=(replace(template, remote_id=current, chapter_sort_key=(100,)),),
            selected_remote_id=current,
        )
        assert repeated == first
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(ChapterRow)) == 1
            assert session.scalar(select(func.count()).select_from(ChapterSourceRow)) == 1
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
    finally:
        database.close()


def test_registration_never_acquires_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, database = _app(tmp_path)

    async def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Registration must not acquire pages")

    monkeypatch.setattr(MockConnector, "acquire_pages", forbidden)
    monkeypatch.setattr(MockConnector, "get_pages", forbidden)
    try:
        response = asyncio.run(
            _post(app, PREFIX + "/reader-chapter", {"chapter_remote_id": "mock-chapter-1"})
        )
        assert response.status_code == 200
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
    finally:
        database.close()


@pytest.mark.parametrize("connector_id,code", [("missing", 404), ("fixture.metadata", 501)])
def test_reader_registration_requires_registered_page_capability(
    tmp_path: Path, connector_id: str, code: int
) -> None:
    app, database = _app(tmp_path)

    class MetadataOnly(MockConnector):
        @property
        def metadata(self):
            return replace(
                super().metadata,
                connector_id="fixture.metadata",
                capabilities=frozenset({ConnectorCapability.METADATA}),
            )

    runtime = app.state.runtime
    runtime.connector_registry = ConnectorRegistry(
        context=ConnectorHostContext(secret_vault=runtime.secret_vault),
        registrations=(
            ConnectorRegistration(MetadataOnly().metadata, lambda _ctx: MetadataOnly()),
        ),
    )
    try:
        response = asyncio.run(
            _post(
                app,
                f"/api/v1/connectors/{connector_id}/series/mock-series-1/reader-chapter",
                {"chapter_remote_id": "mock-chapter-1"},
            )
        )
        assert response.status_code == code
        with database.read_session() as session:
            for model in (SeriesRow, ChapterRow, DownloadJobRow):
                assert session.scalar(select(func.count()).select_from(model)) == 0
    finally:
        database.close()


def test_ready_chapter_cannot_override_an_existing_source_identity(tmp_path: Path) -> None:
    _unused, database = _app(tmp_path)
    connector = MockConnector()
    service = DownloadJobService(database)
    chapter = asyncio.run(connector.get_chapters("mock-series-1"))[0]
    metadata = ConnectorSeriesMetadata(title="Example", language="es")
    try:
        registered = service.register_reader_chapter(
            connector=connector.metadata,
            remote_series_id="original-source",
            series_metadata=metadata,
            chapters=(chapter,),
            selected_remote_id="mock-chapter-1",
        )
        other_chapter = replace(chapter, remote_id="another-chapter")
        alternate = service.create_selected_jobs(
            connector=replace(connector.metadata, connector_id="yomira.other"),
            remote_series_id="other-source",
            series_metadata=metadata,
            chapters=(other_chapter,),
            selected_remote_ids=("another-chapter",),
        )[0]
        assert alternate.chapter_id != registered.chapter_id
        index_test_job(database, alternate.job.job_id)
        reopened = service.register_reader_chapter(
            connector=connector.metadata,
            remote_series_id="original-source",
            series_metadata=metadata,
            chapters=(chapter,),
            selected_remote_id="mock-chapter-1",
        )
        assert reopened == registered
    finally:
        database.close()


def test_ready_number_match_does_not_bypass_series_lineage(tmp_path: Path) -> None:
    _unused, database = _app(tmp_path)
    connector = MockConnector()
    service = DownloadJobService(database)
    chapter = asyncio.run(connector.get_chapters("mock-series-1"))[0]
    try:
        service.register_reader_chapter(
            connector=connector.metadata,
            remote_series_id="first",
            series_metadata=ConnectorSeriesMetadata(title="First"),
            chapters=(chapter,),
            selected_remote_id="mock-chapter-1",
        )
        alternate = service.create_selected_jobs(
            connector=replace(connector.metadata, connector_id="yomira.other"),
            remote_series_id="second",
            series_metadata=ConnectorSeriesMetadata(title="Second"),
            chapters=(replace(chapter, remote_id="other-chapter"),),
            selected_remote_ids=("other-chapter",),
        )[0]
        index_test_job(database, alternate.job.job_id)
        with pytest.raises(InconsistentConnectorSelection):
            service.register_reader_chapter(
                connector=connector.metadata,
                remote_series_id="invalid-alias",
                series_metadata=ConnectorSeriesMetadata(title="Second"),
                chapters=(chapter,),
                selected_remote_id="mock-chapter-1",
            )
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(SeriesSourceRow)) == 2
            assert session.scalar(select(func.count()).select_from(ChapterSourceRow)) == 2
    finally:
        database.close()


@pytest.mark.parametrize("case", ("unique", "language", "manga", "ambiguous"))
def test_novelcool_fresh_reference_preserves_only_an_exact_unique_legacy_identity(
    tmp_path: Path, case: str
) -> None:
    _unused, database = _app(tmp_path)
    service = DownloadJobService(database)
    source = "424747b0135c4240a902eb9cfcb11db9"
    manga = _pack_reference("/manga/fixture.html", source)
    other = _pack_reference("/manga/other.html", source)
    operation = SourceConnectorOperation.GET_CHAPTERS
    old = _pack_chapter(manga, _pack_reference("/go/rds/?data=expired", source), 16, operation)
    fresh = _pack_chapter(
        other if case == "manga" else manga, _pack_reference("/2199440239", source), 16, operation
    )
    template = asyncio.run(MockConnector().get_chapters("mock-series-1"))[0]
    old_chapter = replace(template, remote_id=old, display_label="Capítulo 16", language="es")
    current = replace(old_chapter, remote_id=fresh, language="en" if case == "language" else "es")
    metadata = ConnectorSeriesMetadata(title="Example", language="es")
    try:
        first = service.register_reader_chapter(
            connector=FMD2_METADATA,
            remote_series_id=manga,
            series_metadata=metadata,
            chapters=(old_chapter,),
            selected_remote_id=old,
        )
        catalog = (
            (
                current,
                replace(
                    current,
                    remote_id=_pack_chapter(
                        manga, _pack_reference("/different", source), 16, operation
                    ),
                ),
            )
            if case == "ambiguous"
            else (current,)
        )
        repeated = service.register_reader_chapter(
            connector=FMD2_METADATA,
            remote_series_id=other if case == "manga" else manga,
            series_metadata=metadata,
            chapters=catalog,
            selected_remote_id=fresh,
        )
        assert (repeated.chapter_id == first.chapter_id) is (case == "unique")
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
            original = session.scalar(
                select(ChapterSourceRow).where(ChapterSourceRow.remote_id == old)
            )
            assert original is not None and original.chapter_id == str(first.chapter_id)
    finally:
        database.close()
