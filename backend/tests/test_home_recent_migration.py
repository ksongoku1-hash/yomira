from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from support.sqlite import open_test_database

from app.domain.chapter import Chapter
from app.domain.reader_progress import ReaderProgress
from app.domain.series import Series
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.reader_progress import reader_progress_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.migrations.runner import MigrationRunner

SERIES_ID = UUID("123e4567-e89b-42d3-a456-426614174010")
CHAPTER_ID = UUID("123e4567-e89b-42d3-a456-426614174110")
NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


def _apply_revision(
    database: SQLiteDatabase,
    runner: MigrationRunner,
    revision: str,
    *,
    downgrade: bool = False,
) -> None:
    with database.exclusive_connection() as connection:
        config = Config(str(runner.config_path))
        config.attributes["connection"] = connection
        if downgrade:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)
        connection.commit()


def test_recent_views_migration_adds_only_ordering_indexes_and_is_reversible(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    try:
        _apply_revision(database, runner, "0019_usb_export_history")
        series = Series.new_local("Persisted", "Persisted", series_id=SERIES_ID, now=NOW)
        chapter = Chapter.new_local(
            SERIES_ID,
            "1",
            "Chapter 1",
            chapter_sort_key=(1,),
            chapter_id=CHAPTER_ID,
        )
        progress = ReaderProgress.new_local(
            SERIES_ID,
            CHAPTER_ID,
            0,
            Decimal("0.00"),
            updated_at=NOW,
        )
        with database.write_session() as session:
            session.add(series_to_row(series))
            session.flush()
            session.add(chapter_to_row(chapter))
            session.add(reader_progress_to_row(progress))

        report = runner.upgrade_to_head()
        assert report.current_revision == "0023_whole_manga_export_size"
        assert report.changed is True
        with database.read_session() as session:
            series_count = session.execute(text("SELECT COUNT(*) FROM series")).scalar_one()
            progress_count = session.execute(
                text("SELECT COUNT(*) FROM reader_progress")
            ).scalar_one()
            series_index = session.execute(
                text("PRAGMA index_xinfo('ix_series_created_recent')")
            ).all()
            progress_index = session.execute(
                text("PRAGMA index_xinfo('ix_reader_progress_saved_recent')")
            ).all()
        assert series_count == 1
        assert progress_count == 1
        assert [(row[2], row[3]) for row in series_index if row[5] == 1] == [
            ("created_at_us", 1),
            ("series_id", 0),
        ]
        assert [(row[2], row[3]) for row in progress_index if row[5] == 1] == [
            ("updated_at_us", 1),
            ("series_id", 0),
            ("chapter_id", 0),
        ]

        _apply_revision(
            database,
            runner,
            "0019_usb_export_history",
            downgrade=True,
        )
        with database.read_session() as session:
            assert session.execute(text("SELECT COUNT(*) FROM series")).scalar_one() == 1
            assert session.execute(text("SELECT COUNT(*) FROM reader_progress")).scalar_one() == 1
            index_names = {
                row[1] for row in session.execute(text("PRAGMA index_list('series')")).all()
            }
            progress_index_names = {
                row[1]
                for row in session.execute(text("PRAGMA index_list('reader_progress')")).all()
            }
        assert "ix_series_created_recent" not in index_names
        assert "ix_reader_progress_saved_recent" not in progress_index_names
    finally:
        database.close()
