"""Verified EPUB construction with portable image pages for e-readers."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import struct
import zipfile
from collections.abc import Buffer, Callable, Generator, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import disk_usage
from typing import BinaryIO, cast
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from PIL import Image

from app.domain.asset import validate_relative_path
from app.persistence.disk_budget import MINIMUM_RESERVED_BYTES, DiskBudgetManager
from app.persistence.storage_gate import SharedStorageGate, is_disk_full_os_error
from app.persistence.windows_paths import DataRootIdentity
from app.services.cbr_builder import CbrExportService
from app.services.cbz_builder import _SizeBoundedZipStream  # pyright: ignore[reportPrivateUsage]
from app.services.image_validation import ImageValidationPolicy, validate_image_bytes
from app.services.reader_asset import V1_MAX_PAGE_BYTES, ReaderAssetService

MAX_EPUB_BYTES = 64 * 1024**3
MAX_EPUB_PAGES = 100_000
EPUB_FORMAT_VERSION = "portable-images-v4"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LANGUAGE = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*\Z")
_MIMETYPE = b"application/epub+zip"
_OPF = "http://www.idpf.org/2007/opf"
_XHTML = "http://www.w3.org/1999/xhtml"
_CONTAINER = b"""<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>
<rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/>
</rootfiles></container>"""
_IMAGE_POLICY = ImageValidationPolicy(V1_MAX_PAGE_BYTES, 40_000, 40_000, 100_000_000)
_IMAGE_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png"}


class _PngBuffer(io.BytesIO):
    def write(self, b: Buffer, /) -> int:
        if self.tell() + memoryview(b).nbytes > V1_MAX_PAGE_BYTES:
            raise ValueError("Converted PNG exceeds the supported page size")
        return super().write(b)


def _webp_to_png(payload: bytes) -> bytes:
    with Image.open(io.BytesIO(payload), formats=("WEBP",)) as source:
        with source.convert("RGBA") as rgba, _PngBuffer() as output:
            expected = CbrExportService._hash_rgba_pixels(rgba)  # pyright: ignore[reportPrivateUsage]
            rgba.save(output, format="PNG")
            result = output.getvalue()
            with Image.open(io.BytesIO(result), formats=("PNG",)) as decoded:
                with decoded.convert("RGBA") as pixels:
                    actual = CbrExportService._hash_rgba_pixels(pixels)  # pyright: ignore[reportPrivateUsage]
                    if decoded.size != rgba.size or actual != expected:
                        raise EpubBuildError("png_pixels")
            return result


class EpubBuildError(RuntimeError):
    """A sanitized build failure that leaves an existing publication untouched."""

    def __init__(self, stage: str, *, cause_type: str | None = None) -> None:
        self.stage = stage
        self.cause_type = cause_type
        super().__init__(f"EPUB construction failed during {stage}.")


class EpubBuildCancelled(EpubBuildError):
    def __init__(self) -> None:
        super().__init__("cancelled")


def _uuid(value: object) -> None:
    if type(value) is not UUID or value.version != 4:
        raise ValueError("EPUB identifiers must be UUIDv4")


def _text(value: object, *, empty: bool = False) -> str:
    if type(value) is not str or len(value) > 4096 or (not empty and not value.strip()):
        raise ValueError("EPUB metadata must be bounded text")
    if any(
        ord(c) not in (9, 10, 13)
        and not (
            0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF
        )
        for c in value
    ):
        raise ValueError("EPUB metadata contains invalid XML characters")
    return value


@dataclass(frozen=True, slots=True)
class EpubChapter:
    chapter_id: UUID
    label: str
    page_count: int
    asset_sha256: str
    image_bytes: int

    def __post_init__(self) -> None:
        _uuid(self.chapter_id)
        _text(self.label)
        if type(self.page_count) is not int or not 1 <= self.page_count <= MAX_EPUB_PAGES:
            raise ValueError("EPUB chapter page count is outside its supported bounds")
        if type(self.asset_sha256) is not str or not _SHA256.fullmatch(self.asset_sha256):
            raise ValueError("EPUB source hash must be lowercase SHA-256")
        if type(self.image_bytes) is not int or not 1 <= self.image_bytes <= MAX_EPUB_BYTES:
            raise ValueError("EPUB chapter byte count is outside its supported bounds")


@dataclass(frozen=True, slots=True)
class EpubBuildResult:
    path: Path
    filename: str
    size_bytes: int
    sha256: str
    chapter_count: int
    page_count: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _Page:
    ordinal: int
    mime: str

    @property
    def image(self) -> str:
        return f"images/p{self.ordinal:06d}.{_IMAGE_EXTENSIONS[self.mime]}"

    @property
    def document(self) -> str:
        return f"pages/p{self.ordinal:06d}.xhtml"


def _element(tag: str, text: str | None = None, **attributes: str) -> bytes:
    node = ET.Element(tag, attributes)
    node.text = text
    return ET.tostring(node, encoding="utf-8")


class EpubBuilder:
    def __init__(
        self,
        reader: ReaderAssetService,
        disk_budget: DiskBudgetManager,
        library_root_identity: DataRootIdentity,
        storage_gate: SharedStorageGate,
    ) -> None:
        if not isinstance(cast(object, reader), ReaderAssetService):
            raise TypeError("reader must be a ReaderAssetService")
        if not isinstance(cast(object, disk_budget), DiskBudgetManager):
            raise TypeError("disk_budget must be a DiskBudgetManager")
        if type(library_root_identity) is not DataRootIdentity:
            raise TypeError("library_root_identity must be a DataRootIdentity")
        if type(storage_gate) is not SharedStorageGate:
            raise TypeError("storage_gate must be a SharedStorageGate")
        self._reader = reader
        self._budget = disk_budget
        self._identity = library_root_identity
        self._gate = storage_gate

    @staticmethod
    def _regular(info: os.stat_result) -> bool:
        return (
            stat.S_ISREG(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & 0x0400
            and info.st_nlink == 1
        )

    def _path(self, path: object) -> os.stat_result | None:
        self._identity.assert_current()
        root = self._identity.canonical_path
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("EPUB output must use an absolute managed path")
        relative = path.relative_to(root)
        validate_relative_path(relative.as_posix())
        root_device = root.lstat().st_dev
        parent = root
        for part in relative.parts[:-1]:
            parent /= part
            info = parent.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x0400
                or info.st_dev != root_device
            ):
                raise ValueError("EPUB parent is not a safe managed directory")
        try:
            information = path.lstat()
        except FileNotFoundError:
            return None
        if not self._regular(information) or information.st_dev != root_device:
            raise ValueError("EPUB output is not a regular managed file")
        return information

    @staticmethod
    def _snapshot(info: os.stat_result) -> tuple[int, int, int, int, int]:
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _cancel(self, cancelled: Callable[[], bool] | None) -> None:
        self._gate.ensure_open()
        if cancelled is not None and cancelled():
            raise EpubBuildCancelled()

    @staticmethod
    def _info(name: str, size: int, *, image: bool = False) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = (
            zipfile.ZIP_STORED if image or name == "mimetype" else zipfile.ZIP_DEFLATED
        )
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        info.file_size = size
        return info

    def build(
        self,
        *,
        series_id: UUID,
        title: str,
        author: str | None,
        language: str | None,
        chapters: tuple[EpubChapter, ...],
        fingerprint: str,
        temporary_path: Path,
        destination_path: Path,
        on_progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> EpubBuildResult:
        _uuid(series_id)
        _text(title)
        if author is not None:
            _text(author, empty=True)
        if language is not None:
            _text(language, empty=True)
        language = language if language and _LANGUAGE.fullmatch(language) else "und"
        if (
            type(chapters) is not tuple
            or not chapters
            or any(type(c) is not EpubChapter for c in chapters)
        ):
            raise ValueError("EPUB requires a tuple of chapter snapshots")
        count = sum(chapter.page_count for chapter in chapters)
        if count > MAX_EPUB_PAGES or len({chapter.chapter_id for chapter in chapters}) != len(
            chapters
        ):
            raise ValueError("EPUB chapter selection is too large or contains duplicates")
        if type(fingerprint) is not str or not _SHA256.fullmatch(fingerprint):
            raise ValueError("EPUB fingerprint must be lowercase SHA-256")
        if on_progress is not None and not callable(on_progress):
            raise TypeError("on_progress must be callable")
        if cancelled is not None and not callable(cancelled):
            raise TypeError("cancelled must be callable")
        temporary = self._path(temporary_path)
        previous = self._path(destination_path)
        if temporary_path == destination_path or destination_path.suffix.lower() != ".epub":
            raise ValueError("EPUB staging and destination paths must be distinct")
        if temporary is not None:
            raise ValueError("EPUB staging file must not already exist")
        estimate = sum(
            c.image_bytes
            + c.page_count
            * (2048 + len(escape(c.label).encode()) + len(quoteattr(c.label).encode()))
            + 1024
            + len(escape(c.label).encode())
            for c in chapters
        )
        estimate += 16_384 + len(escape(title).encode()) + len(escape(author or "").encode())
        if estimate > MAX_EPUB_BYTES:
            raise ValueError("EPUB exceeds its supported byte budget")
        operation_id = uuid4()
        reserved = False
        stream: BinaryIO | None = None
        created: tuple[int, int] | None = None
        published = False
        stage = "reserve"
        try:
            self._cancel(cancelled)
            if disk_usage(destination_path.parent).free < estimate + MINIMUM_RESERVED_BYTES:
                raise EpubBuildError("reserve")
            self._budget.reserve_bounded_operation(operation_id, page_bytes=0, cbz_bytes=estimate)
            reserved = True
            stage = "write"
            self._path(temporary_path)
            with self._gate.mutation(), temporary_path.open("xb", buffering=0) as stream:
                info = os.fstat(stream.fileno())
                created = info.st_dev, info.st_ino
                if not self._regular(info):
                    raise EpubBuildError("staging_identity")
                output = _SizeBoundedZipStream(
                    stream,
                    max_bytes=MAX_EPUB_BYTES,
                    disk_budget_manager=self._budget,
                    job_id=operation_id,
                )
                with (
                    self._reusable(destination_path, series_id, previous) as reusable,
                    zipfile.ZipFile(cast(BinaryIO, output), "w", allowZip64=True) as archive,
                ):
                    expected = self._write(
                        archive,
                        series_id,
                        title,
                        author,
                        language,
                        chapters,
                        fingerprint,
                        count,
                        on_progress,
                        cancelled,
                        operation_id,
                        destination_path.parent,
                        reusable,
                    )
                stream.flush()
                os.fsync(stream.fileno())
            stage = "verify"
            written = self._path(temporary_path)
            if written is None or (written.st_dev, written.st_ino) != created:
                raise EpubBuildError("staging_identity")
            self._verify(temporary_path, expected, cancelled)
            sha256, size_bytes = self._hash(temporary_path, cancelled)
            if self._snapshot(written) != self._snapshot(temporary_path.lstat()):
                raise EpubBuildError("staging_changed")
            # No writes remain; releasing before promotion keeps every failure reversible.
            self._budget.release_bounded_operation(operation_id)
            reserved = False
            stage = "publish"
            self._cancel(cancelled)
            current = self._path(destination_path)
            if (None if previous is None else self._snapshot(previous)) != (
                None if current is None else self._snapshot(current)
            ):
                raise EpubBuildError("destination_changed")
            candidate = self._path(temporary_path)
            if candidate is None or self._snapshot(candidate) != self._snapshot(written):
                raise EpubBuildError("staging_changed")
            result = EpubBuildResult(
                destination_path,
                destination_path.name,
                size_bytes,
                sha256,
                len(chapters),
                count,
                fingerprint,
            )
            with self._gate.mutation():
                os.replace(temporary_path, destination_path)
            published = True
            return result
        except EpubBuildError:
            raise
        except OSError as error:
            if is_disk_full_os_error(error):
                self._gate.close("FILESYSTEM_DISK_FULL")
            raise EpubBuildError(stage, cause_type=type(error).__name__) from None
        except Exception as error:
            raise EpubBuildError(stage, cause_type=type(error).__name__) from None
        finally:
            closed = stream is None or stream.closed
            if not published and created is not None and closed:
                try:
                    candidate = self._path(temporary_path)
                    if candidate is not None and (candidate.st_dev, candidate.st_ino) == created:
                        with self._gate.mutation():
                            temporary_path.unlink()
                except OSError, RuntimeError, ValueError:
                    # Leave an unverifiable name for the caller's managed recovery.
                    pass
            if reserved and closed:
                self._budget.release_bounded_operation(operation_id)

    @contextmanager
    def _reusable(
        self, path: Path, series_id: UUID, expected: os.stat_result | None
    ) -> Generator[tuple[zipfile.ZipFile | None, dict[str, tuple[str, str]]]]:
        archive: zipfile.ZipFile | None = None
        index: dict[str, tuple[str, str]] = {}
        try:
            if expected is not None:
                archive = zipfile.ZipFile(path)
                if self._snapshot(path.lstat()) != self._snapshot(expected):
                    raise ValueError("Previous EPUB changed")
                member = archive.getinfo("EPUB/package.opf")
                if member.file_size > 64 * 1024**2:
                    raise ValueError("Previous EPUB metadata is too large")
                payload = archive.read(member)
                if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
                    raise ValueError("Previous EPUB contains XML entities")
                metadata = ET.fromstring(payload).find(f"{{{_OPF}}}metadata")
                if (
                    metadata is None
                    or metadata.findtext("{http://purl.org/dc/elements/1.1/}identifier")
                    != f"urn:uuid:{series_id}"
                ):
                    raise ValueError("Previous EPUB belongs to another series")
                raw = next(
                    (
                        node.text
                        for node in metadata.findall(f"{{{_OPF}}}meta")
                        if node.get("property") == "yomira:converted-pages-v1"
                    ),
                    None,
                )
                values: object = json.loads(raw) if raw else {}
                if not isinstance(values, dict):
                    raise ValueError("Previous EPUB conversion index is invalid")
                entries = cast(dict[object, object], values)
                if len(entries) > MAX_EPUB_PAGES:
                    raise ValueError("Previous EPUB conversion index is too large")
                for source_sha, raw_entry in entries.items():
                    if not isinstance(raw_entry, list):
                        raise ValueError("Previous EPUB conversion entry is invalid")
                    entry = cast(list[object], raw_entry)
                    if (
                        not isinstance(source_sha, str)
                        or not _SHA256.fullmatch(source_sha)
                        or len(entry) != 2
                        or not isinstance(entry[0], str)
                        or not isinstance(entry[1], str)
                        or not re.fullmatch(r"EPUB/images/p[0-9]{6}\.png", entry[0])
                        or not _SHA256.fullmatch(entry[1])
                    ):
                        raise ValueError("Previous EPUB conversion entry is invalid")
                    index[source_sha] = entry[0], entry[1]
        except OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError:
            index.clear()
        try:
            yield archive, index
        finally:
            if archive is not None:
                archive.close()

    @staticmethod
    def _reuse_png(
        reusable: tuple[zipfile.ZipFile | None, dict[str, tuple[str, str]]],
        source_sha: str,
        original: bytes,
    ) -> bytes | None:
        archive, index = reusable
        entry = index.get(source_sha)
        if archive is None or entry is None:
            return None
        try:
            member = archive.getinfo(entry[0])
            if not 0 < member.file_size <= V1_MAX_PAGE_BYTES:
                return None
            content = archive.read(member)
            if hashlib.sha256(content).hexdigest() != entry[1]:
                return None
            with (
                Image.open(io.BytesIO(original), formats=("WEBP",)) as source,
                Image.open(io.BytesIO(content), formats=("PNG",)) as cached,
                source.convert("RGBA") as source_pixels,
                cached.convert("RGBA") as cached_pixels,
            ):
                if source.size != cached.size or (
                    CbrExportService._hash_rgba_pixels(source_pixels)  # pyright: ignore[reportPrivateUsage]
                    != CbrExportService._hash_rgba_pixels(cached_pixels)  # pyright: ignore[reportPrivateUsage]
                ):
                    return None
            return content
        except OSError, ValueError, KeyError, zipfile.BadZipFile:
            return None

    def _write(
        self,
        archive: zipfile.ZipFile,
        series_id: UUID,
        title: str,
        author: str | None,
        language: str,
        chapters: tuple[EpubChapter, ...],
        fingerprint: str,
        count: int,
        on_progress: Callable[[int, int], None] | None,
        cancelled: Callable[[], bool] | None,
        operation_id: UUID,
        output_directory: Path,
        reusable: tuple[zipfile.ZipFile | None, dict[str, tuple[str, str]]],
    ) -> dict[str, tuple[int, str]]:
        expected: dict[str, tuple[int, str]] = {}
        conversions: dict[str, tuple[str, str]] = {}

        def write(name: str, content: bytes, *, image: bool = False) -> None:
            archive.writestr(self._info(name, len(content), image=image), content)
            expected[name] = len(content), hashlib.sha256(content).hexdigest()

        def chunks(name: str, factory: Callable[[], Iterator[bytes]]) -> None:
            size = sum(len(part) for part in factory())
            digest = hashlib.sha256()
            with archive.open(self._info(name, size), "w") as member:
                for part in factory():
                    self._cancel(cancelled)
                    member.write(part)
                    digest.update(part)
            expected[name] = size, digest.hexdigest()

        write("mimetype", _MIMETYPE)
        write("META-INF/container.xml", _CONTAINER)
        pages: list[_Page] = []
        for chapter in chapters:
            start = len(pages)
            self._cancel(cancelled)
            current = self._reader.get_chapter(chapter.chapter_id)
            if (
                current is None
                or current.series_id != series_id
                or current.page_count != chapter.page_count
            ):
                raise EpubBuildError("chapter_changed")
            emitted = 0
            image_bytes = 0
            with closing(self._reader.iter_verified_chapter_pages(chapter.chapter_id)) as source:
                for record, content in source:
                    self._cancel(cancelled)
                    emitted += 1
                    if (
                        emitted > chapter.page_count
                        or record.page_ordinal != emitted
                        or record.link.chapter_id != chapter.chapter_id
                        or record.link.page_count != chapter.page_count
                        or record.asset.sha256 != chapter.asset_sha256
                        or len(content) != record.page_size_bytes
                    ):
                        raise EpubBuildError("chapter_changed")
                    verified = validate_image_bytes(content, record.page_mime, policy=_IMAGE_POLICY)
                    if verified.sha256 != record.page_sha256 or verified.mime != record.page_mime:
                        raise EpubBuildError("page_changed")
                    image_bytes += len(content)
                    mime = verified.mime
                    if mime == "image/webp":
                        converted = self._reuse_png(reusable, verified.sha256, content)
                        if converted is None:
                            converted = _webp_to_png(content)
                        extra = max(0, len(converted) - len(content))
                        if extra:
                            self._cancel(cancelled)
                            if disk_usage(output_directory).free < extra + MINIMUM_RESERVED_BYTES:
                                raise EpubBuildError("reserve")
                            self._budget.increase_bounded_cbz_reservation(operation_id, extra)
                        content, mime = converted, "image/png"
                    page = _Page(len(pages) + 1, mime)
                    write("EPUB/" + page.image, content, image=True)
                    if verified.mime == "image/webp":
                        conversions[verified.sha256] = (
                            "EPUB/" + page.image,
                            hashlib.sha256(content).hexdigest(),
                        )
                    pages.append(page)
                    del content
                    if on_progress is not None:
                        on_progress(len(pages), count)
            if emitted != chapter.page_count or image_bytes != chapter.image_bytes:
                raise EpubBuildError("chapter_changed")
            chapter_pages = pages[start:]
            chunks(
                "EPUB/" + chapter_pages[0].document,
                lambda chapter=chapter, chapter_pages=chapter_pages: self._chapter_document(
                    chapter_pages, chapter.label, language
                ),
            )
        chunks("EPUB/nav.xhtml", lambda: self._navigation(chapters, language))
        chunks("EPUB/toc.ncx", lambda: self._ncx(series_id, title, chapters, language))
        modified = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        chunks(
            "EPUB/package.opf",
            lambda: self._package(
                series_id,
                title,
                author,
                language,
                chapters,
                pages,
                fingerprint,
                modified,
                json.dumps(conversions, separators=(",", ":")),
            ),
        )
        return expected

    @staticmethod
    def _chapter_document(
        pages: list[_Page],
        label: str,
        language: str,
    ) -> Iterator[bytes]:
        yield (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<html xmlns="{_XHTML}" xmlns:epub="http://www.idpf.org/2007/ops" '
            f"lang={quoteattr(language)} xml:lang={quoteattr(language)}><head>"
            f"<title>{escape(label)}</title><style>"
            "html,body{margin:0;padding:0}"
            "img+img{page-break-before:always;break-before:page}"
            "</style></head><body>"
        ).encode()
        # Keep each chapter together so readers can count more than one page.
        for index, page in enumerate(pages, 1):
            heading = f"{label} · Página {index}"
            cover = ' epub:type="cover"' if page.ordinal == 1 else ""
            yield (f'<img{cover} src="../{page.image}" alt={quoteattr(heading)}/>').encode()
        yield b"</body></html>"

    @staticmethod
    def _navigation(chapters: tuple[EpubChapter, ...], language: str) -> Iterator[bytes]:
        yield (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<html xmlns="{_XHTML}" xmlns:epub="http://www.idpf.org/2007/ops" '
            f"lang={quoteattr(language)} xml:lang={quoteattr(language)}>"
            "<head><title>Capítulos</title></head><body>"
            '<nav epub:type="toc" id="toc"><h1>Capítulos</h1><ol>'
        ).encode()
        ordinal = 1
        for chapter in chapters:
            yield (
                f'<li><a href="pages/p{ordinal:06d}.xhtml">{escape(chapter.label)}</a></li>'
            ).encode()
            ordinal += chapter.page_count
        yield (
            b'</ol></nav><nav epub:type="landmarks" hidden="hidden"><ol><li>'
            b'<a epub:type="cover" href="pages/p000001.xhtml">Portada</a>'
            b"</li></ol></nav></body></html>"
        )

    @staticmethod
    def _ncx(
        series_id: UUID, title: str, chapters: tuple[EpubChapter, ...], language: str
    ) -> Iterator[bytes]:
        yield (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" '
            f"xml:lang={quoteattr(language)}><head>"
            f'<meta name="dtb:uid" content="urn:uuid:{series_id}"/>'
            '<meta name="dtb:depth" content="1"/><meta name="dtb:totalPageCount" content="0"/>'
            '<meta name="dtb:maxPageNumber" content="0"/></head>'
            f"<docTitle><text>{escape(title)}</text></docTitle><navMap>"
        ).encode()
        ordinal = 1
        for index, chapter in enumerate(chapters, 1):
            yield (
                f'<navPoint id="chapter-{index}" playOrder="{index}"><navLabel>'
                f"<text>{escape(chapter.label)}</text></navLabel>"
                f'<content src="pages/p{ordinal:06d}.xhtml"/></navPoint>'
            ).encode()
            ordinal += chapter.page_count
        yield b"</navMap></ncx>"

    @staticmethod
    def _package(
        series_id: UUID,
        title: str,
        author: str | None,
        language: str,
        chapters: tuple[EpubChapter, ...],
        pages: list[_Page],
        fingerprint: str,
        modified: str,
        conversions: str,
    ) -> Iterator[bytes]:
        yield (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<package xmlns="{_OPF}" version="3.0" unique-identifier="publication-id" '
            'prefix="yomira: urn:yomira:metadata:"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        ).encode()
        yield _element("dc:identifier", f"urn:uuid:{series_id}", id="publication-id")
        yield _element("dc:title", title)
        yield _element("dc:language", language)
        if author:
            yield _element("dc:creator", author)
        for name, value in (
            ("dcterms:modified", modified),
            ("rendition:layout", "reflowable"),
            ("yomira:fingerprint", fingerprint),
            ("yomira:chapter-count", str(len(chapters))),
            ("yomira:page-count", str(len(pages))),
            ("yomira:converted-pages-v1", conversions),
        ):
            yield _element("meta", value, property=name)
        yield b"</metadata><manifest>"
        yield _element(
            "item", id="ncx", href="toc.ncx", **{"media-type": "application/x-dtbncx+xml"}
        )
        yield _element(
            "item",
            id="nav",
            href="nav.xhtml",
            **{
                "media-type": "application/xhtml+xml",
                "properties": "nav",
            },
        )
        ordinal = 1
        for chapter in chapters:
            yield _element(
                "item",
                id=f"page-{ordinal}",
                href=f"pages/p{ordinal:06d}.xhtml",
                **{"media-type": "application/xhtml+xml"},
            )
            ordinal += chapter.page_count
        for page in pages:
            properties = {"properties": "cover-image"} if page.ordinal == 1 else {}
            yield _element(
                "item",
                id=f"image-{page.ordinal}",
                href=page.image,
                **{"media-type": page.mime, **properties},
            )
        yield b'</manifest><spine toc="ncx">'
        ordinal = 1
        for chapter in chapters:
            yield _element("itemref", idref=f"page-{ordinal}")
            ordinal += chapter.page_count
        yield b"</spine></package>"

    def _verify(
        self,
        path: Path,
        expected: dict[str, tuple[int, str]],
        cancelled: Callable[[], bool] | None,
    ) -> None:
        with path.open("rb") as raw:
            header = raw.read(30)
            if len(header) != 30:
                raise EpubBuildError("verify_mimetype")
            fields = struct.unpack("<4s5H3I2H", header)
            if fields[0] != b"PK\x03\x04" or fields[3] != 0 or fields[-1] != 0:
                raise EpubBuildError("verify_mimetype")
            if raw.read(fields[-2]) != b"mimetype":
                raise EpubBuildError("verify_mimetype")
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if [info.filename for info in infos] != list(expected):
                raise EpubBuildError("verify_structure")
            for info in infos:
                self._cancel(cancelled)
                size, sha256 = expected[info.filename]
                if (
                    info.file_size != size
                    or info.flag_bits & 1
                    or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or not stat.S_ISREG(info.external_attr >> 16)
                ):
                    raise EpubBuildError("verify_structure")
                digest = hashlib.sha256()
                with archive.open(info) as member:
                    while chunk := member.read(65_536):
                        self._cancel(cancelled)
                        digest.update(chunk)
                if digest.hexdigest() != sha256:
                    raise EpubBuildError("verify_hash")
                if info.filename.endswith((".xml", ".opf", ".xhtml", ".ncx")):
                    with archive.open(info) as member:
                        for _event, element in ET.iterparse(member):
                            element.clear()
            if archive.read("mimetype") != _MIMETYPE:
                raise EpubBuildError("verify_mimetype")

    def _hash(self, path: Path, cancelled: Callable[[], bool] | None) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                self._cancel(cancelled)
                size += len(chunk)
                if size > MAX_EPUB_BYTES:
                    raise EpubBuildError("verify_size")
                digest.update(chunk)
        return digest.hexdigest(), size
