from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from support.sqlite import open_test_database

from app.domain import Chapter, Series
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.reader_progress import ReaderProgressRow
from app.routes.reader import router
from app.services.reader_asset import ReaderChapter
from app.services.reader_progress import ReaderProgressService

SERIES_ID = UUID("123e4567-e89b-42d3-a456-426614174050")
CHAPTER_ID = UUID("123e4567-e89b-42d3-a456-426614174051")
NOW = datetime(2026, 9, 27, 15, 0, tzinfo=UTC)


@dataclass
class StubReaderAssetService:
    chapter: ReaderChapter | None

    def get_chapter(self, chapter_id: UUID) -> ReaderChapter | None:
        if self.chapter is None or chapter_id != self.chapter.chapter_id:
            return None
        return self.chapter


@dataclass
class ReaderProgressApiFixture:
    database: SQLiteDatabase
    reader_assets: StubReaderAssetService
    service: ReaderProgressService
    client: Any


@pytest.fixture
def progress_api(tmp_path: Path) -> Generator[ReaderProgressApiFixture]:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    series = Series.new_local("Series", "Series", series_id=SERIES_ID, now=NOW)
    chapter = Chapter.new_local(
        SERIES_ID,
        "8",
        "Chapter 8",
        chapter_sort_key=(8,),
        chapter_id=CHAPTER_ID,
    )
    with database.write_session() as session:
        session.add(series_to_row(series))
        session.flush()
        session.add(chapter_to_row(chapter))

    reader_assets = StubReaderAssetService(
        ReaderChapter(CHAPTER_ID, SERIES_ID, "Chapter 8", 3)
    )
    service = ReaderProgressService(database, reader_assets)
    app = FastAPI()
    app.state.runtime = SimpleNamespace(reader_progress_service=service)
    app.include_router(router)
    test_client_factory = cast(Any, TestClient)
    client: Any = test_client_factory(app)
    try:
        with client:
            yield ReaderProgressApiFixture(database, reader_assets, service, client)
    finally:
        database.shutdown()


def test_reader_progress_get_returns_null_before_the_first_save(
    progress_api: ReaderProgressApiFixture,
) -> None:
    response = progress_api.client.get(f"/api/v1/reader/{CHAPTER_ID}/progress")

    assert response.status_code == 200
    assert response.json() == {"data": None, "meta": {}}


def test_put_derives_identity_and_persists_zero_based_position(
    progress_api: ReaderProgressApiFixture,
) -> None:
    response = progress_api.client.put(
        f"/api/v1/reader/{CHAPTER_ID}/progress",
        json={"page_index": 0},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["series_id"] == str(SERIES_ID)
    assert data["chapter_id"] == str(CHAPTER_ID)
    assert data["page_index"] == 0
    assert data["percentage"] == "0.00"
    assert data["updated_at"].endswith("Z")
    assert response.json()["meta"] == {}

    saved = progress_api.client.get(f"/api/v1/reader/{CHAPTER_ID}/progress")
    assert saved.json()["data"] == data


@pytest.mark.parametrize(
    ("page_count", "page_index", "expected_percentage"),
    [
        (1, 0, "100.00"),
        (4, 1, "33.33"),
        (4, 2, "66.67"),
        (20_001, 1, "0.01"),
        (4, 3, "100.00"),
    ],
)
def test_put_uses_exact_integer_basis_point_percentage(
    progress_api: ReaderProgressApiFixture,
    page_count: int,
    page_index: int,
    expected_percentage: str,
) -> None:
    assert progress_api.reader_assets.chapter is not None
    progress_api.reader_assets.chapter = ReaderChapter(
        CHAPTER_ID,
        SERIES_ID,
        "Chapter 8",
        page_count,
    )

    response = progress_api.client.put(
        f"/api/v1/reader/{CHAPTER_ID}/progress",
        json={"page_index": page_index},
    )

    assert response.status_code == 200
    assert response.json()["data"]["percentage"] == expected_percentage


def test_invalid_or_out_of_range_put_preserves_the_previous_row(
    progress_api: ReaderProgressApiFixture,
) -> None:
    path = f"/api/v1/reader/{CHAPTER_ID}/progress"
    original = progress_api.client.put(path, json={"page_index": 1}).json()["data"]

    for body in (
        {"page_index": -1},
        {"page_index": 3},
        {"page_index": 1.0},
        {"page_index": True},
        {"page_index": 2, "series_id": str(SERIES_ID)},
    ):
        response = progress_api.client.put(path, json=body)
        assert response.status_code == 422
        assert progress_api.client.get(path).json()["data"] == original


def test_unavailable_chapter_cannot_read_or_create_progress(
    progress_api: ReaderProgressApiFixture,
) -> None:
    progress_api.reader_assets.chapter = None
    path = f"/api/v1/reader/{CHAPTER_ID}/progress"

    assert progress_api.client.get(path).status_code == 404
    assert progress_api.client.put(path, json={"page_index": 0}).status_code == 404
    with progress_api.database.read_session() as session:
        from app.persistence.models.reader_progress import ReaderProgressRow

        assert session.query(ReaderProgressRow).count() == 0


def test_get_preserves_saved_index_when_current_cbz_has_fewer_pages(
    progress_api: ReaderProgressApiFixture,
) -> None:
    path = f"/api/v1/reader/{CHAPTER_ID}/progress"
    saved = progress_api.client.put(path, json={"page_index": 2}).json()["data"]
    progress_api.reader_assets.chapter = ReaderChapter(
        CHAPTER_ID,
        SERIES_ID,
        "Chapter 8",
        2,
    )

    response = progress_api.client.get(path)

    assert response.status_code == 200
    assert response.json()["data"] == saved


def test_sqlite_write_failure_rolls_back_the_previous_position(
    progress_api: ReaderProgressApiFixture,
) -> None:
    path = f"/api/v1/reader/{CHAPTER_ID}/progress"
    original = progress_api.client.put(path, json={"page_index": 1}).json()["data"]
    with progress_api.database.write_session() as session:
        session.execute(text(
            "CREATE TRIGGER fail_reader_progress_update "
            "BEFORE UPDATE ON reader_progress BEGIN "
            "SELECT RAISE(ABORT, 'injected progress write failure'); END"
        ))

    with pytest.raises(IntegrityError):
        progress_api.service.save(CHAPTER_ID, 2)

    assert progress_api.client.get(path).json()["data"] == original
    with progress_api.database.read_session() as session:
        assert session.query(ReaderProgressRow).count() == 1
