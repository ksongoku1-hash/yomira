import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from support.cbz import index_test_job
from test_online_reader import _memory_harness  # pyright: ignore[reportPrivateUsage]

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.mock import MockConnector
from app.errors import ErrorCode
from app.persistence.models.download_job import DownloadJobRow
from app.services.jobs import DownloadJobService


@pytest.mark.parametrize("case", ["saved", "another-edition", "missing-file", "not-completed"])
def test_saved_chapter_registration_uses_exact_local_source_without_a_catalog_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    async def exercise() -> None:
        h, online, runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
        service = DownloadJobService(h.database)
        runtime.download_job_service = service
        try:
            chapters = await h.provider.get_chapters("mock-series-1")
            jobs = service.create_selected_jobs(
                connector=h.provider.metadata,
                remote_series_id="mock-series-1",
                series_metadata=h.provider.normalize_metadata(
                    await h.provider.get_series("mock-series-1")
                ),
                chapters=chapters,
                selected_remote_ids=("mock-chapter-1",),
            )
            index_test_job(h.database, jobs[0].job.job_id)
            if case == "missing-file":
                archive = next(tmp_path.rglob("*.cbz"))
                archive.relative_to(tmp_path)
                archive.unlink()
            elif case == "not-completed":
                with h.database.write_session() as session:
                    job = session.get(DownloadJobRow, str(jobs[0].job.job_id))
                    assert job is not None
                    job.status = "FAILED"
            catalog_reference = "another-edition" if case == "another-edition" else "mock-series-1"
            requested: list[str] = []

            async def unavailable(_connector: MockConnector, remote_reference: str) -> object:
                requested.append(remote_reference)
                raise SourceConnectorError(
                    code=ErrorCode.SRC_CONNECTOR_FAILED,
                    operation=SourceConnectorOperation.GET_SERIES,
                    retryable=True,
                )

            monkeypatch.setattr(MockConnector, "get_series", unavailable)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=h.app, raise_app_exceptions=False),
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    f"/api/v1/connectors/yomira.mock/series/{catalog_reference}/reader-chapter",
                    json={"chapter_remote_id": "mock-chapter-1"},
                )
            if case == "saved":
                assert response.status_code == 200, response.text
                assert response.json()["data"]["chapter_id"] == str(h.chapter_id)
                assert not requested
            else:
                assert response.status_code == 503
                assert requested == [catalog_reference]
            with h.database.read_session() as session:
                assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 1
        finally:
            await online.close()
            await h.close()

    asyncio.run(exercise())
