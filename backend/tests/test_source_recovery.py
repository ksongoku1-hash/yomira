import asyncio
import base64
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from test_chapter_fallback import INMANGA, TMO, reference, setup
from test_online_reader import _memory_harness  # pyright: ignore[reportPrivateUsage]

from app.connectors.builtins import create_connector_registry
from app.connectors.errors import SourceConnectorError
from app.connectors.search import ConnectorSearchQuery, ConnectorSearchResult
from app.errors import ErrorCode
from app.fmd2_bridge import (
    FMD2_METADATA,
    FMD2_RETIRED_SOURCES,
    Fmd2Connector,
    fmd2_chapter_location,
    fmd2_original_series_url,
    fmd2_series_source,
    fmd2_source_display_name,
)
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.series_source import SeriesSourceRow
from app.persistence.models.source import SourceRow
from app.secrets.vault import WindowsDpapiSecretVault
from app.services.jobs import DownloadJobService
from app.settings import Settings


@pytest.mark.parametrize("stored_uppercase", [False, True])
@pytest.mark.parametrize("candidate_uppercase", [False, True])
def test_fallback_accepts_equivalent_hex_without_duplicating_an_edition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stored_uppercase: bool,
    candidate_uppercase: bool,
) -> None:
    async def exercise() -> None:
        database, client, connector, service, fallback, summary, result, _ = await setup(tmp_path)
        original_search = connector.search

        def uppercase(value: str) -> str:
            return "fmd2:v1:" + value.removeprefix("fmd2:v1:").upper()

        async def candidates(query: ConnectorSearchQuery) -> tuple[ConnectorSearchResult, ...]:
            return tuple(
                replace(item, remote_id=uppercase(item.remote_id)) if candidate_uppercase else item
                for item in await original_search(query)
                if item.remote_id is not None and fmd2_series_source(item.remote_id) == INMANGA
            )

        monkeypatch.setattr(connector, "search", candidates)
        try:
            with database.write_session() as session:
                association = session.scalar(select(SeriesSourceRow))
                assert association is not None and association.remote_id == reference(TMO)
                if stored_uppercase:
                    association.remote_id = uppercase(reference(TMO))
                session.add(SeriesSourceRow(
                    series_source_id=str(uuid4()), series_id=association.series_id,
                    source_id=association.source_id, remote_id=reference(INMANGA),
                    remote_title="Naruto",
                ))
            assert await fallback.recover(result)
            replacement = service.list_jobs().items[0]
            assert replacement.chapter_id == summary.chapter_id
            assert replacement.series_id == summary.series_id
            assert replacement.job.job_id != summary.job.job_id
            assert not await fallback.recover(result)
            with database.read_session() as session:
                editions = session.scalars(select(SeriesSourceRow)).all()
                assert len(editions) == 2
                assert {fmd2_series_source(row.remote_id or "") for row in editions} == {
                    TMO, INMANGA
                }
        finally:
            await client.aclose()
            database.close()

    asyncio.run(exercise())


def test_fallback_does_not_treat_different_real_path_case_as_equivalent(tmp_path: Path) -> None:
    async def exercise() -> None:
        database, client, connector, _service, fallback, _summary, result, _ = await setup(tmp_path)
        try:
            context = fallback._context(result.job.job_id)  # pyright: ignore[reportPrivateUsage]
            assert context is not None
            metadata = connector.normalize_metadata(await connector.get_series(reference(INMANGA)))
            chapters = await connector.get_chapters(reference(INMANGA))
            with pytest.raises(SourceConnectorError) as failure:
                fallback._enqueue(  # pyright: ignore[reportPrivateUsage]
                    context, reference(INMANGA, "/Naruto"), metadata, chapters[0]
                )
            assert failure.value.code is ErrorCode.SRC_SCHEMA_CHANGED
        finally:
            await client.aclose()
            database.close()

    asyncio.run(exercise())


def test_retired_sources_preserve_references_without_contacting_the_parked_domain(
    tmp_path: Path,
) -> None:
    sid = next(iter(FMD2_RETIRED_SOURCES))
    manga = "fmd2:v1:" + (sid + "\n/manga/example/").encode().hex()
    chapter = "yomira-fmd2:v3:" + base64.urlsafe_b64encode(
        json.dumps([sid, "/manga/example/", "/chapter/1"]).encode()
    ).decode().rstrip("=")

    def reject(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Retired sources must not make requests")

    connector = Fmd2Connector(
        tmp_path / "fmd.exe",
        tmp_path / "stage",
        api_client=httpx.AsyncClient(
            base_url="http://127.0.0.1:18873", transport=httpx.MockTransport(reject)
        ),
    )

    async def run() -> None:
        try:
            for operation in [
                lambda: connector.get_series(manga),
                lambda: connector.get_chapters(manga),
                lambda: connector.open_online_pages(uuid4(), chapter),
                lambda: connector.acquire_pages(uuid4(), chapter, "Chapter 1"),
            ]:
                with pytest.raises(SourceConnectorError) as failure:
                    await operation()
                assert failure.value.code is ErrorCode.SRC_SOURCE_UNAVAILABLE
                assert failure.value.retryable is False
            assert fmd2_chapter_location(chapter)[0] == manga
            assert fmd2_source_display_name(manga) == "Cartel De Manhwas"
            assert fmd2_original_series_url(manga) is None
        finally:
            await connector.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "path", ["//evil.example/", "/../secret", "/manga/\\evil", "https://evil.example/"]
)
def test_original_website_link_rejects_unsafe_references(path: str) -> None:
    ref = "fmd2:v1:" + ("97dcd409f781467eb2da4a8634e5a1d9\n" + path).encode().hex()
    assert fmd2_original_series_url(ref) is None


@pytest.mark.parametrize(
    "sid", ["97dcd409f781467eb2da4a8634e5a1d9", "9a0a1298ca044dd9886a3446092f285e"]
)
def test_reader_error_actions_resolve_the_original_edition_without_fetching_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sid: str,
) -> None:
    async def run() -> None:
        h, online, runtime, _ = await _memory_harness(tmp_path, monkeypatch)

        def reject(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("No remote lookup")

        bridge = Fmd2Connector(
            tmp_path / "fmd.exe",
            tmp_path / "stage",
            api_client=httpx.AsyncClient(
                base_url="http://127.0.0.1:18873",
                transport=httpx.MockTransport(reject),
            ),
        )
        runtime.download_job_service = DownloadJobService(h.database)
        runtime.connector_registry = create_connector_registry(
            Settings(app_env="test", data_root=tmp_path),
            WindowsDpapiSecretVault(tmp_path),
            fmd2_connector=bridge,
        )
        path = "/manga/example/"
        ref = "fmd2:v1:" + (sid + "\n" + path).encode().hex()
        remote = "yomira-fmd2:v3:" + base64.urlsafe_b64encode(
            json.dumps([sid, path, "/chapter/1"]).encode()
        ).decode().rstrip("=")
        try:
            with h.database.write_session() as session:
                ch = session.scalar(select(ChapterSourceRow))
                association = session.scalar(select(SeriesSourceRow))
                assert ch is not None and association is not None
                source = session.get(SourceRow, ch.source_id)
                assert source is not None
                source.connector_id, source.connector_version = (
                    FMD2_METADATA.connector_id,
                    FMD2_METADATA.connector_version,
                )
                ch.remote_id, association.remote_id = remote, ref
                series_id = association.series_id
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=h.app), base_url="http://testserver"
            ) as client:
                response = await client.get(
                    f"/api/v1/reader/{h.chapter_id}/download-source?include_recovery=true"
                )
                assert response.status_code == 200
                data = response.json()["data"]
                assert data["series_id"] == series_id and data["chapter_remote_id"] == remote
                assert data["original_url"] == (
                    None if sid in FMD2_RETIRED_SOURCES else "https://capibaratraductor.com" + path
                )
        finally:
            await online.close()
            await h.close()
            await bridge.close()

    asyncio.run(run())
