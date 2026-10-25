import asyncio
import hashlib
import time
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from support.sqlite import open_test_database

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.http_client import ConnectorHttpSession
from app.connectors.mangadex import (
    MANGADEX_COVER_ONLY_ORIGINS,
    MANGADEX_METADATA,
    MANGADEX_NETWORK_ORIGINS,
    MangaDexConnector,
)
from app.connectors.mock import MockConnector
from app.connectors.registry import ConnectorRegistration, ConnectorRegistry
from app.connectors.sandbox import ConnectorHostContext
from app.domain.metadata import MetadataField
from app.domain.series import Series
from app.domain.series_source import SeriesSource
from app.domain.source import Source, SourceHealthStatus
from app.errors import ErrorCode
from app.factory import create_app
from app.persistence.atomic_file_publisher import ADR_0087_RETRY_POLICY, AtomicFilePublisher
from app.persistence.disk_budget import DiskBudgetManager
from app.persistence.mappers.series_source import series_source_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.runtime import RuntimeState
from app.secrets.vault import WindowsDpapiSecretVault
from app.services.cover_assets import CoverAssetService
from app.services.library import LibraryService
from app.services.metadata import MetadataService
from app.services.reader_asset import ReaderAssetLimits, ReaderAssetService
from app.services.thumbnails import ThumbnailVariant
from app.settings import Settings


def _fixture(
    tmp_path: Path,
    body: bytes,
    mime: str = "image/png",
    *,
    connector_id: str = "yomira.mock",
    catalog: bool = False,
    source_cover: bool = True,
    cover_url: str = "https://source.example/cover.png",
    oversized_original: bool = False,
):
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    library = LibraryService(database)
    series = Series.new_local("Manga", "Manga")
    library.add_series(series)
    source = Source.new_local(connector_id, "1.0.0", "Fixture", True, SourceHealthStatus.HEALTHY)
    association = SeriesSource.new_local(
        series.series_id,
        source.source_id,
        remote_id="mock-series-1",
        remote_cover_url=cover_url if source_cover else None,
    )
    with database.write_session() as session:
        session.add(source_to_row(source))
        session.flush()
        session.add(series_source_to_row(association))
    requests: list[httpx.Request] = []
    manga_id, cover_id = str(uuid4()), str(uuid4())
    catalog_item: dict[str, object] = {
        "id": manga_id,
        "type": "manga",
        "attributes": {
            "title": {"en": "Manga"},
            "year": None,
            "description": {"es": "Sinopsis del catálogo"},
        },
        "relationships": [
            {"id": cover_id, "type": "cover_art", "attributes": {"fileName": f"{cover_id}.jpg"}}
        ],
    }

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.mangadex.org":
            is_search = request.url.path == "/manga"
            return httpx.Response(
                200,
                json={
                    "result": "ok",
                    "response": "collection" if is_search else "entity",
                    "data": [catalog_item] if is_search else catalog_item,
                    "total": 1,
                    "limit": 100,
                    "offset": 0,
                },
            )
        headers = {"content-type": mime}
        if oversized_original and not request.url.path.endswith(".512.jpg"):
            headers["content-length"] = str(7 * 1024 * 1024)
        return httpx.Response(200, headers=headers, content=body)

    http = ConnectorHttpSession(test_transport=httpx.MockTransport(respond))
    metadata = MockConnector().metadata
    parsed_cover = urlsplit(cover_url)
    origins = frozenset({f"{parsed_cover.scheme}://{parsed_cover.netloc}"})
    if connector_id == "yomira.fmd2.webtoons":
        origins |= frozenset({"https://www.manhwaweb.com"})
    if connector_id != metadata.connector_id:
        metadata = replace(metadata, connector_id=connector_id)
    registrations = [ConnectorRegistration(metadata, lambda _ctx: MockConnector(), origins)]
    if catalog:

        def create_catalog(ctx: ConnectorHostContext) -> MangaDexConnector:
            assert ctx.http_client is not None
            return MangaDexConnector(ctx.http_client)

        registrations.append(
            ConnectorRegistration(
                MANGADEX_METADATA,
                create_catalog,
                MANGADEX_NETWORK_ORIGINS,
                MANGADEX_COVER_ONLY_ORIGINS,
            )
        )
    registry = ConnectorRegistry(
        context=ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(tmp_path)),
        registrations=registrations,
        http_client_factory=http.scoped_client,
    )
    reader = ReaderAssetService(
        database,
        data_root_identity=database.data_root_identity,
        limits=ReaderAssetLimits(
            max_cbz_bytes=2_147_483_648, max_page_bytes=67_108_864, chunk_bytes=65_536
        ),
    )
    service = CoverAssetService(
        database=database,
        registry=registry,
        http_session=http,
        reader=reader,
        disk_budget=DiskBudgetManager(database),
        publisher=AtomicFilePublisher(
            data_root_identity=database.data_root_identity,
            retry_policy=ADR_0087_RETRY_POLICY,
            sleep=time.sleep,
            clock=time.monotonic,
            storage_gate=database.storage_gate,
        ),
        admission_allowed=lambda: True,
    )
    return database, series, service, http, requests


def _png() -> bytes:
    with Image.new("RGB", (240, 360), "navy") as image, BytesIO() as output:
        image.save(output, "PNG")
        return output.getvalue()


def test_manhwa_cover_uses_its_public_site_referer(tmp_path: Path) -> None:
    database, series, service, http, requests = _fixture(
        tmp_path, _png(), connector_id="yomira.fmd2.webtoons",
        cover_url="https://img2mw.xyz/manhwas/fixture/cover.webp",
    )
    async def exercise() -> None:
        assert await service.ensure_cover(series.series_id)
        assert requests[0].headers["referer"] == "https://www.manhwaweb.com/"
        await http.close()
    try:
        asyncio.run(exercise())
        saved = LibraryService(database).get_series(series.series_id)
        assert saved is not None
        assert saved.cover_asset_id is not None
    finally:
        database.close()


def test_cover_is_verified_local_original_and_offline_webp(tmp_path: Path) -> None:
    original = _png()
    database, series, service, http, requests = _fixture(tmp_path, original)

    async def exercise() -> None:
        assert await service.ensure_cover(series.series_id)
        current = LibraryService(database).get_series(series.series_id)
        assert current is not None and current.cover_asset_id is not None
        paths = tuple((tmp_path / "library" / "covers").glob("*"))
        assert len(paths) == 1 and paths[0].read_bytes() == original
        assert await service.ensure_cover(series.series_id) is False
        await http.close()
        thumbnail = await service.thumbnail(series.series_id, ThumbnailVariant.SMALL)
        assert thumbnail is not None and thumbnail.mime_type == "image/webp"
        assert thumbnail.width <= 160 and thumbnail.height <= 240
        assert thumbnail.source_sha256 == hashlib.sha256(original).hexdigest()
        assert len(requests) == 1
        assert (await service.thumbnail(series.series_id, ThumbnailVariant.SMALL)) == thumbnail

    try:
        asyncio.run(exercise())
    finally:
        database.close()


def test_cover_fetch_does_not_forward_referer_or_credentials(tmp_path: Path) -> None:
    database, series, service, http, requests = _fixture(
        tmp_path,
        _png(),
        connector_id="fixture.source",
    )

    async def exercise() -> None:
        assert await service.ensure_cover(series.series_id)
        await http.close()

    try:
        asyncio.run(exercise())
        assert len(requests) == 1
        assert "referer" not in requests[0].headers
        assert "cookie" not in requests[0].headers
        assert "authorization" not in requests[0].headers
    finally:
        database.close()


def test_null_override_blocks_download_and_preserves_source(tmp_path: Path) -> None:
    database, series, service, http, requests = _fixture(tmp_path, _png())
    MetadataService(database).set_user_override(series.series_id, MetadataField.COVER, None)

    async def exercise() -> None:
        assert await service.ensure_cover(series.series_id) is False
        assert await service.thumbnail(series.series_id, ThumbnailVariant.SMALL) is None
        await http.close()

    try:
        asyncio.run(exercise())
        assert not requests
        resolutions = MetadataService(database).resolve_series(series.series_id)
        assert resolutions is not None
        assert next(r for r in resolutions if r.field is MetadataField.COVER).has_user_override
    finally:
        database.close()


@pytest.mark.parametrize(
    "mime,body",
    [("text/html", b"<html>challenge</html>"), ("image/png", b"\x89PNG\r\n\x1a\ntruncated")],
)
def test_invalid_cover_preserves_part_without_indexing(
    tmp_path: Path, mime: str, body: bytes
) -> None:
    database, series, service, http, _requests = _fixture(tmp_path, body, mime)

    async def exercise() -> None:
        with pytest.raises(ValueError):
            await service.ensure_cover(series.series_id)
        await http.close()

    try:
        asyncio.run(exercise())
        current = LibraryService(database).get_series(series.series_id)
        assert current is not None and current.cover_asset_id is None
        parts = tuple((tmp_path / "temp" / "covers").rglob("*.part"))
        assert len(parts) == 1 and parts[0].read_bytes() == body
        assert not tuple((tmp_path / "library" / "covers").glob("*"))
    finally:
        database.close()


def test_local_cover_endpoint_etag_and_metadata_summary(tmp_path: Path) -> None:
    database, series, service, http, _requests = _fixture(tmp_path, _png())
    metadata = MetadataService(database)
    sources = metadata.list_sources(series.series_id)
    association_id = sources[0].series_source_id
    metadata.set_source_metadata(association_id, MetadataField.AUTHOR, "Autor de la fuente")
    metadata.set_source_metadata(association_id, MetadataField.GENRES, ("Acción", "Drama"))
    metadata.set_source_metadata(association_id, MetadataField.DESCRIPTION, "Sinopsis completa")
    app = create_app(Settings(app_env="test"))
    runtime = cast(RuntimeState, app.state.runtime)
    runtime.database = database
    runtime.library_service = LibraryService(database)
    runtime.metadata_service = metadata
    runtime.cover_asset_service = service

    async def exercise() -> None:
        assert await service.ensure_cover(series.series_id)
        await http.close()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(f"/api/v1/series/{series.series_id}/cover?variant=small")
            assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
            assert response.headers["cache-control"] == "private, no-cache"
            assert response.headers["x-content-type-options"] == "nosniff"
            cached = await client.get(
                f"/api/v1/series/{series.series_id}/cover?variant=small",
                headers={"if-none-match": response.headers["etag"]},
            )
            assert cached.status_code == 304 and not cached.content
            for route in (
                "/api/v1/library",
                "/api/v1/library/recent",
                f"/api/v1/series/{series.series_id}",
            ):
                response_body = cast(dict[str, object], (await client.get(route)).json())
                payload = response_body["data"]
                if isinstance(payload, list):
                    assert payload
                    item = cast(dict[str, object], payload[0])
                else:
                    item = cast(dict[str, object], payload)
                assert item["author"] == "Autor de la fuente"
                assert item["genres"] == ["Acción", "Drama"]
                assert item["description"] == "Sinopsis completa"
                assert item["cover_asset_id"] is not None
            metadata.set_user_override(series.series_id, MetadataField.DESCRIPTION, None)
            metadata.set_local_value(series.series_id, MetadataField.AUTHOR, "Autor local")
            listing = cast(dict[str, object], (await client.get("/api/v1/library")).json())
            items = cast(list[dict[str, object]], listing["data"])
            item = items[0]
            assert item["description"] is None and item["author"] == "Autor local"

    try:
        asyncio.run(exercise())
    finally:
        database.close()


def test_corrupt_local_original_is_not_served_even_when_thumbnail_cached(tmp_path: Path) -> None:
    database, series, service, http, _requests = _fixture(tmp_path, _png())

    async def exercise() -> None:
        assert await service.ensure_cover(series.series_id)
        assert await service.thumbnail(series.series_id, ThumbnailVariant.SMALL)
        path = next((tmp_path / "library" / "covers").glob("*"))
        path.write_bytes(b"corrupted")
        from app.services.reader_asset import ReaderAssetError

        with pytest.raises(ReaderAssetError):
            await service.thumbnail(series.series_id, ThumbnailVariant.SMALL)
        await http.close()

    try:
        asyncio.run(exercise())
    finally:
        database.close()


def test_catalog_thumbnail_recovers_oversized_primary_cover(tmp_path: Path) -> None:
    database, series, service, http, requests = _fixture(
        tmp_path, _png(), catalog=True, source_cover=True, oversized_original=True
    )

    async def exercise() -> None:
        assert await service.ensure_cover(series.series_id)
        assert requests[-1].url.host == "uploads.mangadex.org"
        assert requests[-1].url.path.endswith(".512.jpg")
        assert any(request.url.host == "source.example" for request in requests)
        assert await service.thumbnail(series.series_id, ThumbnailVariant.MEDIUM) is not None
        assert MetadataService(database).list_sources(series.series_id)[0].remote_cover_url
        await http.close()

    try:
        asyncio.run(exercise())
    finally:
        database.close()


def test_external_metadata_fallback_imports_verified_cover_and_serves_it_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _png()
    database, series, service, http, requests = _fixture(
        tmp_path,
        original,
        catalog=True,
        source_cover=False,
    )
    metadata = MetadataService(database)
    app = create_app(Settings(app_env="test"))
    runtime = cast(RuntimeState, app.state.runtime)
    runtime.database = database
    runtime.library_service = LibraryService(database)
    runtime.metadata_service = metadata
    runtime.cover_asset_service = service

    runtime.connector_registry = service._registry  # pyright: ignore[reportPrivateUsage]

    async def source_metadata(_self: MockConnector, _remote_id: str) -> object:
        return {"title": "Manga"}

    monkeypatch.setattr(MockConnector, "get_series", source_metadata)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            path = "/api/v1/connectors/yomira.mock/series/mock-series-1/cover"
            preview = await client.get(path)
            assert preview.status_code == 200
            assert preview.json()["data"]["cover_url"].startswith(
                "https://uploads.mangadex.org/covers/"
            )
            assert (await client.get(path)).json() == preview.json()
            assert len(requests) == 1
            assert len(metadata.list_sources(series.series_id)) == 1

            async def source_unavailable(_self: MockConnector, _remote_id: str) -> object:
                raise SourceConnectorError(
                    code=ErrorCode.SRC_CONNECTOR_FAILED,
                    operation=SourceConnectorOperation.GET_SERIES,
                    retryable=True,
                )

            monkeypatch.setattr(MockConnector, "get_series", source_unavailable)
            assert (await client.get(path)).status_code == 503
            assert (await client.get(path, params={"title": "Manga"})).json() == preview.json()
            assert (await client.get(path, params={"title": "x" * 201})).status_code == 422
        assert await service.ensure_cover(series.series_id)
        assert len(requests) == 2
        assert requests[0].url.host == "api.mangadex.org"
        assert requests[1].url.host == "uploads.mangadex.org"
        assert all(r.headers["user-agent"].startswith("Yomira/") for r in requests)
        assert all("cookie" not in r.headers and "authorization" not in r.headers for r in requests)
        saved = tuple((tmp_path / "library" / "covers").glob("*"))
        assert len(saved) == 1 and saved[0].read_bytes() == original
        sources = metadata.list_sources(series.series_id)
        assert len(sources) == 2
        external = next(
            s for s in sources if s.source_connector_id == MANGADEX_METADATA.connector_id
        )
        assert external.remote_url == f"https://mangadex.org/title/{external.remote_id}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            base = f"/api/v1/series/{series.series_id}"
            candidates = await client.get(f"{base}/metadata/external-candidates")
            assert candidates.status_code == 200
            assert candidates.json() == {
                "data": [
                    {
                        "remote_id": external.remote_id,
                        "title": "Manga",
                        "year": None,
                        "author": None,
                    }
                ],
                "meta": {"provider": "MangaDex", "total": 1, "limit": 10},
            }
            applied = await client.post(
                f"{base}/metadata/external", json={"remote_id": external.remote_id}
            )
            assert applied.status_code == 200
            assert applied.json()["data"]["description"] == "Sinopsis del catálogo"
            assert len(metadata.list_sources(series.series_id)) == 2
            for bad in (
                {"remote_id": "https://evil.example/?secret=token"},
                {"remote_id": external.remote_id, "url": "https://evil.example"},
            ):
                response = await client.post(f"{base}/metadata/external", json=bad)
                assert response.status_code == 422 and "secret" not in response.text
            other = Series.new_local("Other", "Other")
            LibraryService(database).add_series(other)
            conflict = await client.post(
                f"/api/v1/series/{other.series_id}/metadata/external",
                json={"remote_id": external.remote_id},
            )
            assert conflict.status_code == 409 and conflict.headers["x-trace-id"]
            assert metadata.list_sources(other.series_id) == ()
            await http.close()
            cover = await client.get(f"{base}/cover?variant=small")
            assert cover.status_code == 200 and cover.headers["content-type"] == "image/webp"
            assert (await client.get(base)).json()["data"]["cover_asset_id"] is not None

    try:
        asyncio.run(exercise())
    finally:
        database.close()


def test_primary_cover_is_not_replaced_by_catalog(tmp_path: Path) -> None:
    database, series, service, http, requests = _fixture(tmp_path, _png(), catalog=True)
    try:
        assert asyncio.run(service.ensure_cover(series.series_id))
        assert requests[-1].url.host == "source.example"
        assert all(r.url.host != "uploads.mangadex.org" for r in requests)
    finally:
        asyncio.run(http.close())
        database.close()


def test_tmo_public_versioned_cover_is_saved_offline(tmp_path: Path) -> None:
    database, series, service, http, requests = _fixture(
        tmp_path, _png(), cover_url="https://zonatmo.org/storage/proxy/cover.png?v=2"
    )
    try:
        assert asyncio.run(service.ensure_cover(series.series_id))
        assert str(requests[0].url).endswith("?v=2")
        asyncio.run(http.close())
        thumbnail = asyncio.run(service.thumbnail(series.series_id, ThumbnailVariant.SMALL))
        assert thumbnail is not None
    finally:
        asyncio.run(http.close())
        database.close()
