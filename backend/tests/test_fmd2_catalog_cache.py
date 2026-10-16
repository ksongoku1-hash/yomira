import asyncio
from pathlib import Path
from typing import cast

import httpx
import pytest
from test_fmd2_connector import _connector, _fmd_reference  # pyright: ignore[reportPrivateUsage]

from app.connectors.errors import SourceConnectorError
from app.fmd2_bridge import FMD2_SOURCE_ID


def _catalog(reference: str, *, invalid: bool = False) -> dict[str, object]:
    return {
        "manga": {"manga_ref": reference.lower(), "source_id": FMD2_SOURCE_ID, "title": "Fixture"},
        "chapters": [
            {
                "chapter_ref": _fmd_reference("/en/fixture/1").lower(),
                "title": "1",
                "ordinal": 0 if invalid else 1,
            }
        ],
    }


def test_catalog_is_shared_between_concurrent_metadata_chapters_and_reader_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        reference = _fmd_reference("/en/fixture/list")
        requests: list[str] = []
        clock = 10.0

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            await asyncio.sleep(0)
            return httpx.Response(200, json=_catalog(reference))

        monkeypatch.setattr("app.fmd2_bridge.time.monotonic", lambda: clock)
        connector = _connector(tmp_path, httpx.MockTransport(handler))
        try:
            raw, chapters = await asyncio.gather(
                connector.get_series(reference), connector.get_chapters(reference)
            )
            assert cast(dict[str, object], raw)["title"] == "Fixture" and len(chapters) == 1
            assert len(requests) == 1
            await connector.get_series(reference)
            await connector.get_chapters(reference)
            assert len(requests) == 1
            clock = 70.0
            await connector.get_chapters(reference)
            assert len(requests) == 2
            await connector.invalidate_catalog(reference)
            await connector.get_chapters(reference)
            assert len(requests) == 3
        finally:
            await connector.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["chapter", "metadata"])
def test_catalog_cache_is_bounded_and_never_retains_invalid_chapters(
    tmp_path: Path, failure: str
) -> None:
    async def exercise() -> None:
        requests: list[str] = []
        invalid = True

        def handler(request: httpx.Request) -> httpx.Response:
            reference = request.url.path.removeprefix("/v1/manga/").removesuffix("/chapters")
            requests.append(reference)
            payload = _catalog(reference, invalid=invalid and failure == "chapter")
            if invalid and failure == "metadata":
                payload["manga"] = {"manga_ref": reference.lower(), "source_id": FMD2_SOURCE_ID}
            return httpx.Response(200, json=payload)

        connector = _connector(tmp_path, httpx.MockTransport(handler))
        reference = _fmd_reference("/en/fixture/0")
        try:
            for _ in range(2):
                with pytest.raises(SourceConnectorError):
                    await connector.get_chapters(reference)
            assert len(requests) == 2
            invalid = False
            for index in range(17):
                await connector.get_series(_fmd_reference(f"/en/fixture/{index}"))
            assert len(requests) == 19
            await connector.get_series(_fmd_reference("/en/fixture/16"))
            assert len(requests) == 19
            await connector.get_series(reference)
            assert len(requests) == 20
        finally:
            await connector.close()

    asyncio.run(exercise())
