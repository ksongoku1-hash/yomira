import asyncio
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import cast
from urllib.parse import quote

import httpx
import pytest

from app.connectors.builtins import create_connector_registry
from app.factory import create_app
from app.fmd2_bridge import FMD2_METADATA, FMD2_SOURCE_ID, Fmd2Connector
from app.runtime import RuntimeState
from app.secrets.vault import WindowsDpapiSecretVault
from app.services.cover_assets import CoverAssetService
from app.settings import Settings

MANGA = "fmd2:v1:" + (FMD2_SOURCE_ID + "\n/en/action/example/list").encode().hex()
SOURCE_COVER = "https://swebtoon-phinf.pstatic.net/example.jpg"
CATALOG_COVER = "https://uploads.mangadex.org/covers/example/cover.jpg"
PREFIX = "/api/v1/connectors/" + FMD2_METADATA.connector_id


class CatalogCovers:
    def __init__(self, *, stall: bool = False) -> None:
        self.calls: list[str] = []
        self.stall = stall

    async def catalog_cover(self, title: str) -> str:
        self.calls.append(title)
        if self.stall:
            await asyncio.Event().wait()
        return CATALOG_COVER


def test_search_card_cover_does_not_queue_a_full_source_catalog(tmp_path: Path) -> None:
    async def run() -> None:
        requests: list[str] = []

        async def stalled(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            await asyncio.Event().wait()
            raise AssertionError("A thumbnail must not request a full source catalog")

        bridge = Fmd2Connector(
            tmp_path / "fmd.exe",
            tmp_path / "stage",
            api_client=httpx.AsyncClient(
                base_url="http://127.0.0.1:18873", transport=httpx.MockTransport(stalled)
            ),
        )
        app = create_app(Settings(app_env="test", data_root=tmp_path))
        runtime = cast(RuntimeState, app.state.runtime)
        runtime.connector_registry = create_connector_registry(
            Settings(app_env="test"), WindowsDpapiSecretVault(tmp_path), fmd2_connector=bridge
        )
        covers = CatalogCovers()
        runtime.cover_asset_service = cast(CoverAssetService, covers)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                response = await asyncio.wait_for(
                    client.get(
                        PREFIX + "/series/" + quote(MANGA, safe="") + "/cover",
                        params={"title": "Example"},
                    ),
                    0.5,
                )
                assert response.status_code == 200
                assert response.json()["data"]["cover_url"] == CATALOG_COVER
                assert covers.calls == ["Example"] and requests == []
        finally:
            await bridge.close()

    asyncio.run(run())


@pytest.mark.parametrize("cached", [False, True, "expired"])
def test_source_cover_is_preserved_for_fichas_and_fresh_cached_search_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cached: bool | str,
) -> None:
    async def run() -> None:
        requests: list[str] = []

        def metadata(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            return httpx.Response(
                200,
                json={
                    "manga": {
                        "manga_ref": MANGA,
                        "source_id": FMD2_SOURCE_ID,
                        "title": "Example",
                        "cover_url": SOURCE_COVER,
                    }
                },
            )

        bridge = Fmd2Connector(
            tmp_path / "fmd.exe",
            tmp_path / "stage",
            api_client=httpx.AsyncClient(
                base_url="http://127.0.0.1:18873", transport=httpx.MockTransport(metadata)
            ),
        )
        app = create_app(Settings(app_env="test", data_root=tmp_path))
        runtime = cast(RuntimeState, app.state.runtime)
        runtime.connector_registry = create_connector_registry(
            Settings(app_env="test"), WindowsDpapiSecretVault(tmp_path), fmd2_connector=bridge
        )
        covers = CatalogCovers()
        runtime.cover_asset_service = cast(CoverAssetService, covers)
        try:
            if cached:
                await bridge.get_series(MANGA)
            if cached == "expired":
                monkeypatch.setattr(
                    "app.fmd2_bridge.time", SimpleNamespace(monotonic=lambda: monotonic() + 61)
                )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                response = await client.get(
                    PREFIX + "/series/" + quote(MANGA, safe="") + "/cover",
                    params={"title": "Example"} if cached else None,
                )
                assert response.status_code == 200
                assert response.json()["data"]["cover_url"] == (
                    CATALOG_COVER if cached == "expired" else SOURCE_COVER
                )
                assert len(requests) == 1
                assert covers.calls == (["Example"] if cached == "expired" else [])
        finally:
            await bridge.close()

    asyncio.run(run())


@pytest.mark.parametrize("lookup", ["search", "chapters", "cover"])
def test_slow_source_lookup_has_a_retryable_deadline_and_no_queued_request_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lookup: str,
) -> None:
    async def run() -> None:
        cancelled: list[str] = []

        async def stalled(request: httpx.Request) -> httpx.Response:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(request.url.path)
            raise AssertionError("Stalled request must be cancelled")

        bridge = Fmd2Connector(
            tmp_path / "fmd.exe",
            tmp_path / "stage",
            api_client=httpx.AsyncClient(
                base_url="http://127.0.0.1:18873", transport=httpx.MockTransport(stalled)
            ),
        )
        app = create_app(Settings(app_env="test", data_root=tmp_path))
        runtime = cast(RuntimeState, app.state.runtime)
        runtime.connector_registry = create_connector_registry(
            Settings(app_env="test"), WindowsDpapiSecretVault(tmp_path), fmd2_connector=bridge
        )
        runtime.cover_asset_service = cast(CoverAssetService, CatalogCovers(stall=True))
        monkeypatch.setattr("app.routes.connectors.SOURCE_LOOKUP_TIMEOUT_SECONDS", 0.03)
        path = PREFIX + (
            "/search?query=Example"
            if lookup == "search"
            else "/series/"
            + quote(MANGA, safe="")
            + ("/chapters" if lookup == "chapters" else "/cover?title=Example")
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                response = await asyncio.wait_for(client.get(path), 0.5)
                assert response.status_code == 503
                assert response.json()["error"]["code"] == "SRC_REQUEST_TIMEOUT"
                assert response.json()["error"]["retryable"] is True
                assert response.json()["error"]["trace_id"]
            assert len(cancelled) == (0 if lookup == "cover" else 1)
        finally:
            await bridge.close()

    asyncio.run(run())
