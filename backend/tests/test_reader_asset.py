from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from support.sqlite import open_test_database

import app.services.reader_asset as reader_asset_module
from app.domain import (
    Chapter,
    ChapterSource,
    DownloadJob,
    DownloadJobStatus,
    DownloadPage,
    Series,
    Source,
    SourceHealthStatus,
)
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.download_job import download_job_to_row
from app.persistence.mappers.download_page import download_page_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.windows_paths import DataRootIdentity
from app.services.cbz_builder import (
    MAX_CBZ_BYTES_V1,
    CBZBuilder,
    CBZBuildError,
    CBZBuildResult,
    ComicInfoMetadata,
    ValidatedPage,
)
from app.services.reader_asset import (
    V1_MAX_CBZ_BYTES,
    V1_MAX_PAGE_BYTES,
    V1_STREAM_CHUNK_BYTES,
    ReaderAssetConflict,
    ReaderAssetError,
    ReaderAssetLimits,
    ReaderAssetService,
)

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l5sAAAAASUVORK5CYII="
)
PNG_SHA256 = hashlib.sha256(PNG_BYTES).hexdigest()


@dataclass(frozen=True)
class ReaderFixture:
    database: SQLiteDatabase
    series: Series
    chapter: Chapter
    other_chapter: Chapter
    source: Source
    chapter_source: ChapterSource
    job: DownloadJob
    cbz_path: Path
    result: CBZBuildResult
    service: ReaderAssetService


def _fixture(
    tmp_path: Path,
    *,
    cbz_bytes: bytes | None = None,
    page_payloads: tuple[bytes, ...] | None = None,
) -> ReaderFixture:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    series = Series.new_local("Reader series", "Reader series")
    chapter = Chapter.new_local(
        series.series_id,
        "1",
        "Chapter 1",
        chapter_sort_key=(1,),
    )
    other_chapter = Chapter.new_local(
        series.series_id,
        "2",
        "Chapter 2",
        chapter_sort_key=(2,),
    )
    source = Source.new_local(
        "reader-fixture",
        "1",
        "Reader fixture",
        True,
        SourceHealthStatus.HEALTHY,
    )
    chapter_source = ChapterSource.new_local(
        chapter.chapter_id,
        source.source_id,
        remote_id="chapter-1",
        available=True,
    )
    job = replace(
        DownloadJob.new_local(chapter_source.chapter_source_id),
        status=DownloadJobStatus.INDEXING,
    )
    pages = page_payloads if page_payloads is not None else (PNG_BYTES,)
    download_pages = tuple(
        DownloadPage.new_local(
            job.job_id,
            ordinal,
            f"fixture-page-{ordinal}",
            status="VALIDATED",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            mime="image/png",
        )
        for ordinal, content in enumerate(pages, start=1)
    )
    with database.write_session() as session:
        session.add_all([series_to_row(series), source_to_row(source)])
        session.flush()
        session.add_all([chapter_to_row(chapter), chapter_to_row(other_chapter)])
        session.flush()
        session.add(chapter_source_to_row(chapter_source))
        session.add(download_job_to_row(job))
        session.flush()
        session.add_all(download_page_to_row(page) for page in download_pages)

    cbz_path = database.data_root_identity.canonical_path / "library" / "chapter-1.cbz"
    cbz_path.parent.mkdir(parents=True, exist_ok=True)
    if cbz_bytes is None:
        with zipfile.ZipFile(cbz_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(
                "ComicInfo.xml",
                (
                    '<?xml version="1.0" encoding="utf-8"?>'
                    f"<ComicInfo><PageCount>{len(pages)}</PageCount></ComicInfo>"
                ).encode(),
            )
            for ordinal, content in enumerate(pages, start=1):
                archive.writestr(f"{ordinal:03d}.png", content)
    else:
        cbz_path.write_bytes(cbz_bytes)
    archive_bytes = cbz_path.read_bytes()
    result = CBZBuildResult(
        path=cbz_path,
        sha256=hashlib.sha256(archive_bytes).hexdigest(),
        size_bytes=len(archive_bytes),
        page_count=len(pages),
    )
    service = ReaderAssetService(
        database,
        data_root_identity=database.data_root_identity,
        limits=ReaderAssetLimits(
            max_cbz_bytes=V1_MAX_CBZ_BYTES,
            max_page_bytes=V1_MAX_PAGE_BYTES,
            chunk_bytes=V1_STREAM_CHUNK_BYTES,
        ),
    )
    return ReaderFixture(
        database,
        series,
        chapter,
        other_chapter,
        source,
        chapter_source,
        job,
        cbz_path,
        result,
        service,
    )


def _job_status(database: SQLiteDatabase, job_id: UUID) -> str:
    with database.read_session() as session:
        return str(
            session.execute(
                text("SELECT status FROM download_jobs WHERE job_id = :job_id"),
                {"job_id": str(job_id)},
            ).scalar_one()
        )


def test_reader_keeps_resolving_legacy_cbz_paths_under_data_root(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    try:
        library_root = tmp_path / "Documents" / "Mangas"
        library_root.mkdir(parents=True)
        library_identity = DataRootIdentity.capture(library_root)
        service = ReaderAssetService(
            fixture.database,
            data_root_identity=fixture.database.data_root_identity,
            library_root_identity=library_identity,
            limits=ReaderAssetLimits(
                max_cbz_bytes=V1_MAX_CBZ_BYTES,
                max_page_bytes=V1_MAX_PAGE_BYTES,
                chunk_bytes=V1_STREAM_CHUNK_BYTES,
            ),
        )
        relative_path = "library/chapters/series-1/chapter-1.cbz"
        content = b"legacy CBZ bytes"
        legacy_path = fixture.database.data_root_identity.canonical_path.joinpath(
            *relative_path.split("/")
        )
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_bytes(content)

        with service._open_verified_asset(  # pyright: ignore[reportPrivateUsage]
            relative_path,
            expected_sha256=hashlib.sha256(content).hexdigest(),
            expected_size_bytes=len(content),
            root_identity=library_identity,
        ) as stream:
            assert stream.read() == content
    finally:
        fixture.database.close()


def test_availability_requires_verified_reader_asset_and_exact_series_link(tmp_path: Path) -> None:
    from uuid import uuid4

    from app.persistence.models.series_source import SeriesSourceRow
    from app.services.jobs import DownloadJobService

    fixture = _fixture(tmp_path)
    try:
        with fixture.database.write_session() as session:
            session.add(
                SeriesSourceRow(
                    series_source_id=str(uuid4()),
                    series_id=str(fixture.series.series_id),
                    source_id=str(fixture.source.source_id),
                    remote_id="reader-series",
                )
            )
        jobs = DownloadJobService(fixture.database)
        assert jobs.chapter_availability("reader-fixture", "reader-series") == {
            "linked": True,
            "ready_remote_ids": [],
            "ready_labels": [],
            "busy_remote_ids": ["chapter-1"],
        }
        fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=fixture.result,
        )
        assert jobs.chapter_availability("reader-fixture", "reader-series") == {
            "linked": True,
            "ready_remote_ids": ["chapter-1"],
            "ready_labels": [],
            "busy_remote_ids": [],
        }
        assert jobs.chapter_availability("reader-fixture", "reader-full-color")["linked"] is False
    finally:
        fixture.database.close()


@pytest.mark.parametrize(
    ("local_label", "remote_label", "duplicate", "language", "indexed", "expected_created"),
    [
        ("Chapter 1", "Capítulo: 001", False, "es", True, False),
        ("Capítulo 2.50", "2,5", False, "es", True, False),
        ("Chapter 1", "Capítulo 1 extra", False, "es", True, True),
        ("Chapter 1", "Capítulo 1", True, "es", True, True),
        ("Chapter 1", "Capítulo 1", False, "en", True, True),
        ("Chapter 1", "Capítulo 1", False, "es", False, True),
    ],
)
def test_download_selection_reuses_only_unique_verified_local_chapters(
    tmp_path, local_label, remote_label, duplicate, language, indexed, expected_created
):
    import asyncio
    from uuid import uuid4

    from app.connectors.mock import MockConnector
    from app.persistence.models.chapter import ChapterRow
    from app.persistence.models.download_job import DownloadJobRow
    from app.persistence.models.series_source import SeriesSourceRow
    from app.persistence.models.source import SourceRow
    from app.services.jobs import DownloadJobService

    fixture = _fixture(tmp_path)
    connector = MockConnector()
    source = Source.new_local("yomira.mock", "1", "Mock", True, SourceHealthStatus.HEALTHY)
    try:
        with fixture.database.write_session() as session:
            session.get(
                SourceRow, str(fixture.source.source_id)
            ).connector_id = "yomira.local.import"
            chapter = session.get(ChapterRow, str(fixture.chapter.chapter_id))
            chapter.display_label = local_label
            chapter.language = language
            session.add(source_to_row(source))
            session.flush()
            session.add(
                SeriesSourceRow(
                    series_source_id=str(uuid4()),
                    series_id=str(fixture.series.series_id),
                    source_id=str(source.source_id),
                    remote_id="mock-series-1",
                    remote_language="es",
                )
            )
        if indexed:
            fixture.service.index_verified_cbz(
                job_id=fixture.job.job_id,
                chapter_id=fixture.chapter.chapter_id,
                result=fixture.result,
            )
        else:
            with fixture.database.write_session() as session:
                session.get(DownloadJobRow, str(fixture.job.job_id)).status = "COMPLETED"
        template = asyncio.run(connector.get_chapters("mock-series-1"))[0]
        remote = replace(template, display_label=remote_label)
        chapters = (
            (remote, replace(remote, remote_id="second-translation")) if duplicate else (remote,)
        )
        metadata = replace(
            connector.normalize_metadata(asyncio.run(connector.get_series("mock-series-1"))),
            language="es",
        )
        service = DownloadJobService(fixture.database)
        selected = service.create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="mock-series-1",
            series_metadata=metadata,
            chapters=chapters,
            selected_remote_ids=(remote.remote_id,),
        )[0]
        assert selected.created is expected_created
        if not expected_created:
            assert selected.job.job_id == fixture.job.job_id
            assert selected.chapter_id == fixture.chapter.chapter_id
            assert selected.job.status is DownloadJobStatus.COMPLETED
            assert service.list_jobs().states["QUEUED"] == 0
            assert service.chapter_availability("yomira.mock", "mock-series-1")["ready_labels"] == [
                local_label
            ]
        else:
            assert selected.job.status is DownloadJobStatus.QUEUED
        # Another edition is never deduplicated solely by its title.
        other = service.create_selected_jobs(
            connector=connector.metadata,
            remote_series_id="other-edition",
            series_metadata=metadata,
            chapters=(replace(remote, remote_id="other-chapter"),),
            selected_remote_ids=("other-chapter",),
        )[0]
        assert other.created and other.series_id != fixture.series.series_id
    finally:
        fixture.database.close()


def test_indexing_atomically_links_cbz_and_completes_job_and_reader_streams_page(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    try:
        indexed = fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=fixture.result,
        )

        assert indexed.asset.asset_type.value == "CBZ"
        assert indexed.page_count == 1
        assert _job_status(fixture.database, fixture.job.job_id) == "COMPLETED"
        ready = fixture.service.list_chapters_for_series(fixture.series.series_id)
        assert ready.total == 1
        assert ready.items[0].chapter_id == fixture.chapter.chapter_id
        assert ready.items[0].page_count == 1

        stream = fixture.service.open_page(fixture.chapter.chapter_id, 1)
        assert stream.media_type == "image/png"
        assert stream.etag == f'"{PNG_SHA256}"'
        assert stream.size_bytes == len(PNG_BYTES)
        assert b"".join(stream.iter_chunks()) == PNG_BYTES
    finally:
        fixture.database.close()


def test_verified_chapter_page_iterator_reads_one_cbz_in_page_order(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, page_payloads=(PNG_BYTES, PNG_BYTES))
    try:
        fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=fixture.result,
        )

        pages = list(fixture.service.iter_verified_chapter_pages(fixture.chapter.chapter_id))

        assert [record.page_ordinal for record, _content in pages] == [1, 2]
        assert [content for _record, content in pages] == [PNG_BYTES, PNG_BYTES]
    finally:
        fixture.database.close()


def test_verified_chapter_page_iterator_rejects_changed_cbz_before_yield(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    try:
        fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=fixture.result,
        )
        with fixture.cbz_path.open("r+b") as stream:
            stream.seek(0)
            stream.write(b"X")

        pages = fixture.service.iter_verified_chapter_pages(fixture.chapter.chapter_id)
        with pytest.raises(ReaderAssetError):
            next(pages)
    finally:
        fixture.database.close()


def test_small_cbz_limit_boundary_flows_from_builder_to_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    page_path = tmp_path / "validated-page.png"
    page_path.write_bytes(PNG_BYTES)
    page = ValidatedPage(
        ordinal=1,
        path=page_path,
        size_bytes=len(PNG_BYTES),
        sha256=PNG_SHA256,
    )

    class LocalNoReplacePublisher:
        def __init__(self) -> None:
            self.calls: list[tuple[Path, Path]] = []

        def publish_no_replace(self, source: Path, destination: Path) -> None:
            self.calls.append((source, destination))
            os.link(source, destination)
            source.unlink()

    publisher = LocalNoReplacePublisher()
    library = fixture.database.data_root_identity.canonical_path / "library"
    probe_path = library / "cbz-limit-probe.cbz"
    exact_path = library / "cbz-limit-exact.cbz"
    over_builder_path = library / "cbz-limit-builder-over.cbz"
    over_reader_path = library / "cbz-limit-reader-over.cbz"

    try:
        probe = CBZBuilder(
            publisher=publisher,
            max_archive_bytes=MAX_CBZ_BYTES_V1,
        ).build(
            destination=probe_path,
            metadata=ComicInfoMetadata(),
            pages=(page,),
        )
        exact_limit = probe.size_bytes
        probe_path.unlink()

        exact_builder = CBZBuilder(
            publisher=publisher,
            max_archive_bytes=exact_limit,
        )
        exact_result = exact_builder.build(
            destination=exact_path,
            metadata=ComicInfoMetadata(),
            pages=(page,),
        )
        assert exact_result.size_bytes == exact_limit
        assert exact_path.stat().st_size == exact_limit
        assert not exact_path.with_name(f"{exact_path.name}.tmp").exists()

        below_limit_builder = CBZBuilder(
            publisher=publisher,
            max_archive_bytes=exact_limit - 1,
        )
        with pytest.raises(CBZBuildError) as builder_error:
            below_limit_builder.build(
                destination=over_builder_path,
                metadata=ComicInfoMetadata(),
                pages=(page,),
            )
        assert builder_error.value.stage == "archive_size_limit_preflight"
        assert not over_builder_path.exists()
        assert not over_builder_path.with_name(f"{over_builder_path.name}.tmp").exists()

        monkeypatch.setattr(reader_asset_module, "V1_MAX_CBZ_BYTES", exact_limit)
        bounded_reader = ReaderAssetService(
            fixture.database,
            data_root_identity=fixture.database.data_root_identity,
            limits=ReaderAssetLimits(
                max_cbz_bytes=exact_limit,
                max_page_bytes=V1_MAX_PAGE_BYTES,
                chunk_bytes=V1_STREAM_CHUNK_BYTES,
            ),
        )
        indexed = bounded_reader.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=exact_result,
        )
        assert indexed.asset.size_bytes == exact_limit
        assert _job_status(fixture.database, fixture.job.job_id) == "COMPLETED"
        stream = bounded_reader.open_page(fixture.chapter.chapter_id, 1)
        assert stream.size_bytes == len(PNG_BYTES)
        assert b"".join(stream.iter_chunks()) == PNG_BYTES

        over_reader_path.write_bytes(exact_path.read_bytes() + b"\0")
        over_reader_result = replace(
            exact_result,
            path=over_reader_path,
            sha256=hashlib.sha256(over_reader_path.read_bytes()).hexdigest(),
            size_bytes=exact_limit + 1,
        )
        with pytest.raises(ReaderAssetError, match="size limit"):
            bounded_reader.index_verified_cbz(
                job_id=fixture.job.job_id,
                chapter_id=fixture.chapter.chapter_id,
                result=over_reader_result,
            )
        assert _job_status(fixture.database, fixture.job.job_id) == "COMPLETED"
        assert len(publisher.calls) == 2
    finally:
        fixture.database.close()


def test_migration_downgrade_preserves_existing_reader_link(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    try:
        fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=fixture.result,
        )
        runner = MigrationRunner(fixture.database, tmp_path / "backups")
        with pytest.raises(RuntimeError, match="downgrade is blocked"):
            with fixture.database.exclusive_connection() as connection:
                config = Config(str(runner.config_path))
                config.attributes["connection"] = connection
                command.downgrade(config, "0017_job_recovery_checkpoint")
        with fixture.database.read_session() as session:
            assert (
                session.execute(
                    text("SELECT count(*) FROM chapter_cbz_assets WHERE chapter_id = :chapter_id"),
                    {"chapter_id": str(fixture.chapter.chapter_id)},
                ).scalar_one()
                == 1
            )
        assert _job_status(fixture.database, fixture.job.job_id) == "COMPLETED"
    finally:
        fixture.database.close()


def test_indexing_rejects_job_chapter_lineage_mismatch_without_mutation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    try:
        with pytest.raises(ReaderAssetError):
            fixture.service.index_verified_cbz(
                job_id=fixture.job.job_id,
                chapter_id=fixture.other_chapter.chapter_id,
                result=fixture.result,
            )

        assert _job_status(fixture.database, fixture.job.job_id) == "INDEXING"
        with fixture.database.read_session() as session:
            assert session.execute(text("SELECT count(*) FROM assets")).scalar_one() == 0
            assert (
                session.execute(text("SELECT count(*) FROM chapter_cbz_assets")).scalar_one() == 0
            )
    finally:
        fixture.database.close()


def test_indexing_is_idempotent_for_same_result_and_never_replaces_chapter_asset(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    try:
        first = fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=fixture.result,
        )
        repeated = fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=fixture.result,
        )
        assert repeated.asset.asset_id == first.asset.asset_id

        other_result = replace(fixture.result, sha256="a" * 64)
        with pytest.raises(ReaderAssetConflict):
            fixture.service.index_verified_cbz(
                job_id=fixture.job.job_id,
                chapter_id=fixture.chapter.chapter_id,
                result=other_result,
            )
        assert _job_status(fixture.database, fixture.job.job_id) == "COMPLETED"
    finally:
        fixture.database.close()


def test_indexing_rejects_artifact_hash_or_page_mismatch_and_keeps_job_indexing(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, cbz_bytes=b"different-page-bytes")
    try:
        with pytest.raises(ReaderAssetError):
            fixture.service.index_verified_cbz(
                job_id=fixture.job.job_id,
                chapter_id=fixture.chapter.chapter_id,
                result=fixture.result,
            )
        assert _job_status(fixture.database, fixture.job.job_id) == "INDEXING"
        assert fixture.cbz_path.exists()
    finally:
        fixture.database.close()


def test_indexing_rejects_result_outside_data_root_and_oversized_cbz_metadata(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    try:
        outside_path = tmp_path.parent / f"{tmp_path.name}-outside.cbz"
        outside_path.write_bytes(fixture.cbz_path.read_bytes())
        outside_result = replace(fixture.result, path=outside_path)
        with pytest.raises(ReaderAssetError):
            fixture.service.index_verified_cbz(
                job_id=fixture.job.job_id,
                chapter_id=fixture.chapter.chapter_id,
                result=outside_result,
            )
        oversized_result = replace(
            fixture.result,
            size_bytes=V1_MAX_CBZ_BYTES + 1,
        )
        with pytest.raises(ReaderAssetError):
            fixture.service.index_verified_cbz(
                job_id=fixture.job.job_id,
                chapter_id=fixture.chapter.chapter_id,
                result=oversized_result,
            )
        assert _job_status(fixture.database, fixture.job.job_id) == "INDEXING"
    finally:
        fixture.database.close()


def test_indexing_rejects_reparse_point_in_cbz_path(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    link_path = fixture.cbz_path.with_name("linked.cbz")
    try:
        try:
            link_path.symlink_to(fixture.cbz_path)
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"file reparse points are unavailable: {type(error).__name__}")
        result = replace(fixture.result, path=link_path)
        with pytest.raises(ReaderAssetError):
            fixture.service.index_verified_cbz(
                job_id=fixture.job.job_id,
                chapter_id=fixture.chapter.chapter_id,
                result=result,
            )
        assert _job_status(fixture.database, fixture.job.job_id) == "INDEXING"
    finally:
        fixture.database.close()


def test_migration_0018_adds_unique_asset_and_job_foreign_keys(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    try:
        with fixture.database.engine.connect() as connection:
            columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info('chapter_cbz_assets')"
                ).all()
            }
            assert columns == {
                "chapter_id",
                "asset_id",
                "download_job_id",
                "page_count",
                "indexed_at_us",
            }
            foreign_tables = {
                row[2]
                for row in connection.exec_driver_sql(
                    "PRAGMA foreign_key_list('chapter_cbz_assets')"
                ).all()
            }
            assert foreign_tables == {"chapters", "assets", "download_jobs"}
            unique_indexes = [
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA index_list('chapter_cbz_assets')"
                ).all()
                if row[2] == 1
            ]
            unique_columns = {
                tuple(
                    index_row[2]
                    for index_row in connection.exec_driver_sql(
                        f"PRAGMA index_info('{index_name}')"
                    ).all()
                )
                for index_name in unique_indexes
            }
            assert ("chapter_id",) in unique_columns
            assert ("asset_id",) in unique_columns
            assert ("download_job_id",) in unique_columns
    finally:
        fixture.database.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE download_jobs SET status = 'VERIFYING' WHERE job_id = :job_id",
        "UPDATE download_jobs SET cancel_requested_at_us = created_at_us, "
        "updated_at_us = created_at_us WHERE job_id = :job_id",
        "UPDATE download_jobs SET recovery_blocked = 1 WHERE job_id = :job_id",
    ],
    ids=["not-indexing", "cancellation-pending", "recovery-blocked"],
)
def test_indexing_refuses_non_indexing_or_unsafe_job_state(
    tmp_path: Path,
    mutation: str,
) -> None:
    fixture = _fixture(tmp_path)
    try:
        with fixture.database.write_session() as session:
            session.execute(text(mutation), {"job_id": str(fixture.job.job_id)})
        with pytest.raises(ReaderAssetError):
            fixture.service.index_verified_cbz(
                job_id=fixture.job.job_id,
                chapter_id=fixture.chapter.chapter_id,
                result=fixture.result,
            )
        with fixture.database.read_session() as session:
            assert session.execute(text("SELECT count(*) FROM assets")).scalar_one() == 0
            assert (
                session.execute(text("SELECT count(*) FROM chapter_cbz_assets")).scalar_one() == 0
            )
    finally:
        fixture.database.close()


def test_indexing_database_failure_rolls_back_asset_link_and_job_status(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    def fail_job_update(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "UPDATE download_jobs SET status" in statement:
            raise sqlite3.OperationalError("injected statement failure")

    event.listen(fixture.database.engine, "before_cursor_execute", fail_job_update)
    try:
        with pytest.raises(sqlite3.OperationalError):
            fixture.service.index_verified_cbz(
                job_id=fixture.job.job_id,
                chapter_id=fixture.chapter.chapter_id,
                result=fixture.result,
            )
    finally:
        event.remove(fixture.database.engine, "before_cursor_execute", fail_job_update)
    try:
        assert _job_status(fixture.database, fixture.job.job_id) == "INDEXING"
        with fixture.database.read_session() as session:
            assert session.execute(text("SELECT count(*) FROM assets")).scalar_one() == 0
            assert (
                session.execute(text("SELECT count(*) FROM chapter_cbz_assets")).scalar_one() == 0
            )
        assert fixture.cbz_path.is_file()
    finally:
        fixture.database.close()


def test_reader_route_never_exposes_filesystem_paths_and_streams_headers(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    try:
        fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=fixture.result,
        )
        from app.routes.reader import router

        app = FastAPI()
        app.state.runtime = SimpleNamespace(reader_asset_service=fixture.service)
        app.include_router(router)

        with cast(Any, TestClient(app)) as client:
            listing = client.get(f"/api/v1/reader/series/{fixture.series.series_id}/chapters")
            page = client.get(f"/api/v1/reader/{fixture.chapter.chapter_id}/pages/1")
            chapter = client.get(f"/api/v1/reader/{fixture.chapter.chapter_id}")

        assert listing.status_code == 200
        assert str(fixture.database.data_root_identity.canonical_path) not in listing.text
        assert chapter.status_code == 200
        assert "relative_path" not in chapter.text
        assert str(fixture.database.data_root_identity.canonical_path) not in chapter.text
        assert page.status_code == 200
        assert page.content == PNG_BYTES
        assert page.headers["content-type"] == "image/png"
        assert page.headers["etag"] == f'"{PNG_SHA256}"'
        assert page.headers["cache-control"] == "private, no-transform"
    finally:
        fixture.database.close()


def test_reader_stream_limits_are_required_and_reject_oversized_page(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    try:
        with pytest.raises(TypeError):
            cast(Any, ReaderAssetService)(
                fixture.database,
                data_root_identity=fixture.database.data_root_identity,
            )

        with pytest.raises(ValueError):
            ReaderAssetLimits(
                max_cbz_bytes=V1_MAX_CBZ_BYTES,
                max_page_bytes=V1_MAX_PAGE_BYTES - 1,
                chunk_bytes=V1_STREAM_CHUNK_BYTES,
            )

        fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=fixture.result,
        )
        small_limit_service = ReaderAssetService(
            fixture.database,
            data_root_identity=fixture.database.data_root_identity,
            limits=ReaderAssetLimits(
                max_cbz_bytes=V1_MAX_CBZ_BYTES,
                max_page_bytes=V1_MAX_PAGE_BYTES,
                chunk_bytes=V1_STREAM_CHUNK_BYTES,
            ),
        )
        with fixture.database.write_session() as session:
            session.execute(
                text(
                    "UPDATE download_pages SET size_bytes = :too_large "
                    "WHERE job_id = :job_id AND ordinal = 1"
                ),
                {
                    "too_large": V1_MAX_PAGE_BYTES + 1,
                    "job_id": str(fixture.job.job_id),
                },
            )
        with pytest.raises(ReaderAssetError):
            small_limit_service.open_page(fixture.chapter.chapter_id, 1)
    finally:
        fixture.database.close()


def test_reader_rejects_corruption_after_indexing_before_emitting_bytes(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    try:
        fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=fixture.result,
        )
        with fixture.cbz_path.open("r+b") as stream:
            stream.seek(0)
            stream.write(b"X")
        with pytest.raises(ReaderAssetError):
            fixture.service.open_page(fixture.chapter.chapter_id, 1)
    finally:
        fixture.database.close()
