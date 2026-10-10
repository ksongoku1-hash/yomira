from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from mock_manga_server import MockMangaServer
from offline_preview import (
    PreviewInstanceGuard,
    create_preview_data_root,
    isolated_preview_composition,
)

from app.domain.download_job import DownloadJobStatus
from app.factory import create_app
from app.settings import Settings, load_settings


class _PreviewApiClient(Protocol):
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> Response: ...

    def post(self, url: str, *, json: object) -> Response: ...

    def put(self, url: str, *, json: object) -> Response: ...


def _data_items(response: Response) -> list[dict[str, object]]:
    payload = cast(dict[str, object], response.json())
    data = payload.get("data")
    if not isinstance(data, list):
        raise AssertionError("The API response does not contain a data list.")
    return cast(list[dict[str, object]], data)


def _string_field(item: dict[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise AssertionError(f"The API field {key!r} is not a string.")
    return value


def _integer_field(item: dict[str, object], key: str) -> int:
    value = item.get(key)
    if not isinstance(value, int):
        raise AssertionError(f"The API field {key!r} is not an integer.")
    return value


def test_offline_preview_search_downloads_cbz_and_reads_fixture_bytes(
    tmp_path: Path,
) -> None:
    from app import lifecycle

    with MockMangaServer() as fixture_server:
        data_root = create_preview_data_root(base_dir=tmp_path.parent)
        settings = Settings(
            app_env="test",
            api_host="127.0.0.1",
            api_port=8000,
            data_root=data_root,
        )

        with isolated_preview_composition(fixture_server, settings=settings):
            assert isinstance(lifecycle.create_instance_guard(), PreviewInstanceGuard)
            with pytest.raises(RuntimeError, match="app_env='test'"):
                PreviewInstanceGuard(app_env="prod").acquire()
            app = create_app(settings)
            assert isinstance(app, FastAPI)
            with TestClient(app) as raw_client:
                client = cast(_PreviewApiClient, raw_client)
                connector_response = client.get("/api/v1/connectors")
                assert connector_response.status_code == 200
                assert [
                    _string_field(item, "connector_id")
                    for item in _data_items(connector_response)
                ] == ["yomira.mock"]

                search_response = client.get(
                    "/api/v1/connectors/yomira.mock/search",
                    params={"query": "fixture"},
                )
                assert search_response.status_code == 200
                assert (
                    _string_field(_data_items(search_response)[0], "remote_id")
                    == "mock-series-1"
                )

                chapters_response = client.get(
                    "/api/v1/connectors/yomira.mock/series/mock-series-1/chapters"
                )
                assert chapters_response.status_code == 200
                assert (
                    _string_field(_data_items(chapters_response)[0], "remote_id")
                    == "mock-chapter-1"
                )

                create_response = client.post(
                    "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs",
                    json={"chapter_remote_ids": ["mock-chapter-1"]},
                )
                assert create_response.status_code == 200
                created_job = _data_items(create_response)[0]
                job_id = _string_field(created_job, "job_id")
                chapter_id = _string_field(created_job, "chapter_id")
                series_id = _string_field(created_job, "series_id")

                # The Windows worker can take more than 15 seconds through
                # validation and packaging; retain a finite, bounded wait.
                deadline = time.monotonic() + 40
                final_status: str | None = None
                while time.monotonic() < deadline:
                    jobs_response = client.get("/api/v1/download-jobs")
                    assert jobs_response.status_code == 200
                    matching_jobs = [
                        item
                        for item in _data_items(jobs_response)
                        if _string_field(item, "job_id") == job_id
                    ]
                    assert len(matching_jobs) == 1
                    final_status = _string_field(matching_jobs[0], "status")
                    if final_status in {
                        DownloadJobStatus.COMPLETED.value,
                        DownloadJobStatus.FAILED.value,
                        DownloadJobStatus.CANCELLED.value,
                        DownloadJobStatus.INTERRUPTED.value,
                    }:
                        break
                    time.sleep(0.25)

                assert final_status == DownloadJobStatus.COMPLETED.value

                library_response = client.get("/api/v1/library")
                assert library_response.status_code == 200
                assert any(
                    _string_field(item, "series_id") == series_id
                    for item in _data_items(library_response)
                )

                reader_chapters_response = client.get(
                    f"/api/v1/reader/series/{series_id}/chapters"
                )
                assert reader_chapters_response.status_code == 200
                reader_chapters = _data_items(reader_chapters_response)
                assert len(reader_chapters) == 1
                assert _integer_field(reader_chapters[0], "page_count") == 3

                for page_number in (1, 2, 3):
                    page_response = client.get(
                        f"/api/v1/reader/{chapter_id}/pages/{page_number}"
                    )
                    assert page_response.status_code == 200
                    assert page_response.headers["content-type"] == "image/png"
                    assert page_response.content.startswith(b"\x89PNG\r\n\x1a\n")

                progress_path = f"/api/v1/reader/{chapter_id}/progress"
                saved_progress_response = client.put(
                    progress_path,
                    json={"page_index": 1},
                )
                assert saved_progress_response.status_code == 200
                assert saved_progress_response.json()["data"]["page_index"] == 1

                assert data_root.is_dir()
                normal_data_root = load_settings().data_root.resolve(strict=False)
                assert data_root != normal_data_root
                assert normal_data_root not in data_root.parents
                assert data_root not in normal_data_root.parents

            # Closing TestClient shuts down the entire app lifecycle and DB;
            # recreate it over the same isolated root to simulate a reopen.
            reopened_app = create_app(settings)
            assert isinstance(reopened_app, FastAPI)
            with TestClient(reopened_app) as reopened_raw_client:
                reopened_client = cast(_PreviewApiClient, reopened_raw_client)
                reopened_library_response = reopened_client.get("/api/v1/library")
                assert reopened_library_response.status_code == 200
                assert any(
                    _string_field(item, "series_id") == series_id
                    for item in _data_items(reopened_library_response)
                )

                reopened_reader_chapters_response = reopened_client.get(
                    f"/api/v1/reader/series/{series_id}/chapters"
                )
                assert reopened_reader_chapters_response.status_code == 200
                reopened_chapters = _data_items(reopened_reader_chapters_response)
                assert len(reopened_chapters) == 1
                assert _integer_field(reopened_chapters[0], "page_count") == 3

                reopened_page_response = reopened_client.get(
                    f"/api/v1/reader/{chapter_id}/pages/2"
                )
                assert reopened_page_response.status_code == 200
                assert reopened_page_response.content.startswith(b"\x89PNG\r\n\x1a\n")

                reopened_progress_response = reopened_client.get(progress_path)
                assert reopened_progress_response.status_code == 200
                assert reopened_progress_response.json()["data"]["page_index"] == 1


def test_preview_instance_guard_is_rejected_outside_test_profile() -> None:
    guard = PreviewInstanceGuard(app_env="prod")

    with pytest.raises(RuntimeError, match="app_env='test'"):
        guard.acquire()
