from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Client
from sqlalchemy import event
from support.sqlite import open_test_database

from app.domain.chapter import Chapter
from app.domain.reader_progress import ReaderProgress
from app.domain.series import Series
from app.factory import create_app
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.reader_progress import reader_progress_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.reader_progress import ReaderProgressRow
from app.runtime import RuntimeState
from app.services.library import LibraryService
from app.services.reader_asset import ReaderChapter
from app.services.reader_progress import ReaderProgressService
from app.settings import Settings

SERIES_A = UUID("123e4567-e89b-42d3-a456-426614174001")
SERIES_B = UUID("123e4567-e89b-42d3-a456-426614174002")
SERIES_C = UUID("123e4567-e89b-42d3-a456-426614174003")
SERIES_D = UUID("123e4567-e89b-42d3-a456-426614174004")
SERIES_E = UUID("123e4567-e89b-42d3-a456-426614174005")
CHAPTER_A1 = UUID("123e4567-e89b-42d3-a456-426614174101")
CHAPTER_A2 = UUID("123e4567-e89b-42d3-a456-426614174102")
CHAPTER_B1 = UUID("123e4567-e89b-42d3-a456-426614174103")
CHAPTER_B2 = UUID("123e4567-e89b-42d3-a456-426614174104")
BASE_TIME = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


class UnusedReaderChapterLookup:
    def get_chapter(self, chapter_id: UUID) -> ReaderChapter | None:
        return None


@pytest.fixture
def recent_api(
    tmp_path: Path,
) -> Generator[tuple[FastAPI, SQLiteDatabase, Client]]:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    target_app = create_app(Settings(app_env="test"))
    runtime = cast(RuntimeState, target_app.state.runtime)
    runtime.database = database
    runtime.library_service = LibraryService(database)
    runtime.reader_progress_service = ReaderProgressService(
        database,
        UnusedReaderChapterLookup(),
    )
    client = cast(Client, TestClient(target_app))
    try:
        _seed_recent_data(database)
        yield target_app, database, client
    finally:
        client.close()
        database.close()


def _seed_recent_data(database: SQLiteDatabase) -> None:
    series = (
        replace(
            Series.new_local("Zulu", "Old but recently edited", series_id=SERIES_A, now=BASE_TIME),
            updated_at=BASE_TIME + timedelta(days=10),
        ),
        Series.new_local("Alpha", "Tie B", series_id=SERIES_B, now=BASE_TIME + timedelta(days=3)),
        Series.new_local("Beta", "Tie C", series_id=SERIES_C, now=BASE_TIME + timedelta(days=3)),
        Series.new_local(
            "Gamma", "Recent D", series_id=SERIES_D, now=BASE_TIME + timedelta(days=2)
        ),
        Series.new_local("Delta", "Old E", series_id=SERIES_E, now=BASE_TIME - timedelta(days=1)),
    )
    chapters = (
        Chapter.new_local(
            SERIES_A, "1", "A capítulo 1", chapter_sort_key=(1,), chapter_id=CHAPTER_A1
        ),
        Chapter.new_local(
            SERIES_A, "2", "A capítulo 2", chapter_sort_key=(2,), chapter_id=CHAPTER_A2
        ),
        Chapter.new_local(
            SERIES_B, "1", "B capítulo 1", chapter_sort_key=(1,), chapter_id=CHAPTER_B1
        ),
        Chapter.new_local(
            SERIES_B, "2", "B capítulo 2", chapter_sort_key=(2,), chapter_id=CHAPTER_B2
        ),
    )
    recent_time = BASE_TIME + timedelta(days=5)
    progress = (
        ReaderProgress.new_local(SERIES_A, CHAPTER_A1, 3, Decimal("50.00"), updated_at=recent_time),
        ReaderProgress.new_local(SERIES_A, CHAPTER_A2, 0, Decimal("0.00"), updated_at=recent_time),
        ReaderProgress.new_local(SERIES_B, CHAPTER_B1, 2, Decimal("66.67"), updated_at=recent_time),
        ReaderProgress.new_local(
            SERIES_B,
            CHAPTER_B2,
            1,
            Decimal("33.33"),
            updated_at=recent_time - timedelta(hours=1),
        ),
    )
    with database.write_session() as session:
        session.add_all(series_to_row(item) for item in series)
        session.flush()
        session.add_all(chapter_to_row(item) for item in chapters)
        session.add_all(reader_progress_to_row(item) for item in progress)


def test_recent_library_uses_creation_time_and_stable_id_tie_breaker(
    recent_api: tuple[FastAPI, SQLiteDatabase, Client],
) -> None:
    _target_app, _database, client = recent_api

    response = client.get("/api/v1/library/recent")

    assert response.status_code == 200
    body = response.json()
    assert [UUID(item["series_id"]) for item in body["data"]] == [
        SERIES_B,
        SERIES_C,
        SERIES_D,
        SERIES_A,
    ]
    assert body["data"][0]["display_title"] == "Tie B"
    assert body["meta"] == {"limit": 4}
    assert set(body["meta"]) == {"limit"}


def test_recent_saved_progress_joins_display_fields_and_keeps_multiple_chapters(
    recent_api: tuple[FastAPI, SQLiteDatabase, Client],
) -> None:
    _target_app, _database, client = recent_api

    response = client.get("/api/v1/reader/progress/recent")

    assert response.status_code == 200
    body = response.json()
    assert body["data"] == [
        {
            "series_id": str(SERIES_A),
            "display_title": "Old but recently edited",
            "chapter_id": str(CHAPTER_A1),
            "display_label": "A capítulo 1",
            "page_index": 3,
            "percentage": "50.00",
            "updated_at": "2026-09-25T10:00:00Z",
        },
        {
            "series_id": str(SERIES_A),
            "display_title": "Old but recently edited",
            "chapter_id": str(CHAPTER_A2),
            "display_label": "A capítulo 2",
            "page_index": 0,
            "percentage": "0.00",
            "updated_at": "2026-09-25T10:00:00Z",
        },
        {
            "series_id": str(SERIES_B),
            "display_title": "Tie B",
            "chapter_id": str(CHAPTER_B1),
            "display_label": "B capítulo 1",
            "page_index": 2,
            "percentage": "66.67",
            "updated_at": "2026-09-25T10:00:00Z",
        },
        {
            "series_id": str(SERIES_B),
            "display_title": "Tie B",
            "chapter_id": str(CHAPTER_B2),
            "display_label": "B capítulo 2",
            "page_index": 1,
            "percentage": "33.33",
            "updated_at": "2026-09-25T09:00:00Z",
        },
    ]
    assert body["meta"] == {"limit": 4}
    assert body["data"][0]["series_id"] == body["data"][1]["series_id"]


def test_recent_progress_can_return_one_latest_chapter_per_series_before_limiting(
    recent_api: tuple[FastAPI, SQLiteDatabase, Client],
) -> None:
    _target_app, database, client = recent_api
    chapter_c = Chapter.new_local(SERIES_C, "1", "C capítulo 1", chapter_sort_key=(1,))
    with database.write_session() as session:
        session.add(chapter_to_row(chapter_c))
        session.add(
            reader_progress_to_row(
                ReaderProgress.new_local(
                    SERIES_C, chapter_c.chapter_id, 0, Decimal("0.00"), updated_at=BASE_TIME
                )
            )
        )
    response = client.get("/api/v1/reader/progress/recent?limit=4&per_series=true")
    assert response.status_code == 200
    assert [item["chapter_id"] for item in response.json()["data"]] == [
        str(CHAPTER_A1),
        str(CHAPTER_B1),
        str(chapter_c.chapter_id),
    ]
    with database.write_session() as session:
        row = session.get(ReaderProgressRow, (str(SERIES_A), str(CHAPTER_A2)))
        assert row is not None
        row.updated_at_us += 86_400_000_000
        row.percentage_basis_points = 10_000
    latest = client.get("/api/v1/reader/progress/recent?limit=1&per_series=true")
    assert latest.status_code == 200
    assert len(latest.json()["data"]) == 1
    assert latest.json()["data"][0]["chapter_id"] == str(CHAPTER_A2)
    assert latest.json()["data"][0]["percentage"] == "100.00"
    assert client.get("/api/v1/reader/progress/recent?per_series=invalid").status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/library/recent",
        "/api/v1/reader/progress/recent",
        "/api/v1/reader/progress/recent?per_series=true",
    ],
)
@pytest.mark.parametrize(("limit", "status_code"), [(1, 200), (20, 200), (0, 422), (21, 422)])
def test_recent_endpoints_enforce_independent_bounded_limit(
    recent_api: tuple[FastAPI, SQLiteDatabase, Client],
    path: str,
    limit: int,
    status_code: int,
) -> None:
    _target_app, _database, client = recent_api

    response = client.get(f"{path}{'&' if '?' in path else '?'}limit={limit}")

    assert response.status_code == status_code
    if status_code == 200:
        assert response.json()["meta"] == {"limit": limit}


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/library/recent",
        "/api/v1/reader/progress/recent",
        "/api/v1/reader/progress/recent?per_series=true",
    ],
)
def test_recent_endpoint_uses_one_limited_select_without_count_or_offset(
    recent_api: tuple[FastAPI, SQLiteDatabase, Client],
    path: str,
) -> None:
    _target_app, database, client = recent_api
    statements: list[str] = []

    def capture_sql(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", capture_sql)
    try:
        response = client.get(path)
    finally:
        event.remove(database.engine, "before_cursor_execute", capture_sql)

    assert response.status_code == 200
    assert len(statements) == 1
    normalized_sql = statements[0].upper()
    assert "COUNT(" not in normalized_sql
    assert " OFFSET " not in normalized_sql
    assert " LIMIT " in normalized_sql
