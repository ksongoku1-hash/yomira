from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from support.sqlite import open_test_database

from app.domain.chapter import Chapter
from app.domain.metadata import MetadataField
from app.domain.reader_progress import ReaderProgress
from app.domain.series import Series
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.reader_progress import reader_progress_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.services.library import LibraryReadingStatus, LibraryService
from app.services.metadata import MetadataService

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def test_global_search_counts_matches_before_pagination(tmp_path: Path) -> None:
    database, service = _library(tmp_path)
    try:
        for index in range(205):
            service.add_series(Series.new_local(f"Manga {index:03}", f"Manga {index:03}", now=NOW))
        page = service.list_series(query="Manga 20", sort="title", limit=2, offset=2)
        assert [item.display_title for item in page.items] == ["Manga 202", "Manga 203"]
        assert (page.total, page.offset, page.limit) == (5, 2, 2)
        assert service.list_series(query="Manga 20", reading_status="completed").total == 0
    finally:
        database.close()


def test_search_uses_unicode_and_visible_user_titles_and_literal_wildcards(tmp_path: Path) -> None:
    database, service = _library(tmp_path)
    metadata = MetadataService(database)
    original = Series.new_local("Canonical", "Original", now=NOW)
    special = Series.new_local("100%_story", "100%_story", now=NOW)
    try:
        service.add_series(original)
        service.add_series(special)
        metadata.set_user_override(original.series_id, MetadataField.TITLE, "ÉL MANGA")
        assert service.list_series(query="él manga").items == (original,)
        assert service.list_series(query="canonical").items == (original,)
        assert service.list_series(query="original").total == 0
        assert service.list_series(query="%_").items == (special,)
        assert service.list_series(query="' OR 1=1 --").total == 0
        metadata.set_user_override(original.series_id, MetadataField.TITLE, None)
        assert service.list_series(query="canonical").items == (original,)
        metadata.set_user_override(original.series_id, MetadataField.TITLE, "")
        assert service.list_series(query="original").total == 0
    finally:
        database.close()


def test_global_sort_uses_user_titles_and_dates_before_pagination(tmp_path: Path) -> None:
    database, service = _library(tmp_path)
    first = Series.new_local("First", "Zulu", now=NOW)
    second = Series.new_local("Second", "Bravo", now=NOW + timedelta(days=1))
    second = replace(second, updated_at=NOW + timedelta(days=2))
    try:
        service.add_series(first)
        service.add_series(second)
        MetadataService(database).set_user_override(first.series_id, MetadataField.TITLE, "Alfa")
        assert service.list_series(sort="title", limit=1).items == (first,)
        assert service.list_series(sort="title", limit=1, offset=1).items == (second,)
        assert service.list_series(sort="recent", limit=1).items == (second,)
        assert service.list_series(sort="updated", limit=1).items == (second,)
    finally:
        database.close()


@pytest.mark.parametrize(("query", "sort"), [("x" * 201, "title"), ("", "arbitrary")])
def test_global_search_rejects_invalid_options(tmp_path: Path, query: str, sort: str) -> None:
    database, service = _library(tmp_path)
    try:
        with pytest.raises(ValueError):
            service.list_series(query=query, sort=sort)
    finally:
        database.close()


def _library(tmp_path: Path) -> tuple[SQLiteDatabase, LibraryService]:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    return database, LibraryService(database)


def _add_local_chapters(
    database: SQLiteDatabase,
    series: Series,
    percentages: tuple[Decimal | None, ...],
) -> None:
    with database.write_session() as session:
        for index, percentage in enumerate(percentages, start=1):
            chapter = Chapter.new_local(
                series.series_id,
                f"Chapter {index}",
                f"Chapter {index}",
                chapter_sort_key=(index,),
            )
            session.add(chapter_to_row(chapter))
            if percentage is not None:
                progress = ReaderProgress.new_local(
                    series.series_id,
                    chapter.chapter_id,
                    0,
                    percentage,
                    updated_at=NOW,
                )
                session.add(reader_progress_to_row(progress))


def test_library_service_round_trips_local_series_without_normalizing_text(
    tmp_path: Path,
) -> None:
    database, service = _library(tmp_path)
    series = Series.new_local(
        "  漫画—Noche / Manga  ",
        "Título de usuario 🪷",
        description=" Descripción conservada. ",
        status="En curso",
        language="es",
        now=NOW,
    )
    try:
        service.add_series(series)

        assert service.get_series(series.series_id) == series
    finally:
        database.close()


def test_library_service_does_not_merge_distinct_series_with_the_same_title(
    tmp_path: Path,
) -> None:
    database, service = _library(tmp_path)
    first = Series.new_local("Same canonical title", "First display title", now=NOW)
    second = Series.new_local("Same canonical title", "Second display title", now=NOW)
    try:
        service.add_series(first)
        service.add_series(second)

        assert service.get_series(first.series_id) == first
        assert service.get_series(second.series_id) == second
    finally:
        database.close()


def test_library_service_returns_none_for_an_unknown_series(
    tmp_path: Path,
) -> None:
    database, service = _library(tmp_path)
    try:
        assert service.get_series(uuid4()) is None
    finally:
        database.close()


def test_library_service_returns_a_bounded_stable_page(
    tmp_path: Path,
) -> None:
    database, service = _library(tmp_path)
    try:
        for title in ("Charlie", "Alpha", "Bravo"):
            service.add_series(Series.new_local(title, title, now=NOW))

        page = service.list_series(limit=1, offset=1)

        assert [series.canonical_title for series in page.items] == ["Bravo"]
        assert (page.total, page.limit, page.offset) == (3, 1, 1)
    finally:
        database.close()


def test_library_reading_filters_derive_from_local_chapter_progress_and_count_before_paging(
    tmp_path: Path,
) -> None:
    database, service = _library(tmp_path)
    reading = Series.new_local("Reading", "Reading", status="completed", now=NOW)
    completed = Series.new_local("Completed", "Completed", status="ongoing", now=NOW)
    incomplete = Series.new_local("Incomplete", "Incomplete", status="completed", now=NOW)
    untouched = Series.new_local("Untouched", "Untouched", status="completed", now=NOW)
    chapterless = Series.new_local("Chapterless", "Chapterless", status="completed", now=NOW)
    try:
        for series in (reading, completed, incomplete, untouched, chapterless):
            service.add_series(series)
        _add_local_chapters(database, reading, (Decimal("0"), Decimal("42.50")))
        _add_local_chapters(database, completed, (Decimal("100"), Decimal("100")))
        _add_local_chapters(database, incomplete, (Decimal("0"), None))
        _add_local_chapters(database, untouched, (None, None))

        reading_page = service.list_series(
            reading_status=LibraryReadingStatus.READING,
            limit=1,
            offset=0,
        )
        completed_page = service.list_series(
            reading_status=LibraryReadingStatus.COMPLETED,
        )
        incomplete_page = service.list_series(
            reading_status=LibraryReadingStatus.INCOMPLETE,
        )
        all_page = service.list_series()

        assert [item.canonical_title for item in reading_page.items] == ["Reading"]
        assert (reading_page.total, reading_page.limit, reading_page.offset) == (1, 1, 0)
        assert [item.canonical_title for item in completed_page.items] == ["Completed"]
        assert completed_page.total == 1
        assert [item.canonical_title for item in incomplete_page.items] == ["Incomplete"]
        assert incomplete_page.total == 1
        assert [item.canonical_title for item in all_page.items] == [
            "Chapterless",
            "Completed",
            "Incomplete",
            "Reading",
            "Untouched",
        ]
        assert all_page.total == 5
    finally:
        database.close()


@pytest.mark.parametrize("limit", [0, 501])
def test_library_service_rejects_page_sizes_outside_the_resource_bound(
    tmp_path: Path,
    limit: int,
) -> None:
    database, service = _library(tmp_path)
    try:
        with pytest.raises(ValueError):
            service.list_series(limit=limit)
    finally:
        database.close()


def test_library_service_rejects_negative_offset(tmp_path: Path) -> None:
    database, service = _library(tmp_path)
    try:
        with pytest.raises(ValueError):
            service.list_series(offset=-1)
    finally:
        database.close()


def test_library_service_duplicate_identity_rolls_back_without_overwriting(
    tmp_path: Path,
) -> None:
    database, service = _library(tmp_path)
    series = Series.new_local("Original title", "Original display", now=NOW)
    conflict = Series.new_local(
        "Replacement title",
        "Replacement display",
        series_id=series.series_id,
        now=NOW,
    )
    try:
        service.add_series(series)
        with pytest.raises(IntegrityError):
            service.add_series(conflict)

        assert service.get_series(series.series_id) == series
    finally:
        database.close()
