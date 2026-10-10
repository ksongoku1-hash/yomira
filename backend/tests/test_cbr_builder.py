from __future__ import annotations

import errno
import hashlib
import io
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text
from support.sqlite import open_test_database

import app.services.cbr_builder as cbr_builder
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
from app.domain.asset import AssetType
from app.persistence.atomic_file_publisher import ADR_0087_RETRY_POLICY, AtomicFilePublisher
from app.persistence.database import SQLiteDatabase
from app.persistence.disk_budget import DiskBudgetManager
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.download_job import download_job_to_row
from app.persistence.mappers.download_page import download_page_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.routes.cbr_exports import router as cbr_exports_router
from app.services.cbr_builder import CbrExportFailure, CbrExportService
from app.services.cbz_builder import CBZBuildResult
from app.services.library import LibraryService
from app.services.rar5_store_writer import inspect_rar5_store_archive
from app.services.reader_asset import (
    V1_MAX_CBZ_BYTES,
    V1_MAX_PAGE_BYTES,
    V1_STREAM_CHUNK_BYTES,
    ReaderAssetLimits,
    ReaderAssetService,
)

_GIB = 1024 * 1024 * 1024


def _private_member(target: object, name: str) -> Any:
    return getattr(target, name)


@dataclass(frozen=True, slots=True)
class _Fixture:
    database: SQLiteDatabase
    service: CbrExportService
    series: Series
    chapter_ids: tuple[UUID, ...]
    cbz_paths: tuple[Path, ...]
    page_pixels: tuple[bytes, ...]
    usage: list[int]


def _png(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    with Image.new("RGB", (7, 5), color) as image:
        image.save(output, format="PNG")
    return output.getvalue()


def _fixture(tmp_path: Path, *, page_counts: tuple[int, int] = (2, 2)) -> _Fixture:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    identity = database.data_root_identity
    series = Series.new_local("CBR fixture", "CBR fixture")
    source = Source.new_local(
        "cbr-fixture",
        "1",
        "CBR fixture",
        True,
        SourceHealthStatus.HEALTHY,
    )
    chapters = (
        Chapter.new_local(series.series_id, "10", "Capítulo 10", chapter_sort_key=(10,)),
        Chapter.new_local(series.series_id, "2", "Capítulo 2", chapter_sort_key=(2,)),
    )
    chapter_sources = tuple(
        ChapterSource.new_local(
            chapter.chapter_id,
            source.source_id,
            remote_id=f"chapter-{chapter.chapter_raw_label}",
            available=True,
        )
        for chapter in chapters
    )
    jobs = tuple(
        replace(
            DownloadJob.new_local(chapter_source.chapter_source_id),
            status=DownloadJobStatus.INDEXING,
        )
        for chapter_source in chapter_sources
    )
    page_index = 0
    payloads_by_chapter: list[tuple[bytes, ...]] = []
    for page_count in page_counts:
        contents: list[bytes] = []
        for _ in range(page_count):
            contents.append(
                _png(
                    (
                        page_index % 256,
                        (page_index * 59) % 256,
                        (page_index * 131) % 256,
                    )
                )
            )
            page_index += 1
        payloads_by_chapter.append(tuple(contents))
    payloads = tuple(payloads_by_chapter)
    with database.write_session() as session:
        session.add_all([series_to_row(series), source_to_row(source)])
        session.flush()
        session.add_all(chapter_to_row(chapter) for chapter in chapters)
        session.flush()
        session.add_all(chapter_source_to_row(item) for item in chapter_sources)
        session.add_all(download_job_to_row(job) for job in jobs)
        session.flush()
        for job, page_payloads in zip(jobs, payloads, strict=True):
            session.add_all(
                download_page_to_row(
                    DownloadPage.new_local(
                        job.job_id,
                        ordinal,
                        f"fixture-page-{ordinal}",
                        status="VALIDATED",
                        size_bytes=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                        mime="image/png",
                    )
                )
                for ordinal, content in enumerate(page_payloads, start=1)
            )

    cbz_root = identity.canonical_path / "library" / "chapters" / str(series.series_id)
    cbz_root.mkdir(parents=True, exist_ok=True)
    cbz_paths: list[Path] = []
    reader = ReaderAssetService(
        database,
        data_root_identity=identity,
        library_root_identity=identity,
        limits=ReaderAssetLimits(
            max_cbz_bytes=V1_MAX_CBZ_BYTES,
            max_page_bytes=V1_MAX_PAGE_BYTES,
            chunk_bytes=V1_STREAM_CHUNK_BYTES,
        ),
    )
    for chapter, job, page_payloads in zip(chapters, jobs, payloads, strict=True):
        cbz_path = cbz_root / f"chapter-{chapter.chapter_raw_label}.cbz"
        with zipfile.ZipFile(cbz_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(
                "ComicInfo.xml",
                (
                    '<?xml version="1.0" encoding="utf-8"?>'
                    f"<ComicInfo><PageCount>{len(page_payloads)}</PageCount></ComicInfo>"
                ).encode(),
            )
            for ordinal, content in enumerate(page_payloads, start=1):
                archive.writestr(f"{ordinal:03d}.png", content)
        archive_bytes = cbz_path.read_bytes()
        reader.index_verified_cbz(
            job_id=job.job_id,
            chapter_id=chapter.chapter_id,
            result=CBZBuildResult(
                path=cbz_path,
                sha256=hashlib.sha256(archive_bytes).hexdigest(),
                size_bytes=len(archive_bytes),
                page_count=len(page_payloads),
            ),
        )
        cbz_paths.append(cbz_path)

    usage = [24 * _GIB]

    def disk_usage(_path: Path) -> object:
        return SimpleNamespace(total=32 * _GIB, used=0, free=usage[0])

    disk_budget = DiskBudgetManager(database, disk_usage_provider=disk_usage)
    publisher = AtomicFilePublisher(
        data_root_identity=identity,
        retry_policy=ADR_0087_RETRY_POLICY,
        sleep=lambda _delay: None,
        clock=lambda: 0.0,
        storage_gate=database.storage_gate,
    )
    service = CbrExportService(
        database=database,
        reader=reader,
        library=LibraryService(database),
        disk_budget=disk_budget,
        library_root_identity=identity,
        publisher=publisher,
        disk_usage_provider=disk_usage,
        sleep=lambda _delay: None,
    )
    return _Fixture(
        database=database,
        service=service,
        series=series,
        chapter_ids=tuple(chapter.chapter_id for chapter in chapters),
        cbz_paths=tuple(cbz_paths),
        page_pixels=tuple(content for chapter_pages in payloads for content in chapter_pages),
        usage=usage,
    )


def _rgba_bytes(payload: bytes) -> bytes:
    with Image.open(io.BytesIO(payload)) as image:
        converted = image.convert("RGBA")
        try:
            return converted.tobytes()
        finally:
            converted.close()


def test_create_aggregate_cbr_orders_pages_preserves_pixels_and_keeps_cbz(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    original_cbz = tuple(path.read_bytes() for path in fixture.cbz_paths)
    try:
        result = fixture.service.create(
            series_id=fixture.series.series_id,
            chapter_ids=tuple(reversed(fixture.chapter_ids)),
        )

        assert result.asset.asset_type is AssetType.CBR
        assert result.chapter_labels == ("Capítulo 2", "Capítulo 10")
        assert result.page_count == 4
        result_path = fixture.database.data_root_identity.canonical_path.joinpath(
            *result.asset.relative_path.split("/")
        )
        archive_bytes = result_path.read_bytes()
        assert result.filename == result_path.name
        assert result.asset.size_bytes == len(archive_bytes)
        assert result.asset.sha256 == hashlib.sha256(archive_bytes).hexdigest()
        members = inspect_rar5_store_archive(result_path)
        assert [member.name for member in members] == sorted(member.name for member in members)
        expected_names = [
            f"Capitulo_2_{fixture.chapter_ids[1].hex}_Pagina_00001.png",
            f"Capitulo_2_{fixture.chapter_ids[1].hex}_Pagina_00002.png",
            f"Capitulo_10_{fixture.chapter_ids[0].hex}_Pagina_00001.png",
            f"Capitulo_10_{fixture.chapter_ids[0].hex}_Pagina_00002.png",
        ]
        assert [member.name for member in members] == [
            f"{ordinal:05d}_{name}" for ordinal, name in enumerate(expected_names, start=1)
        ]
        expected_pages = (
            fixture.page_pixels[2],
            fixture.page_pixels[3],
            fixture.page_pixels[0],
            fixture.page_pixels[1],
        )
        with result_path.open("rb") as stream:
            for member, expected_page in zip(members, expected_pages, strict=True):
                stream.seek(member.data_offset)
                extracted = stream.read(member.size_bytes)
                assert len(extracted) == member.size_bytes
                assert extracted == expected_page
                assert _rgba_bytes(extracted) == _rgba_bytes(expected_page)
        assert tuple(path.read_bytes() for path in fixture.cbz_paths) == original_cbz
        stage_root = (
            fixture.database.data_root_identity.canonical_path
            / "exports"
            / "cbr"
            / ".staging"
        )
        assert list(stage_root.iterdir()) == []
        with fixture.database.read_session() as session:
            row = session.execute(
                text("SELECT asset_type, sha256 FROM assets WHERE asset_id = :asset_id"),
                {"asset_id": str(result.asset.asset_id)},
            ).one()
        assert row.asset_type == "CBR"
        assert row.sha256 == result.asset.sha256
    finally:
        fixture.database.close()


def test_16_bit_png_uses_the_rgba8_reader_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import struct
    import zlib

    def png16(color: tuple[int, int, int]) -> bytes:
        def chunk(kind: bytes, payload: bytes) -> bytes:
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(
                ">I", zlib.crc32(kind + payload) & 0xFFFFFFFF,
            )
        header = struct.pack(">IIBBBBB", 7, 5, 16, 2, 0, 0, 0)
        pixel = struct.pack(">HHH", *(channel * 257 for channel in color))
        scanlines = (b"\x00" + pixel * 7) * 5
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(
            b"IDAT", zlib.compress(scanlines),
        ) + chunk(b"IEND", b"")

    monkeypatch.setattr(__import__(__name__, fromlist=["_png"]), "_png", png16)
    fixture = _fixture(tmp_path)
    try:
        result = fixture.service.create(
            series_id=fixture.series.series_id, chapter_ids=fixture.chapter_ids,
        )
        path = fixture.database.data_root_identity.canonical_path / result.asset.relative_path
        members = inspect_rar5_store_archive(path)
        expected = fixture.page_pixels[2:] + fixture.page_pixels[:2]
        with path.open("rb") as stream:
            for member, original in zip(members, expected, strict=True):
                stream.seek(member.data_offset)
                encoded = stream.read(member.size_bytes)
                assert original[24] == 16
                assert encoded[24] == 8
                assert _rgba_bytes(encoded) == _rgba_bytes(original)
    finally:
        fixture.database.close()


def test_k287_cbr_integrity_gate_with_111_pages(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, page_counts=(56, 55))
    original_cbz = tuple(path.read_bytes() for path in fixture.cbz_paths)
    try:
        result = fixture.service.create(
            series_id=fixture.series.series_id,
            chapter_ids=tuple(reversed(fixture.chapter_ids)),
        )
        result_path = fixture.database.data_root_identity.canonical_path.joinpath(
            *result.asset.relative_path.split("/")
        )
        members = inspect_rar5_store_archive(result_path)
        expected_pages = fixture.page_pixels[56:] + fixture.page_pixels[:56]

        assert result.page_count == 111
        assert len(members) == 111
        assert result.chapter_labels == ("Capítulo 2", "Capítulo 10")
        expected_names = [
            *(
                f"Capitulo_2_{fixture.chapter_ids[1].hex}_Pagina_{ordinal:05d}.png"
                for ordinal in range(1, 56)
            ),
            *(
                f"Capitulo_10_{fixture.chapter_ids[0].hex}_Pagina_{ordinal:05d}.png"
                for ordinal in range(1, 57)
            ),
        ]
        assert [member.name for member in members] == [
            f"{ordinal:05d}_{name}" for ordinal, name in enumerate(expected_names, start=1)
        ]
        with result_path.open("rb") as stream:
            for member, expected_page in zip(members, expected_pages, strict=True):
                stream.seek(member.data_offset)
                encoded_png = stream.read(member.size_bytes)
                assert len(encoded_png) == member.size_bytes
                assert _rgba_bytes(encoded_png) == _rgba_bytes(expected_page)
        assert tuple(path.read_bytes() for path in fixture.cbz_paths) == original_cbz
    finally:
        fixture.database.close()


def test_cbr_interrupt_cleans_staging_and_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    original_cbz = tuple(path.read_bytes() for path in fixture.cbz_paths)
    original_convert: Any = _private_member(
        cbr_builder.CbrExportService, "_convert_verified_page_to_png"
    )
    calls = 0

    def interrupt_after_first_page(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = original_convert(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                cbr_builder.CbrExportService,
                "_convert_verified_page_to_png",
                interrupt_after_first_page,
            )
            with pytest.raises(KeyboardInterrupt):
                fixture.service.create(
                    series_id=fixture.series.series_id,
                    chapter_ids=fixture.chapter_ids,
                )

        output_root = fixture.database.data_root_identity.canonical_path / "exports" / "cbr"
        assert not list(output_root.glob("*/*.cbr"))
        assert list((output_root / ".staging").iterdir()) == []
        assert _private_member(fixture.service, "_disk_budget").snapshot().reserved_bytes == 0
        assert tuple(path.read_bytes() for path in fixture.cbz_paths) == original_cbz

        try:
            result = fixture.service.create(
                series_id=fixture.series.series_id,
                chapter_ids=fixture.chapter_ids,
            )
        except CbrExportFailure as error:
            pytest.fail(
                f"interrupted export did not recover: {error.code} "
                f"stage={error.stage} cause={error.cause_type}"
            )
        assert result.page_count == 4
        assert tuple(path.read_bytes() for path in fixture.cbz_paths) == original_cbz
    finally:
        fixture.database.close()


def test_cbr_disk_full_mid_write_cleans_staging_and_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    original_cbz = tuple(path.read_bytes() for path in fixture.cbz_paths)
    png_write_budget: Any = _private_member(cbr_builder, "_PngWriteBudget")
    original_write: Any = _private_member(png_write_budget, "write")
    calls = 0

    def disk_full_after_partial_page(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.ENOSPC, "injected disk full during CBR page staging")
        return original_write(*args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                png_write_budget,
                "write",
                disk_full_after_partial_page,
            )
            with pytest.raises(CbrExportFailure) as raised:
                fixture.service.create(
                    series_id=fixture.series.series_id,
                    chapter_ids=fixture.chapter_ids,
                )

        assert raised.value.code.value == "EXP_DISK_FULL"
        assert raised.value.trace_id
        output_root = fixture.database.data_root_identity.canonical_path / "exports" / "cbr"
        assert not list(output_root.glob("*/*.cbr"))
        assert list((output_root / ".staging").iterdir()) == []
        assert _private_member(fixture.service, "_disk_budget").snapshot().reserved_bytes == 0
        assert tuple(path.read_bytes() for path in fixture.cbz_paths) == original_cbz

        try:
            result = fixture.service.create(
                series_id=fixture.series.series_id,
                chapter_ids=fixture.chapter_ids,
            )
        except CbrExportFailure as error:
            pytest.fail(
                f"disk-full export did not recover: {error.code} "
                f"stage={error.stage} cause={error.cause_type}"
            )
        assert result.page_count == 4
        assert tuple(path.read_bytes() for path in fixture.cbz_paths) == original_cbz
    finally:
        fixture.database.close()


def test_cbr_destination_collision_preserves_existing_file_and_reports_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    original_cbz = tuple(path.read_bytes() for path in fixture.cbz_paths)
    operation_id = UUID("00000000-0000-4000-8000-000000000287")
    try:
        output_directory = (
            fixture.database.data_root_identity.canonical_path
            / "exports"
            / "cbr"
            / str(fixture.series.series_id)
        )
        output_directory.mkdir(parents=True)
        archive_filename: Any = _private_member(
            cbr_builder.CbrExportService, "_archive_filename"
        )
        destination = output_directory / archive_filename(
            fixture.series.canonical_title,
            "2",
            "10",
            operation_id,
        )
        existing_bytes = b"preserve pre-existing CBR"
        destination.write_bytes(existing_bytes)

        with monkeypatch.context() as patch:
            patch.setattr(cbr_builder, "uuid4", lambda: operation_id)
            with pytest.raises(CbrExportFailure) as raised:
                fixture.service.create(
                    series_id=fixture.series.series_id,
                    chapter_ids=tuple(reversed(fixture.chapter_ids)),
                )

        assert raised.value.code.value == "EXP_DESTINATION_CONFLICT"
        assert raised.value.status_code == 409
        assert raised.value.trace_id
        assert destination.read_bytes() == existing_bytes
        assert list(output_directory.glob("*.cbr")) == [destination]
        staging_root = (
            fixture.database.data_root_identity.canonical_path
            / "exports"
            / "cbr"
            / ".staging"
        )
        assert list(staging_root.iterdir()) == []
        assert _private_member(fixture.service, "_disk_budget").snapshot().reserved_bytes == 0
        assert tuple(path.read_bytes() for path in fixture.cbz_paths) == original_cbz

        retry = fixture.service.create(
            series_id=fixture.series.series_id,
            chapter_ids=fixture.chapter_ids,
        )
        assert retry.page_count == 4
        assert destination.read_bytes() == existing_bytes
        assert tuple(path.read_bytes() for path in fixture.cbz_paths) == original_cbz
    finally:
        fixture.database.close()


def test_cbr_atomic_publication_race_reports_destination_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    original_cbz = tuple(path.read_bytes() for path in fixture.cbz_paths)
    existing_bytes = b"destination created immediately before publish"
    publisher: Any = _private_member(fixture.service, "_publisher")
    original_publish = publisher.publish_no_replace
    collided_destination: list[Path] = []

    def collide_before_publish(
        source: Path,
        destination: Path,
        *,
        job_id: UUID | str | None = None,
    ) -> None:
        destination.write_bytes(existing_bytes)
        collided_destination.append(destination)
        original_publish(source, destination, job_id=job_id)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(publisher, "publish_no_replace", collide_before_publish)
            with pytest.raises(CbrExportFailure) as raised:
                fixture.service.create(
                    series_id=fixture.series.series_id,
                    chapter_ids=fixture.chapter_ids,
                )

        assert raised.value.code.value == "EXP_DESTINATION_CONFLICT"
        assert raised.value.status_code == 409
        assert len(collided_destination) == 1
        assert collided_destination[0].read_bytes() == existing_bytes
        assert list(collided_destination[0].parent.glob("*.cbr")) == collided_destination
        staging_root = (
            fixture.database.data_root_identity.canonical_path
            / "exports"
            / "cbr"
            / ".staging"
        )
        assert list(staging_root.iterdir()) == []
        assert _private_member(fixture.service, "_disk_budget").snapshot().reserved_bytes == 0
        assert tuple(path.read_bytes() for path in fixture.cbz_paths) == original_cbz
    finally:
        fixture.database.close()


def test_cbr_startup_recovery_removes_only_abandoned_managed_staging(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    original_cbz = tuple(path.read_bytes() for path in fixture.cbz_paths)
    stage_root = (
        fixture.database.data_root_identity.canonical_path
        / "exports"
        / "cbr"
        / ".staging"
    )
    stale_stage = stage_root / "00000000-0000-4000-8000-000000000287"
    unknown_stage = stage_root / "00000000-0000-4000-8000-000000000288"
    unmanaged_entry = stage_root / "keep-this-entry"
    try:
        stale_stage.mkdir(parents=True)
        (stale_stage / "page-00001.png").write_bytes(b"partial page after process stop")
        (stale_stage / "archive.tmp").write_bytes(b"partial RAR after process stop")
        unknown_stage.mkdir()
        (unknown_stage / "user-data.txt").write_text("preserve", encoding="utf-8")
        unmanaged_entry.mkdir()

        assert fixture.service.recover_stale_staging() == 1
        assert not stale_stage.exists()
        assert (unknown_stage / "user-data.txt").read_text(encoding="utf-8") == "preserve"
        assert unmanaged_entry.is_dir()
        assert tuple(path.read_bytes() for path in fixture.cbz_paths) == original_cbz

        result = fixture.service.create(
            series_id=fixture.series.series_id,
            chapter_ids=fixture.chapter_ids,
        )
        assert result.page_count == 4
        assert tuple(path.read_bytes() for path in fixture.cbz_paths) == original_cbz
    finally:
        fixture.database.close()


def test_changed_source_cbz_aborts_without_publishing_or_mutating_any_cbz(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    changed_path = fixture.cbz_paths[1]
    changed_path.write_bytes(changed_path.read_bytes() + b"changed")
    expected_cbz = tuple(path.read_bytes() for path in fixture.cbz_paths)
    try:
        with pytest.raises(CbrExportFailure) as raised:
            fixture.service.create(
                series_id=fixture.series.series_id,
                chapter_ids=fixture.chapter_ids,
            )

        assert raised.value.code.value == "EXP_HASH_MISMATCH"
        assert raised.value.trace_id
        output_root = fixture.database.data_root_identity.canonical_path / "exports" / "cbr"
        assert not list(output_root.glob("*/*.cbr"))
        assert list((output_root / ".staging").iterdir()) == []
        assert tuple(path.read_bytes() for path in fixture.cbz_paths) == expected_cbz
    finally:
        fixture.database.close()


def test_disk_preflight_failure_releases_budget_and_lock_for_retry(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    try:
        fixture.usage[0] = _GIB
        with pytest.raises(CbrExportFailure) as raised:
            fixture.service.create(
                series_id=fixture.series.series_id,
                chapter_ids=fixture.chapter_ids,
            )
        assert raised.value.code.value == "SYS_DISK_BUDGET_EXCEEDED"
        assert raised.value.status_code == 507

        fixture.usage[0] = 24 * _GIB
        result = fixture.service.create(
            series_id=fixture.series.series_id,
            chapter_ids=fixture.chapter_ids,
        )
        assert result.asset.asset_type is AssetType.CBR
    finally:
        fixture.database.close()


def test_cbr_api_returns_verified_asset_metadata_without_filesystem_paths(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    application = FastAPI()
    application.state.runtime = SimpleNamespace(cbr_export_service=fixture.service)
    application.include_router(cbr_exports_router)
    client: Any = TestClient(application)
    try:
        with client:
            response: Any = client.post(
                "/api/v1/cbr-exports",
                json={
                    "series_id": str(fixture.series.series_id),
                    "chapter_ids": [
                        str(chapter_id)
                        for chapter_id in reversed(fixture.chapter_ids)
                    ],
                },
            )

        assert response.status_code == 200, response.text
        payload: dict[str, Any] = response.json()
        assert set(payload) == {"data"}
        assert set(payload["data"]) == {
            "asset_id",
            "filename",
            "series_title",
            "chapter_labels",
            "page_count",
            "size_bytes",
            "sha256",
        }
        assert payload["data"]["chapter_labels"] == ["Capítulo 2", "Capítulo 10"]
        assert payload["data"]["page_count"] == 4
        assert "relative_path" not in response.text
        assert str(tmp_path) not in response.text
    finally:
        fixture.database.close()
