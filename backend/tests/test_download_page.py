import logging
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from support.sqlite import open_test_database

from app.domain import (
    Chapter,
    ChapterSource,
    DownloadJob,
    DownloadPage,
    Series,
    Source,
    SourceHealthStatus,
)
from app.domain.job_manifest import (
    MAX_MANIFEST_PAGE_COUNT,
    DownloadJobManifest,
    InvalidManifestError,
)
from app.errors import ErrorCode
from app.persistence.base import PersistenceBase
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.download_job import download_job_from_row, download_job_to_row
from app.persistence.mappers.download_page import download_page_from_row, download_page_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.migrations.runner import (
    DatabaseMigrationError,
    MigrationReport,
    MigrationRunner,
)
from app.persistence.models import DownloadJobRow, DownloadPageRow
from app.persistence.repositories.download_page import DownloadPageRepository

SHA256 = "b" * 64


def _create_database_with_job(
    tmp_path: Path,
    *,
    before_revision: str | None = None,
) -> tuple[SQLiteDatabase, DownloadJob]:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    if before_revision is None:
        runner.upgrade_to_head()
    else:
        with database.exclusive_connection() as connection:
            config = Config(str(runner.config_path))
            config.attributes["connection"] = connection
            command.upgrade(config, before_revision)
            connection.commit()
    series = Series.new_local("Series", "Series")
    chapter = Chapter.new_local(
        series.series_id,
        "1",
        "Chapter 1",
        chapter_sort_key=(1, 0, 0),
    )
    source = Source.new_local(
        "connector",
        "v1",
        "Connector",
        True,
        SourceHealthStatus.HEALTHY,
    )
    chapter_source = ChapterSource.new_local(
        chapter.chapter_id,
        source.source_id,
        remote_id="remote-chapter-1",
        available=True,
    )
    job = DownloadJob.new_local(chapter_source.chapter_source_id)
    with database.write_session() as session:
        if before_revision is None:
            session.add(series_to_row(series))
        else:
            session.execute(
                text(
                    "INSERT INTO series (series_id, canonical_title, display_title, "
                    "description, status, language, cover_asset_id, created_at_us, "
                    "updated_at_us) VALUES (:series_id, :canonical_title, :display_title, "
                    ":description, :status, :language, NULL, :created_at_us, :updated_at_us)"
                ),
                {
                    "series_id": str(series.series_id),
                    "canonical_title": series.canonical_title,
                    "display_title": series.display_title,
                    "description": series.description,
                    "status": series.status,
                    "language": series.language,
                    "created_at_us": datetime_to_epoch_us(series.created_at),
                    "updated_at_us": datetime_to_epoch_us(series.updated_at),
                },
            )
        session.add(source_to_row(source))
        session.flush()
        session.add(chapter_to_row(chapter))
        session.flush()
        session.add(chapter_source_to_row(chapter_source))
        session.flush()
        if before_revision is None:
            session.add(download_job_to_row(job))
        else:
            session.execute(
                text(
                    "INSERT INTO download_jobs (job_id, chapter_source_id, status, "
                    "priority, created_at_us, updated_at_us) VALUES (:job_id, "
                    ":chapter_source_id, :status, :priority, :created_at_us, :updated_at_us)"
                ),
                {
                    "job_id": str(job.job_id),
                    "chapter_source_id": str(job.chapter_source_id),
                    "status": job.status.value,
                    "priority": job.priority.value,
                    "created_at_us": datetime_to_epoch_us(job.created_at),
                    "updated_at_us": datetime_to_epoch_us(job.updated_at),
                },
            )
    return database, job


def _insert_page(
    session: Session,
    *,
    job_id: str,
    ordinal: int | float,
    remote_reference: str,
    temporary_path: str | None = None,
    status: str = "PENDING",
    size_bytes: int | float | None = None,
    sha256: str | None = None,
    width: int | float | None = None,
    height: int | float | None = None,
    mime: str | None = None,
    attempts: int | float = 0,
) -> None:
    session.execute(
        text(
            "INSERT INTO download_pages (job_id, ordinal, remote_reference, temporary_path, "
            "status, size_bytes, sha256, width, height, mime, attempts) VALUES (:job_id, "
            ":ordinal, :remote_reference, :temporary_path, :status, :size_bytes, :sha256, "
            ":width, :height, :mime, :attempts)"
        ),
        {
            "job_id": job_id,
            "ordinal": ordinal,
            "remote_reference": remote_reference,
            "temporary_path": temporary_path,
            "status": status,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "width": width,
            "height": height,
            "mime": mime,
            "attempts": attempts,
        },
    )


def test_download_page_preserves_reference_status_and_optional_metadata() -> None:
    job_id = uuid4()
    remote_reference = "  https://example.invalid/page/001?token=opaque  "
    page = DownloadPage.new_local(job_id, 1, remote_reference)

    assert page.job_id == job_id
    assert page.ordinal == 1
    assert page.remote_reference == remote_reference
    assert page.temporary_path is None
    assert page.status == "PENDING"
    assert page.size_bytes is None
    assert page.sha256 is None
    assert page.width is None
    assert page.height is None
    assert page.mime is None
    assert page.attempts == 0

    observed = DownloadPage.new_local(
        job_id,
        2,
        "connector-page-id-2",
        status="FUTURE_STATUS",
        temporary_path=f"jobs/{job_id}/002.jpg.part",
        size_bytes=0,
        sha256=SHA256,
        width=1200,
        height=1800,
        mime=" image/jpeg ",
        attempts=3,
    )
    assert observed.status == "FUTURE_STATUS"
    assert observed.remote_reference == "connector-page-id-2"
    assert observed.temporary_path == f"jobs/{job_id}/002.jpg.part"
    assert observed.size_bytes == 0
    assert observed.sha256 == SHA256
    assert (observed.width, observed.height) == (1200, 1800)
    assert observed.mime == " image/jpeg "
    assert observed.attempts == 3


def test_download_page_rejects_invalid_identity_paths_and_partial_metadata() -> None:
    job_id = uuid4()

    for ordinal in (0, -1, 1.5):
        with pytest.raises(ValueError, match="ordinal must be a positive integer"):
            DownloadPage.new_local(job_id, cast(int, ordinal), "remote-1")

    with pytest.raises(ValueError, match="remote_reference must not be empty"):
        DownloadPage.new_local(job_id, 1, "")
    with pytest.raises(ValueError, match="status must be a non-empty string"):
        DownloadPage.new_local(job_id, 1, "remote-1", status="")
    with pytest.raises(ValueError, match="attempts must be a non-negative integer"):
        DownloadPage.new_local(job_id, 1, "remote-1", attempts=-1)
    with pytest.raises(ValueError, match="attempts must be a non-negative integer"):
        DownloadPage.new_local(job_id, 1, "remote-1", attempts=cast(int, 1.5))

    for unsafe_path in (
        "",
        "/absolute/page.part",
        "C:/temp/page.part",
        "jobs\\page.part",
        "../outside.part",
        "jobs/../outside.part",
        "jobs/./page.part",
        "jobs//page.part",
        "jobs/bad:name.part",
        "jobs/CON.part",
    ):
        with pytest.raises(ValueError, match="temporary_path"):
            DownloadPage.new_local(job_id, 1, "remote-1", temporary_path=unsafe_path)

    for size in (-1, 1.5):
        with pytest.raises(ValueError, match="size_bytes must be a non-negative integer"):
            DownloadPage.new_local(job_id, 1, "remote-1", size_bytes=cast(int, size))

    for digest in ("A" * 64, "g" * 64, "short"):
        with pytest.raises(ValueError, match="sha256 must be 64 lowercase hexadecimal"):
            DownloadPage.new_local(job_id, 1, "remote-1", sha256=digest)

    for dimensions in ((100, None), (None, 100)):
        with pytest.raises(ValueError, match="width and height must both be present"):
            DownloadPage.new_local(
                job_id,
                1,
                "remote-1",
                width=dimensions[0],
                height=dimensions[1],
            )

    for dimensions in ((0, 100), (100, 0), (1.5, 100)):
        with pytest.raises(ValueError, match="width and height must be positive integers"):
            DownloadPage.new_local(
                job_id,
                1,
                "remote-1",
                width=cast(int | None, dimensions[0]),
                height=cast(int | None, dimensions[1]),
            )

    with pytest.raises(ValueError, match="mime must not be empty"):
        DownloadPage.new_local(job_id, 1, "remote-1", mime="")


def test_download_page_mapper_round_trips_raw_and_verified_values() -> None:
    job_id = uuid4()
    page = DownloadPage.new_local(
        job_id,
        11,
        "  source-specific opaque reference  ",
        status="VERIFIED_BY_FUTURE_PROTOCOL",
        temporary_path=f"jobs/{job_id}/011.webp",
        size_bytes=123_456,
        sha256=SHA256,
        width=640,
        height=960,
        mime="image/webp",
        attempts=2,
    )

    assert download_page_from_row(download_page_to_row(page)) == page


@pytest.mark.parametrize(
    "temporary_path",
    ["jobs/Page/A.part", "jobs/Straße/001.part", "jobs/cafe\u0301/雪.part"],
)
def test_download_page_mapper_persists_casefold_key_without_rewriting_path(
    temporary_path: str,
) -> None:
    page = DownloadPage.new_local(uuid4(), 1, "opaque-page", temporary_path=temporary_path)

    row = download_page_to_row(page)

    assert row.temporary_path == temporary_path
    assert row.temporary_path_key == temporary_path.casefold()
    assert download_page_from_row(row) == page


def test_download_page_mapper_does_not_reserve_a_null_temporary_path() -> None:
    page = DownloadPage.new_local(uuid4(), 1, "opaque-page")

    row = download_page_to_row(page)

    assert row.temporary_path is None
    assert row.temporary_path_key is None


def test_download_page_schema_enforces_identity_metadata_and_retention(
    tmp_path: Path,
) -> None:
    database, job = _create_database_with_job(tmp_path)
    try:
        page = DownloadPage.new_local(
            job.job_id,
            1,
            "opaque-page-1",
            temporary_path=f"jobs/{job.job_id}/001.jpg.part",
            size_bytes=123,
            sha256=SHA256,
            width=600,
            height=900,
            mime="image/jpeg",
            attempts=1,
        )
        with database.write_session() as session:
            session.add(download_page_to_row(page))

        with database.read_session() as session:
            row = session.get(DownloadPageRow, (str(job.job_id), 1))
            primary_key_columns = inspect(session.connection()).get_pk_constraint("download_pages")[
                "constrained_columns"
            ]
            index_names = {
                index_row[1]
                for index_row in session.connection()
                .exec_driver_sql("PRAGMA index_list('download_pages')")
                .all()
            }
            foreign_key_errors = (
                session.connection().exec_driver_sql("PRAGMA foreign_key_check").all()
            )

        assert row is not None
        assert download_page_from_row(row) == page
        assert primary_key_columns == ["job_id", "ordinal"]
        assert {
            "ux_download_pages_temporary_path",
            "ix_download_pages_job_status",
        } <= index_names
        assert foreign_key_errors == []
        assert {table.name for table in PersistenceBase.metadata.sorted_tables} >= {
            "download_jobs",
            "download_pages",
        }

        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text("DELETE FROM download_jobs WHERE job_id = :job_id"),
                    {"job_id": str(job.job_id)},
                )
    finally:
        database.close()


def test_download_page_schema_rejects_invalid_rows_and_duplicate_identity_or_path(
    tmp_path: Path,
) -> None:
    database, job = _create_database_with_job(tmp_path)
    try:
        invalid_writes: tuple[dict[str, object], ...] = (
            {"ordinal": 0},
            {"ordinal": -1},
            {"ordinal": 1.5},
            {"remote_reference": ""},
            {"temporary_path": "../escape.part"},
            {"temporary_path": "/absolute/page.part"},
            {"status": ""},
            {"size_bytes": -1},
            {"size_bytes": 1.5},
            {"sha256": "A" * 64},
            {"sha256": "g" * 64},
            {"width": 100},
            {"height": 100},
            {"width": 0, "height": 100},
            {"width": 100, "height": 0},
            {"width": 1.5, "height": 100},
            {"mime": ""},
            {"attempts": -1},
            {"attempts": 1.5},
            {"job_id": str(uuid4())},
        )
        for changes in invalid_writes:
            values: dict[str, object] = {
                "job_id": str(job.job_id),
                "ordinal": 1,
                "remote_reference": "remote-1",
                "temporary_path": None,
                "status": "PENDING",
                "size_bytes": None,
                "sha256": None,
                "width": None,
                "height": None,
                "mime": None,
                "attempts": 0,
            }
            values.update(changes)
            with pytest.raises(IntegrityError):
                with database.write_session() as session:
                    session.execute(
                        text(
                            "INSERT INTO download_pages (job_id, ordinal, remote_reference, "
                            "temporary_path, status, size_bytes, sha256, width, height, mime, "
                            "attempts) VALUES (:job_id, :ordinal, :remote_reference, "
                            ":temporary_path, :status, :size_bytes, :sha256, :width, :height, "
                            ":mime, :attempts)"
                        ),
                        values,
                    )

        shared_path = f"jobs/{job.job_id}/shared.part"
        with database.write_session() as session:
            _insert_page(
                session,
                job_id=str(job.job_id),
                ordinal=1,
                remote_reference="remote-1",
                temporary_path=shared_path,
            )
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                _insert_page(
                    session,
                    job_id=str(job.job_id),
                    ordinal=1,
                    remote_reference="remote-duplicate-ordinal",
                )
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                _insert_page(
                    session,
                    job_id=str(job.job_id),
                    ordinal=2,
                    remote_reference="remote-duplicate-path",
                    temporary_path=shared_path,
                )
    finally:
        database.close()


def test_download_page_migration_and_schema_history_are_append_only(tmp_path: Path) -> None:
    database, job = _create_database_with_job(
        tmp_path,
        before_revision="0008_download_jobs",
    )
    try:
        report = MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
        with database.read_session() as session:
            saved_job = session.get(DownloadJobRow, str(job.job_id))
            revisions = session.execute(
                text("SELECT revision_id, resulting_revision FROM schema_history ORDER BY event_id")
            ).all()
        assert report.previous_revision == "0008_download_jobs"
        assert report.current_revision == "0024_reader_sessions"
        assert report.changed is True
        assert report.backup_path is not None and report.backup_path.is_file()
        assert saved_job is not None
        assert download_job_from_row(saved_job) == job
        assert tuple(revisions[-1]) == (
            report.current_revision,
            report.current_revision,
        )
        assert len(revisions) == 24

        with database.write_session() as session:
            with pytest.raises(IntegrityError, match="append-only"):
                session.execute(text("UPDATE schema_history SET direction = 'downgrade'"))
    finally:
        database.close()


def test_download_page_schema_enforces_global_casefold_key_and_allows_nulls(
    tmp_path: Path,
) -> None:
    database, first_job = _create_database_with_job(tmp_path)
    second_job = DownloadJob.new_local(first_job.chapter_source_id)
    first_path = "jobs/Straße/001.part"
    alias_path = "jobs/STRASSE/001.part"
    try:
        with database.write_session() as session:
            session.add(download_job_to_row(second_job))
            session.add(
                download_page_to_row(
                    DownloadPage.new_local(
                        first_job.job_id, 1, "remote-1", temporary_path=first_path
                    )
                )
            )

        with pytest.raises(IntegrityError):
            with database.write_session() as session:
                session.add(
                    download_page_to_row(
                        DownloadPage.new_local(
                            second_job.job_id, 1, "remote-2", temporary_path=alias_path
                        )
                    )
                )

        with database.read_session() as session:
            saved_paths = session.execute(
                text(
                    "SELECT temporary_path, temporary_path_key FROM download_pages "
                    "ORDER BY job_id, ordinal"
                )
            ).all()
        assert saved_paths == [(first_path, first_path.casefold())]

        with database.write_session() as session:
            session.add(
                download_page_to_row(DownloadPage.new_local(first_job.job_id, 2, "remote-3"))
            )
            session.add(
                download_page_to_row(DownloadPage.new_local(second_job.job_id, 1, "remote-4"))
            )
        with database.read_session() as session:
            null_key_count = session.execute(
                text(
                    "SELECT count(*) FROM download_pages "
                    "WHERE temporary_path IS NULL AND temporary_path_key IS NULL"
                )
            ).scalar_one()
        assert null_key_count == 2
    finally:
        database.close()


@pytest.mark.parametrize(
    ("first_path", "second_path"),
    [
        ("jobs/page/A.part", "jobs/page/a.part"),
        ("jobs/Straße/001.part", "jobs/STRASSE/001.part"),
    ],
    ids=["ascii-casefold", "unicode-casefold"],
)
def test_download_page_migration_preflight_rejects_casefold_collisions_without_changes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    first_path: str,
    second_path: str,
) -> None:
    database, job = _create_database_with_job(
        tmp_path,
        before_revision="0015_job_cancellation_intent",
    )
    runner = MigrationRunner(database, tmp_path / "backups")
    marker = tmp_path / "YomiraData" / "temp" / "jobs" / "page.part"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"preserve-page-bytes")
    caplog.set_level(logging.ERROR, logger="yomira.migrations")
    try:
        with database.write_session() as session:
            _insert_page(
                session,
                job_id=str(job.job_id),
                ordinal=1,
                remote_reference="remote-1",
                temporary_path=first_path,
            )
            _insert_page(
                session,
                job_id=str(job.job_id),
                ordinal=2,
                remote_reference="remote-2",
                temporary_path=second_path,
            )

        with database.read_session() as session:
            original_rows = session.execute(
                text(
                    "SELECT job_id, ordinal, remote_reference, temporary_path, status, "
                    "size_bytes, sha256, width, height, mime, attempts FROM download_pages "
                    "ORDER BY job_id, ordinal"
                )
            ).all()

        statements: list[str] = []

        def record_statements(
            _connection: object,
            _cursor: object,
            statement: str,
            parameters: object,
            _context: object,
            _executemany: bool,
        ) -> tuple[str, object]:
            statements.append(" ".join(statement.upper().split()))
            return statement, parameters

        event.listen(database.engine, "before_cursor_execute", record_statements, retval=True)
        try:
            with pytest.raises(DatabaseMigrationError) as raised:
                runner.upgrade_to_head()
        finally:
            event.remove(database.engine, "before_cursor_execute", record_statements)

        assert raised.value.code is ErrorCode.DB_MIGRATION_FAILED
        assert raised.value.restored is True
        assert not any(
            statement.startswith(("ALTER TABLE DOWNLOAD_PAGES", "UPDATE DOWNLOAD_PAGES"))
            or (
                statement.startswith("CREATE UNIQUE INDEX")
                and "DOWNLOAD_PAGES" in statement
                and "TEMPORARY_PATH" in statement
            )
            for statement in statements
        )
        with database.read_session() as session:
            revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            columns = {
                column["name"]
                for column in inspect(session.connection()).get_columns("download_pages")
            }
            rows = session.execute(
                text(
                    "SELECT job_id, ordinal, remote_reference, temporary_path, status, "
                    "size_bytes, sha256, width, height, mime, attempts FROM download_pages "
                    "ORDER BY job_id, ordinal"
                )
            ).all()

        assert revision == "0015_job_cancellation_intent"
        assert "temporary_path_key" not in columns
        assert rows == original_rows
        assert marker.read_bytes() == b"preserve-page-bytes"
        assert any(
            getattr(record, "job_id", None) == str(job.job_id)
            and getattr(record, "ordinal", None) == 2
            and getattr(record, "conflicting_job_id", None) == str(job.job_id)
            and getattr(record, "conflicting_ordinal", None) == 1
            for record in caplog.records
        )
        assert first_path not in caplog.text
        assert second_path not in caplog.text
    finally:
        database.close()


def test_download_page_migration_backfills_key_for_path_changed_after_preflight(
    tmp_path: Path,
) -> None:
    database, job = _create_database_with_job(
        tmp_path,
        before_revision="0015_job_cancellation_intent",
    )
    runner = MigrationRunner(database, tmp_path / "backups")
    original_path = "jobs/page-before-preflight.part"
    concurrent_path = "jobs/page-after-preflight.part"
    with database.write_session() as session:
        _insert_page(
            session,
            job_id=str(job.job_id),
            ordinal=1,
            remote_reference="remote-concurrent-update",
            temporary_path=original_path,
        )

    writer_outcomes: list[str] = []

    def inject_concurrent_update(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> tuple[str, object]:
        normalized = " ".join(statement.upper().split())
        if not writer_outcomes and normalized.startswith(
            "INSERT INTO TEMP._K093_TEMPORARY_PATH_KEY_PREFLIGHT"
        ):
            try:
                with closing(sqlite3.connect(str(database.path), timeout=0.0)) as concurrent:
                    cursor = concurrent.execute(
                        "UPDATE download_pages SET temporary_path = ? "
                        "WHERE job_id = ? AND ordinal = 1 AND temporary_path = ?",
                        (concurrent_path, str(job.job_id), original_path),
                    )
                    assert cursor.rowcount == 1
                    concurrent.commit()
                writer_outcomes.append("committed")
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).casefold():
                    raise
                writer_outcomes.append("blocked")
        return statement, parameters

    event.listen(
        database.engine,
        "before_cursor_execute",
        inject_concurrent_update,
        retval=True,
    )
    try:
        report: MigrationReport | None = None
        migration_error: DatabaseMigrationError | None = None
        try:
            report = runner.upgrade_to_head()
        except DatabaseMigrationError as error:
            migration_error = error

        assert len(writer_outcomes) == 1
        if writer_outcomes[0] == "blocked":
            assert migration_error is None
            assert report is not None and report.changed is True
            assert report.current_revision == "0024_reader_sessions"
            with database.read_session() as session:
                saved_path, saved_key = session.execute(
                    text(
                        "SELECT temporary_path, temporary_path_key FROM download_pages "
                        "WHERE job_id = :job_id AND ordinal = 1"
                    ),
                    {"job_id": str(job.job_id)},
                ).one()
            assert saved_path == original_path
            assert saved_key == original_path.casefold()
        else:
            assert writer_outcomes[0] == "committed"
            with closing(sqlite3.connect(str(database.path), timeout=5.0)) as raw_database:
                revision = raw_database.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()[0]
                saved_path = raw_database.execute(
                    "SELECT temporary_path FROM download_pages WHERE job_id = ? AND ordinal = 1",
                    (str(job.job_id),),
                ).fetchone()[0]

                if migration_error is not None:
                    column_names = {
                        row[1]
                        for row in raw_database.execute(
                            "PRAGMA table_info(download_pages)"
                        ).fetchall()
                    }
                    assert revision == "0015_job_cancellation_intent"
                    assert saved_path == concurrent_path
                    assert "temporary_path_key" not in column_names
                else:
                    assert report is not None and report.changed is True
                    assert revision == "0024_reader_sessions"
                    saved_key = raw_database.execute(
                        "SELECT temporary_path_key FROM download_pages "
                        "WHERE job_id = ? AND ordinal = 1",
                        (str(job.job_id),),
                    ).fetchone()[0]
                    assert saved_path == concurrent_path
                    assert saved_key == concurrent_path.casefold()
    finally:
        event.remove(
            database.engine,
            "before_cursor_execute",
            inject_concurrent_update,
        )
        database.close()


def test_download_page_migration_reports_oversized_path_without_selecting_its_text(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database, job = _create_database_with_job(
        tmp_path,
        before_revision="0015_job_cancellation_intent",
    )
    runner = MigrationRunner(database, tmp_path / "backups")
    oversized_path = "a" * 4097
    caplog.set_level(logging.ERROR, logger="yomira.migrations")
    try:
        with database.write_session() as session:
            _insert_page(
                session,
                job_id=str(job.job_id),
                ordinal=1,
                remote_reference="remote-oversized-path",
                temporary_path=oversized_path,
            )

        with database.engine.connect() as connection:
            driver_connection = connection.connection.driver_connection
            assert isinstance(driver_connection, sqlite3.Connection)
            original_length_limit = driver_connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)

        with database.read_session() as session:
            original_rows = session.execute(
                text(
                    "SELECT job_id, ordinal, remote_reference, temporary_path, status, "
                    "size_bytes, sha256, width, height, mime, attempts FROM download_pages "
                    "ORDER BY job_id, ordinal"
                )
            ).all()

        statements: list[str] = []
        observed_length_limits: list[int] = []

        def record_statements(
            connection: Any,
            _cursor: object,
            statement: str,
            parameters: object,
            _context: object,
            _executemany: bool,
        ) -> tuple[str, object]:
            driver_connection = connection.connection.driver_connection
            assert isinstance(driver_connection, sqlite3.Connection)
            observed_length_limits.append(driver_connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH))
            statements.append(" ".join(statement.upper().split()))
            return statement, parameters

        event.listen(database.engine, "before_cursor_execute", record_statements, retval=True)
        try:
            with pytest.raises(DatabaseMigrationError) as raised:
                runner.upgrade_to_head()
        finally:
            event.remove(database.engine, "before_cursor_execute", record_statements)

        assert raised.value.code is ErrorCode.DB_MIGRATION_FAILED
        assert raised.value.restored is True
        assert observed_length_limits
        assert all(limit == original_length_limit for limit in observed_length_limits)
        metadata_queries = [
            statement
            for statement in statements
            if "OCTET_LENGTH(TEMPORARY_PATH)" in statement and "DOWNLOAD_PAGES" in statement
        ]
        assert metadata_queries
        for statement in metadata_queries:
            projection = statement.split(" FROM ", 1)[0].split("SELECT", 1)[-1]
            projected_items = [
                item.split(" AS ", 1)[0].strip().split(".")[-1].strip('"`[]')
                for item in projection.split(",")
            ]
            assert set(projected_items) == {
                "JOB_ID",
                "ORDINAL",
                "OCTET_LENGTH(TEMPORARY_PATH)",
            }
            assert len(projected_items) == 3

        download_page_reads = [
            statement
            for statement in statements
            if statement.lstrip().startswith("SELECT") and "DOWNLOAD_PAGES" in statement
        ]
        for statement in download_page_reads:
            projection = statement.split(" FROM ", 1)[0].split("SELECT", 1)[-1]
            projected_items = [
                item.split(" AS ", 1)[0].strip().split(".")[-1].strip('"`[]')
                for item in projection.split(",")
            ]
            assert "*" not in projected_items
            assert "TEMPORARY_PATH" not in projected_items

        assert not any(
            statement.startswith(("ALTER TABLE DOWNLOAD_PAGES", "UPDATE DOWNLOAD_PAGES"))
            or (
                statement.startswith("CREATE UNIQUE INDEX")
                and "DOWNLOAD_PAGES" in statement
                and "TEMPORARY_PATH" in statement
            )
            for statement in statements
        )
        assert any(
            getattr(record, "job_id", None) == str(job.job_id)
            and getattr(record, "ordinal", None) == 1
            for record in caplog.records
        )
        assert oversized_path not in caplog.text
        with database.read_session() as session:
            revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            columns = {
                column["name"]
                for column in inspect(session.connection()).get_columns("download_pages")
            }
            rows = session.execute(
                text(
                    "SELECT job_id, ordinal, remote_reference, temporary_path, status, "
                    "size_bytes, sha256, width, height, mime, attempts FROM download_pages "
                    "ORDER BY job_id, ordinal"
                )
            ).all()
        assert revision == "0015_job_cancellation_intent"
        assert "temporary_path_key" not in columns
        assert rows == original_rows
    finally:
        database.close()


def test_download_page_migration_allows_remote_reference_above_4096_bytes(
    tmp_path: Path,
) -> None:
    database, job = _create_database_with_job(
        tmp_path,
        before_revision="0015_job_cancellation_intent",
    )
    runner = MigrationRunner(database, tmp_path / "backups")
    remote_reference = "r" * 4097
    temporary_path = "jobs/page.part"
    try:
        with database.write_session() as session:
            _insert_page(
                session,
                job_id=str(job.job_id),
                ordinal=1,
                remote_reference=remote_reference,
                temporary_path=temporary_path,
            )

        with database.engine.connect() as connection:
            driver_connection = connection.connection.driver_connection
            assert isinstance(driver_connection, sqlite3.Connection)
            original_length_limit = driver_connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)

        statements: list[str] = []

        def record_statements(
            _connection: object,
            _cursor: object,
            statement: str,
            parameters: object,
            _context: object,
            _executemany: bool,
        ) -> tuple[str, object]:
            statements.append(" ".join(statement.upper().split()))
            return statement, parameters

        event.listen(database.engine, "before_cursor_execute", record_statements, retval=True)
        try:
            report = runner.upgrade_to_head()
        finally:
            event.remove(database.engine, "before_cursor_execute", record_statements)

        assert report.changed is True
        assert report.current_revision == "0024_reader_sessions"
        assert driver_connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == original_length_limit
        preflight_queries = [
            statement
            for statement in statements
            if "OCTET_LENGTH(TEMPORARY_PATH)" in statement and "DOWNLOAD_PAGES" in statement
        ]
        assert preflight_queries
        for statement in preflight_queries:
            projection = statement.split(" FROM ", 1)[0].split("SELECT", 1)[-1]
            projected_items = [
                item.split(" AS ", 1)[0].strip().split(".")[-1].strip('"`[]')
                for item in projection.split(",")
            ]
            assert set(projected_items) == {
                "JOB_ID",
                "ORDINAL",
                "OCTET_LENGTH(TEMPORARY_PATH)",
            }
            assert len(projected_items) == 3
        for statement in statements:
            if not statement.lstrip().startswith("SELECT") or "DOWNLOAD_PAGES" not in statement:
                continue
            projection = statement.split(" FROM ", 1)[0].split("SELECT", 1)[-1]
            projected_items = [
                item.split(" AS ", 1)[0].strip().split(".")[-1].strip('"`[]')
                for item in projection.split(",")
            ]
            assert "REMOTE_REFERENCE" not in projected_items
        with database.read_session() as session:
            saved_reference, saved_path, saved_key = session.execute(
                text(
                    "SELECT remote_reference, temporary_path, temporary_path_key "
                    "FROM download_pages WHERE job_id = :job_id AND ordinal = 1"
                ),
                {"job_id": str(job.job_id)},
            ).one()
        assert saved_reference == remote_reference
        assert saved_path == temporary_path
        assert saved_key == temporary_path.casefold()
    finally:
        database.close()


@pytest.mark.parametrize(
    ("second_component_length", "should_migrate"),
    [(3, True), (4, False)],
    ids=["relative-path-259-utf16-units", "relative-path-260-utf16-units"],
)
def test_download_page_migration_preflights_historical_path_length(
    tmp_path: Path,
    second_component_length: int,
    should_migrate: bool,
) -> None:
    database, job = _create_database_with_job(
        tmp_path,
        before_revision="0015_job_cancellation_intent",
    )
    runner = MigrationRunner(database, tmp_path / "backups")
    temporary_path = f"{'a' * 255}/{'b' * second_component_length}"
    assert len(temporary_path.encode("utf-16-le")) // 2 == 255 + 1 + second_component_length
    try:
        with database.write_session() as session:
            _insert_page(
                session,
                job_id=str(job.job_id),
                ordinal=1,
                remote_reference="remote-1",
                temporary_path=temporary_path,
            )

        if should_migrate:
            report = runner.upgrade_to_head()
            assert report.changed is True
            assert report.current_revision == "0024_reader_sessions"
            with database.read_session() as session:
                saved_path, saved_key = session.execute(
                    text(
                        "SELECT temporary_path, temporary_path_key FROM download_pages "
                        "WHERE job_id = :job_id AND ordinal = 1"
                    ),
                    {"job_id": str(job.job_id)},
                ).one()
            assert saved_path == temporary_path
            assert saved_key == temporary_path.casefold()
        else:
            with pytest.raises(DatabaseMigrationError) as raised:
                runner.upgrade_to_head()
            assert raised.value.code is ErrorCode.DB_MIGRATION_FAILED
            assert raised.value.restored is True
            with database.read_session() as session:
                revision = session.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                saved_path = session.execute(
                    text(
                        "SELECT temporary_path FROM download_pages "
                        "WHERE job_id = :job_id AND ordinal = 1"
                    ),
                    {"job_id": str(job.job_id)},
                ).scalar_one()
            assert revision == "0015_job_cancellation_intent"
            assert saved_path == temporary_path
    finally:
        database.close()


def test_download_page_rejects_unpaired_utf16_surrogate_in_temporary_path() -> None:
    with pytest.raises(ValueError, match="temporary_path"):
        DownloadPage.new_local(
            uuid4(),
            1,
            "remote-1",
            temporary_path="jobs/\ud800.part",
        )


def test_download_page_repository_projects_ordered_pages_and_source_lineage(
    tmp_path: Path,
) -> None:
    database, job = _create_database_with_job(tmp_path)
    repository = DownloadPageRepository()
    try:
        with database.write_session() as session:
            for ordinal in (3, 1, 2):
                _insert_page(
                    session,
                    job_id=str(job.job_id),
                    ordinal=ordinal,
                    remote_reference=f"remote-page-{ordinal}",
                )

        with database.read_session() as session:
            source_id, chapter_id = session.execute(
                text(
                    "SELECT sources.source_id, chapters.chapter_id "
                    "FROM chapter_sources "
                    "JOIN sources ON sources.source_id = chapter_sources.source_id "
                    "JOIN chapters ON chapters.chapter_id = chapter_sources.chapter_id "
                    "WHERE chapter_sources.chapter_source_id = :chapter_source_id"
                ),
                {"chapter_source_id": str(job.chapter_source_id)},
            ).one()
            manifest = repository.project_manifest(session, job)

        assert isinstance(manifest, DownloadJobManifest)
        assert manifest.job_id == job.job_id
        assert manifest.source_id == UUID(source_id)
        assert manifest.chapter_id == UUID(chapter_id)
        assert manifest.expected_pages == 3
        assert manifest.pages == tuple(
            DownloadPage.new_local(
                job.job_id,
                ordinal,
                f"remote-page-{ordinal}",
            )
            for ordinal in (1, 2, 3)
        )
    finally:
        database.close()


def test_download_page_repository_rejects_missing_chapter_source_lineage(
    tmp_path: Path,
) -> None:
    database, job = _create_database_with_job(tmp_path)
    repository = DownloadPageRepository()
    job_with_missing_lineage = replace(job, chapter_source_id=uuid4())
    try:
        with database.read_session() as session:
            with pytest.raises(InvalidManifestError):
                repository.project_manifest(session, job_with_missing_lineage)
    finally:
        database.close()


@pytest.mark.parametrize(
    "ordinals",
    [(2,), (1, 3)],
    ids=["ordinal-does-not-start-at-one", "missing-ordinal-gap"],
)
def test_download_page_repository_rejects_noncontiguous_ordinals(
    tmp_path: Path,
    ordinals: tuple[int, ...],
) -> None:
    database, job = _create_database_with_job(tmp_path)
    repository = DownloadPageRepository()
    try:
        with database.write_session() as session:
            for ordinal in ordinals:
                _insert_page(
                    session,
                    job_id=str(job.job_id),
                    ordinal=ordinal,
                    remote_reference=f"remote-page-{ordinal}",
                )

        with database.read_session() as session:
            with pytest.raises(InvalidManifestError):
                repository.project_manifest(session, job)
    finally:
        database.close()


def test_download_page_repository_rejects_unknown_page_status(
    tmp_path: Path,
) -> None:
    database, job = _create_database_with_job(tmp_path)
    repository = DownloadPageRepository()
    try:
        with database.write_session() as session:
            _insert_page(
                session,
                job_id=str(job.job_id),
                ordinal=1,
                remote_reference="remote-page-1",
                status="FUTURE_STATUS",
            )

        with database.read_session() as session:
            with pytest.raises(InvalidManifestError):
                repository.project_manifest(session, job)
    finally:
        database.close()


def test_download_page_repository_reads_at_most_limit_plus_one_rows(
    tmp_path: Path,
) -> None:
    database, job = _create_database_with_job(tmp_path)
    repository = DownloadPageRepository()
    statements: list[tuple[str, object]] = []
    try:
        with database.write_session() as session:
            session.execute(
                text(
                    "INSERT INTO download_pages "
                    "(job_id, ordinal, remote_reference, status, attempts) "
                    "VALUES (:job_id, :ordinal, :remote_reference, 'PENDING', 0)"
                ),
                [
                    {
                        "job_id": str(job.job_id),
                        "ordinal": ordinal,
                        "remote_reference": f"remote-page-{ordinal}",
                    }
                    for ordinal in range(1, MAX_MANIFEST_PAGE_COUNT + 2)
                ],
            )

        def capture_page_select(
            _connection: object,
            _cursor: object,
            statement: str,
            parameters: object,
            _context: object,
            _executemany: bool,
        ) -> tuple[str, object]:
            normalized = " ".join(statement.upper().split())
            if normalized.startswith("SELECT") and "DOWNLOAD_PAGES" in normalized:
                statements.append((normalized, parameters))
            return statement, parameters

        event.listen(
            database.engine,
            "before_cursor_execute",
            capture_page_select,
            retval=True,
        )
        try:
            with database.read_session() as session:
                with pytest.raises(InvalidManifestError):
                    repository.project_manifest(session, job)
        finally:
            event.remove(
                database.engine,
                "before_cursor_execute",
                capture_page_select,
            )

        assert len(statements) == 1
        statement, parameters = statements[0]
        assert "LIMIT" in statement
        if isinstance(parameters, dict):
            parameter_values = tuple(cast(dict[object, object], parameters).values())
        elif isinstance(parameters, (tuple, list)):
            parameter_values = cast(tuple[object, ...] | list[object], parameters)
        else:
            pytest.fail("The bounded page query did not expose its LIMIT parameter.")
        assert MAX_MANIFEST_PAGE_COUNT + 1 in parameter_values
    finally:
        database.close()
