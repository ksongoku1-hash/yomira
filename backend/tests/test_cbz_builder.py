from __future__ import annotations

import errno
import hashlib
import logging
import os
import time
import zipfile
from collections.abc import Callable
from dataclasses import replace
from io import BytesIO
from operator import attrgetter
from pathlib import Path
from typing import Any, cast
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

import app.persistence.filesystem_retry as filesystem_retry_module
import app.services.cbz_builder as cbz_builder_module
from app.domain.job_manifest import MAX_MANIFEST_PAGE_COUNT
from app.persistence.atomic_file_publisher import (
    ADR_0087_RETRY_POLICY,
    AtomicFilePublisher,
)
from app.persistence.disk_budget import DiskBudgetManager
from app.persistence.storage_gate import SharedStorageGate
from app.persistence.windows_paths import DataRootIdentity
from app.services.cbz_builder import (
    MAX_CBZ_BYTES_V1,
    MAX_PAGE_BYTES_V1,
    AtomicNoReplacePublisher,
    CBZBuilder,
    CBZBuildError,
    ComicInfoMetadata,
    ValidatedPage,
)


def _image_bytes(image_format: str, *, color: str = "red") -> bytes:
    image = Image.new("RGB", (8, 6), color)
    try:
        with BytesIO() as output:
            image.save(output, format=image_format)
            return output.getvalue()
    finally:
        image.close()


def _validated_page(path: Path, ordinal: int, payload: bytes | None = None) -> ValidatedPage:
    content = path.read_bytes() if payload is None else payload
    return ValidatedPage(
        ordinal=ordinal,
        path=path,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


class _Win32LockError(OSError):
    def __init__(self, code: int) -> None:
        super().__init__(f"injected Win32 error {code}")
        self.winerror = code


class _AtomicTestPublisher:
    """Test adapter models same-directory atomic publication without replacement."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    def publish_no_replace(self, source: Path, destination: Path) -> None:
        self.calls.append((source, destination))
        os.link(source, destination)
        source.unlink()


class _GrowingReader:
    def __init__(self, stream: Any, *, extra_bytes: bytes) -> None:
        self._stream = stream
        self._extra_bytes = extra_bytes
        self._read = False

    def __enter__(self) -> _GrowingReader:
        return self

    def __exit__(self, *args: object) -> None:
        self._stream.close()

    def fileno(self) -> int:
        return self._stream.fileno()

    def close(self) -> None:
        self._stream.close()

    def read(self, size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._stream.read(size) + self._extra_bytes


def _builder(
    publisher: AtomicNoReplacePublisher | None = None,
    *,
    max_archive_bytes: int,
    storage_gate: SharedStorageGate | None = None,
) -> CBZBuilder:
    return CBZBuilder(
        publisher=publisher or cast(AtomicNoReplacePublisher, _AtomicTestPublisher()),
        max_archive_bytes=max_archive_bytes,
        storage_gate=storage_gate,
    )


def _metadata(**overrides: object) -> ComicInfoMetadata:
    values: dict[str, object] = {
        "title": "Chapter <1> & one",
        "series": "The <Series>",
        "number": "001",
        "volume": "2",
        "summary": "A & B",
        "writer": "Author",
        "genres": ("Drama", "Mystery"),
        "language_iso": "en",
        "manga": None,
        "web": None,
    }
    values.update(overrides)
    return ComicInfoMetadata(**values)  # type: ignore[arg-type]


def _prepared_pages_for_archive_size(
    tmp_path: Path,
    *,
    max_archive_bytes: int,
    page_count: int = 32,
) -> tuple[tuple[ValidatedPage, ...], tuple[Any, ...], bytes]:
    metadata = _metadata()
    comic_info_factory = cast(Callable[..., bytes], attrgetter("_comic_info_bytes")(CBZBuilder))
    comic_info = comic_info_factory(metadata, page_count=page_count)
    comic_info_name = "ComicInfo.xml"
    archive_overhead = (
        22
        + len(comic_info)
        + 76
        + (2 * len(comic_info_name))
        + sum(
            76 + (2 * len(f"{ordinal:03d}.jpg"))
            for ordinal in range(1, page_count + 1)
        )
    )
    payload_bytes = max_archive_bytes - archive_overhead
    if not 0 < payload_bytes <= page_count * MAX_PAGE_BYTES_V1:
        raise ValueError("requested archive size cannot fit within page limits")
    sizes = [MAX_PAGE_BYTES_V1] * page_count
    for index in range(page_count):
        excess = sum(sizes) - payload_bytes
        if excess <= 0:
            break
        reduction = min(excess, sizes[index])
        sizes[index] -= reduction

    image_bytes = _image_bytes("JPEG")
    image_path = tmp_path / "sizing-source.jpg"
    image_path.write_bytes(image_bytes)
    digest = hashlib.sha256(image_bytes).hexdigest()
    actual_pages = tuple(
        ValidatedPage(ordinal, image_path, len(image_bytes), digest)
        for ordinal in range(1, page_count + 1)
    )
    prepare_pages = cast(
        Callable[..., tuple[Any, ...]],
        attrgetter("_prepare_pages")(_builder(max_archive_bytes=MAX_CBZ_BYTES_V1)),
    )
    actual_prepared = prepare_pages(
        actual_pages,
        temporary=tmp_path / "sizing-source.cbz.tmp",
    )
    pages = tuple(
        replace(page, size_bytes=size) for page, size in zip(actual_pages, sizes, strict=True)
    )
    prepared = tuple(
        replace(item, page=page)
        for item, page in zip(actual_prepared, pages, strict=True)
    )
    return pages, prepared, comic_info


def _estimate_archive_size(prepared: tuple[Any, ...], comic_info: bytes) -> int:
    estimator = cast(
        Callable[[tuple[Any, ...], bytes], int],
        attrgetter("_estimate_archive_size")(CBZBuilder),
    )
    return estimator(prepared, comic_info)


def test_builds_one_deterministic_cbz_with_verified_xml_pages_and_hashes(
    tmp_path: Path,
) -> None:
    png = _image_bytes("PNG", color="blue")
    jpeg = _image_bytes("JPEG", color="green")
    page_1 = tmp_path / "source-one.bin"
    page_2 = tmp_path / "source-two.bin"
    page_1.write_bytes(png)
    page_2.write_bytes(jpeg)
    publisher = _AtomicTestPublisher()
    builder = _builder(publisher, max_archive_bytes=MAX_CBZ_BYTES_V1)
    metadata = _metadata(manga="Yes", web="https://source.invalid/chapter/1")

    result = builder.build(
        destination=tmp_path / "chapter.cbz",
        metadata=metadata,
        pages=(_validated_page(page_2, 2), _validated_page(page_1, 1)),
    )

    assert result.path == tmp_path / "chapter.cbz"
    assert result.page_count == 2
    assert result.size_bytes == result.path.stat().st_size
    assert result.sha256 == hashlib.sha256(result.path.read_bytes()).hexdigest()
    assert publisher.calls == [(tmp_path / "chapter.cbz.tmp", result.path)]
    assert not (tmp_path / "chapter.cbz.tmp").exists()
    with zipfile.ZipFile(result.path) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == ["ComicInfo.xml", "001.png", "002.jpg"]
        assert [
            (entry.date_time, entry.compress_type, entry.create_system)
            for entry in archive.infolist()
        ] == [((1980, 1, 1, 0, 0, 0), zipfile.ZIP_STORED, 0)] * 3
        xml = ET.fromstring(archive.read("ComicInfo.xml"))
        assert xml.tag == "ComicInfo"
        assert {child.tag: child.text for child in xml} == {
            "Title": "Chapter <1> & one",
            "Series": "The <Series>",
            "Number": "001",
            "Volume": "2",
            "Summary": "A & B",
            "Writer": "Author",
            "Genre": "Drama, Mystery",
            "PageCount": "2",
            "LanguageISO": "en",
            "Manga": "Yes",
            "Web": "https://source.invalid/chapter/1",
        }
        for member, expected in (("001.png", png), ("002.jpg", jpeg)):
            assert archive.read(member) == expected
            with archive.open(member) as stream:
                with Image.open(stream) as image:
                    image.load()


def test_cbz_seekable_staging_write_retries_and_archive_remains_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_bytes = _image_bytes("PNG", color="purple")
    page = tmp_path / "source.png"
    page.write_bytes(page_bytes)
    temporary = tmp_path / "chapter.cbz.tmp"
    real_open = Path.open
    write_failures = 0
    failed_backpatches: list[tuple[int, int]] = []
    sleeps: list[float] = []

    class LockOnceStream:
        def __init__(self, stream: Any) -> None:
            self._stream = stream
            self._backpatch_pending = False

        def write(self, value: bytes) -> int:
            nonlocal write_failures
            if self._backpatch_pending:
                self._backpatch_pending = False
                write_failures += 1
                failed_backpatches.append(
                    (self._stream.tell(), temporary.stat().st_size)
                )
                raise _Win32LockError(32)
            return self._stream.write(value)

        def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
            before = self._stream.tell()
            result = self._stream.seek(offset, whence)
            if result < before:
                self._backpatch_pending = True
            return result

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patched_open(
        path: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        stream = real_open(path, mode, *args, **kwargs)
        if path == temporary and mode == "x+b":
            return LockOnceStream(stream)
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(filesystem_retry_module.time, "sleep", sleeps.append)
    result = _builder(max_archive_bytes=MAX_CBZ_BYTES_V1).build(
        destination=tmp_path / "chapter.cbz",
        metadata=_metadata(),
        pages=(_validated_page(page, 1),),
    )

    assert write_failures == 1
    assert failed_backpatches
    assert all(offset < size for offset, size in failed_backpatches)
    assert sleeps == [0.1]
    assert result.path.read_bytes()
    assert not temporary.exists()
    with zipfile.ZipFile(result.path) as archive:
        assert archive.testzip() is None
        assert archive.read("001.png") == page_bytes


@pytest.mark.skipif(os.name != "nt", reason="production publisher uses MoveFileExW")
def test_builder_publishes_through_atomic_publisher_with_adr_0087_policy(
    tmp_path: Path,
) -> None:
    payload = _image_bytes("PNG")
    page = tmp_path / "page.png"
    page.write_bytes(payload)
    publisher = AtomicFilePublisher(
        data_root_identity=DataRootIdentity.capture(tmp_path),
        retry_policy=ADR_0087_RETRY_POLICY,
        sleep=time.sleep,
        clock=time.monotonic,
    )
    builder = _builder(publisher, max_archive_bytes=MAX_CBZ_BYTES_V1)

    result = builder.build(
        destination=tmp_path / "chapter.cbz",
        metadata=_metadata(),
        pages=(_validated_page(page, 1),),
    )

    assert result.path.is_file()
    assert result.size_bytes <= MAX_CBZ_BYTES_V1
    assert not (tmp_path / "chapter.cbz.tmp").exists()


def test_estimator_accepts_exact_two_gib_preflight_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages, prepared, comic_info = _prepared_pages_for_archive_size(
        tmp_path,
        max_archive_bytes=MAX_CBZ_BYTES_V1,
    )
    assert _estimate_archive_size(prepared, comic_info) == MAX_CBZ_BYTES_V1
    builder = _builder(max_archive_bytes=MAX_CBZ_BYTES_V1)
    destination = tmp_path / "exact-boundary.cbz"
    temporary = tmp_path / "exact-boundary.cbz.tmp"
    writer_calls: list[Path] = []

    def return_prepared_pages(
        _pages: tuple[ValidatedPage, ...],
        *,
        temporary: Path,
    ) -> tuple[Any, ...]:
        del temporary
        return prepared

    monkeypatch.setattr(builder, "_prepare_pages", return_prepared_pages)

    def write_marker(
        path: Path,
        _pages: Any,
        _comic_info: bytes,
        *,
        max_archive_bytes: int,
        storage_gate: SharedStorageGate | None = None,
        disk_budget_manager: DiskBudgetManager | None = None,
    ) -> None:
        del storage_gate
        del disk_budget_manager
        assert max_archive_bytes == MAX_CBZ_BYTES_V1
        writer_calls.append(path)
        path.write_bytes(b"bounded-writer-entered")

    def stop_after_preflight(*_args: object, **_kwargs: object) -> None:
        raise CBZBuildError("preflight_accepted")

    monkeypatch.setattr(builder, "_write_archive", write_marker)
    monkeypatch.setattr(builder, "_verify_archive", stop_after_preflight)

    with pytest.raises(CBZBuildError) as error:
        builder.build(destination=destination, metadata=_metadata(), pages=pages)

    assert error.value.stage == "preflight_accepted"
    assert writer_calls == [temporary]
    assert temporary.read_bytes() == b"bounded-writer-entered"
    assert not destination.exists()


def test_one_byte_over_two_gib_is_rejected_before_creating_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact_pages, exact_prepared, comic_info = _prepared_pages_for_archive_size(
        tmp_path,
        max_archive_bytes=MAX_CBZ_BYTES_V1,
    )
    pages = tuple(
        replace(page, size_bytes=page.size_bytes + 1) if page.ordinal == 1 else page
        for page in exact_pages
    )
    prepared = tuple(
        replace(item, page=pages[item.page.ordinal - 1])
        for item in exact_prepared
    )
    assert _estimate_archive_size(prepared, comic_info) == MAX_CBZ_BYTES_V1 + 1
    builder = _builder(max_archive_bytes=MAX_CBZ_BYTES_V1)
    destination = tmp_path / "over-limit.cbz"
    temporary = tmp_path / "over-limit.cbz.tmp"
    writer_calls: list[Path] = []
    def return_over_limit_pages(
        _pages: tuple[ValidatedPage, ...],
        *,
        temporary: Path,
    ) -> tuple[Any, ...]:
        del temporary
        return prepared

    def record_writer(path: Path, *_args: Any, **_kwargs: Any) -> None:
        writer_calls.append(path)

    monkeypatch.setattr(builder, "_prepare_pages", return_over_limit_pages)
    monkeypatch.setattr(builder, "_write_archive", record_writer)

    with pytest.raises(CBZBuildError) as error:
        builder.build(destination=destination, metadata=_metadata(), pages=pages)

    assert error.value.stage == "archive_size_limit_preflight"
    assert writer_calls == []
    assert not temporary.exists()
    assert not destination.exists()


def test_builder_rejects_page_count_and_page_size_beyond_approved_limits(
    tmp_path: Path,
) -> None:
    too_many_pages = tuple(
        ValidatedPage(
            ordinal=ordinal,
            path=tmp_path / f"page-{ordinal}.bin",
            size_bytes=0,
            sha256="0" * 64,
        )
        for ordinal in range(1, MAX_MANIFEST_PAGE_COUNT + 2)
    )
    builder = _builder(max_archive_bytes=MAX_CBZ_BYTES_V1)
    with pytest.raises(CBZBuildError) as count_error:
        builder.build(
            destination=tmp_path / "too-many.cbz",
            metadata=_metadata(),
            pages=too_many_pages,
        )
    assert count_error.value.stage == "page_count_limit_exceeded"
    assert not (tmp_path / "too-many.cbz.tmp").exists()

    oversized_page = ValidatedPage(
        ordinal=1,
        path=tmp_path / "oversized-page.bin",
        size_bytes=MAX_PAGE_BYTES_V1 + 1,
        sha256="0" * 64,
    )
    with pytest.raises(CBZBuildError) as page_error:
        builder.build(
            destination=tmp_path / "oversized-page.cbz",
            metadata=_metadata(),
            pages=(oversized_page,),
        )
    assert page_error.value.stage == "page_size_limit_exceeded"
    assert not (tmp_path / "oversized-page.cbz.tmp").exists()


def test_zip_overhead_discrepancy_hits_write_guard_and_preserves_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _image_bytes("PNG")
    page_path = tmp_path / "page.png"
    page_path.write_bytes(payload)
    publisher = _AtomicTestPublisher()
    builder = _builder(publisher, max_archive_bytes=4096)
    original_zip_info = cast(
        Callable[[str], zipfile.ZipInfo],
        attrgetter("_zip_info")(CBZBuilder),
    )

    def add_unexpected_extra(member_name: str) -> zipfile.ZipInfo:
        info = original_zip_info(member_name)
        extra_payload = b"x" * 4096
        info.extra = b"\xfe\xca" + len(extra_payload).to_bytes(2, "little") + extra_payload
        return info

    monkeypatch.setattr(CBZBuilder, "_zip_info", staticmethod(add_unexpected_extra))

    with pytest.raises(CBZBuildError) as error:
        builder.build(
            destination=tmp_path / "overhead-growth.cbz",
            metadata=_metadata(),
            pages=(_validated_page(page_path, 1),),
        )

    temporary = tmp_path / "overhead-growth.cbz.tmp"
    assert error.value.stage == "archive_size_limit_during_write"
    assert temporary.is_file()
    assert temporary.stat().st_size <= 4096
    assert publisher.calls == []
    assert not (tmp_path / "overhead-growth.cbz").exists()


def test_source_growth_during_streaming_preserves_temporary_and_does_not_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _image_bytes("PNG")
    page_path = tmp_path / "page.png"
    page_path.write_bytes(payload)
    page = _validated_page(page_path, 1)
    publisher = _AtomicTestPublisher()
    builder = _builder(publisher, max_archive_bytes=4096)
    temporary = tmp_path / "grown-source.cbz.tmp"
    prepared = builder._prepare_pages((page,), temporary=temporary)  # pyright: ignore[reportPrivateUsage]
    def return_prepared_pages(
        _pages: tuple[ValidatedPage, ...],
        *,
        temporary: Path,
    ) -> Any:
        return prepared

    monkeypatch.setattr(builder, "_prepare_pages", return_prepared_pages)
    original_open = Path.open

    def grow_page_stream(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        stream = original_open(path, mode, buffering, encoding, errors, newline)
        if path == page_path and mode == "rb":
            return _GrowingReader(stream, extra_bytes=b"unexpected-growth")
        return stream

    monkeypatch.setattr(Path, "open", grow_page_stream)

    with pytest.raises(CBZBuildError) as error:
        builder.build(
            destination=tmp_path / "grown-source.cbz",
            metadata=_metadata(),
            pages=(page,),
        )

    assert error.value.stage == "page_source_size_mismatch"
    assert temporary.is_file()
    assert temporary.stat().st_size <= 4096
    assert publisher.calls == []
    assert not (tmp_path / "grown-source.cbz").exists()


def test_repeated_builds_have_identical_archive_bytes(tmp_path: Path) -> None:
    payload = _image_bytes("WEBP", color="purple")
    page = tmp_path / "page-source"
    page.write_bytes(payload)
    validated = _validated_page(page, 1)
    metadata = _metadata()
    builder = _builder(max_archive_bytes=MAX_CBZ_BYTES_V1)

    first = builder.build(
        destination=tmp_path / "first.cbz", metadata=metadata, pages=(validated,)
    )
    second = builder.build(
        destination=tmp_path / "second.cbz", metadata=metadata, pages=(validated,)
    )

    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.sha256 == second.sha256


def test_unknown_comicinfo_fields_are_omitted_instead_of_fabricated(
    tmp_path: Path,
) -> None:
    page_bytes = _image_bytes("PNG")
    page = tmp_path / "page.png"
    page.write_bytes(page_bytes)
    metadata = ComicInfoMetadata(title="Known title", series=None)

    result = _builder(max_archive_bytes=MAX_CBZ_BYTES_V1).build(
        destination=tmp_path / "chapter.cbz",
        metadata=metadata,
        pages=(_validated_page(page, 1),),
    )

    with zipfile.ZipFile(result.path) as archive:
        xml = ET.fromstring(archive.read("ComicInfo.xml"))
    assert [(child.tag, child.text) for child in xml] == [
        ("Title", "Known title"),
        ("PageCount", "1"),
    ]


def test_existing_destination_is_never_replaced_or_published_over(
    tmp_path: Path,
) -> None:
    page_bytes = _image_bytes("PNG")
    page = tmp_path / "page.png"
    page.write_bytes(page_bytes)
    destination = tmp_path / "chapter.cbz"
    original = b"existing user data"
    destination.write_bytes(original)
    publisher = _AtomicTestPublisher()

    with pytest.raises(CBZBuildError) as error:
        _builder(publisher, max_archive_bytes=MAX_CBZ_BYTES_V1).build(
            destination=destination,
            metadata=_metadata(),
            pages=(_validated_page(page, 1),),
        )

    assert error.value.stage == "destination_exists"
    assert destination.read_bytes() == original
    assert publisher.calls == []
    assert not (tmp_path / "chapter.cbz.tmp").exists()


def test_preexisting_temporary_is_preserved_and_not_overwritten(tmp_path: Path) -> None:
    page_bytes = _image_bytes("PNG")
    page = tmp_path / "page.png"
    page.write_bytes(page_bytes)
    temporary = tmp_path / "chapter.cbz.tmp"
    original = b"unowned temporary evidence"
    temporary.write_bytes(original)
    publisher = _AtomicTestPublisher()

    with pytest.raises(CBZBuildError) as error:
        _builder(publisher, max_archive_bytes=MAX_CBZ_BYTES_V1).build(
            destination=tmp_path / "chapter.cbz",
            metadata=_metadata(),
            pages=(_validated_page(page, 1),),
        )

    assert error.value.stage == "temporary_exists"
    assert temporary.read_bytes() == original
    assert publisher.calls == []


def test_prepare_pages_retries_transient_stat_and_image_open_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_bytes = _image_bytes("PNG")
    page_path = tmp_path / "page.png"
    page_path.write_bytes(page_bytes)
    page = _validated_page(page_path, 1)
    real_lstat = Path.lstat
    real_image_open = Image.open
    lstat_attempts = 0
    image_open_attempts = 0
    sleeps: list[float] = []

    def flaky_lstat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal lstat_attempts
        if path == page_path and lstat_attempts == 0:
            lstat_attempts += 1
            raise _Win32LockError(32)
        if path == page_path:
            lstat_attempts += 1
        return real_lstat(path, *args, **kwargs)

    def flaky_image_open(*args: Any, **kwargs: Any) -> Any:
        nonlocal image_open_attempts
        image_open_attempts += 1
        if image_open_attempts == 1:
            raise _Win32LockError(33)
        return real_image_open(*args, **kwargs)

    monkeypatch.setattr(Path, "lstat", flaky_lstat)
    monkeypatch.setattr(Image, "open", flaky_image_open)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    prepared = CBZBuilder._prepare_pages(  # pyright: ignore[reportPrivateUsage]
        (page,), temporary=tmp_path / "chapter.cbz.tmp"
    )

    assert prepared[0].page == page
    assert lstat_attempts >= 2
    assert image_open_attempts == 2
    assert sleeps == [0.1, 0.1]


def test_cbz_page_read_lock_resumes_from_last_successful_offset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = Image.effect_noise((512, 512), 100).convert("RGB")
    page_path = tmp_path / "large.png"
    try:
        image.save(page_path, format="PNG")
    finally:
        image.close()
    page = _validated_page(page_path, 1)
    prepared = CBZBuilder._prepare_pages(  # pyright: ignore[reportPrivateUsage]
        (page,), temporary=tmp_path / "chapter.cbz.tmp"
    )
    real_open = Path.open
    source_read_offsets: list[int] = []
    source_open_count = 0
    failure_injected = False
    sleeps: list[float] = []

    class LockOnceReader:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        @property
        def closed(self) -> bool:
            return self.stream.closed

        def __enter__(self) -> LockOnceReader:
            return self

        def __exit__(self, *_args: object) -> None:
            self.stream.close()

        def fileno(self) -> int:
            return self.stream.fileno()

        def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
            return self.stream.seek(offset, whence)

        def tell(self) -> int:
            return self.stream.tell()

        def close(self) -> None:
            self.stream.close()

        def read(self, size: int = -1) -> bytes:
            nonlocal failure_injected
            source_read_offsets.append(self.stream.tell())
            if not failure_injected and self.stream.tell() >= 64 * 1024:
                failure_injected = True
                raise _Win32LockError(36)
            return self.stream.read(size)

    def tracked_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        nonlocal source_open_count
        stream = real_open(path, mode, *args, **kwargs)
        if path == page_path and mode == "rb":
            source_open_count += 1
            return LockOnceReader(stream)
        return stream

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    temporary = tmp_path / "chapter.cbz.tmp"

    CBZBuilder._write_archive(  # pyright: ignore[reportPrivateUsage]
        temporary,
        prepared,
        CBZBuilder._comic_info_bytes(_metadata(), page_count=1),  # pyright: ignore[reportPrivateUsage]
        max_archive_bytes=MAX_CBZ_BYTES_V1,
    )

    assert source_open_count == 2
    with zipfile.ZipFile(temporary) as archive:
        assert archive.read("001.png") == page_path.read_bytes()
    assert source_read_offsets[:3] == [0, 64 * 1024, 64 * 1024]
    assert sleeps == [0.1]


def test_page_final_stat_closes_old_handle_before_retrying_transient_fstat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_path = tmp_path / "page.bin"
    page_path.write_bytes(b"stable page content")
    snapshot = cbz_builder_module._FileSnapshot.from_stat(page_path.lstat())  # pyright: ignore[reportPrivateUsage]
    reader = cbz_builder_module._RestartablePageReader(  # pyright: ignore[reportPrivateUsage]
        page_path,
        snapshot,
        stage="cbz_page_copy_read",
        changed_stage="page_source_changed",
    )
    reader.__enter__()
    old_stream = reader._stream  # pyright: ignore[reportPrivateUsage]
    assert old_stream is not None
    real_fstat = os.fstat
    failed_once = False
    sleeps: list[float] = []

    def fail_one_fstat(fd: int) -> os.stat_result:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise _Win32LockError(32)
        return real_fstat(fd)

    def require_closed_before_retry(delay: float) -> None:
        assert old_stream.closed
        sleeps.append(delay)

    monkeypatch.setattr(cbz_builder_module.os, "fstat", fail_one_fstat)
    monkeypatch.setattr(time, "sleep", require_closed_before_retry)

    reader.ensure_unchanged()

    assert failed_once
    assert old_stream.closed
    assert reader._stream is None  # pyright: ignore[reportPrivateUsage]
    assert sleeps == [0.1]


def test_cbz_zip_verification_reopens_and_restarts_after_transient_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_bytes = _image_bytes("PNG")
    page_path = tmp_path / "page.png"
    page_path.write_bytes(page_bytes)
    prepared = CBZBuilder._prepare_pages(  # pyright: ignore[reportPrivateUsage]
        (_validated_page(page_path, 1),), temporary=tmp_path / "chapter.cbz.tmp"
    )
    comic_info = CBZBuilder._comic_info_bytes(_metadata(), page_count=1)  # pyright: ignore[reportPrivateUsage]
    temporary = tmp_path / "chapter.cbz.tmp"
    CBZBuilder._write_archive(  # pyright: ignore[reportPrivateUsage]
        temporary,
        prepared,
        comic_info,
        max_archive_bytes=MAX_CBZ_BYTES_V1,
    )
    real_zip_init = zipfile.ZipFile.__init__
    real_member_read = zipfile.ZipExtFile.read
    archive_open_count = 0
    archive_streams: list[Any] = []
    archive_start_offsets: list[int] = []
    failure_injected = False
    sleeps: list[float] = []

    def count_archive_open(
        self: zipfile.ZipFile,
        file: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        nonlocal archive_open_count
        archive_open_count += 1
        archive_streams.append(file)
        archive_start_offsets.append(file.tell())
        real_zip_init(self, file, *args, **kwargs)

    def fail_one_member_read(self: zipfile.ZipExtFile, size: int = -1) -> bytes:
        nonlocal failure_injected
        if not failure_injected:
            failure_injected = True
            raise _Win32LockError(32)
        return real_member_read(self, size)

    def require_closed_stream_before_retry(delay: float) -> None:
        assert len(archive_streams) == 1
        assert archive_streams[0].closed
        sleeps.append(delay)

    monkeypatch.setattr(zipfile.ZipFile, "__init__", count_archive_open)
    monkeypatch.setattr(zipfile.ZipExtFile, "read", fail_one_member_read)
    monkeypatch.setattr(time, "sleep", require_closed_stream_before_retry)

    CBZBuilder._verify_archive(  # pyright: ignore[reportPrivateUsage]
        temporary,
        prepared,
        comic_info,
        expected_archive_size=temporary.stat().st_size,
        max_archive_bytes=MAX_CBZ_BYTES_V1,
    )

    assert failure_injected
    assert archive_open_count == 2
    assert len(archive_streams) == 2
    assert archive_start_offsets == [0, 0]
    assert all(stream.closed for stream in archive_streams)
    assert sleeps == [0.1]


def test_cbz_archive_hash_restarts_from_zero_after_transient_read_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"archive bytes" * 20_000
    path = tmp_path / "archive.cbz"
    path.write_bytes(payload)
    real_open = Path.open
    open_count = 0
    read_offsets: list[int] = []
    failure_injected = False
    sleeps: list[float] = []

    class LockOnceReader:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        @property
        def closed(self) -> bool:
            return self.stream.closed

        def __enter__(self) -> LockOnceReader:
            return self

        def __exit__(self, *_args: object) -> None:
            self.stream.close()

        def fileno(self) -> int:
            return self.stream.fileno()

        def close(self) -> None:
            self.stream.close()

        def read(self, size: int = -1) -> bytes:
            nonlocal failure_injected
            read_offsets.append(self.stream.tell())
            if not failure_injected:
                failure_injected = True
                raise _Win32LockError(33)
            return self.stream.read(size)

    def tracked_open(path_arg: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        nonlocal open_count
        stream = real_open(path_arg, mode, *args, **kwargs)
        if path_arg == path and mode == "rb":
            open_count += 1
            return LockOnceReader(stream)
        return stream

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    actual = CBZBuilder._hash_regular_file(path, max_bytes=len(payload))  # pyright: ignore[reportPrivateUsage]

    assert actual == (hashlib.sha256(payload).hexdigest(), len(payload))
    assert open_count == 2
    assert read_offsets[:2] == [0, 0]
    assert sleeps == [0.1]


def test_hash_open_obeys_four_retry_budget_and_does_not_retry_permanent_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "archive.cbz"
    path.write_bytes(b"archive")
    real_open = Path.open
    open_attempts = 0
    sleeps: list[float] = []

    def always_locked(path_arg: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        nonlocal open_attempts
        if path_arg == path and mode == "rb":
            open_attempts += 1
            raise _Win32LockError(36)
        return real_open(path_arg, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", always_locked)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    with pytest.raises(_Win32LockError):
        CBZBuilder._hash_regular_file(path, max_bytes=1024)  # pyright: ignore[reportPrivateUsage]
    assert open_attempts == 5
    assert sleeps == [0.1, 0.2, 0.4, 0.8]

    open_attempts = 0
    sleeps.clear()

    def permanent_failure(path_arg: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        nonlocal open_attempts
        if path_arg == path and mode == "rb":
            open_attempts += 1
            error = _Win32LockError(5)
            raise error
        return real_open(path_arg, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", permanent_failure)
    with pytest.raises(_Win32LockError) as error:
        CBZBuilder._hash_regular_file(path, max_bytes=1024)  # pyright: ignore[reportPrivateUsage]
    assert error.value.winerror == 5
    assert open_attempts == 1
    assert sleeps == []


def test_hash_restarts_after_transient_close_only_when_handle_is_proven_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"verified CBZ staging payload" * 3000
    path = tmp_path / "archive.cbz.tmp"
    path.write_bytes(payload)
    real_open = Path.open
    streams: list[Any] = []
    offsets: list[int] = []
    sleeps: list[float] = []
    close_failure_injected = False

    class CloseOnceReader:
        def __init__(self, stream: Any) -> None:
            self._stream = stream
            self._first_close = True

        @property
        def closed(self) -> bool:
            return self._stream.closed

        def close(self) -> None:
            nonlocal close_failure_injected
            if self._first_close and not close_failure_injected:
                self._first_close = False
                close_failure_injected = True
                self._stream.close()
                raise _Win32LockError(32)
            self._stream.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def tracked_open(path_arg: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        stream = real_open(path_arg, mode, *args, **kwargs)
        if path_arg == path and mode == "rb":
            offsets.append(stream.tell())
            wrapped = CloseOnceReader(stream)
            streams.append(wrapped)
            return wrapped
        return stream

    def require_closed_before_retry(delay: float) -> None:
        assert streams[0].closed
        sleeps.append(delay)

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(time, "sleep", require_closed_before_retry)

    result = CBZBuilder._hash_regular_file(path, max_bytes=len(payload))  # pyright: ignore[reportPrivateUsage]

    assert result == (hashlib.sha256(payload).hexdigest(), len(payload))
    assert len(streams) == 2
    assert offsets == [0, 0]
    assert all(stream.closed for stream in streams)
    assert sleeps == [0.1]


def test_hash_fails_closed_when_transient_close_does_not_prove_handle_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"preserve ambiguous read candidate" * 100
    path = tmp_path / "archive.cbz.tmp"
    path.write_bytes(payload)
    real_open = Path.open
    streams: list[Any] = []
    sleeps: list[float] = []

    class UnprovenCloseReader:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        @property
        def closed(self) -> bool:
            return self._stream.closed

        def close(self) -> None:
            raise _Win32LockError(33)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def tracked_open(path_arg: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        stream = real_open(path_arg, mode, *args, **kwargs)
        if path_arg == path and mode == "rb":
            wrapped = UnprovenCloseReader(stream)
            streams.append(wrapped)
            return wrapped
        return stream

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    try:
        with pytest.raises(CBZBuildError) as error:
            CBZBuilder._hash_regular_file(path, max_bytes=len(payload))  # pyright: ignore[reportPrivateUsage]

        assert error.value.stage == "read_handle_close_unverified"
        assert len(streams) == 1
        assert not streams[0].closed
        assert sleeps == []
        assert path.is_file()
    finally:
        for stream in streams:
            if not stream.closed:
                stream._stream.close()


def test_zip_verification_restarts_after_transient_close_only_when_handle_is_proven_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_bytes = _image_bytes("PNG")
    page_path = tmp_path / "page.png"
    page_path.write_bytes(page_bytes)
    temporary = tmp_path / "chapter.cbz.tmp"
    prepared = CBZBuilder._prepare_pages(  # pyright: ignore[reportPrivateUsage]
        (_validated_page(page_path, 1),), temporary=temporary
    )
    comic_info = CBZBuilder._comic_info_bytes(_metadata(), page_count=1)  # pyright: ignore[reportPrivateUsage]
    CBZBuilder._write_archive(  # pyright: ignore[reportPrivateUsage]
        temporary,
        prepared,
        comic_info,
        max_archive_bytes=MAX_CBZ_BYTES_V1,
    )
    real_open = Path.open
    streams: list[Any] = []
    offsets: list[int] = []
    sleeps: list[float] = []
    close_failure_injected = False

    class CloseOnceReader:
        def __init__(self, stream: Any) -> None:
            self._stream = stream
            self._first_close = True

        @property
        def closed(self) -> bool:
            return self._stream.closed

        def close(self) -> None:
            nonlocal close_failure_injected
            if self._first_close and not close_failure_injected:
                self._first_close = False
                close_failure_injected = True
                self._stream.close()
                raise _Win32LockError(36)
            self._stream.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def tracked_open(path_arg: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        stream = real_open(path_arg, mode, *args, **kwargs)
        if path_arg == temporary and mode == "rb":
            offsets.append(stream.tell())
            wrapped = CloseOnceReader(stream)
            streams.append(wrapped)
            return wrapped
        return stream

    def require_closed_before_retry(delay: float) -> None:
        assert streams[0].closed
        sleeps.append(delay)

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(time, "sleep", require_closed_before_retry)

    CBZBuilder._verify_archive(  # pyright: ignore[reportPrivateUsage]
        temporary,
        prepared,
        comic_info,
        expected_archive_size=temporary.stat().st_size,
        max_archive_bytes=MAX_CBZ_BYTES_V1,
    )

    assert len(streams) == 2
    assert offsets == [0, 0]
    assert all(stream.closed for stream in streams)
    assert sleeps == [0.1]


def test_zip_verification_fails_closed_when_transient_close_is_unproven(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_bytes = _image_bytes("PNG")
    page_path = tmp_path / "page.png"
    page_path.write_bytes(page_bytes)
    temporary = tmp_path / "chapter.cbz.tmp"
    prepared = CBZBuilder._prepare_pages(  # pyright: ignore[reportPrivateUsage]
        (_validated_page(page_path, 1),), temporary=temporary
    )
    comic_info = CBZBuilder._comic_info_bytes(_metadata(), page_count=1)  # pyright: ignore[reportPrivateUsage]
    CBZBuilder._write_archive(  # pyright: ignore[reportPrivateUsage]
        temporary,
        prepared,
        comic_info,
        max_archive_bytes=MAX_CBZ_BYTES_V1,
    )
    real_open = Path.open
    streams: list[Any] = []
    sleeps: list[float] = []

    class UnprovenCloseReader:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        @property
        def closed(self) -> bool:
            return self._stream.closed

        def close(self) -> None:
            raise _Win32LockError(32)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def tracked_open(path_arg: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        stream = real_open(path_arg, mode, *args, **kwargs)
        if path_arg == temporary and mode == "rb":
            wrapped = UnprovenCloseReader(stream)
            streams.append(wrapped)
            return wrapped
        return stream

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    try:
        with pytest.raises(CBZBuildError) as error:
            CBZBuilder._verify_archive(  # pyright: ignore[reportPrivateUsage]
                temporary,
                prepared,
                comic_info,
                expected_archive_size=temporary.stat().st_size,
                max_archive_bytes=MAX_CBZ_BYTES_V1,
            )

        assert error.value.stage == "read_handle_close_unverified"
        assert len(streams) == 1
        assert not streams[0].closed
        assert sleeps == []
        assert temporary.is_file()
    finally:
        for stream in streams:
            if not stream.closed:
                stream._stream.close()


def test_builder_writer_close_failure_preserves_candidate_and_logs_safe_job_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    page_path = tmp_path / "page.png"
    page_path.write_bytes(_image_bytes("PNG"))
    destination = tmp_path / "chapter.cbz"
    temporary = tmp_path / "chapter.cbz.tmp"
    publisher = _AtomicTestPublisher()
    builder = _builder(publisher, max_archive_bytes=MAX_CBZ_BYTES_V1)
    job_id = "d1453a71-a51a-4f1b-b2c7-84ea2145a83a"
    real_open = cast(Callable[..., Any], Path.open)
    staging_open_attempts = 0
    close_attempts = 0
    sleeps: list[float] = []

    class CloseFailureWriter:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        def close(self) -> None:
            nonlocal close_attempts
            close_attempts += 1
            raise _Win32LockError(32)

        @property
        def closed(self) -> bool:
            return self._stream.closed

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    writer: CloseFailureWriter | None = None

    def tracked_open(path_arg: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        nonlocal staging_open_attempts, writer
        stream = real_open(path_arg, mode, *args, **kwargs)
        if path_arg == temporary and mode == "x+b":
            staging_open_attempts += 1
            writer = CloseFailureWriter(stream)
            return writer
        return stream

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    caplog.set_level(logging.WARNING, logger="yomira.cbz_builder")

    try:
        with pytest.raises(CBZBuildError) as error:
            builder.build(
                destination=destination,
                metadata=_metadata(),
                pages=(_validated_page(page_path, 1),),
                job_id=job_id,
            )

        assert error.value.stage == "temporary_close_unverified"
        assert writer is not None and not writer.closed
        assert staging_open_attempts == 1
        assert close_attempts == 1
        assert sleeps == []
        assert temporary.is_file()
        assert publisher.calls == []
        assert not destination.exists()
        close_records = [
            record
            for record in caplog.records
            if record.getMessage() == "cbz_writer_close_unverified"
        ]
        assert len(close_records) == 1
        record = close_records[0]
        assert record.job_id == job_id
        assert isinstance(record.trace_id, str) and record.trace_id
        assert record.winerror == 32
        assert record.stage == "cbz_staging_close"
        assert "path" not in record.__dict__
        assert "payload" not in record.__dict__
    finally:
        if writer is not None and not writer.closed:
            writer._stream.close()


def test_builder_retry_exhaustion_logs_job_id_and_attempt_five(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    page_path = tmp_path / "page.png"
    page_path.write_bytes(_image_bytes("PNG"))
    page = _validated_page(page_path, 1)
    destination = tmp_path / "chapter.cbz"
    temporary = tmp_path / "chapter.cbz.tmp"
    publisher = _AtomicTestPublisher()
    builder = _builder(publisher, max_archive_bytes=MAX_CBZ_BYTES_V1)
    prepared = builder._prepare_pages((page,), temporary=temporary)  # pyright: ignore[reportPrivateUsage]
    def return_prepared_pages(
        _pages: tuple[ValidatedPage, ...],
        *,
        temporary: Path,
    ) -> Any:
        return prepared

    monkeypatch.setattr(builder, "_prepare_pages", return_prepared_pages)
    job_id = "d1453a71-a51a-4f1b-b2c7-84ea2145a83a"
    real_open = cast(Callable[..., Any], Path.open)
    open_attempts = 0
    sleeps: list[float] = []

    def always_locked(path_arg: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        nonlocal open_attempts
        if path_arg == page_path and mode == "rb":
            open_attempts += 1
            raise _Win32LockError(36)
        return real_open(path_arg, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", always_locked)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    caplog.set_level(logging.WARNING, logger="yomira.cbz_builder")

    with pytest.raises(CBZBuildError):
        builder.build(
            destination=destination,
            metadata=_metadata(),
            pages=(page,),
            job_id=job_id,
        )

    assert open_attempts == 5
    assert sleeps == [0.1, 0.2, 0.4, 0.8]
    retry_records = [
        record for record in caplog.records
        if record.getMessage() == "cbz_filesystem_lock_retry"
    ]
    assert retry_records
    assert all(record.__dict__["job_id"] == job_id for record in retry_records)
    exhausted = [
        record
        for record in caplog.records
        if record.getMessage() == "cbz_filesystem_lock_retries_exhausted"
    ]
    assert len(exhausted) == 1
    assert exhausted[0].__dict__["job_id"] == job_id
    assert exhausted[0].__dict__["attempt"] == 5
    assert exhausted[0].__dict__["winerror"] == 36
    assert "path" not in exhausted[0].__dict__
    assert "payload" not in exhausted[0].__dict__
    assert temporary.is_file()
    assert publisher.calls == []
    assert not destination.exists()


def test_page_hash_mismatch_blocks_publish_and_preserves_temporary(
    tmp_path: Path,
) -> None:
    page_bytes = _image_bytes("PNG")
    page = tmp_path / "page.png"
    page.write_bytes(page_bytes)
    validated = replace(_validated_page(page, 1), sha256="0" * 64)
    publisher = _AtomicTestPublisher()

    with pytest.raises(CBZBuildError) as error:
        _builder(publisher, max_archive_bytes=MAX_CBZ_BYTES_V1).build(
            destination=tmp_path / "chapter.cbz",
            metadata=_metadata(),
            pages=(validated,),
        )

    assert error.value.stage == "page_source_hash_mismatch"
    assert publisher.calls == []
    assert (tmp_path / "chapter.cbz.tmp").is_file()
    assert not (tmp_path / "chapter.cbz").exists()


def test_hash_reconciliation_runs_against_the_closed_archive_on_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_bytes = _image_bytes("PNG")
    page = tmp_path / "page.png"
    page.write_bytes(page_bytes)
    publisher = _AtomicTestPublisher()
    builder = _builder(publisher, max_archive_bytes=MAX_CBZ_BYTES_V1)
    original_writer = cast(Callable[..., None], attrgetter("_write_archive")(builder))

    def corrupt_closed_archive(
        temporary: Path,
        prepared_pages: Any,
        comic_info: bytes,
        *,
        max_archive_bytes: int,
        storage_gate: SharedStorageGate | None = None,
        disk_budget_manager: DiskBudgetManager | None = None,
    ) -> None:
        original_writer(
            temporary,
            prepared_pages,
            comic_info,
            max_archive_bytes=max_archive_bytes,
            storage_gate=storage_gate,
            disk_budget_manager=disk_budget_manager,
        )
        rewritten = temporary.with_name("rewritten-test-archive.cbz")
        with (
            zipfile.ZipFile(temporary, mode="r") as source,
            zipfile.ZipFile(rewritten, mode="w") as target,
        ):
            for info in source.infolist():
                content = source.read(info.filename)
                if info.filename == "001.png":
                    changed = bytearray(content)
                    changed[-1] ^= 0x01
                    content = bytes(changed)
                target.writestr(info, content)
        os.replace(rewritten, temporary)

    monkeypatch.setattr(builder, "_write_archive", corrupt_closed_archive)
    with pytest.raises(CBZBuildError) as error:
        builder.build(
            destination=tmp_path / "chapter.cbz",
            metadata=_metadata(),
            pages=(_validated_page(page, 1),),
        )

    assert error.value.stage == "page_hash_mismatch"
    assert publisher.calls == []
    assert (tmp_path / "chapter.cbz.tmp").is_file()
    assert not (tmp_path / "chapter.cbz").exists()


def test_page_ordinals_must_be_unique_and_contiguous(tmp_path: Path) -> None:
    page_bytes = _image_bytes("PNG")
    page = tmp_path / "page.png"
    page.write_bytes(page_bytes)
    publisher = _AtomicTestPublisher()

    with pytest.raises(CBZBuildError) as error:
        _builder(publisher, max_archive_bytes=MAX_CBZ_BYTES_V1).build(
            destination=tmp_path / "chapter.cbz",
            metadata=_metadata(),
            pages=(_validated_page(page, 1), _validated_page(page, 1)),
        )

    assert error.value.stage == "invalid_page_ordinals"
    assert publisher.calls == []
    assert not (tmp_path / "chapter.cbz.tmp").exists()


def test_corrupt_image_bytes_fail_archive_decode_and_keep_temporary(
    tmp_path: Path,
) -> None:
    valid_png = bytearray(_image_bytes("PNG"))
    chunk_offset = 8
    while chunk_offset < len(valid_png):
        chunk_length = int.from_bytes(valid_png[chunk_offset : chunk_offset + 4], "big")
        chunk_type = bytes(valid_png[chunk_offset + 4 : chunk_offset + 8])
        if chunk_type == b"IDAT":
            valid_png[chunk_offset + 8] ^= 0xFF
            break
        chunk_offset += 12 + chunk_length
    page = tmp_path / "truncated.png"
    page.write_bytes(valid_png)
    publisher = _AtomicTestPublisher()

    with pytest.raises(CBZBuildError) as error:
        _builder(publisher, max_archive_bytes=MAX_CBZ_BYTES_V1).build(
            destination=tmp_path / "chapter.cbz",
            metadata=_metadata(),
            pages=(_validated_page(page, 1),),
        )

    assert error.value.stage == "page_decode_failed"
    assert publisher.calls == []
    assert (tmp_path / "chapter.cbz.tmp").is_file()
    assert not (tmp_path / "chapter.cbz").exists()


def test_publisher_failure_preserves_verified_temporary_and_destination(
    tmp_path: Path,
) -> None:
    page_bytes = _image_bytes("PNG")
    page = tmp_path / "page.png"
    page.write_bytes(page_bytes)

    class FailingPublisher:
        def publish_no_replace(self, source: Path, destination: Path) -> None:
            raise PermissionError("simulated OS lock")

    with pytest.raises(CBZBuildError) as error:
        _builder(
            cast(AtomicNoReplacePublisher, FailingPublisher()),
            max_archive_bytes=MAX_CBZ_BYTES_V1,
        ).build(
            destination=tmp_path / "chapter.cbz",
            metadata=_metadata(),
            pages=(_validated_page(page, 1),),
        )

    assert error.value.stage == "publish_failed"
    assert (tmp_path / "chapter.cbz.tmp").is_file()
    with zipfile.ZipFile(tmp_path / "chapter.cbz.tmp") as archive:
        assert archive.testzip() is None
    assert not (tmp_path / "chapter.cbz").exists()


def test_publisher_is_called_only_after_archive_integrity_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_bytes = _image_bytes("PNG")
    page = tmp_path / "page.png"
    page.write_bytes(page_bytes)
    publisher = _AtomicTestPublisher()
    builder = _builder(publisher, max_archive_bytes=MAX_CBZ_BYTES_V1)

    def verification_failure(*args: object, **kwargs: object) -> None:
        raise CBZBuildError("zip_verification_failed")

    monkeypatch.setattr(builder, "_verify_archive", verification_failure)
    with pytest.raises(CBZBuildError) as error:
        builder.build(
            destination=tmp_path / "chapter.cbz",
            metadata=_metadata(),
            pages=(_validated_page(page, 1),),
        )

    assert error.value.stage == "zip_verification_failed"
    assert publisher.calls == []
    assert (tmp_path / "chapter.cbz.tmp").is_file()
    assert not (tmp_path / "chapter.cbz").exists()


@pytest.mark.parametrize("failure_stage", ["write", "flush"])
def test_cbz_temporary_disk_full_closes_shared_gate_and_preserves_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    page_bytes = _image_bytes("PNG")
    page = tmp_path / "page.png"
    page.write_bytes(page_bytes)
    destination = tmp_path / "chapter.cbz"
    temporary = tmp_path / "chapter.cbz.tmp"
    gate = SharedStorageGate()
    publisher = _AtomicTestPublisher()
    builder = _builder(
        publisher,
        max_archive_bytes=MAX_CBZ_BYTES_V1,
        storage_gate=gate,
    )
    real_open = cast(Callable[..., Any], Path.open)

    class DiskFullStream:
        def __init__(self, stream: Any) -> None:
            self._stream = stream
            self._failed_write = False
            self._failed_flush = False

        def __enter__(self) -> DiskFullStream:
            return self

        def __exit__(self, exc_type: object, _exc: object, _traceback: object) -> None:
            self._stream.close()

        def write(self, value: bytes) -> int:
            if failure_stage == "write" and not self._failed_write:
                self._failed_write = True
                self._stream.write(value[: min(8, len(value))])
                raise OSError(errno.ENOSPC, "injected CBZ staging write disk full")
            return self._stream.write(value)

        def flush(self) -> None:
            self._stream.flush()
            if failure_stage == "flush" and not self._failed_flush:
                self._failed_flush = True
                raise OSError(errno.ENOSPC, "injected CBZ staging flush disk full")

        def close(self) -> None:
            self._stream.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._stream, name)

    def patch_open(
        path: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        stream = real_open(path, mode, *args, **kwargs)
        if path == temporary and mode == "x+b":
            return DiskFullStream(stream)
        return stream

    monkeypatch.setattr(Path, "open", patch_open)

    with pytest.raises(CBZBuildError):
        builder.build(
            destination=destination,
            metadata=_metadata(),
            pages=(_validated_page(page, 1),),
        )

    assert gate.closed
    assert temporary.is_file()
    assert temporary.stat().st_size > 0
    assert publisher.calls == []
    assert not destination.exists()

