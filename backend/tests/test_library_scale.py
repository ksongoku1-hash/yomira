from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from uuid import UUID

import pytest
from sqlalchemy.engine import Connection
from support.sqlite import open_test_database

from app.domain.library_reading_status import LibraryReadingStatus
from app.persistence.migrations.runner import MigrationRunner
from app.services.library import LibraryService

CHAPTERS_PER_SERIES = 10
INSERT_BATCH_SERIES = 500
NOW_US = 1_790_420_400_000_000


def _measure[Result](action: Callable[[], Result]) -> tuple[Result, float]:
    started_at = perf_counter()
    result = action()
    return result, (perf_counter() - started_at) * 1_000


def _seed_library(connection: Connection, series_count: int) -> None:
    for batch_start in range(0, series_count, INSERT_BATCH_SERIES):
        batch_end = min(batch_start + INSERT_BATCH_SERIES, series_count)
        series_rows: list[tuple[str, str, str, None, None, str, None, None, None, int, int]] = []
        chapter_rows: list[tuple[str, str, str, int, None, None, None, str, None, str, str]] = []
        progress_rows: list[tuple[str, str, int, int, int]] = []

        for series_index in range(batch_start, batch_end):
            series_id = str(UUID(int=series_index + 1, version=4))
            canonical_title = f"synthetic-series-{series_index:05d}"
            display_title = f"Synthetic Series {series_index:05d}"
            series_rows.append(
                (
                    series_id,
                    canonical_title,
                    display_title,
                    None,
                    None,
                    "ja",
                    None,
                    None,
                    None,
                    NOW_US,
                    NOW_US,
                )
            )

            reading_category = series_index % 3
            for chapter_index in range(CHAPTERS_PER_SERIES):
                chapter_number = chapter_index + 1
                chapter_id = str(
                    UUID(
                        int=(1 << 80) + series_index * CHAPTERS_PER_SERIES + chapter_index,
                        version=4,
                    )
                )
                label = f"Chapter {chapter_number:02d}"
                chapter_rows.append(
                    (
                        chapter_id,
                        series_id,
                        label,
                        chapter_number,
                        None,
                        None,
                        None,
                        f"[{chapter_number}]",
                        None,
                        "ja",
                        label,
                    )
                )

                if reading_category == 0 or chapter_index == 0:
                    if reading_category == 0:
                        percentage = 10_000
                    elif reading_category == 1:
                        percentage = 5_000
                    else:
                        percentage = 0
                    progress_rows.append((series_id, chapter_id, 0, percentage, NOW_US))

        connection.exec_driver_sql(
            "INSERT INTO series (series_id, canonical_title, display_title, description, "
            "status, language, author, genres_json, cover_asset_id, created_at_us, updated_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            series_rows,
        )
        connection.exec_driver_sql(
            "INSERT INTO chapters (chapter_id, series_id, chapter_raw_label, chapter_major, "
            "chapter_minor, chapter_variant, chapter_type, chapter_sort_key, volume_number, "
            "language, display_label) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            chapter_rows,
        )
        connection.exec_driver_sql(
            "INSERT INTO reader_progress (series_id, chapter_id, page_index, "
            "percentage_basis_points, updated_at_us) VALUES (?, ?, ?, ?, ?)",
            progress_rows,
        )
        connection.commit()


@pytest.mark.parametrize("series_count", [100, 1_000, 10_000])
def test_library_gate_scales_pages_filters_search_and_detail(
    tmp_path: Path,
    series_count: int,
) -> None:
    database = open_test_database(tmp_path / "library-scale")
    runner = MigrationRunner(
        database,
        database.data_root_identity.canonical_path / "backups",
    )
    try:
        runner.upgrade_to_head()
        with database.exclusive_connection() as connection:
            _, seed_ms = _measure(lambda: _seed_library(connection, series_count))

        service = LibraryService(database)
        first_page, first_page_ms = _measure(lambda: service.list_series(limit=100, offset=0))
        assert first_page.total == series_count
        assert len(first_page.items) == min(series_count, 100)
        assert first_page.items[0].canonical_title == "synthetic-series-00000"

        offsets = tuple(
            dict.fromkeys((0, max(0, series_count // 2 - 50), max(0, series_count - 100)))
        )
        page_measurements: list[tuple[int, float]] = []
        last_page = first_page
        for offset in offsets:
            page, elapsed_ms = _measure(
                lambda offset=offset: service.list_series(limit=100, offset=offset)
            )
            assert page.total == series_count
            assert len(page.items) == min(100, series_count - offset)
            assert page.items[0].canonical_title == f"synthetic-series-{offset:05d}"
            page_measurements.append((offset, elapsed_ms))
            last_page = page

        filter_expectations = (
            (LibraryReadingStatus.COMPLETED, (series_count + 2) // 3),
            (LibraryReadingStatus.READING, (series_count + 1) // 3),
            (LibraryReadingStatus.INCOMPLETE, series_count // 3),
        )
        filter_measurements: list[tuple[str, float]] = []
        for reading_status, expected_total in filter_expectations:
            filtered_page, elapsed_ms = _measure(
                lambda reading_status=reading_status: service.list_series(
                    limit=100,
                    offset=0,
                    reading_status=reading_status,
                )
            )
            assert filtered_page.total == expected_total
            assert len(filtered_page.items) == min(100, expected_total)
            filter_measurements.append((reading_status.value, elapsed_ms))

        search_term = f"{series_count - 1:05d}".casefold()
        matches, search_ms = _measure(
            lambda: tuple(
                series
                for series in last_page.items
                if search_term in series.canonical_title.casefold()
                or search_term in series.display_title.casefold()
            )
        )
        assert len(matches) == 1
        assert matches[0].canonical_title == f"synthetic-series-{series_count - 1:05d}"

        global_matches, global_search_ms = _measure(
            lambda: service.list_series(query=search_term, sort="title", limit=1)
        )
        assert global_matches.total == 1
        assert global_matches.items[0].canonical_title == f"synthetic-series-{series_count - 1:05d}"

        last_series = last_page.items[-1]
        opened_series, detail_ms = _measure(lambda: service.get_series(last_series.series_id))
        assert opened_series == last_series

        pages_text = ",".join(
            f"{offset}:{elapsed_ms:.2f}" for offset, elapsed_ms in page_measurements
        )
        filters_text = ",".join(
            f"{name}:{elapsed_ms:.2f}" for name, elapsed_ms in filter_measurements
        )
        print(
            "K058_LIBRARY_GATE "
            f"series={series_count} chapters={series_count * CHAPTERS_PER_SERIES} "
            f"seed_ms={seed_ms:.2f} first_page_ms={first_page_ms:.2f} "
            f"page_offset_ms=[{pages_text}] filters_ms=[{filters_text}] "
            f"page_search_ms={search_ms:.2f} global_search_ms={global_search_ms:.2f} "
            f"detail_ms={detail_ms:.2f}"
        )
    finally:
        database.close()
