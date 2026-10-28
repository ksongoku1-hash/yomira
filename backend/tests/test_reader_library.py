from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from support.sqlite import open_test_database, reopen_test_database

from app.persistence.database import SQLiteDatabase
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.series import SeriesRow
from app.persistence.models.setting import SettingRow
from app.routes.reader_library import router
from app.runtime import RuntimeState
from app.services.reader_library import (
    READER_LIBRARY_KEY,
    ReaderBookmark,
    ReaderLibraryConflict,
    ReaderLibraryCorrupt,
    ReaderLibraryImport,
    ReaderLibraryService,
    ReaderWorkPreferences,
    ReaderWorkProfile,
)
from app.services.reader_settings import ReaderSettings, ReaderSettingsService

SERIES = UUID("123e4567-e89b-42d3-a456-426614174050")
CHAPTER = UUID("123e4567-e89b-42d3-a456-426614174051")
OTHER_SERIES = UUID("223e4567-e89b-42d3-a456-426614174050")
OTHER_CHAPTER = UUID("223e4567-e89b-42d3-a456-426614174051")
STAMP = "2026-10-25T12:00:00.000Z"
PROFILE = ReaderWorkPreferences(format="manga", mode="single", rtl=True, continuous=False)


def _series(series_id: UUID) -> SeriesRow:
    return SeriesRow(
        series_id=str(series_id),
        canonical_title=str(series_id),
        display_title="Obra",
        created_at_us=0,
        updated_at_us=0,
    )


@pytest.fixture
def database(tmp_path: Path) -> Generator[SQLiteDatabase]:
    db = open_test_database(tmp_path)
    MigrationRunner(db, tmp_path / "backups").upgrade_to_head()
    with db.write_session() as session:
        session.add_all([_series(SERIES), _series(OTHER_SERIES)])
        session.flush()
        session.add_all(
            [
                ChapterRow(
                    chapter_id=str(chapter_id),
                    series_id=str(series_id),
                    chapter_raw_label="1",
                    display_label="Capítulo 1",
                    chapter_sort_key="[1]",
                )
                for series_id, chapter_id in [(SERIES, CHAPTER), (OTHER_SERIES, OTHER_CHAPTER)]
            ]
        )
    yield db
    db.close()


def _bookmark(page: int = 0, *, chapter_id: UUID = CHAPTER) -> ReaderBookmark:
    return ReaderBookmark(
        series_id=SERIES,
        chapter_id=chapter_id,
        page_index=page,
        chapter_label="Capítulo 1",
        created_at=STAMP,
    )


def _raw(database: SQLiteDatabase) -> str:
    with database.read_session() as session:
        row = session.get(SettingRow, READER_LIBRARY_KEY)
        assert row is not None
        return row.value_json


def _app(database: SQLiteDatabase) -> FastAPI:
    app = FastAPI()
    app.state.runtime = RuntimeState(database=database)
    app.include_router(router)
    return app


async def _request(app: FastAPI, method: str, path: str = "", body: object = None) -> Response:
    async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
        return await client.request(method, f"/api/v1/reader/library{path}", json=body)


def test_empty_library_identity_and_saved_data_survive_reopening(database: SQLiteDatabase) -> None:
    service = ReaderLibraryService(database)
    initial = service.get()
    assert initial.preferences == initial.bookmarks == []
    service.put_preferences(SERIES, PROFILE)
    service.put_bookmark(_bookmark(2))
    reopened = reopen_test_database(database)
    try:
        restored = ReaderLibraryService(reopened).get()
        assert restored.library_id == initial.library_id
        assert restored.preferences[0].rtl is True
        assert restored.bookmarks[0].page_index == 2
    finally:
        reopened.close()


def test_import_merges_without_overwriting_server_values_and_deduplicates(
    database: SQLiteDatabase,
) -> None:
    service = ReaderLibraryService(database)
    service.put_preferences(SERIES, PROFILE)
    service.put_bookmark(_bookmark())
    incoming = ReaderLibraryImport(
        migration_id="a" * 64,
        preferences=[
            ReaderWorkProfile(
                series_id=SERIES, format="auto", mode="vertical", rtl=False, continuous=True
            )
        ],
        bookmarks=[_bookmark(), _bookmark(1), _bookmark(1)],
    )
    merged = service.import_legacy(incoming)
    assert len(merged.preferences) == 1
    assert merged.preferences[0].format == "manga"
    assert [item.page_index for item in merged.bookmarks] == [0, 1]
    assert "migrations" not in merged.model_dump()


def test_repeated_import_does_not_resurrect_a_deleted_bookmark(database: SQLiteDatabase) -> None:
    service = ReaderLibraryService(database)
    incoming = ReaderLibraryImport(migration_id="b" * 64, preferences=[], bookmarks=[_bookmark()])
    service.import_legacy(incoming)
    service.remove_bookmark(CHAPTER, 0)
    before = _raw(database)
    assert service.import_legacy(incoming).bookmarks == []
    assert _raw(database) == before


def test_failed_import_is_atomic_and_can_be_retried(database: SQLiteDatabase) -> None:
    service = ReaderLibraryService(database)
    before = service.get()
    raw = _raw(database)
    unknown = _bookmark(chapter_id=uuid4())
    incoming = ReaderLibraryImport(
        migration_id="c" * 64,
        preferences=[ReaderWorkProfile(series_id=SERIES, **PROFILE.model_dump())],
        bookmarks=[unknown],
    )
    with pytest.raises(ReaderLibraryConflict):
        service.import_legacy(incoming)
    assert service.get() == before
    assert _raw(database) == raw
    incoming.bookmarks = [_bookmark()]
    assert len(service.import_legacy(incoming).bookmarks) == 1


def test_refs_are_checked_and_closed_online_chapters_need_no_active_session(
    database: SQLiteDatabase,
) -> None:
    service = ReaderLibraryService(database)
    with pytest.raises(ReaderLibraryConflict):
        service.put_preferences(uuid4(), PROFILE)
    with pytest.raises(ReaderLibraryConflict):
        service.put_bookmark(_bookmark(chapter_id=OTHER_CHAPTER))
    assert service.put_bookmark(_bookmark(9999)).bookmarks[0].page_index == 9999


def test_limit_rejection_keeps_all_existing_markers(database: SQLiteDatabase) -> None:
    service = ReaderLibraryService(database)
    service.import_legacy(
        ReaderLibraryImport(
            migration_id="d" * 64, preferences=[], bookmarks=[_bookmark(i) for i in range(500)]
        )
    )
    raw = _raw(database)
    with pytest.raises(ReaderLibraryConflict):
        service.put_bookmark(_bookmark(500))
    assert _raw(database) == raw
    with pytest.raises(ReaderLibraryConflict):
        service.import_legacy(
            ReaderLibraryImport(migration_id="f" * 64, preferences=[], bookmarks=[_bookmark(500)])
        )
    assert _raw(database) == raw
    service.put_bookmark(_bookmark(499))
    assert len(service.get().bookmarks) == 500


def test_work_limit_counts_profiles_and_markers_together(database: SQLiteDatabase) -> None:
    service = ReaderLibraryService(database)
    ids = [SERIES, *[uuid4() for _ in range(200)]]
    with database.write_session() as session:
        session.add_all([_series(item) for item in ids[1:]])
    service.import_legacy(
        ReaderLibraryImport(
            migration_id="e" * 64,
            bookmarks=[],
            preferences=[
                ReaderWorkProfile(series_id=item, **PROFILE.model_dump()) for item in ids[:200]
            ],
        )
    )
    raw = _raw(database)
    with pytest.raises(ReaderLibraryConflict):
        service.put_preferences(ids[-1], PROFILE)
    assert _raw(database) == raw


@pytest.mark.parametrize("corruption", ["schema", "structure", "duplicates", "ledger"])
def test_corrupt_data_is_reported_and_never_overwritten(
    database: SQLiteDatabase,
    corruption: str,
) -> None:
    service = ReaderLibraryService(database)
    service.put_bookmark(_bookmark())
    with database.write_session() as session:
        row = session.get(SettingRow, READER_LIBRARY_KEY)
        assert row is not None
        content = json.loads(row.value_json)
        if corruption == "schema":
            row.schema_version = 2
        elif corruption == "structure":
            content["preferences"] = "bad"
        elif corruption == "duplicates":
            content["bookmarks"] *= 2
        else:
            content["migrations"] = ["bad"]
        row.value_json = json.dumps(content)
    raw = _raw(database)
    with pytest.raises(ReaderLibraryCorrupt):
        service.get()
    with pytest.raises(ReaderLibraryCorrupt):
        service.put_preferences(SERIES, PROFILE)
    assert _raw(database) == raw


def test_import_ledger_has_a_non_evicting_limit(database: SQLiteDatabase) -> None:
    service = ReaderLibraryService(database)
    service.get()
    with database.write_session() as session:
        row = session.get(SettingRow, READER_LIBRARY_KEY)
        assert row is not None
        content = json.loads(row.value_json)
        content["migrations"] = [f"{index:064x}" for index in range(200)]
        row.value_json = json.dumps(content)
    raw = _raw(database)
    with pytest.raises(ReaderLibraryConflict):
        service.import_legacy(
            ReaderLibraryImport(migration_id="f" * 64, preferences=[], bookmarks=[])
        )
    assert _raw(database) == raw


def test_concurrent_updates_on_two_handles_do_not_lose_markers(database: SQLiteDatabase) -> None:
    other = reopen_test_database(database)
    services = [ReaderLibraryService(database), ReaderLibraryService(other)]
    services[0].get()
    try:

        def save(page: int) -> None:
            services[page % 2].put_bookmark(_bookmark(page))

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(save, range(12)))
        assert {item.page_index for item in services[0].get().bookmarks} == set(range(12))
    finally:
        other.close()


def test_mutations_preserve_global_settings(database: SQLiteDatabase) -> None:
    global_service = ReaderSettingsService(database)
    chosen = ReaderSettings(cache_limit_mb=64, preload_ahead=0, mode="double", rtl=False)
    global_service.put(chosen)
    service = ReaderLibraryService(database)
    service.put_preferences(SERIES, PROFILE)
    service.put_bookmark(_bookmark())
    service.remove_bookmark(CHAPTER, 0)
    assert global_service.get() == chosen


def test_api_roundtrip_and_delete(database: SQLiteDatabase) -> None:
    app = _app(database)
    response = asyncio.run(_request(app, "GET"))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    data = response.json()["data"]
    assert data.keys() == {"library_id", "preferences", "bookmarks"}
    assert (
        asyncio.run(
            _request(app, "PUT", f"/preferences/{SERIES}", PROFILE.model_dump())
        ).status_code
        == 200
    )
    saved = asyncio.run(_request(app, "PUT", "/bookmarks", _bookmark(2).model_dump(mode="json")))
    assert saved.status_code == 200
    assert saved.json()["data"]["bookmarks"][0]["created_at"] == STAMP
    deleted = asyncio.run(_request(app, "DELETE", f"/bookmarks/{CHAPTER}/2"))
    assert deleted.status_code == 200
    assert deleted.json()["data"]["bookmarks"] == []
    imported = asyncio.run(
        _request(
            app,
            "POST",
            "/import",
            {
                "migration_id": "a" * 64,
                "preferences": [],
                "bookmarks": [_bookmark(3).model_dump(mode="json")],
            },
        )
    )
    assert imported.status_code == 200
    assert imported.json()["meta"] == {}
    assert imported.json()["data"]["library_id"] == data["library_id"]
    assert imported.json()["data"]["bookmarks"][0]["page_index"] == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("series_id", "not-an-id"),
        ("chapter_id", "123e4567-e89b-12d3-a456-426614174051"),
        ("page_index", -1),
        ("page_index", 10000),
        ("page_index", True),
        ("chapter_label", " "),
        ("chapter_label", "a" * 501),
        ("created_at", "2026-10-25T12:00:00+02:00"),
        ("created_at", "2026-02-31T12:00:00Z"),
    ],
)
def test_api_rejects_invalid_markers(
    database: SQLiteDatabase,
    field: str,
    value: object,
) -> None:
    body = _bookmark().model_dump(mode="json")
    body[field] = value
    assert asyncio.run(_request(_app(database), "PUT", "/bookmarks", body)).status_code == 422


def test_api_unknown_refs_and_invalid_profiles(database: SQLiteDatabase) -> None:
    app = _app(database)
    assert (
        asyncio.run(
            _request(app, "PUT", f"/preferences/{uuid4()}", PROFILE.model_dump())
        ).status_code
        == 409
    )
    body = PROFILE.model_dump()
    body["rtl"] = "true"
    assert asyncio.run(_request(app, "PUT", f"/preferences/{SERIES}", body)).status_code == 422
    bad_import: dict[str, object] = {"migration_id": "bad", "preferences": [], "bookmarks": []}
    assert asyncio.run(_request(app, "POST", "/import", bad_import)).status_code == 422


def test_api_corrupt_library_is_an_error_not_an_empty_success(database: SQLiteDatabase) -> None:
    ReaderLibraryService(database).get()
    with database.write_session() as session:
        row = session.get(SettingRow, READER_LIBRARY_KEY)
        assert row is not None
        row.schema_version = 2
    assert asyncio.run(_request(_app(database), "GET")).status_code == 500
