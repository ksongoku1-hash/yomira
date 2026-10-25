import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from support.cbz import index_test_job
from support.sqlite import sqlite_test_context

from app.connectors.chapter import ConnectorChapter
from app.connectors.mock import MockConnector
from app.domain.download_job import DownloadJobStatus, DownloadPriority
from app.domain.job_pause import JobPauseController, JobPauseState
from app.factory import create_app
from app.persistence.database import SQLiteDatabase
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.download_page import DownloadPageRow
from app.persistence.models.series import SeriesRow
from app.persistence.models.source import SourceRow
from app.persistence.repositories.download_job import DownloadJobRepository
from app.runtime import RuntimeState
from app.services.jobs import DownloadJobService, DownloadJobSummary
from app.services.reader_asset import (
    V1_MAX_CBZ_BYTES,
    V1_MAX_PAGE_BYTES,
    V1_STREAM_CHUNK_BYTES,
    ReaderAssetLimits,
    ReaderAssetService,
)
from app.settings import Settings


def _ready_app(data_root: Path) -> tuple[FastAPI, SQLiteDatabase]:
    app = create_app(Settings(app_env="test", data_root=data_root))
    database = sqlite_test_context(data_root).open()
    MigrationRunner(database, data_root / "backups").upgrade_to_head()
    runtime = app.state.runtime
    assert isinstance(runtime, RuntimeState)
    runtime.database = database
    runtime.download_job_service = DownloadJobService(database)
    return app, database


def _remote_ids(chapters: Sequence[ConnectorChapter]) -> tuple[str, ...]:
    ids: list[str] = []
    for chapter in chapters:
        assert chapter.remote_id is not None
        ids.append(chapter.remote_id)
    return tuple(ids)


def test_download_filters_apply_before_paging_and_preserve_whole_queue_counts(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    connector = MockConnector()
    template = asyncio.run(connector.get_chapters("mock-series-1"))[0]
    chapters = tuple(
        replace(template, remote_id=f"filter-{index}", chapter_sort_key=(index,))
        for index in range(58)
    )
    service = DownloadJobService(database)
    try:
        created = service.create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=connector.normalize_metadata(
                asyncio.run(connector.get_series("mock-series-1"))
            ),
            chapters=chapters,
            selected_remote_ids=tuple(f"filter-{index}" for index in range(58)),
        )
        statuses = ["COMPLETED"] * 52 + ["FAILED"] * 3 + ["INTERRUPTED", "DOWNLOADING", "VERIFYING"]
        with database.write_session() as session:
            for item, status in zip(created, statuses, strict=True):
                row = session.get(DownloadJobRow, str(item.job.job_id))
                assert row is not None
                row.status = status
        path = "/api/v1/download-jobs?status=FAILED&limit=1"
        first = asyncio.run(_get(app, path)).json()
        second = asyncio.run(_get(app, path + "&offset=1")).json()
        assert first["meta"]["total"] == 58
        assert first["meta"]["filtered_total"] == 3
        assert sum(first["meta"]["states"].values()) == 58
        assert first["meta"]["states"]["COMPLETED"] == 52
        assert first["data"][0]["status"] == second["data"][0]["status"] == "FAILED"
        assert first["data"][0]["job_id"] != second["data"][0]["job_id"]
        active = asyncio.run(
            _get(app, "/api/v1/download-jobs?status=DOWNLOADING&status=VERIFYING")
        ).json()
        assert active["meta"]["filtered_total"] == 2
        assert {item["status"] for item in active["data"]} == {"DOWNLOADING", "VERIFYING"}
        assert (
            "filtered_total" not in asyncio.run(_get(app, "/api/v1/download-jobs")).json()["meta"]
        )
        archived = asyncio.run(
            _post_without_body(app, f"/api/v1/download-jobs/{first['data'][0]['job_id']}/archive")
        )
        assert archived.status_code == 204
        history = asyncio.run(_get(app, "/api/v1/download-jobs?archived=true&status=FAILED")).json()
        assert history["meta"]["total"] == history["meta"]["filtered_total"] == 1
        assert asyncio.run(_get(app, path)).json()["meta"]["filtered_total"] == 2
        assert asyncio.run(_get(app, "/api/v1/download-jobs?status=unknown")).status_code == 422
        too_many = "/api/v1/download-jobs?" + "&".join(["status=FAILED"] * 13)
        assert asyncio.run(_get(app, too_many)).status_code == 422
    finally:
        database.close()


async def _post(
    app: FastAPI,
    path: str,
    body: object,
) -> Response:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path, json=body)


async def _get(app: FastAPI, path: str) -> Response:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


async def _post_without_body(app: FastAPI, path: str) -> Response:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path)


def test_group_pause_persists_and_other_manga_dispatches_without_recreating_jobs(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    connector = MockConnector()
    template = asyncio.run(connector.get_chapters("mock-series-1"))[0]
    chapters = (template, replace(template, remote_id="chapter-2", chapter_sort_key=(2,)))
    metadata = connector.normalize_metadata(asyncio.run(connector.get_series("mock-series-1")))
    service = DownloadJobService(database)
    try:
        first = service.create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=metadata,
            chapters=chapters,
            selected_remote_ids=_remote_ids(chapters),
        )
        second = service.create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="other-series",
            series_metadata=replace(metadata, title="Other manga"),
            chapters=(replace(template, remote_id="other-chapter"),),
            selected_remote_ids=("other-chapter",),
        )
        series_id = first[0].series_id
        pause_path = f"/api/v1/download-jobs/groups/{series_id}/pause"

        async def put(body: object) -> Response:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                return await client.put(pause_path, json=body)

        assert asyncio.run(put({"paused": "true"})).status_code == 422
        for _ in range(2):
            assert asyncio.run(put({"paused": True})).status_code == 204
        restarted = DownloadJobService(database)
        groups = asyncio.run(_get(app, "/api/v1/download-jobs/groups?limit=1&offset=0"))
        assert groups.status_code == 200
        assert groups.json()["meta"] == {"limit": 1, "offset": 0, "total": 2}
        all_groups = asyncio.run(_get(app, "/api/v1/download-jobs/groups")).json()["data"]
        group = next(row for row in all_groups if row["series_id"] == str(series_id))
        assert group["paused"] is True
        assert group["queued"] == len(first)
        with database.read_session() as session:
            for sequential in (False, True):
                window = DownloadJobRepository().list_queued_for_dispatch(
                    session,
                    limit=8,
                    sequential=sequential,
                    preferred_series_id=series_id,
                )
                assert [row.job.job_id for row in window.candidates] == [second[0].job.job_id]
        # An already admitted chapter keeps running; only the queued chapters are paused.
        with database.write_session() as session:
            row = session.get(DownloadJobRow, str(first[0].job.job_id))
            assert row is not None
            row.status = "DOWNLOADING"
        group = next(
            row for row in restarted.list_groups()["data"] if row["series_id"] == str(series_id)
        )
        assert group["active"] == 1 and group["queued"] == len(first) - 1
        assert asyncio.run(put({"paused": False})).status_code == 204
        with database.read_session() as session:
            window = DownloadJobRepository().list_queued_for_dispatch(session, limit=8)
            assert {row.job.job_id for row in window.candidates} == {
                row.job.job_id for row in (*first[1:], *second)
            }
            assert (
                session.scalar(select(func.count()).select_from(DownloadJobRow)) == len(first) + 1
            )
        assert (
            restarted.set_group_paused(UUID("00000000-0000-4000-8000-000000000000"), True) is False
        )
    finally:
        database.close()


def test_reader_descending_order_applies_before_pagination(tmp_path: Path) -> None:
    app, database = _ready_app(tmp_path)
    try:
        connector = MockConnector()
        template = asyncio.run(connector.get_chapters("mock-series-1"))[0]
        chapters = (
            replace(template, display_label="Capítulo 1"),
            replace(
                template,
                remote_id="chapter-2",
                chapter_sort_key=(2,),
                display_label="Capítulo 2 · EDICIÓN 100%_extra",
            ),
        )
        jobs = DownloadJobService(database).create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=connector.normalize_metadata(
                asyncio.run(connector.get_series("mock-series-1"))
            ),
            chapters=chapters,
            selected_remote_ids=_remote_ids(chapters),
        )
        for job in jobs:
            index_test_job(database, job.job.job_id)
        app.state.runtime.reader_asset_service = ReaderAssetService(
            database,
            data_root_identity=database.data_root_identity,
            limits=ReaderAssetLimits(
                max_cbz_bytes=V1_MAX_CBZ_BYTES,
                max_page_bytes=V1_MAX_PAGE_BYTES,
                chunk_bytes=V1_STREAM_CHUNK_BYTES,
            ),
        )
        path = f"/api/v1/reader/series/{jobs[0].series_id}/chapters?limit=1"
        ascending = asyncio.run(_get(app, path)).json()
        descending = asyncio.run(_get(app, path + "&order=desc")).json()
        next_page = asyncio.run(_get(app, path + "&order=desc&offset=1")).json()
        assert ascending["meta"]["total"] == descending["meta"]["total"] == 2
        assert descending["data"][0]["chapter_id"] != ascending["data"][0]["chapter_id"]
        assert next_page["data"] == ascending["data"]
        assert asyncio.run(_get(app, path + "&order=invalid")).status_code == 422
        for search in ("edición", "%_", "  100%_  "):
            result = asyncio.run(_get(app, path + "&q=" + quote(search)))
            assert result.status_code == 200
            assert result.json()["meta"]["total"] == 1
            assert result.json()["data"] == descending["data"]
        assert (
            asyncio.run(_get(app, path + "&q=CAPÍTULO&offset=1")).json()["data"]
            == descending["data"]
        )
        empty = asyncio.run(_get(app, path + "&q=not-found")).json()
        assert empty["data"] == [] and empty["meta"]["total"] == 0
        assert asyncio.run(_get(app, path + "&q=" + "a" * 201)).status_code == 422
    finally:
        database.close()


@pytest.mark.parametrize("action", ["library-series", "download-jobs"])
def test_ambiguous_library_entry_returns_conflict_without_writing(
    tmp_path: Path, action: str
) -> None:
    from app.domain.series import Series
    from app.persistence.mappers.series import series_to_row
    from app.persistence.models.series_source import SeriesSourceRow

    app, database = _ready_app(tmp_path)
    try:
        with database.write_session() as session:
            for year in (2000, 2020):
                title = f"Mock Fixture Series ({year})"
                session.add(series_to_row(Series.new_local(title, title)))
        path = f"/api/v1/connectors/yomira.mock/series/mock-series-1/{action}"
        response = asyncio.run(
            _post(app, path, {"chapter_remote_ids": ["mock-chapter-1"]})
            if action == "download-jobs"
            else _post_without_body(app, path)
        )
        assert response.status_code == 409
        assert "Hay varias fichas compatibles" in response.json()["error"]["message"]
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(SeriesRow)) == 2
            assert session.scalar(select(func.count()).select_from(SeriesSourceRow)) == 0
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
    finally:
        database.close()


def test_batch_downloads_dispatch_and_display_in_numeric_chapter_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, database = _ready_app(tmp_path)
    connector = MockConnector()
    template = asyncio.run(connector.get_chapters("mock-series-1"))[0]
    keys = ((10,), (2,), (1,), (2, 5), (2, 5, 1))
    chapters = tuple(
        replace(
            template,
            remote_id=f"chapter-{index}",
            chapter_sort_key=key,
            display_label=f"Capítulo {'.'.join(str(part) for part in key)}",
        )
        for index, key in enumerate(keys)
    )
    # Same-time batch with IDs deliberately ordered differently from chapter numbers.
    ids = iter(UUID(f"00000000-0000-4000-8000-{index:012d}") for index in range(1, 7))
    monkeypatch.setattr("app.domain.download_job.uuid4", lambda: next(ids))
    service = DownloadJobService(database)
    metadata = connector.normalize_metadata(asyncio.run(connector.get_series("mock-series-1")))
    observed_at = datetime.fromisoformat("2026-10-07T12:00:00+00:00")
    try:
        summaries = service.create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=metadata,
            chapters=chapters,
            selected_remote_ids=_remote_ids(chapters),
            now=observed_at,
        )
        expected = tuple(summaries[index].job.job_id for index in (2, 1, 3, 4, 0))
        with database.read_session() as session:
            dispatched = DownloadJobRepository().list_queued_for_dispatch(session, limit=8)
        assert tuple(item.job.job_id for item in dispatched.candidates) == expected
        assert tuple(item.job.job_id for item in service.list_jobs(limit=2).items) == expected[:2]
        assert (
            tuple(item.job.job_id for item in service.list_jobs(limit=3, offset=2).items)
            == expected[2:]
        )

        later = replace(template, remote_id="later-chapter", chapter_sort_key=(0,))
        new_job = service.create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=metadata,
            chapters=(later,),
            selected_remote_ids=_remote_ids((later,)),
            now=observed_at + timedelta(seconds=1),
        )[0].job.job_id
        with database.read_session() as session:
            dispatched = DownloadJobRepository().list_queued_for_dispatch(session, limit=8)
        assert tuple(item.job.job_id for item in dispatched.candidates) == (*expected, new_job)
        assert service.list_jobs(limit=1).items[0].job.job_id == new_job
    finally:
        database.close()


def test_sequential_queue_finishes_current_series_across_sources_and_batches(
    tmp_path: Path,
) -> None:
    _app, database = _ready_app(tmp_path)
    connector = MockConnector()
    template = asyncio.run(connector.get_chapters("mock-series-1"))[0]
    metadata = connector.normalize_metadata(asyncio.run(connector.get_series("mock-series-1")))
    service = DownloadJobService(database)
    repository = DownloadJobRepository()
    observed_at = datetime.fromisoformat("2026-10-08T12:00:00+00:00")
    try:
        first_chapter = replace(template, remote_id="chapter-10", chapter_sort_key=(10,))
        first = service.create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="series-a",
            series_metadata=metadata,
            chapters=(first_chapter,),
            selected_remote_ids=_remote_ids((first_chapter,)),
            now=observed_at,
        )[0]
        second = service.create_selected_jobs(
            connector=replace(connector.metadata, connector_id="yomira.second-source"),
            remote_series_id="series-b",
            series_metadata=replace(metadata, title="Another manga"),
            chapters=(template,),
            selected_remote_ids=_remote_ids((template,)),
            now=observed_at + timedelta(seconds=1),
        )[0]
        lower_chapters = tuple(
            replace(template, remote_id=f"chapter-{index}", chapter_sort_key=key)
            for index, key in enumerate(((2, 5), (2,)))
        )
        decimal, lower = service.create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="series-a",
            series_metadata=metadata,
            chapters=lower_chapters,
            selected_remote_ids=_remote_ids(lower_chapters),
            now=observed_at + timedelta(seconds=2),
        )
        expected = (lower, decimal, first, second)
        assert [item.job.job_id for item in service.list_jobs().items] == [
            item.job.job_id for item in expected
        ]
        current_series = None
        for index, wanted in enumerate(expected):
            # A priority added to another manga must not interrupt the current one.
            if index == 1:
                with database.write_session() as session:
                    row = session.get(DownloadJobRow, str(second.job.job_id))
                    assert row is not None
                    row.priority = DownloadPriority.HIGH.value
            with database.read_session() as session:
                window = repository.list_queued_for_dispatch(
                    session,
                    limit=8,
                    sequential=True,
                    preferred_series_id=current_series,
                )
            assert len(window.candidates) == 1
            candidate = window.candidates[0]
            assert candidate.job.job_id == wanted.job.job_id
            current_series = candidate.series_id
            with database.write_session() as session:
                row = session.get(DownloadJobRow, str(candidate.job.job_id))
                assert row is not None
                row.status = "DOWNLOADING"
            visible = service.list_jobs().items
            assert visible[0].job.job_id == wanted.job.job_id
            if index == 1:
                assert visible[1].job.job_id == first.job.job_id
            with database.write_session() as session:
                row = session.get(DownloadJobRow, str(candidate.job.job_id))
                assert row is not None
                row.status = "COMPLETED"
        with database.read_session() as session:
            assert not repository.list_queued_for_dispatch(
                session, limit=1, sequential=True
            ).candidates
    finally:
        database.close()


def test_queue_counts_all_pages_and_only_verified_assets_are_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, database = _ready_app(tmp_path)
    template = asyncio.run(MockConnector().get_chapters("mock-series-1"))[0]

    async def two_chapters(
        self: MockConnector, remote_series_id: str
    ) -> tuple[ConnectorChapter, ...]:
        return template, replace(template, remote_id="mock-chapter-2", chapter_sort_key=(2,))

    monkeypatch.setattr(MockConnector, "get_chapters", two_chapters)
    try:
        response = asyncio.run(
            _post(
                app,
                "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs",
                {"chapter_remote_ids": ["mock-chapter-1", "mock-chapter-2"]},
            )
        )
        assert response.status_code == 200
        first, second = response.json()["data"]
        with database.write_session() as session:
            first_row = session.get(DownloadJobRow, first["job_id"])
            second_row = session.get(DownloadJobRow, second["job_id"])
            assert first_row is not None
            assert second_row is not None
            first_row.status = "DOWNLOADING"
            second_row.status = "COMPLETED"
            session.add_all(
                [
                    DownloadPageRow(
                        job_id=first["job_id"],
                        ordinal=ordinal,
                        remote_reference=f"page-{ordinal}",
                        status=status,
                        attempts=0,
                    )
                    for ordinal, status in ((1, "VALIDATED"), (2, "PENDING"), (3, "PENDING"))
                ]
            )
        page = asyncio.run(_get(app, "/api/v1/download-jobs?limit=1"))
        assert page.status_code == 200
        meta = page.json()["meta"]
        assert meta["total"] == 2
        assert meta["states"]["DOWNLOADING"] == meta["states"]["COMPLETED"] == 1
        assert sum(meta["states"].values()) == 2
        progress = DownloadJobService(database).list_jobs().page_progress
        assert progress == ({"job_id": first["job_id"], "known_pages": 3, "validated_pages": 1},)
        available = asyncio.run(
            _get(
                app,
                "/api/v1/connectors/yomira.mock/series/mock-series-1/availability",
            )
        )
        assert available.status_code == 200
        assert available.json()["data"] == {
            "linked": True,
            "ready_remote_ids": [],
            "ready_labels": [],
            "busy_remote_ids": ["mock-chapter-1"],
        }
        assert (
            DownloadJobService(database).chapter_availability(
                "yomira.mock",
                "another-edition",
            )["linked"]
            is False
        )
        archived = asyncio.run(
            _post_without_body(
                app,
                f"/api/v1/download-jobs/{second['job_id']}/archive",
            )
        )
        assert archived.status_code == 204
        remaining = DownloadJobService(database).list_jobs(limit=1)
        assert remaining.total == 1 and remaining.states["COMPLETED"] == 0
    finally:
        database.close()


def test_blocked_interrupted_job_retries_from_zero_preserving_its_evidence(tmp_path: Path) -> None:
    app, database = _ready_app(tmp_path)
    try:
        original = asyncio.run(
            _post(
                app,
                "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs",
                {"chapter_remote_ids": ["mock-chapter-1"]},
            )
        ).json()["data"][0]
        with database.write_session() as session:
            job = session.get(DownloadJobRow, original["job_id"])
            assert job is not None
            job.status = "INTERRUPTED"
            job.interrupted_from_status = "VALIDATING"
            job.recovery_blocked = True
            session.add(
                DownloadPageRow(
                    job_id=original["job_id"],
                    ordinal=1,
                    remote_reference="old-page",
                    status="VALIDATED",
                    attempts=1,
                )
            )
        assert (
            DownloadJobService(database).chapter_availability(
                "yomira.mock",
                "mock-series-1",
            )["busy_remote_ids"]
            == []
        )
        retried = asyncio.run(
            _post_without_body(
                app,
                f"/api/v1/downloads/{original['job_id']}/retry",
            )
        )
        assert retried.status_code == 200
        new = retried.json()["data"]
        assert new["job_id"] != original["job_id"] and new["status"] == "QUEUED"
        repeated = asyncio.run(
            _post_without_body(
                app,
                f"/api/v1/downloads/{original['job_id']}/retry",
            )
        )
        assert repeated.json()["data"]["job_id"] == new["job_id"]
        with database.read_session() as session:
            original_job = session.get(DownloadJobRow, original["job_id"])
            original_page = session.get(DownloadPageRow, (original["job_id"], 1))
            assert original_job is not None
            assert original_page is not None
            assert original_job.status == "INTERRUPTED"
            assert original_page.remote_reference == "old-page"
            assert session.get(DownloadPageRow, (new["job_id"], 1)) is None
        assert (
            asyncio.run(
                _post_without_body(
                    app,
                    f"/api/v1/download-jobs/{original['job_id']}/archive",
                )
            ).status_code
            == 204
        )
    finally:
        database.close()


def test_queue_remains_responsive_while_selected_jobs_are_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, database = _ready_app(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original = DownloadJobService.create_selected_jobs

    def slow_create(service: DownloadJobService, **kwargs: Any) -> tuple[DownloadJobSummary, ...]:
        entered.set()
        release.wait(timeout=3)
        return original(service, **kwargs)

    monkeypatch.setattr(DownloadJobService, "create_selected_jobs", slow_create)

    async def exercise() -> None:
        creation = asyncio.create_task(
            _post(
                app,
                "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs",
                {"chapter_remote_ids": ["mock-chapter-1"]},
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            queue = await asyncio.wait_for(_get(app, "/api/v1/download-jobs"), timeout=1)
            assert queue.status_code == 200
            assert not creation.done()
        finally:
            release.set()
            response = await creation
        assert response.status_code == 200

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        database.close()


def test_terminal_job_can_leave_queue_and_be_restored_without_deleting_evidence(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    try:
        created = asyncio.run(
            _post(
                app,
                "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs",
                {"chapter_remote_ids": ["mock-chapter-1"]},
            )
        )
        assert created.status_code == 200
        job_id = created.json()["data"][0]["job_id"]
        with database.write_session() as session:
            job = session.get(DownloadJobRow, job_id)
            assert job is not None
            job.status = DownloadJobStatus.FAILED.value
            session.add(
                DownloadPageRow(
                    job_id=job_id,
                    ordinal=1,
                    remote_reference="fixture-page-1",
                    temporary_path=None,
                    temporary_path_key=None,
                    status="FAILED",
                    size_bytes=None,
                    sha256=None,
                    width=None,
                    height=None,
                    mime=None,
                    attempts=1,
                )
            )

        archived = asyncio.run(_post_without_body(app, f"/api/v1/download-jobs/{job_id}/archive"))
        assert archived.status_code == 204
        queued = asyncio.run(_get(app, "/api/v1/download-jobs?limit=50&offset=0"))
        assert queued.status_code == 200
        assert queued.json()["data"] == []
        assert queued.json()["meta"]["total"] == 0
        history = asyncio.run(_get(app, "/api/v1/download-jobs?limit=50&offset=0&archived=true"))
        assert history.status_code == 200
        assert [item["job_id"] for item in history.json()["data"]] == [job_id]

        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 1
            assert session.scalar(select(func.count()).select_from(DownloadPageRow)) == 1

        restored = asyncio.run(_post_without_body(app, f"/api/v1/download-jobs/{job_id}/restore"))
        assert restored.status_code == 204
        visible_again = asyncio.run(_get(app, "/api/v1/download-jobs?limit=50&offset=0"))
        assert [item["job_id"] for item in visible_again.json()["data"]] == [job_id]
    finally:
        database.close()


def test_active_job_cannot_be_archived(tmp_path: Path) -> None:
    app, database = _ready_app(tmp_path)
    try:
        created = asyncio.run(
            _post(
                app,
                "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs",
                {"chapter_remote_ids": ["mock-chapter-1"]},
            )
        )
        job_id = created.json()["data"][0]["job_id"]
        response = asyncio.run(_post_without_body(app, f"/api/v1/download-jobs/{job_id}/archive"))
        assert response.status_code == 409
        queued = asyncio.run(_get(app, "/api/v1/download-jobs?limit=50&offset=0"))
        assert [item["job_id"] for item in queued.json()["data"]] == [job_id]
    finally:
        database.close()


def test_selected_chapter_creates_durable_job_and_repeated_request_reuses_it(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"

    try:
        first = asyncio.run(_post(app, path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        second = asyncio.run(_post(app, path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        page = asyncio.run(_get(app, "/api/v1/download-jobs?limit=10&offset=0"))

        assert first.status_code == 200
        assert second.status_code == 200
        first_job = first.json()["data"][0]
        second_job = second.json()["data"][0]
        assert first_job["job_id"] == second_job["job_id"]
        assert first_job["created"] is True
        assert second_job["created"] is False
        assert first_job["status"] == "QUEUED"
        assert first_job["series_title"] == "Mock Fixture Series"
        assert first_job["chapter_label"] == "Capítulo 1"
        assert UUID(first_job["series_id"]).version == 4
        assert UUID(first_job["chapter_id"]).version == 4

        assert page.status_code == 200
        assert page.json()["data"][0]["job_id"] == first_job["job_id"]
        assert page.json()["data"][0]["series_id"] == first_job["series_id"]
        assert page.json()["data"][0]["chapter_id"] == first_job["chapter_id"]
        meta = page.json()["meta"]
        assert (meta["limit"], meta["offset"], meta["total"]) == (10, 0, 1)
        assert meta["states"]["QUEUED"] == sum(meta["states"].values()) == 1
        assert meta["page_progress"] == []
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 1
            assert session.scalar(select(func.count()).select_from(SeriesRow)) == 1
            assert session.scalar(select(func.count()).select_from(SourceRow)) == 1

        database.shutdown()
        database = sqlite_test_context(tmp_path).open()
        runtime = app.state.runtime
        assert isinstance(runtime, RuntimeState)
        runtime.download_job_service = DownloadJobService(database)
        reopened_page = asyncio.run(_get(app, "/api/v1/download-jobs?limit=10&offset=0"))
        assert reopened_page.status_code == 200
        assert reopened_page.json()["data"][0]["job_id"] == first_job["job_id"]
    finally:
        database.shutdown()


def test_selected_chapter_accepts_explicit_priority_from_json_request(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"

    try:
        response = asyncio.run(
            _post(
                app,
                path,
                {
                    "chapter_remote_ids": ["mock-chapter-1"],
                    "priority": "LOW",
                },
            )
        )
        invalid_priority = asyncio.run(
            _post(
                app,
                path,
                {
                    "chapter_remote_ids": ["mock-chapter-1"],
                    "priority": "URGENT",
                },
            )
        )

        assert response.status_code == 200
        assert response.json()["data"][0]["priority"] == "LOW"
        assert invalid_priority.status_code == 422
    finally:
        database.shutdown()


def test_unknown_selected_chapter_rejects_whole_batch_without_persistence(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"

    try:
        response = asyncio.run(
            _post(
                app,
                path,
                {"chapter_remote_ids": ["mock-chapter-1", "unknown-chapter"]},
            )
        )

        assert response.status_code == 404
        assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
            assert session.scalar(select(func.count()).select_from(SeriesRow)) == 0
            assert session.scalar(select(func.count()).select_from(SourceRow)) == 0
    finally:
        database.shutdown()


def test_failed_job_retry_creates_a_new_identity_and_preserves_history(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"

    try:
        first = asyncio.run(_post(app, path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        first_job_id = UUID(first.json()["data"][0]["job_id"])
        repository = DownloadJobRepository()
        with database.write_session() as session:
            job = repository.get(session, first_job_id)
            assert job is not None
            failed = repository.transition_status(
                session,
                current_job=job,
                target_status=DownloadJobStatus.FAILED,
                changed_at=job.updated_at + timedelta(seconds=1),
            )
            assert failed is not None

        retry = asyncio.run(_post(app, path, {"chapter_remote_ids": ["mock-chapter-1"]}))

        assert retry.status_code == 200
        retried_job = retry.json()["data"][0]
        assert retried_job["created"] is True
        assert UUID(retried_job["job_id"]) != first_job_id
        with database.read_session() as session:
            jobs = session.scalars(select(DownloadJobRow)).all()
            assert len(jobs) == 2
            assert {row.status for row in jobs} == {"FAILED", "QUEUED"}
    finally:
        database.shutdown()


def test_explicit_retry_creates_one_new_job_and_preserves_failed_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app, database = _ready_app(tmp_path)
    create_path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"
    try:
        created = asyncio.run(_post(app, create_path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        job_id = created.json()["data"][0]["job_id"]
        part_path = f"jobs/{job_id}/pages/00001/attempt-1.part"
        preserved_bytes = b"verified original attempt must survive manual retry"
        preserved_part = tmp_path / "temp" / part_path
        preserved_part.parent.mkdir(parents=True)
        preserved_part.write_bytes(preserved_bytes)
        with database.write_session() as session:
            row = session.get(DownloadJobRow, job_id)
            assert row is not None
            row.status = DownloadJobStatus.FAILED.value
            # A backward clock must not make the historical job sort after its retry.
            row.created_at_us += 60_000_000
            row.updated_at_us = row.created_at_us
            session.add(
                DownloadPageRow(
                    job_id=job_id,
                    ordinal=1,
                    remote_reference="fixture-page-1",
                    temporary_path=part_path,
                    temporary_path_key=part_path.casefold(),
                    status="FAILED",
                    size_bytes=len(preserved_bytes),
                    sha256=hashlib.sha256(preserved_bytes).hexdigest(),
                    width=None,
                    height=None,
                    mime=None,
                    attempts=1,
                )
            )
        with database.read_session() as session:
            failed_before = DownloadJobRepository().get(session, UUID(job_id))
            page_before = session.get(DownloadPageRow, (job_id, 1))
            assert page_before is not None
            preserved_page = (page_before.status, page_before.attempts, page_before.sha256)

        async def retry_concurrently() -> tuple[Response, Response]:
            results = await asyncio.gather(
                _post_without_body(app, f"/api/v1/downloads/{job_id}/retry"),
                _post_without_body(app, f"/api/v1/downloads/{job_id}/retry"),
            )
            return results[0], results[1]

        first, repeated = asyncio.run(retry_concurrently())
        assert first.status_code == repeated.status_code == 200
        retried_job_id = first.json()["data"]["job_id"]
        assert retried_job_id != job_id
        assert UUID(retried_job_id).version == 4
        assert repeated.json()["data"]["job_id"] == retried_job_id
        assert first.json()["data"]["status"] == "QUEUED"
        assert first.json()["data"]["priority"] == "HIGH"
        assert (
            first.json()["data"]["chapter_source_id"]
            == created.json()["data"][0]["chapter_source_id"]
        )
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 2
            assert DownloadJobRepository().get(session, UUID(job_id)) == failed_before
            page_after = session.get(DownloadPageRow, (job_id, 1))
            assert page_after is not None
            assert (page_after.status, page_after.attempts, page_after.sha256) == preserved_page
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(DownloadPageRow)
                    .where(DownloadPageRow.job_id == retried_job_id)
                )
                == 0
            )
        assert preserved_part.read_bytes() == preserved_bytes
        # A retry already queued by an older version must be promoted, not duplicated.
        with database.write_session() as session:
            retried_job = session.get(DownloadJobRow, retried_job_id)
            assert retried_job is not None
            retried_job.priority = "NORMAL"
        promoted = asyncio.run(_post_without_body(app, f"/api/v1/downloads/{job_id}/retry"))
        assert promoted.status_code == 200
        assert promoted.json()["data"]["job_id"] == retried_job_id
        assert promoted.json()["data"]["priority"] == "HIGH"
        visible = asyncio.run(_get(app, "/api/v1/download-jobs?limit=10"))
        assert [item["job_id"] for item in visible.json()["data"]] == [retried_job_id]
        assert visible.json()["meta"]["states"]["FAILED"] == 0
        history = asyncio.run(_get(app, "/api/v1/download-jobs?limit=10&archived=true"))
        assert [item["job_id"] for item in history.json()["data"]] == [job_id]
        records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
        retry_records = [
            record for record in records if record["message"].startswith("download_job.retry_")
        ]
        assert len(retry_records) == 6
        for trace_id in {record["trace_id"] for record in retry_records}:
            pair = [record for record in retry_records if record["trace_id"] == trace_id]
            assert len(pair) == 2
            assert pair[0]["message"] == "download_job.retry_requested"
            assert pair[0]["job_id"] == job_id
            assert pair[1]["job_id"] == retried_job_id
    finally:
        database.shutdown()


@pytest.mark.parametrize(
    "job_status",
    tuple(value for value in DownloadJobStatus if value is not DownloadJobStatus.FAILED),
)
def test_explicit_retry_rejects_nonfailed_jobs_without_mutating_them(
    tmp_path: Path,
    job_status: DownloadJobStatus,
) -> None:
    app, database = _ready_app(tmp_path)
    create_path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"
    try:
        created = asyncio.run(_post(app, create_path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        job_id = created.json()["data"][0]["job_id"]
        with database.write_session() as session:
            row = session.get(DownloadJobRow, job_id)
            assert row is not None
            row.status = job_status.value
        response = asyncio.run(_post_without_body(app, f"/api/v1/downloads/{job_id}/retry"))
        assert response.status_code == 409
        assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 1
            unchanged = session.get(DownloadJobRow, job_id)
            assert unchanged is not None
            assert unchanged.status == job_status.value
    finally:
        database.shutdown()


@pytest.mark.parametrize("unavailable", ("source", "chapter", "cancellation"))
def test_explicit_retry_rejects_disabled_source_unavailable_chapter_or_pending_cancel(
    tmp_path: Path,
    unavailable: str,
) -> None:
    app, database = _ready_app(tmp_path)
    create_path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"
    try:
        created = asyncio.run(_post(app, create_path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        job_id = created.json()["data"][0]["job_id"]
        with database.write_session() as session:
            job = session.get(DownloadJobRow, job_id)
            assert job is not None
            job.status = DownloadJobStatus.FAILED.value
            chapter_source = session.get(ChapterSourceRow, job.chapter_source_id)
            assert chapter_source is not None
            source = session.get(SourceRow, chapter_source.source_id)
            assert source is not None
            if unavailable == "source":
                source.enabled = False
            elif unavailable == "chapter":
                chapter_source.available = False
            else:
                job.cancel_requested_at_us = job.updated_at_us
        response = asyncio.run(_post_without_body(app, f"/api/v1/downloads/{job_id}/retry"))
        assert response.status_code == 409
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 1
    finally:
        database.shutdown()


def test_explicit_retry_unknown_or_invalid_uuid_is_traceable(tmp_path: Path) -> None:
    app, database = _ready_app(tmp_path)
    try:
        for job_id, expected_status in (
            ("c6a84de9-93fc-4f4a-ae9e-25a6c6258860", 404),
            ("not-a-uuid", 422),
            ("c6a84de9-93fc-1f4a-ae9e-25a6c6258860", 422),
        ):
            response = asyncio.run(_post_without_body(app, f"/api/v1/downloads/{job_id}/retry"))
            assert response.status_code == expected_status
            assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
    finally:
        database.shutdown()


@pytest.mark.parametrize("later_status", (DownloadJobStatus.CANCELLED, DownloadJobStatus.FAILED))
def test_explicit_retry_reuses_later_completed_job_and_respects_later_cancellation(
    tmp_path: Path,
    later_status: DownloadJobStatus,
) -> None:
    app, database = _ready_app(tmp_path)
    create_path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"
    try:
        created = asyncio.run(_post(app, create_path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        old_id = created.json()["data"][0]["job_id"]
        with database.write_session() as session:
            old = session.get(DownloadJobRow, old_id)
            assert old is not None
            old.status = DownloadJobStatus.FAILED.value
        later = asyncio.run(_post(app, create_path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        later_id = later.json()["data"][0]["job_id"]
        with database.write_session() as session:
            latest = session.get(DownloadJobRow, later_id)
            assert latest is not None
            latest.status = DownloadJobStatus.COMPLETED.value
        index_test_job(database, UUID(later_id))
        retry = asyncio.run(_post_without_body(app, f"/api/v1/downloads/{old_id}/retry"))
        assert retry.status_code == 200
        assert retry.json()["data"]["job_id"] == later_id
        assert retry.json()["data"]["status"] == "COMPLETED"
        with database.write_session() as session:
            latest = session.get(DownloadJobRow, later_id)
            assert latest is not None
            latest.status = later_status.value
            latest.cancel_requested_at_us = latest.updated_at_us
        cancelled = asyncio.run(_post_without_body(app, f"/api/v1/downloads/{old_id}/retry"))
        assert cancelled.status_code == 409
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 2
    finally:
        database.shutdown()


def test_completed_chapter_selection_reuses_completed_job(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"

    try:
        first = asyncio.run(_post(app, path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        completed_job_id = UUID(first.json()["data"][0]["job_id"])
        repository = DownloadJobRepository()
        statuses = (
            DownloadJobStatus.FETCHING_METADATA,
            DownloadJobStatus.DOWNLOADING,
            DownloadJobStatus.VALIDATING,
            DownloadJobStatus.PROCESSING,
            DownloadJobStatus.PACKAGING,
            DownloadJobStatus.VERIFYING,
            DownloadJobStatus.INDEXING,
            DownloadJobStatus.COMPLETED,
        )
        with database.write_session() as session:
            job = repository.get(session, completed_job_id)
            assert job is not None
            for target_status in statuses:
                job = repository.transition_status(
                    session,
                    current_job=job,
                    target_status=target_status,
                    changed_at=job.updated_at + timedelta(seconds=1),
                )
                assert job is not None

        index_test_job(database, completed_job_id)
        repeated = asyncio.run(_post(app, path, {"chapter_remote_ids": ["mock-chapter-1"]}))

        assert repeated.status_code == 200
        completed_job = repeated.json()["data"][0]
        assert completed_job["job_id"] == str(completed_job_id)
        assert completed_job["status"] == "COMPLETED"
        assert completed_job["created"] is False
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 1
    finally:
        database.shutdown()


def test_selected_chapter_request_is_bounded_and_requires_unique_ids(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"

    try:
        empty = asyncio.run(_post(app, path, {"chapter_remote_ids": []}))
        duplicate = asyncio.run(
            _post(
                app,
                path,
                {"chapter_remote_ids": ["mock-chapter-1", "mock-chapter-1"]},
            )
        )

        assert empty.status_code == 422
        assert duplicate.status_code == 422
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
    finally:
        database.shutdown()


def test_cancel_queued_job_is_immediate_durable_and_idempotent(tmp_path: Path) -> None:
    app, database = _ready_app(tmp_path)
    create_path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"

    try:
        created = asyncio.run(_post(app, create_path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        job_id = created.json()["data"][0]["job_id"]

        first = asyncio.run(_post_without_body(app, f"/api/v1/downloads/{job_id}/cancel"))
        repeated = asyncio.run(_post_without_body(app, f"/api/v1/downloads/{job_id}/cancel"))

        assert first.status_code == 200
        assert repeated.status_code == 200
        assert first.json()["data"]["job_id"] == job_id
        assert first.json()["data"]["status"] == "CANCELLED"
        assert first.json()["data"]["cancel_requested_at"] is not None
        assert (
            repeated.json()["data"]["cancel_requested_at"]
            == first.json()["data"]["cancel_requested_at"]
        )
        listed = asyncio.run(_get(app, "/api/v1/download-jobs?limit=10&offset=0"))
        assert (
            listed.json()["data"][0]["cancel_requested_at"]
            == first.json()["data"]["cancel_requested_at"]
        )

        with database.read_session() as session:
            row = session.get(DownloadJobRow, job_id)
            assert row is not None
            assert row.status == "CANCELLED"
            assert row.cancel_requested_at_us is not None
    finally:
        database.shutdown()


def test_cancel_fetching_metadata_job_is_immediate(tmp_path: Path) -> None:
    app, database = _ready_app(tmp_path)
    create_path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"

    try:
        created = asyncio.run(_post(app, create_path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        job_id = UUID(created.json()["data"][0]["job_id"])
        with database.write_session() as session:
            repository = DownloadJobRepository()
            job = repository.get(session, job_id)
            assert job is not None
            fetching = repository.transition_status(
                session,
                current_job=job,
                target_status=DownloadJobStatus.FETCHING_METADATA,
                changed_at=job.updated_at + timedelta(seconds=1),
            )
            assert fetching is not None

        response = asyncio.run(_post_without_body(app, f"/api/v1/downloads/{job_id}/cancel"))

        assert response.status_code == 200
        assert response.json()["data"]["status"] == "CANCELLED"
        assert response.json()["data"]["cancel_requested_at"] is not None
        with database.read_session() as session:
            row = session.get(DownloadJobRow, str(job_id))
            assert row is not None
            assert row.status == "CANCELLED"
            assert row.cancel_requested_at_us is not None
    finally:
        database.shutdown()


def test_cancel_active_job_persists_intent_and_notifies_registered_worker(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    create_path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"

    async def request_cancel(job_id: UUID) -> Response:
        runtime = app.state.runtime
        assert isinstance(runtime, RuntimeState)
        controller = JobPauseController()
        runtime.job_pause_controller = controller
        await controller.register(job_id)
        response = await _post_without_body(
            app,
            f"/api/v1/downloads/{job_id}/cancel",
        )
        assert controller.state(job_id) is JobPauseState.CANCELLATION_REQUESTED
        return response

    try:
        created = asyncio.run(_post(app, create_path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        job_id = UUID(created.json()["data"][0]["job_id"])
        repository = DownloadJobRepository()
        with database.write_session() as session:
            job = repository.get(session, job_id)
            assert job is not None
            for target in (
                DownloadJobStatus.FETCHING_METADATA,
                DownloadJobStatus.DOWNLOADING,
            ):
                job = repository.transition_status(
                    session,
                    current_job=job,
                    target_status=target,
                    changed_at=job.updated_at + timedelta(seconds=1),
                )
                assert job is not None

        response = asyncio.run(request_cancel(job_id))

        assert response.status_code == 200
        assert response.json()["data"]["status"] == "DOWNLOADING"
        assert response.json()["data"]["cancel_requested_at"] is not None
        with database.read_session() as session:
            row = session.get(DownloadJobRow, str(job_id))
            assert row is not None
            assert row.status == "DOWNLOADING"
            assert row.cancel_requested_at_us is not None
    finally:
        database.shutdown()


def test_cancel_unknown_or_terminal_job_returns_traceable_conflict_or_not_found(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    create_path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"

    try:
        unknown_id = "c6a84de9-93fc-4f4a-ae9e-25a6c6258860"
        missing = asyncio.run(_post_without_body(app, f"/api/v1/downloads/{unknown_id}/cancel"))
        created = asyncio.run(_post(app, create_path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        job_id = UUID(created.json()["data"][0]["job_id"])
        repository = DownloadJobRepository()
        with database.write_session() as session:
            queued = repository.get(session, job_id)
            assert queued is not None
            failed = repository.transition_status(
                session,
                current_job=queued,
                target_status=DownloadJobStatus.FAILED,
                changed_at=queued.updated_at + timedelta(seconds=1),
            )
            assert failed is not None
        terminal = asyncio.run(_post_without_body(app, f"/api/v1/downloads/{job_id}/cancel"))

        assert missing.status_code == 404
        assert missing.json()["error"]["trace_id"] == missing.headers["x-trace-id"]
        assert terminal.status_code == 409
        assert terminal.json()["error"]["trace_id"] == terminal.headers["x-trace-id"]
        with database.read_session() as session:
            row = session.get(DownloadJobRow, str(job_id))
            assert row is not None
            assert row.status == "FAILED"
            assert row.cancel_requested_at_us is None
    finally:
        database.shutdown()


def test_cancel_legacy_cancelled_job_without_intent_timestamp_is_idempotent(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    create_path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"

    try:
        created = asyncio.run(_post(app, create_path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        job_id = created.json()["data"][0]["job_id"]
        with database.write_session() as session:
            # Simulate a pre-migration terminal row: CANCELLED existed before
            # cancel_requested_at_us was added and therefore can have NULL.
            row = session.get(DownloadJobRow, job_id)
            assert row is not None
            row.status = DownloadJobStatus.CANCELLED.value

        response = asyncio.run(_post_without_body(app, f"/api/v1/downloads/{job_id}/cancel"))

        assert response.status_code == 200
        assert response.json()["data"]["status"] == "CANCELLED"
        assert response.json()["data"]["cancel_requested_at"] is None
    finally:
        database.shutdown()


def test_cancel_endpoint_keeps_event_loop_responsive_during_sqlite_work(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    create_path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"
    entered = threading.Event()
    release = threading.Event()
    release_timer = threading.Timer(2, release.set)

    try:
        created = asyncio.run(_post(app, create_path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        job_id = created.json()["data"][0]["job_id"]
        runtime = app.state.runtime
        assert isinstance(runtime, RuntimeState)
        service = runtime.download_job_service
        assert service is not None
        original_cancel = service.cancel_job

        def block_sqlite(
            job_id: UUID,
            *,
            now: datetime | None = None,
        ) -> DownloadJobSummary | None:
            entered.set()
            if not release.wait(timeout=3):
                raise TimeoutError("test did not release the simulated SQLite wait")
            return original_cancel(job_id, now=now)

        service.cancel_job = block_sqlite  # type: ignore[method-assign]

        async def exercise() -> tuple[Response, Response, float]:
            started_at = time.monotonic()
            cancel_task = asyncio.create_task(
                _post_without_body(app, f"/api/v1/downloads/{job_id}/cancel")
            )
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=1)
            liveness = await asyncio.wait_for(
                _get(app, "/api/v1/system/liveness"),
                timeout=0.75,
            )
            elapsed = time.monotonic() - started_at
            release.set()
            cancellation = await cancel_task
            return cancellation, liveness, elapsed

        release_timer.start()
        cancellation, liveness, elapsed = asyncio.run(exercise())
        assert elapsed < 0.75
        assert liveness.status_code == 200
        assert liveness.json() == {"status": "alive"}
        assert cancellation.status_code == 200
    finally:
        release.set()
        release_timer.cancel()
        database.shutdown()


def test_cancel_internal_value_error_keeps_traceable_server_error(
    tmp_path: Path,
) -> None:
    app, database = _ready_app(tmp_path)
    create_path = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"

    try:
        created = asyncio.run(_post(app, create_path, {"chapter_remote_ids": ["mock-chapter-1"]}))
        job_id = created.json()["data"][0]["job_id"]
        runtime = app.state.runtime
        assert isinstance(runtime, RuntimeState)
        service = runtime.download_job_service
        assert service is not None

        def internal_value_error(*_args: object, **_kwargs: object):
            raise ValueError("internal persistence invariant failure")

        service.cancel_job = internal_value_error  # type: ignore[method-assign]
        response = asyncio.run(_post_without_body(app, f"/api/v1/downloads/{job_id}/cancel"))

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "SYS_INTERNAL_ERROR"
        assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]
    finally:
        database.shutdown()
