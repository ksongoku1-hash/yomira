import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from support.sqlite import open_test_database

from app.domain.chapter import Chapter
from app.domain.metadata import MetadataAuthority, MetadataField
from app.domain.reader_progress import ReaderProgress
from app.domain.series import Series
from app.domain.series_source import SeriesSource
from app.domain.source import Source, SourceHealthStatus
from app.factory import create_app
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.reader_progress import reader_progress_to_row
from app.persistence.mappers.series_source import series_source_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.storage_gate import StorageGateClosedError
from app.runtime import RuntimeState
from app.services.library import LibraryService
from app.services.local_import import LocalImportService
from app.services.metadata import MetadataService
from app.settings import Settings

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def test_library_search_and_global_order_are_bounded_and_validated(tmp_path: Path) -> None:
    target_app, database, library = _configured_app(tmp_path)
    try:
        for title in ("Zulu", "Bravo", "Alfa"):
            library.add_series(Series.new_local(title, title, now=NOW))
        response = get("/api/v1/library?limit=1&offset=1&q=a&sort=title", target_app)
        assert response.status_code == 200, response.text
        assert [item["display_title"] for item in response.json()["data"]] == ["Bravo"]
        assert response.json()["meta"] == {"limit": 1, "offset": 1, "total": 2}
        for invalid in ("sort=unknown", "q=" + "a" * 201):
            error = get("/api/v1/library?" + invalid, target_app)
            assert error.status_code == 422
            assert error.json()["error"]["trace_id"] == error.headers["x-trace-id"]
    finally:
        database.close()


def test_folder_open_has_fixed_targets_and_a_readable_error(tmp_path: Path) -> None:
    app, database, _library = _configured_app(tmp_path)
    service = Mock(spec=LocalImportService)
    app.state.runtime.local_import_service = service

    async def exercise() -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            assert (await client.post("/api/v1/library/folders/inbox/open")).status_code == 204
            service.open_folder.assert_called_once_with("inbox")
            assert (await client.post("/api/v1/library/folders/arbitrary/open")).status_code == 422
            assert (
                await client.post(
                    "/api/v1/library/folders/inbox/open",
                    headers={"Origin": "https://outside.example"},
                )
            ).status_code == 403
            assert service.open_folder.call_count == 1
            service.open_folder.side_effect = PermissionError()
            response = await client.post("/api/v1/library/folders/downloads/open")
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "LIB_PATH_UNAVAILABLE"
            assert "Copia su ruta" in response.json()["error"]["message"]
            service.open_folder.side_effect = StorageGateClosedError("FILESYSTEM_DISK_FULL")
            response = await client.post("/api/v1/library/folders/inbox/open")
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "LIB_PATH_UNAVAILABLE"

    try:
        asyncio.run(exercise())
    finally:
        database.close()


def _configured_app(tmp_path: Path) -> tuple[FastAPI, SQLiteDatabase, LibraryService]:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    library_service = LibraryService(database)
    target_app = create_app(Settings(app_env="dev"))
    runtime = cast(RuntimeState, target_app.state.runtime)
    runtime.database = database
    runtime.library_service = library_service
    runtime.metadata_service = MetadataService(database)
    return target_app, database, library_service


async def _get(path: str, target_app: FastAPI) -> Response:
    transport = ASGITransport(app=target_app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.get(path)


async def _request(
    method: str,
    path: str,
    target_app: FastAPI,
    *,
    json: object | None = None,
) -> Response:
    transport = ASGITransport(app=target_app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.request(method, path, json=json)


def get(path: str, target_app: FastAPI) -> Response:
    return asyncio.run(_get(path, target_app))


def test_library_list_returns_stable_order_and_pagination_envelope(
    tmp_path: Path,
) -> None:
    target_app, database, library_service = _configured_app(tmp_path)
    series_items = [
        Series.new_local("Zulu", "Display Z", now=NOW),
        Series.new_local("Alpha", "Display A1", now=NOW),
        Series.new_local("Alpha", "Display A2", now=NOW),
    ]
    try:
        for series in series_items:
            library_service.add_series(series)

        response = get("/api/v1/library", target_app)

        assert response.status_code == 200
        body = response.json()
        assert [item["canonical_title"] for item in body["data"]] == [
            "Alpha",
            "Alpha",
            "Zulu",
        ]
        same_title_ids = sorted(
            item["series_id"] for item in body["data"] if item["canonical_title"] == "Alpha"
        )
        assert [
            item["series_id"] for item in body["data"] if item["canonical_title"] == "Alpha"
        ] == same_title_ids
        assert body["meta"] == {"limit": 100, "offset": 0, "total": 3}
        assert response.headers["x-trace-id"]
    finally:
        database.close()


def test_library_list_applies_requested_limit_and_offset(tmp_path: Path) -> None:
    target_app, database, library_service = _configured_app(tmp_path)
    for title in ("Alpha", "Bravo", "Charlie"):
        library_service.add_series(Series.new_local(title, title, now=NOW))
    try:
        response = get("/api/v1/library?limit=1&offset=1", target_app)

        assert response.status_code == 200
        assert [item["canonical_title"] for item in response.json()["data"]] == ["Bravo"]
        assert response.json()["meta"] == {"limit": 1, "offset": 1, "total": 3}
    finally:
        database.close()


def test_library_list_filters_and_counts_local_reading_progress(tmp_path: Path) -> None:
    target_app, database, library_service = _configured_app(tmp_path)
    completed = Series.new_local("Completed", "Completed", status="ongoing", now=NOW)
    unread = Series.new_local("Unread", "Unread", status="completed", now=NOW)
    library_service.add_series(completed)
    library_service.add_series(unread)
    with database.write_session() as session:
        for series, percentage in ((completed, Decimal("100")), (unread, None)):
            chapter = Chapter.new_local(
                series.series_id,
                "Chapter 1",
                "Chapter 1",
                chapter_sort_key=(1,),
            )
            session.add(chapter_to_row(chapter))
            if percentage is not None:
                session.add(
                    reader_progress_to_row(
                        ReaderProgress.new_local(
                            series.series_id,
                            chapter.chapter_id,
                            0,
                            percentage,
                            updated_at=NOW,
                        )
                    )
                )
    try:
        response = get("/api/v1/library?reading_status=completed", target_app)

        assert response.status_code == 200
        assert [item["canonical_title"] for item in response.json()["data"]] == ["Completed"]
        assert response.json()["meta"] == {"limit": 100, "offset": 0, "total": 1}
    finally:
        database.close()


def test_library_list_rejects_an_unknown_reading_filter_with_traced_api_error(
    tmp_path: Path,
) -> None:
    target_app, database, _library_service = _configured_app(tmp_path)
    try:
        response = get("/api/v1/library?reading_status=finished", target_app)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "API_VALIDATION_FAILED"
        assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]
        assert response.json()["error"]["details"] == [
            {"field": "query.reading_status", "issue": "enum"}
        ]
    finally:
        database.close()


def test_library_list_rejects_unbounded_page_size_with_sanitized_error(
    tmp_path: Path,
) -> None:
    target_app, database, _library_service = _configured_app(tmp_path)
    try:
        response = get("/api/v1/library?limit=501", target_app)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "API_VALIDATION_FAILED"
        assert response.json()["error"]["details"] == [
            {"field": "query.limit", "issue": "less_than_equal"}
        ]
        assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]
        assert response.json()["error"]["message"] == (
            "La solicitud no cumple el contrato de la API."
        )
        assert set(response.json()["error"]) == {
            "code",
            "message",
            "trace_id",
            "retryable",
            "details",
        }
    finally:
        database.close()


def test_series_detail_returns_entity_and_missing_series_is_traceable_404(
    tmp_path: Path,
) -> None:
    target_app, database, library_service = _configured_app(tmp_path)
    series = Series.new_local(
        "Exact title  ",
        "Título visible",
        description="Description",
        language="es",
        now=NOW,
    )
    library_service.add_series(series)
    try:
        detail_response = get(f"/api/v1/series/{series.series_id}", target_app)
        missing_response = get(f"/api/v1/series/{uuid4()}", target_app)

        assert detail_response.status_code == 200
        assert detail_response.json()["data"]["series_id"] == str(series.series_id)
        assert detail_response.json()["data"]["canonical_title"] == "Exact title  "
        assert detail_response.json()["data"]["created_at"] == "2026-09-26T12:00:00Z"
        assert detail_response.json()["meta"] == {}
        resolved = {item["field"]: item for item in detail_response.json()["data"]["metadata"]}
        assert resolved["description"]["value"] == "Description"
        assert resolved["description"]["authority"] == MetadataAuthority.LOCAL_CANONICAL.value
        assert resolved["description"]["has_user_override"] is False
        assert missing_response.status_code == 404
        assert missing_response.json()["error"]["code"] == "API_REQUEST_REJECTED"
        assert (
            missing_response.json()["error"]["trace_id"] == missing_response.headers["x-trace-id"]
        )
    finally:
        database.close()


def test_metadata_override_api_resolves_and_clears_a_user_value(tmp_path: Path) -> None:
    target_app, database, library_service = _configured_app(tmp_path)
    series = Series.new_local(
        "Metadata API",
        "Metadata API",
        description="Local description",
        now=NOW,
    )
    library_service.add_series(series)
    detail_path = f"/api/v1/series/{series.series_id}"
    override_path = f"{detail_path}/metadata-overrides"
    try:
        set_response = asyncio.run(
            _request(
                "PUT",
                override_path,
                target_app,
                json={"field": MetadataField.DESCRIPTION.value, "value": ""},
            )
        )
        detail_response = get(detail_path, target_app)
        assert set_response.status_code == 204, set_response.text
        description = next(
            item
            for item in detail_response.json()["data"]["metadata"]
            if item["field"] == MetadataField.DESCRIPTION.value
        )
        assert description["value"] == ""
        assert description["authority"] == MetadataAuthority.USER_OVERRIDE.value
        assert description["has_user_override"] is True

        clear_response = asyncio.run(
            _request(
                "DELETE",
                f"{override_path}/{MetadataField.DESCRIPTION.value}",
                target_app,
            )
        )
        after_clear = get(detail_path, target_app)
        description_after_clear = next(
            item
            for item in after_clear.json()["data"]["metadata"]
            if item["field"] == MetadataField.DESCRIPTION.value
        )
        assert clear_response.status_code == 204
        assert description_after_clear["value"] == "Local description"
        assert description_after_clear["authority"] == MetadataAuthority.LOCAL_CANONICAL.value
    finally:
        database.close()


def test_metadata_api_preserves_source_candidates_and_conflicts(tmp_path: Path) -> None:
    target_app, database, library_service = _configured_app(tmp_path)
    series = Series.new_local("Metadata conflict", "Metadata conflict", now=NOW)
    sources = (
        Source.new_local("source-a", "1.0", "Source A", True, SourceHealthStatus.HEALTHY),
        Source.new_local("source-b", "1.0", "Source B", True, SourceHealthStatus.HEALTHY),
    )
    associations = tuple(
        SeriesSource.new_local(
            series.series_id,
            source.source_id,
            remote_id=f"work-{index}",
            remote_status=remote_status,
        )
        for index, (source, remote_status) in enumerate(
            zip(sources, ("ongoing", "completed"), strict=True)
        )
    )
    library_service.add_series(series)
    with database.write_session() as session:
        for source in sources:
            session.add(source_to_row(source))
        session.flush()
        for association in associations:
            session.add(series_source_to_row(association))
    try:
        response = get(f"/api/v1/series/{series.series_id}", target_app)
        status_metadata = next(
            item
            for item in response.json()["data"]["metadata"]
            if item["field"] == MetadataField.STATUS.value
        )
        assert status_metadata["value"] is None
        assert status_metadata["authority"] is None
        assert status_metadata["source_conflict"] is True
        assert {item["value"] for item in status_metadata["source_values"]} == {
            "ongoing",
            "completed",
        }
        assert {
            item["source_display_name"]: item["value"] for item in status_metadata["source_values"]
        } == {"Source A": "ongoing", "Source B": "completed"}
        assert len(status_metadata["source_values"]) == 2

        save_response = asyncio.run(
            _request(
                "PUT",
                f"/api/v1/series/{series.series_id}/metadata-overrides",
                target_app,
                json={"field": MetadataField.STATUS.value, "value": "completed"},
            )
        )
        resolved_response = get(f"/api/v1/series/{series.series_id}", target_app)
        resolved_status = next(
            item
            for item in resolved_response.json()["data"]["metadata"]
            if item["field"] == MetadataField.STATUS.value
        )
        assert save_response.status_code == 204, save_response.text
        assert resolved_status["value"] == "completed"
        assert resolved_status["authority"] == MetadataAuthority.USER_OVERRIDE.value
        assert resolved_status["source_conflict"] is True
    finally:
        database.close()


def test_library_api_is_unavailable_until_a_service_is_injected() -> None:
    target_app = create_app(Settings(app_env="dev"))

    response = get("/api/v1/library", target_app)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SYS_INTERNAL_ERROR"
    assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]
