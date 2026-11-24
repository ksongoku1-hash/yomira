from __future__ import annotations

import asyncio
import io
import threading
import time
import zipfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_cbr_builder import (
    _Fixture,  # pyright: ignore[reportPrivateUsage]
    _fixture,  # pyright: ignore[reportPrivateUsage]
    _private_member,  # pyright: ignore[reportPrivateUsage]
)

import app.services.series_epub as epub_service
from app.persistence.mappers.chapter_cbz_asset import chapter_cbz_asset_to_row
from app.persistence.models.chapter_cbz_asset import ChapterCbzAssetRow
from app.persistence.repositories.chapter_cbz_asset import ChapterCbzAssetRepository
from app.routes.series_epub import router
from app.services.epub_builder import EpubBuildCancelled, EpubBuilder
from app.services.series_epub import SeriesEpubService


def _service(fixture: _Fixture) -> SeriesEpubService:
    return SeriesEpubService(
        database=fixture.database,
        reader=_private_member(fixture.service, "_reader"),
        library=_private_member(fixture.service, "_library"),
        library_root_identity=_private_member(fixture.service, "_identity"),
        builder=EpubBuilder(
            reader=_private_member(fixture.service, "_reader"),
            disk_budget=_private_member(fixture.service, "_disk_budget"),
            library_root_identity=_private_member(fixture.service, "_identity"),
            storage_gate=fixture.database.storage_gate,
        ),
    )


def _app(service: SeriesEpubService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        yield
        await service.close()

    app = FastAPI(lifespan=lifespan)
    app.state.runtime = SimpleNamespace(series_epub_service=service)
    app.include_router(router)
    return app


def _wait(client: Any, route: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(route)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        if data["status"] != "building":
            return data
        time.sleep(0.01)
    raise AssertionError("EPUB preparation did not finish")


def test_manual_epub_reuses_one_book_updates_all_chapters_and_survives_restart(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    service = _service(fixture)
    route = f"/api/v1/series/{fixture.series.series_id}/epub"
    originals = [path.read_bytes() for path in fixture.cbz_paths]
    repository = ChapterCbzAssetRepository()
    with fixture.database.write_session() as session:
        linked = repository.get_for_chapter(session, fixture.chapter_ids[0])
        assert linked is not None
        first_link = linked[0]
        row = session.get(ChapterCbzAssetRow, str(fixture.chapter_ids[0]))
        assert row is not None
        session.delete(row)
    try:
        with TestClient(_app(service)) as client_context:
            client: Any = client_context
            assert client.get(route).json()["data"]["status"] == "not_created"
            assert client.post(route).status_code == 202
            first = _wait(client, route)
            assert first["status"] == "ready" and first["chapter_count"] == 1
            payload = client.get(route + "/file")
            assert payload.headers["content-type"] == "application/epub+zip"
            assert payload.headers["cache-control"] == "no-store"
            old = payload.content
            with zipfile.ZipFile(io.BytesIO(old)) as archive:
                assert archive.testzip() is None
            with fixture.database.write_session() as session:
                session.add(chapter_cbz_asset_to_row(first_link))
            assert client.get(route).json()["data"]["status"] == "outdated"
            assert client.post(route).status_code == 202
            updated = _wait(client, route)
            assert updated["status"] == "ready" and updated["chapter_count"] == 2
            assert updated["filename"] == first["filename"]
            assert client.get(route + "/file").content != old
            assert len(list(tmp_path.rglob("*.epub"))) == 1
            assert [path.read_bytes() for path in fixture.cbz_paths] == originals
        with TestClient(_app(_service(fixture))) as client_context:
            client: Any = client_context
            assert client.get(route).json()["data"]["status"] == "ready"
            assert client.get(route + "/file").status_code == 200
    finally:
        fixture.database.close()


def test_failed_rebuild_keeps_previous_epub_downloadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    service = _service(fixture)
    route = f"/api/v1/series/{fixture.series.series_id}/epub"
    try:
        with TestClient(_app(service)) as client_context:
            client: Any = client_context
            client.post(route)
            assert _wait(client, route)["status"] == "ready"
            previous = client.get(route + "/file").content

            def fail(*_args: object, **_kwargs: object) -> None:
                raise OSError("simulated full disk")

            monkeypatch.setattr(EpubBuilder, "build", fail)
            assert client.post(route).status_code == 202
            state = _wait(client, route)
            assert state["status"] == "failed" and state["has_file"] is True
            assert state["error"]
            assert client.get(route + "/file").content == previous
    finally:
        fixture.database.close()


def test_unknown_series_cannot_create_or_download_book(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    try:
        with TestClient(_app(_service(fixture))) as client_context:
            client: Any = client_context
            route = f"/api/v1/series/{uuid4()}/epub"
            assert client.get(route).status_code == 404
            assert client.post(route).status_code == 404
            assert client.get(route + "/file").status_code == 404
    finally:
        fixture.database.close()


def test_concurrent_requests_build_once_and_shutdown_waits_for_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    service = _service(fixture)
    started, finished = threading.Event(), threading.Event()
    calls: list[object] = []

    def build(*_args: object, **kwargs: object) -> None:
        calls.append(kwargs)
        started.set()
        cancelled = kwargs["cancelled"]
        assert callable(cancelled)
        assert service._closing.wait(5)  # pyright: ignore[reportPrivateUsage]
        assert cancelled()
        finished.set()
        raise EpubBuildCancelled()

    monkeypatch.setattr(EpubBuilder, "build", build)

    async def exercise() -> None:
        responses = await asyncio.gather(
            *(service.create(fixture.series.series_id) for _ in range(3))
        )
        assert all(response["status"] == "building" for response in responses)
        assert await asyncio.to_thread(started.wait, 5)
        await service.close()
        assert finished.is_set() and len(calls) == 1
        with pytest.raises(RuntimeError, match="cerrando"):
            await service.create(fixture.series.series_id)

    try:
        asyncio.run(exercise())
    finally:
        fixture.database.close()


def test_unrecognized_epub_in_managed_folder_is_preserved(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    folder = tmp_path / "EPUB" / str(fixture.series.series_id)
    folder.mkdir(parents=True)
    personal = folder / "Libro personal.epub"
    personal.write_bytes(b"personal file that must be preserved")
    try:
        with TestClient(_app(_service(fixture))) as context:
            client: Any = context
            route = f"/api/v1/series/{fixture.series.series_id}/epub"
            assert client.post(route).status_code == 202
            assert _wait(client, route)["status"] == "failed"
            assert personal.read_bytes() == b"personal file that must be preserved"
            assert list(folder.iterdir()) == [personal]
    finally:
        fixture.database.close()


def test_reader_format_upgrade_requires_manual_update_of_previous_book(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    version = epub_service.EPUB_FORMAT_VERSION
    route = f"/api/v1/series/{fixture.series.series_id}/epub"
    try:
        with TestClient(_app(_service(fixture))) as context:
            client: Any = context
            monkeypatch.setattr(epub_service, "EPUB_FORMAT_VERSION", "previous-format")
            client.post(route)
            assert _wait(client, route)["status"] == "ready"
            before = client.get(route + "/file").content
            monkeypatch.setattr(epub_service, "EPUB_FORMAT_VERSION", version)
            assert client.get(route).json()["data"]["status"] == "outdated"
            assert client.get(route + "/file").content == before
            client.post(route)
            assert _wait(client, route)["status"] == "ready"
            assert client.get(route + "/file").content != before
    finally:
        fixture.database.close()
