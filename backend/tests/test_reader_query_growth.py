import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from test_reader_asset import _fixture

from app.domain.chapter_cbz_asset import ChapterCbzAsset
from app.domain.download_job import DownloadJobStatus
from app.persistence.mappers.asset import asset_to_row
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.chapter_cbz_asset import chapter_cbz_asset_to_row
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.download_job import download_job_to_row
from app.persistence.repositories.chapter_cbz_asset import ChapterCbzAssetRepository


def test_reader_query_stays_bounded_after_small_library_grows(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    database = fixture.database
    try:
        indexed = fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=fixture.result,
        )
        with database.write_session() as session:
            session.execute(text("ANALYZE"))
        chapters, sources, jobs, assets, links = [], [], [], [], []
        for number in range(2, 202):
            chapter = replace(fixture.chapter, chapter_id=uuid4(), chapter_sort_key=(number,))
            source = replace(
                fixture.chapter_source,
                chapter_source_id=uuid4(),
                chapter_id=chapter.chapter_id,
                remote_id=str(number),
            )
            job = replace(
                fixture.job,
                job_id=uuid4(),
                chapter_source_id=source.chapter_source_id,
                status=DownloadJobStatus.COMPLETED,
            )
            asset = replace(indexed.asset, asset_id=uuid4(), relative_path=f"library/{number}.cbz")
            link = ChapterCbzAsset.new_local(chapter.chapter_id, asset.asset_id, job.job_id, 1)
            chapters.append(chapter_to_row(chapter))
            sources.append(chapter_source_to_row(source))
            jobs.append(download_job_to_row(job))
            assets.append(asset_to_row(asset))
            links.append(chapter_cbz_asset_to_row(link))
        with database.write_session() as session:
            for group in (chapters, sources, jobs, assets, links):
                session.add_all(group)
                session.flush()
        with database.read_session() as session:
            connection = session.connection().connection.driver_connection
            assert isinstance(connection, sqlite3.Connection)
            calls = 0

            def bounded_work() -> int:
                nonlocal calls
                calls += 1
                return int(calls > 1000)

            connection.set_progress_handler(bounded_work, 1000)
            try:
                items, total = ChapterCbzAssetRepository().list_ready_chapters(
                    session, fixture.series.series_id, limit=100, offset=0
                )
                assert total == 201 and len(items) == 100
            finally:
                connection.set_progress_handler(None, 0)
    finally:
        database.close()
