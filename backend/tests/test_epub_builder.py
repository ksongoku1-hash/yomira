from __future__ import annotations

import errno
import hashlib
import io
import os
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

import pytest
from PIL import Image
from support.sqlite import open_test_database

import app.services.epub_builder as epub_builder
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
from app.persistence.disk_budget import MINIMUM_RESERVED_BYTES, DiskBudgetManager
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.download_job import download_job_to_row
from app.persistence.mappers.download_page import download_page_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.repositories.chapter_cbz_asset import ReaderPageRecord
from app.services.cbz_builder import CBZBuildResult
from app.services.epub_builder import (
    MAX_EPUB_BYTES,
    MAX_EPUB_PAGES,
    EpubBuildCancelled,
    EpubBuilder,
    EpubBuildError,
    EpubBuildResult,
    EpubChapter,
)
from app.services.local_import import _archive_pages  # pyright: ignore[reportPrivateUsage]
from app.services.reader_asset import ReaderAssetLimits, ReaderAssetService

_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "x": "http://www.w3.org/1999/xhtml",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_FINGERPRINT = "a" * 64


def test_update_reuses_verified_conversion_and_rechecks_original_pages(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bytes] = []
    convert = epub_builder._webp_to_png  # pyright: ignore[reportPrivateUsage]

    def converted(body: bytes) -> bytes:
        calls.append(body)
        return convert(body)

    monkeypatch.setattr(epub_builder, "_webp_to_png", converted)
    fixture.build()
    assert calls == [fixture.bodies[-1]]
    with zipfile.ZipFile(fixture.output) as book:
        original_png = book.read("EPUB/images/p000003.png")
    fixture.build(title="Título actualizado", fingerprint="b" * 64)
    assert len(calls) == 1
    with zipfile.ZipFile(fixture.output) as book:
        assert book.testzip() is None
        assert book.read("EPUB/images/p000003.png") == original_png
        assert b"actualizado" in book.read("EPUB/package.opf")
    fixture.source_paths[-1].write_bytes(b"damaged source")
    previous = fixture.output.read_bytes()
    with pytest.raises(EpubBuildError):
        fixture.build(fingerprint="c" * 64)
    assert fixture.output.read_bytes() == previous


def test_corrupt_reused_image_is_reconverted(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture.build()
    with zipfile.ZipFile(fixture.output) as book:
        resources = [(info, book.read(info)) for info in book.infolist()]
    with zipfile.ZipFile(fixture.output, "w") as book:
        for info, content in resources:
            book.writestr(
                info, b"broken PNG" if info.filename == "EPUB/images/p000003.png" else content
            )
    calls: list[bytes] = []
    convert = epub_builder._webp_to_png  # pyright: ignore[reportPrivateUsage]

    def converted(body: bytes) -> bytes:
        calls.append(body)
        return convert(body)

    monkeypatch.setattr(epub_builder, "_webp_to_png", converted)
    fixture.build(fingerprint="b" * 64)
    assert calls == [fixture.bodies[-1]]
    with zipfile.ZipFile(fixture.output) as book:
        assert book.testzip() is None
        assert book.read("EPUB/images/p000003.png").startswith(b"\x89PNG")


def test_reuse_rejects_wrong_pixels_even_when_conversion_index_matches(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture.build()
    with zipfile.ZipFile(fixture.output) as book:
        resources = [(info, book.read(info)) for info in book.infolist()]
        expected_png = book.read("EPUB/images/p000003.png")
    wrong_png = fixture.bodies[0]
    old_hash = hashlib.sha256(expected_png).hexdigest().encode()
    wrong_hash = hashlib.sha256(wrong_png).hexdigest().encode()
    with zipfile.ZipFile(fixture.output, "w") as book:
        for info, content in resources:
            if info.filename == "EPUB/images/p000003.png":
                content = wrong_png
            elif info.filename == "EPUB/package.opf":
                content = content.replace(old_hash, wrong_hash)
            book.writestr(info, content)
    fixture.build(fingerprint="b" * 64)
    with zipfile.ZipFile(fixture.output) as book:
        assert book.read("EPUB/images/p000003.png") == expected_png


@dataclass
class Fixture:
    database: SQLiteDatabase
    reader: ReaderAssetService
    budget: DiskBudgetManager
    builder: EpubBuilder
    series_id: UUID
    chapters: tuple[EpubChapter, ...]
    source_paths: tuple[Path, ...]
    bodies: tuple[bytes, ...]
    formats: tuple[str, ...]
    output: Path
    temporary: Path
    free: list[int]

    def build(self, **overrides: object) -> EpubBuildResult:
        arguments: dict[str, object] = dict(
            series_id=self.series_id,
            title='Título & <manga> "日本語"',
            author="A & B <autor>",
            language="es-MX",
            chapters=self.chapters,
            fingerprint=_FINGERPRINT,
            temporary_path=self.temporary,
            destination_path=self.output,
        )
        arguments.update(overrides)
        return self.builder.build(**arguments)  # pyright: ignore[reportArgumentType]


def _image(format_name: str, width: int, height: int) -> bytes:
    output = io.BytesIO()
    with Image.new("RGB", (width, height), (40 + width, 90, 150)) as image:
        image.save(output, format=format_name)
    return output.getvalue()


def make_fixture(tmp_path: Path) -> Fixture:
    """A real indexed CBZ input reusable by the independent EPUB conformance check."""
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    identity = database.data_root_identity
    reader = ReaderAssetService(
        database,
        data_root_identity=identity,
        library_root_identity=identity,
        limits=ReaderAssetLimits(2_147_483_648, 67_108_864, 65_536),
    )
    series = Series.new_local("EPUB fixture", "EPUB fixture")
    source = Source.new_local("epub-fixture", "1", "EPUB fixture", True, SourceHealthStatus.HEALTHY)
    chapters = tuple(
        Chapter.new_local(series.series_id, str(number), label, chapter_sort_key=(number,))
        for number, label in (
            (2, 'Capítulo 2 & "日本語" <inicio>'),
            (10, "Capítulo 10 — final"),
        )
    )
    chapter_sources = tuple(
        ChapterSource.new_local(
            chapter.chapter_id,
            source.source_id,
            remote_id=str(index),
            available=True,
        )
        for index, chapter in enumerate(chapters)
    )
    jobs = tuple(
        replace(DownloadJob.new_local(item.chapter_source_id), status=DownloadJobStatus.INDEXING)
        for item in chapter_sources
    )
    formats = ("PNG", "JPEG", "WEBP")
    bodies = tuple(_image(name, index + 7, index + 5) for index, name in enumerate(formats))
    page_sets = (bodies[:2], bodies[2:])
    mimes = ("image/png", "image/jpeg", "image/webp")
    with database.write_session() as session:
        session.add_all([series_to_row(series), source_to_row(source)])
        session.flush()
        session.add_all(chapter_to_row(chapter) for chapter in chapters)
        session.flush()
        session.add_all(chapter_source_to_row(item) for item in chapter_sources)
        session.add_all(download_job_to_row(job) for job in jobs)
        session.flush()
        for chapter_index, (job, contents) in enumerate(zip(jobs, page_sets, strict=True)):
            for ordinal, content in enumerate(contents, start=1):
                session.add(
                    download_page_to_row(
                        DownloadPage.new_local(
                            job.job_id,
                            ordinal,
                            f"fixture-{ordinal}",
                            status="VALIDATED",
                            size_bytes=len(content),
                            sha256=hashlib.sha256(content).hexdigest(),
                            mime=mimes[ordinal - 1 if chapter_index == 0 else 2],
                        )
                    )
                )
    source_root = identity.canonical_path / "chapters"
    output_root = identity.canonical_path / "EPUB" / str(series.series_id)
    source_root.mkdir()
    output_root.mkdir(parents=True)
    snapshots: list[EpubChapter] = []
    paths: list[Path] = []
    for index, (chapter, job, contents) in enumerate(zip(chapters, jobs, page_sets, strict=True)):
        path = source_root / f"chapter-{index}.cbz"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(
                "ComicInfo.xml", f"<ComicInfo><PageCount>{len(contents)}</PageCount></ComicInfo>"
            )
            extensions = ("png", "jpg") if index == 0 else ("webp",)
            for ordinal, (content, extension) in enumerate(
                zip(contents, extensions, strict=True), 1
            ):
                archive.writestr(f"{ordinal:03d}.{extension}", content)
        content = path.read_bytes()
        sha256 = hashlib.sha256(content).hexdigest()
        reader.index_verified_cbz(
            job_id=job.job_id,
            chapter_id=chapter.chapter_id,
            result=CBZBuildResult(path, sha256, len(content), len(contents)),
        )
        snapshots.append(
            EpubChapter(
                chapter.chapter_id,
                chapter.display_label,
                len(contents),
                sha256,
                sum(map(len, contents)),
            )
        )
        paths.append(path)
    free = [256 * 1024**3]
    budget = DiskBudgetManager(
        database,
        disk_usage_provider=lambda _path: SimpleNamespace(
            total=512 * 1024**3,
            used=0,
            free=free[0],
        ),
    )
    return Fixture(
        database,
        reader,
        budget,
        EpubBuilder(reader, budget, identity, database.storage_gate),
        series.series_id,
        tuple(snapshots),
        tuple(paths),
        bodies,
        formats,
        output_root / "Manga.epub",
        output_root / ".candidate.epub.tmp",
        free,
    )


@pytest.fixture
def fixture(tmp_path: Path) -> Iterator[Fixture]:
    value = make_fixture(tmp_path)
    try:
        yield value
    finally:
        value.database.close()


def test_build_preserves_original_images_metadata_order_and_importable_spine(
    fixture: Fixture,
) -> None:
    before = tuple(path.read_bytes() for path in fixture.source_paths)
    progress: list[tuple[int, int]] = []

    def on_progress(completed: int, total: int) -> None:
        progress.append((completed, total))

    result = fixture.build(on_progress=on_progress)
    assert result.path == fixture.output and result.filename == "Manga.epub"
    assert result.chapter_count == 2 and result.page_count == 3
    assert result.fingerprint == _FINGERPRINT and progress == [(1, 3), (2, 3), (3, 3)]
    assert result.sha256 == hashlib.sha256(fixture.output.read_bytes()).hexdigest()
    assert result.size_bytes == fixture.output.stat().st_size
    assert not fixture.temporary.exists() and fixture.budget.snapshot().reserved_bytes == 0
    assert tuple(path.read_bytes() for path in fixture.source_paths) == before
    with zipfile.ZipFile(result.path) as archive:
        assert archive.namelist()[0] == "mimetype"
        mime = archive.getinfo("mimetype")
        assert (
            mime.header_offset == 0 and mime.compress_type == zipfile.ZIP_STORED and not mime.extra
        )
        assert archive.read(mime) == b"application/epub+zip" and archive.testzip() is None
        package = ET.fromstring(archive.read("EPUB/package.opf"))
        assert package.attrib["version"] == "3.0"
        assert package.attrib["prefix"] == "yomira: urn:yomira:metadata:"
        assert (
            package.findtext("opf:metadata/dc:title", namespaces=_NS) == 'Título & <manga> "日本語"'
        )
        assert package.findtext("opf:metadata/dc:creator", namespaces=_NS) == "A & B <autor>"
        assert package.findtext("opf:metadata/dc:language", namespaces=_NS) == "es-MX"
        metadata = {
            meta.attrib["property"]: meta.text
            for meta in package.findall("opf:metadata/opf:meta", _NS)
        }
        assert metadata["yomira:fingerprint"] == _FINGERPRINT
        assert metadata["yomira:chapter-count"] == "2" and metadata["yomira:page-count"] == "3"
        assert metadata["rendition:layout"] == "reflowable"
        assert package.find("opf:spine", _NS).get("toc") == "ncx"  # type: ignore[union-attr]
        spine = package.findall("opf:spine/opf:itemref", _NS)
        assert [item.attrib["idref"] for item in spine] == ["page-1", "page-3"]
        assert "EPUB/pages/p000002.xhtml" not in archive.namelist()
        images, imported = _archive_pages(archive, epub=True)
        assert [archive.read(name) for name in images[:2]] == list(fixture.bodies[:2])
        assert all(not name.endswith(".webp") for name in archive.namelist())
        for name, original in zip(images, fixture.bodies, strict=True):
            with (
                Image.open(io.BytesIO(archive.read(name))) as actual,
                Image.open(io.BytesIO(original)) as source,
            ):
                assert actual.size == source.size
                assert actual.convert("RGBA").tobytes() == source.convert("RGBA").tobytes()
        assert imported["Title"] == 'Título & <manga> "日本語"'
        manifest = package.findall("opf:manifest/opf:item", _NS)
        cover = [item for item in manifest if item.attrib.get("properties") == "cover-image"]
        assert len(cover) == 1 and cover[0].attrib["href"] == "images/p000001.png"
        nav = ET.fromstring(archive.read("EPUB/nav.xhtml"))
        links = nav.findall("x:body/x:nav[@id='toc']/x:ol/x:li/x:a", _NS)
        assert [node.text for node in links] == [chapter.label for chapter in fixture.chapters]
        assert [node.attrib["href"] for node in links] == [
            "pages/p000001.xhtml",
            "pages/p000003.xhtml",
        ]
        ncx = ET.fromstring(archive.read("EPUB/toc.ncx"))
        ncx_links = ncx.findall("{*}navMap/{*}navPoint/{*}content")
        assert [node.get("src") for node in ncx_links] == [node.get("href") for node in links]
        for index, expected_pages in [(1, [1, 2]), (3, [3])]:
            document = ET.fromstring(archive.read(f"EPUB/pages/p{index:06d}.xhtml"))
            viewport = document.find("x:head/x:meta", _NS)
            assert viewport is None
            assert "width:100%" not in (document.findtext("x:head/x:style", namespaces=_NS) or "")
            page_images = document.findall("x:body/x:img", _NS)
            assert [image.get("src") for image in page_images] == [
                "../" + images[ordinal - 1].removeprefix("EPUB/") for ordinal in expected_pages
            ]
            assert all(
                image.get("width") is None and image.get("height") is None for image in page_images
            )
            style = document.findtext("x:head/x:style", namespaces=_NS) or ""
            assert "page-break-before:always" in style


def test_success_updates_the_same_destination(fixture: Fixture) -> None:
    fixture.output.write_bytes(b"previous publication")
    result = fixture.build()
    assert zipfile.is_zipfile(result.path) and not fixture.temporary.exists()


@pytest.mark.parametrize(
    "failure", ["cancel", "callback", "write", "verify", "crc", "publish", "page"]
)
def test_failure_preserves_previous_publication_and_releases_staging(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    original = b"previous publication"
    fixture.output.write_bytes(original)
    source_before = tuple(path.read_bytes() for path in fixture.source_paths)
    arguments: dict[str, object] = {}
    if failure == "cancel":
        completed = [0]

        def on_progress(done: int, _total: int) -> None:
            completed[0] = done

        arguments.update(cancelled=lambda: completed[0] > 0, on_progress=on_progress)
    elif failure == "callback":

        def progress(_done: int, _total: int) -> None:
            raise RuntimeError("callback failed")

        arguments["on_progress"] = progress
    elif failure == "write":

        def write(*_args: object, **_kwargs: object) -> int:
            raise OSError(errno.ENOSPC, "full")

        monkeypatch.setattr(fixture.budget, "write_reserved", write)
    elif failure == "verify":

        def verify(*_args: object, **_kwargs: object) -> None:
            raise EpubBuildError("injected_verify")

        monkeypatch.setattr(fixture.builder, "_verify", verify)
    elif failure == "crc":
        verify_original = fixture.builder._verify  # pyright: ignore[reportPrivateUsage]

        def corrupt_candidate(
            path: Path,
            expected: dict[str, tuple[int, str]],
            cancelled: object,
        ) -> None:
            with zipfile.ZipFile(path) as archive:
                image = archive.getinfo("EPUB/images/p000001.png")
            with path.open("r+b") as stream:
                stream.seek(image.header_offset + 26)
                name_length = int.from_bytes(stream.read(2), "little")
                extra_length = int.from_bytes(stream.read(2), "little")
                stream.seek(image.header_offset + 30 + name_length + extra_length)
                value = stream.read(1)
                stream.seek(-1, os.SEEK_CUR)
                stream.write(bytes([value[0] ^ 1]))
            assert cancelled is None
            verify_original(path, expected, None)

        monkeypatch.setattr(fixture.builder, "_verify", corrupt_candidate)
    elif failure == "publish":

        def fail_replace(*_args: object) -> None:
            raise PermissionError("locked")

        monkeypatch.setattr(epub_builder.os, "replace", fail_replace)
    else:
        read = fixture.reader.iter_verified_chapter_pages

        def corrupt(chapter_id: UUID) -> Iterator[tuple[ReaderPageRecord, bytes]]:
            for record, _body in read(chapter_id):
                body = b"not a real image"
                yield (
                    replace(
                        record,
                        page_size_bytes=len(body),
                        page_sha256=hashlib.sha256(body).hexdigest(),
                    ),
                    body,
                )

        monkeypatch.setattr(fixture.reader, "iter_verified_chapter_pages", corrupt)
    with pytest.raises(EpubBuildCancelled if failure == "cancel" else EpubBuildError):
        fixture.build(**arguments)
    assert fixture.output.read_bytes() == original
    assert fixture.budget.snapshot().reserved_bytes == 0
    assert fixture.temporary.exists() == (failure == "write")
    assert tuple(path.read_bytes() for path in fixture.source_paths) == source_before


@pytest.mark.parametrize("field", ["asset_sha256", "image_bytes", "page_count"])
def test_rejects_changed_chapter_snapshot(fixture: Fixture, field: str) -> None:
    first = fixture.chapters[0]
    changes: dict[str, object] = {
        "asset_sha256": "b" * 64,
        "image_bytes": first.image_bytes + 1,
        "page_count": first.page_count + 1,
    }
    changed = replace(first, **{field: changes[field]})  # pyright: ignore[reportArgumentType]
    with pytest.raises(EpubBuildError, match="chapter_changed"):
        fixture.build(chapters=(changed, fixture.chapters[1]))
    assert not fixture.output.exists() and not fixture.temporary.exists()
    assert fixture.budget.snapshot().reserved_bytes == 0


def test_budget_admission_precedes_file_creation(fixture: Fixture) -> None:
    fixture.free[0] = MINIMUM_RESERVED_BYTES
    fixture.output.write_bytes(b"old")
    with pytest.raises(EpubBuildError, match="reserve"):
        fixture.build()
    assert fixture.output.read_bytes() == b"old" and not fixture.temporary.exists()
    assert fixture.budget.snapshot().reserved_bytes == 0


def test_zip64_is_used_only_when_sizes_require_it(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 128)
    result = fixture.build()
    data = result.path.read_bytes()
    assert b"PK\x06\x06" in data
    with zipfile.ZipFile(result.path) as archive:
        assert not archive.getinfo("mimetype").extra
        assert any(info.extract_version >= 45 for info in archive.infolist())
        assert archive.testzip() is None
        images, _metadata = _archive_pages(archive, epub=True)
        assert [archive.read(name) for name in images[:2]] == list(fixture.bodies[:2])
        assert images[2].endswith(".png")


def test_rejects_unsafe_paths_and_never_deletes_preexisting_staging(fixture: Fixture) -> None:
    fixture.temporary.write_bytes(b"not ours")
    with pytest.raises(ValueError, match="already exist"):
        fixture.build()
    assert fixture.temporary.read_bytes() == b"not ours"
    fixture.temporary.unlink()
    with pytest.raises(ValueError):
        fixture.build(destination_path=fixture.output.parent / ".." / "unsafe.epub")
    assert not fixture.temporary.exists()


def test_rejects_reparse_or_hardlinked_destinations(fixture: Fixture, tmp_path: Path) -> None:
    original = tmp_path / "original.epub"
    original.write_bytes(b"outside")
    try:
        os.link(original, fixture.output)
    except OSError:
        pytest.skip("test volume does not support hardlinks")
    with pytest.raises(ValueError, match="regular managed"):
        fixture.build()
    assert original.read_bytes() == b"outside" and not fixture.temporary.exists()


def test_limits_are_large_but_explicit_and_validate_metadata(fixture: Fixture) -> None:
    assert MAX_EPUB_BYTES == 64 * 1024**3 and MAX_EPUB_PAGES == 100_000
    with pytest.raises(ValueError):
        replace(fixture.chapters[0], page_count=True)
    with pytest.raises(ValueError):
        fixture.build(chapters=(fixture.chapters[0], fixture.chapters[0]))
    with pytest.raises(ValueError, match="invalid XML"):
        fixture.build(title="bad\x00title")
    with pytest.raises(ValueError):
        fixture.build(series_id=uuid4().hex)
    assert not fixture.temporary.exists()


def test_output_volume_space_is_checked_separately(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture.output.write_bytes(b"old")

    def no_space(_path: Path) -> SimpleNamespace:
        return SimpleNamespace(free=0)

    monkeypatch.setattr(epub_builder, "disk_usage", no_space)
    with pytest.raises(EpubBuildError, match="reserve"):
        fixture.build()
    assert fixture.output.read_bytes() == b"old" and not fixture.temporary.exists()
    assert fixture.budget.snapshot().reserved_bytes == 0


def test_zip_backpatching_only_consumes_physical_growth(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_reserved = fixture.budget.write_reserved
    growth: list[int] = []

    def measure(
        operation_id: UUID, *, category: str, requested_bytes: int, write: Callable[[], int]
    ) -> int:
        assert category == "cbz" and callable(write)
        growth.append(requested_bytes)
        return write_reserved(
            operation_id, category="cbz", requested_bytes=requested_bytes, write=write
        )

    monkeypatch.setattr(fixture.budget, "write_reserved", measure)
    result = fixture.build()
    assert sum(growth) == result.size_bytes and 0 in growth
    assert fixture.budget.snapshot().reserved_bytes == 0


def test_large_reservation_is_not_limited_to_eight_gibibytes(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reserve = fixture.budget.reserve_bounded_operation
    requested: list[int] = []

    def reserve_and_cancel(operation_id: UUID, *, page_bytes: int, cbz_bytes: int = 0) -> None:
        reserve(operation_id, page_bytes=page_bytes, cbz_bytes=cbz_bytes)
        requested.append(cbz_bytes)

    monkeypatch.setattr(fixture.budget, "reserve_bounded_operation", reserve_and_cancel)

    def abundant_space(_path: Path) -> SimpleNamespace:
        return SimpleNamespace(free=256 * 1024**3)

    monkeypatch.setattr(epub_builder, "disk_usage", abundant_space)
    larger = replace(fixture.chapters[0], image_bytes=20 * 1024**3)
    with pytest.raises(EpubBuildCancelled):
        fixture.build(chapters=(larger, fixture.chapters[1]), cancelled=lambda: bool(requested))
    assert 20 * 1024**3 < requested[0] <= MAX_EPUB_BYTES
    assert not fixture.temporary.exists() and fixture.budget.snapshot().reserved_bytes == 0


def test_does_not_replace_a_destination_changed_during_the_build(fixture: Fixture) -> None:
    fixture.output.write_bytes(b"old")

    def external_update(_done: int, _total: int) -> None:
        fixture.output.write_bytes(b"externally updated")

    with pytest.raises(EpubBuildError, match="destination_changed"):
        fixture.build(on_progress=external_update)
    assert fixture.output.read_bytes() == b"externally updated"
    assert not fixture.temporary.exists() and fixture.budget.snapshot().reserved_bytes == 0


def test_storage_closure_during_build_keeps_old_book_and_tracks_mutation(fixture: Fixture) -> None:
    fixture.output.write_bytes(b"old book")
    gate = fixture.database.storage_gate

    def close_storage(_done: int, _total: int) -> None:
        assert gate.active_mutations == 1
        gate.close("FILESYSTEM_DISK_FULL")

    with pytest.raises(EpubBuildError):
        fixture.build(on_progress=close_storage)
    assert fixture.output.read_bytes() == b"old book"
    assert gate.active_mutations == 0 and fixture.budget.snapshot().reserved_bytes == 0
    assert fixture.temporary.exists()


def test_converted_page_growth_reserves_space_and_cannot_overallocate(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = epub_builder._webp_to_png  # pyright: ignore[reportPrivateUsage]
    increase = fixture.budget.increase_bounded_cbz_reservation
    sizes: list[int] = []

    def larger(payload: bytes) -> bytes:
        return original(payload) + b"padding" * 2000

    def measure(operation_id: UUID, additional_bytes: int) -> None:
        sizes.append(additional_bytes)
        increase(operation_id, additional_bytes)

    monkeypatch.setattr(epub_builder, "_webp_to_png", larger)
    monkeypatch.setattr(fixture.budget, "increase_bounded_cbz_reservation", measure)
    fixture.build()
    assert sizes and sizes[0] > 10_000
    assert fixture.budget.snapshot().reserved_bytes == 0
    old = fixture.output.read_bytes()

    def reject(_operation_id: UUID, _additional_bytes: int) -> None:
        raise OSError("no more space")

    monkeypatch.setattr(fixture.budget, "increase_bounded_cbz_reservation", reject)
    with pytest.raises(EpubBuildError):
        fixture.build()
    assert fixture.output.read_bytes() == old
    assert not fixture.temporary.exists() and fixture.budget.snapshot().reserved_bytes == 0


def test_conversion_output_is_bounded_and_keeps_previous_book(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture.output.write_bytes(b"old book")
    monkeypatch.setattr(epub_builder, "V1_MAX_PAGE_BYTES", 8)
    with pytest.raises(EpubBuildError):
        fixture.build()
    assert fixture.output.read_bytes() == b"old book"
    assert not fixture.temporary.exists() and fixture.budget.snapshot().reserved_bytes == 0
