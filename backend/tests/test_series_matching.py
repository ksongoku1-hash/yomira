import asyncio
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select
from support.cbz import index_test_job
from support.sqlite import open_test_database

from app.connectors.mock import MockConnector
from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.domain.chapter import Chapter
from app.domain.series import Series
from app.fmd2_bridge import _chapter  # pyright: ignore[reportPrivateUsage]
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.series import SeriesRow
from app.persistence.models.series_source import SeriesSourceRow
from app.services.jobs import AmbiguousLibrarySeries, DownloadJobService


@pytest.fixture
def database(tmp_path: Path) -> Iterator[SQLiteDatabase]:
    db = open_test_database(tmp_path)
    MigrationRunner(db, tmp_path / "backups").upgrade_to_head()
    yield db
    db.close()


def test_download_joins_catalog_work_despite_original_language_and_author_order(
    database: SQLiteDatabase,
) -> None:
    service = DownloadJobService(database)
    connector = MockConnector()
    old = service.import_library_series(
        connector=replace(connector.metadata, connector_id="yomira.metadata.mangadex"),
        remote_series_id="catalog-ippo",
        series_metadata=ConnectorSeriesMetadata(
            title="Hajime no Ippo", author="Morikawa George", language="ja"
        ),
    )
    chapter = _chapter("spanish-1513", "Capítulo 1513", 1512, "es")
    first, repeated = tuple(
        service.create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="spanish-ippo",
            series_metadata=ConnectorSeriesMetadata(
                title="Hajime no ippo      (1989)", author="George Morikawa", language="es"
            ),
            chapters=(chapter,),
            selected_remote_ids=("spanish-1513",),
        )[0]
        for _ in range(2)
    )
    assert first.series_id == old.series_id
    assert repeated.job.job_id == first.job.job_id
    with database.read_session() as session:
        assert session.scalar(select(func.count()).select_from(SeriesRow)) == 1
        assert session.scalar(select(func.count()).select_from(SeriesSourceRow)) == 2
        saved = session.get(ChapterRow, str(first.chapter_id))
        assert saved is not None
        assert saved.language == "es"
        assert saved.chapter_major == 1513


@pytest.mark.parametrize(
    "title,author,language",
    [
        ("Hajime no Ippo (1989)", "Another Author", "es"),
        ("Hajime no Ippo (1989)", "George Morikawa", "en"),
        ("Hajime no Ippo (2020)", "George Morikawa", "es"),
        ("Hajime no Ippo Full Color (1989)", "George Morikawa", "es"),
        ("Hajime no Ippo (1989)", "George, Another", "es"),
    ],
)
def test_incompatible_editions_are_separate(
    database: SQLiteDatabase, title: str, author: str, language: str
) -> None:
    connector = MockConnector().metadata
    service = DownloadJobService(database)
    original = service.import_library_series(
        connector=connector,
        remote_series_id="first",
        series_metadata=ConnectorSeriesMetadata(
            title="Hajime no Ippo (1989)", author="Morikawa George", language="es"
        ),
    )
    second = service.import_library_series(
        connector=connector,
        remote_series_id="second",
        series_metadata=ConnectorSeriesMetadata(title=title, author=author, language=language),
    )
    assert second.created and second.series_id != original.series_id


def test_known_chapter_language_blocks_catalog_language_false_match(
    database: SQLiteDatabase,
) -> None:
    series = Series.new_local("Hajime no Ippo", "Hajime no Ippo")
    with database.write_session() as session:
        session.add(series_to_row(series))
        session.flush()
        session.add(
            chapter_to_row(
                Chapter.new_local(
                    series.series_id, "Chapter 1", "Chapter 1", chapter_sort_key=(1,), language="ja"
                )
            )
        )
    imported = DownloadJobService(database).import_library_series(
        connector=MockConnector().metadata,
        remote_series_id="es-ippo",
        series_metadata=ConnectorSeriesMetadata(title="Hajime no Ippo", language="es"),
    )
    assert imported.created and imported.series_id != series.series_id


def test_ambiguous_match_rolls_back_instead_of_adding_a_third_entry(
    database: SQLiteDatabase,
) -> None:
    with database.write_session() as session:
        for year in (1989, 2020):
            session.add(
                series_to_row(
                    Series.new_local(f"Hajime no Ippo ({year})", f"Hajime no Ippo ({year})")
                )
            )
    with pytest.raises(AmbiguousLibrarySeries):
        DownloadJobService(database).import_library_series(
            connector=MockConnector().metadata,
            remote_series_id="unknown-edition",
            series_metadata=ConnectorSeriesMetadata(title="Hajime no Ippo"),
        )
    with database.read_session() as session:
        assert session.scalar(select(func.count()).select_from(SeriesRow)) == 2
        assert session.scalar(select(func.count()).select_from(SeriesSourceRow)) == 0


def test_another_source_reuses_readable_chapter_without_another_download(
    database: SQLiteDatabase,
) -> None:
    connector = MockConnector()
    service = DownloadJobService(database)
    chapter = replace(
        asyncio.run(connector.get_chapters("mock-series-1"))[0],
        display_label="Chapter 1",
        language="es",
        chapter_variant=None,
        volume_number=None,
        chapter_type=None,
    )
    metadata = ConnectorSeriesMetadata(title="Example", author="Writer", language="es")
    first = service.create_selected_jobs(
        connector=connector.metadata,
        remote_series_id="first",
        series_metadata=metadata,
        chapters=(chapter,),
        selected_remote_ids=("mock-chapter-1",),
    )[0]
    index_test_job(database, first.job.job_id)
    other = replace(chapter, remote_id="another-chapter")
    second = service.create_selected_jobs(
        connector=replace(connector.metadata, connector_id="yomira.other"),
        remote_series_id="second",
        series_metadata=metadata,
        chapters=(other,),
        selected_remote_ids=("another-chapter",),
    )[0]
    assert second.chapter_id == first.chapter_id
    assert second.job.job_id == first.job.job_id
    assert service.chapter_availability("yomira.other", "second")["ready_labels"] == ["Chapter 1"]


@pytest.mark.parametrize(
    "label,ordinal,major,minor,key",
    [
        ("Capítulo 1513", 1512, 1513, None, (1513,)),
        ("Chapter 12.5", 13, 12, 5, (12, 5)),
        ("Especial de verano", 50, 50, None, (50,)),
    ],
)
def test_source_ordinal_does_not_replace_printed_number(
    label: str, ordinal: int, major: int, minor: int | None, key: tuple[int, ...]
) -> None:
    chapter = _chapter("stable-reference", label, ordinal)
    assert (chapter.chapter_major, chapter.chapter_minor, chapter.chapter_sort_key) == (
        major,
        minor,
        key,
    )
    assert chapter.remote_id == "stable-reference"
