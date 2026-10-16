import asyncio
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_online_reader import _memory_harness  # pyright: ignore[reportPrivateUsage]

from app.connectors.builtins import create_connector_registry
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.series_source import SeriesSourceRow
from app.persistence.models.source import SourceRow
from app.runtime import RuntimeState
from app.secrets.vault import WindowsDpapiSecretVault
from app.services.jobs import DownloadJobService
from app.settings import Settings


def test_reader_source_queues_the_same_chapter_without_losing_online_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        h, online, runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
        runtime.download_job_service = DownloadJobService(h.database)
        runtime.connector_registry = create_connector_registry(
            Settings(app_env="test", data_root=tmp_path), WindowsDpapiSecretVault(tmp_path)
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=h.app), base_url="http://testserver"
            ) as client:
                path = f"/api/v1/reader/{h.chapter_id}"
                assert (await client.post(path + "/session", json={})).status_code == 200
                assert (await client.get(path + "/pages/1")).status_code == 200
                progress = await client.put(path + "/progress", json={"page_index": 0})
                source = await client.get(path + "/download-source")
                assert source.status_code == progress.status_code == 200
                refs = source.json()["data"]
                assert refs == {"chapter_id": str(h.chapter_id), "connector_id": "yomira.mock",
                                "remote_series_id": "mock-series-1",
                                "chapter_remote_id": "mock-chapter-1"}
                with h.database.read_session() as session:
                    assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
                endpoint = "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs"
                body = {"chapter_remote_ids": [refs["chapter_remote_id"]]}
                first = await client.post(endpoint, json=body)
                repeated = await client.post(endpoint, json=body)
                assert first.status_code == repeated.status_code == 200
                assert first.json()["data"][0]["chapter_id"] == str(h.chapter_id)
                assert first.json()["data"][0]["job_id"] == repeated.json()["data"][0]["job_id"]
                assert (await client.get(path + "/progress")).json() == progress.json()
                assert not tuple(tmp_path.rglob("*.cbz"))
                assert not tuple(tmp_path.rglob("*.page"))
        finally:
            await online.close()
            await h.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("unavailable", [
    "chapter", "source", "version", "ambiguous", "missing", "unregistered", "reference"
])
def test_reader_download_source_never_guesses_an_unavailable_or_ambiguous_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    async def exercise() -> None:
        h, online, _runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
        runtime = cast(RuntimeState, h.app.state.runtime)
        runtime.download_job_service = DownloadJobService(h.database)
        runtime.connector_registry = create_connector_registry(
            Settings(app_env="test", data_root=tmp_path), WindowsDpapiSecretVault(tmp_path)
        )
        try:
            with h.database.write_session() as session:
                chapter_source = session.scalar(select(ChapterSourceRow))
                assert chapter_source is not None
                source = session.get(SourceRow, chapter_source.source_id)
                assert source is not None
                if unavailable == "chapter":
                    chapter_source.available = False
                elif unavailable == "source":
                    source.enabled = False
                elif unavailable == "version":
                    source.connector_version = "old-version"
                elif unavailable == "unregistered":
                    source.connector_id = "unregistered"
                elif unavailable == "reference":
                    chapter_source.remote_id = "x" * 513
                elif unavailable == "ambiguous":
                    association = session.scalar(select(SeriesSourceRow))
                    assert association is not None
                    session.add(SeriesSourceRow(series_source_id=str(uuid4()),
                        series_id=association.series_id, source_id=association.source_id,
                        remote_id="another-edition"))
            chapter_id = uuid4() if unavailable == "missing" else h.chapter_id
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=h.app), base_url="http://testserver"
            ) as client:
                response = await client.get(f"/api/v1/reader/{chapter_id}/download-source")
                assert response.status_code == (404 if unavailable == "missing" else 200)
                if unavailable != "missing":
                    assert response.json() == {"data": None, "meta": {}}
            with h.database.read_session() as session:
                assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
        finally:
            await online.close()
            await h.close()
    asyncio.run(exercise())
