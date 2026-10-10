"""Discover stable local comics and publish them through the verified CBZ pipeline."""

import asyncio
import hashlib
import logging
import mimetypes
import os
import posixpath
import re
import stat
import time
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.asset import Asset, AssetType
from app.domain.chapter import Chapter, chapter_number
from app.domain.chapter_cbz_asset import ChapterCbzAsset
from app.domain.chapter_source import ChapterSource
from app.domain.download_job import DownloadJob, DownloadJobStatus, DownloadPriority
from app.domain.metadata import MetadataField, resolve_series_metadata
from app.domain.series import Series
from app.domain.source import Source, SourceHealthStatus
from app.persistence.database import SQLiteDatabase
from app.persistence.disk_budget import DiskBudgetManager
from app.persistence.managed_directories import ensure_managed_directory
from app.persistence.mappers.asset import asset_from_row, asset_to_row
from app.persistence.mappers.chapter import chapter_from_row, chapter_to_row
from app.persistence.mappers.chapter_cbz_asset import chapter_cbz_asset_to_row
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.download_job import download_job_to_row
from app.persistence.mappers.series import series_from_row, series_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.models.asset import AssetRow
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_cbz_asset import ChapterCbzAssetRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job_archive import DownloadJobArchiveRow
from app.persistence.models.download_page import DownloadPageRow
from app.persistence.models.series import SeriesRow
from app.persistence.models.source import SourceRow
from app.persistence.repositories.chapter_cbz_asset import ChapterCbzAssetRepository
from app.persistence.repositories.metadata import MetadataRepository
from app.persistence.storage_gate import StorageGateClosedError
from app.persistence.windows_paths import DataRootIdentity, DataRootIdentityError
from app.services.cbz_builder import (
    MAX_CBZ_BYTES_V1,
    CBZBuilder,
    ComicInfoMetadata,
    ValidatedPage,
)
from app.services.cover_assets import (  # pyright: ignore[reportPrivateUsage]
    CoverAssetService,
    _completed_io,  # pyright: ignore[reportPrivateUsage]
)
from app.services.external_metadata import (  # pyright: ignore[reportPrivateUsage]
    _query_title,  # pyright: ignore[reportPrivateUsage]
    _title_key,  # pyright: ignore[reportPrivateUsage]
)
from app.services.image_validation import (
    ImageValidationError,
    ImageValidationPolicy,
    ValidatedImage,
    validate_image_file,
)
from app.services.reader_asset import ReaderAssetLimits, ReaderAssetService
from app.tracing import create_trace_id

logger = logging.getLogger("yomira.local_import")
_IMAGES = {".png", ".jpg", ".jpeg", ".webp"}
_ARCHIVES = {".cbz", ".zip", ".epub"}
_LOCAL_SOURCE = "yomira.local.import"
IMPORT_FOLDER_NAME = "Para añadir"
_MAX_FILES = 10_000
_MAX_XML_BYTES = 1_048_576
_MAX_PAGE_BYTES = 67_108_864
_CHAPTER = re.compile(
    r"(?i)(?:^|[\s._-])(?:cap(?:[ií]tulo)?\.?|chapter|ch\.?|episodio|episode|vol(?:umen|ume)?\.?|tomo)\s*(\d+(?:[.,]\d+)?)"
)


class LocalImportPageError(ValueError):
    """A safe local page diagnostic, without archive contents or internal paths."""


def _natural(value: str) -> str:
    return re.sub(r"\d+", lambda match: match.group().zfill(16), value.casefold())


def _plain(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return not (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400)


def _xml(data: bytes) -> ET.Element:
    upper = data.upper()
    # Free Manga emits external XHTML doctypes; ElementTree does not fetch them.
    if (
        b"\x00" in data
        or len(data) > _MAX_XML_BYTES
        or b"<!ENTITY" in upper
        or re.search(rb"<!DOCTYPE[^>]*\[", upper)
    ):
        raise ValueError("XML no válido o demasiado grande")
    return ET.fromstring(data)


def _member(base: str, reference: str) -> str:
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc or reference.startswith("/") or "\\" in reference:
        raise ValueError("Referencia externa o ruta no válida")
    value = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(parsed.path)))
    if value.startswith("../") or value == ".." or value.startswith("/"):
        raise ValueError("Ruta fuera del archivo")
    return value


def _read_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    info = archive.getinfo(name)
    if info.file_size > _MAX_XML_BYTES:
        raise ValueError("XML demasiado grande")
    with archive.open(info) as stream:
        return _xml(stream.read(_MAX_XML_BYTES + 1))


def _archive_pages(archive: zipfile.ZipFile, *, epub: bool) -> tuple[list[str], dict[str, str]]:
    infos = archive.infolist()
    if len(infos) > _MAX_FILES * 4 or len({info.filename for info in infos}) != len(infos):
        raise ValueError("Demasiadas entradas o nombres duplicados")
    for info in infos:
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
            raise ValueError("Ruta no válida dentro del archivo")
        if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
            raise ValueError("Enlaces o archivo cifrado no admitidos")
    metadata: dict[str, str] = {}
    if not epub:
        if "ComicInfo.xml" in archive.namelist():
            metadata = {
                child.tag: child.text.strip()
                for child in _read_xml(archive, "ComicInfo.xml")
                if child.text
            }
        return sorted(
            (
                info.filename
                for info in infos
                if not info.is_dir()
                and PurePosixPath(info.filename).suffix.lower() in _IMAGES
                and not any(
                    part.startswith(".") or part == "__MACOSX"
                    for part in PurePosixPath(info.filename).parts
                )
            ),
            key=_natural,
        ), metadata
    container = _read_xml(archive, "META-INF/container.xml")
    rootfile = next((item for item in container.iter() if item.tag.endswith("}rootfile")), None)
    if rootfile is None:
        raise ValueError("EPUB sin documento principal")
    package_name = _member("", rootfile.attrib["full-path"])
    package = _read_xml(archive, package_name)
    items = {item.attrib["id"]: item for item in package.iter() if item.tag.endswith("}item")}
    for item in package.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        field = {
            "title": "Title",
            "creator": "Writer",
            "description": "Summary",
            "language": "LanguageISO",
        }.get(tag)
        if field and item.text:
            metadata[field] = item.text.strip()
    pages: list[str] = []
    for reference in package.iter():
        if not reference.tag.endswith("}itemref"):
            continue
        item = items[reference.attrib["idref"]]
        name = _member(package_name, item.attrib["href"])
        if item.attrib.get("media-type", "").startswith("image/"):
            pages.append(name)
            continue
        document = _read_xml(archive, name)
        for element in document.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            if tag not in {"img", "image"}:
                continue
            image = (
                element.attrib.get("src")
                or element.attrib.get("{http://www.w3.org/1999/xlink}href")
                or element.attrib.get("href")
            )
            if image:
                pages.append(_member(name, image))
    if not pages:
        raise ValueError("EPUB sin páginas de manga en su orden de lectura")
    return pages, metadata


class LocalImportService:
    def __init__(
        self,
        *,
        database: SQLiteDatabase,
        library_root: DataRootIdentity,
        builder: CBZBuilder,
        covers: CoverAssetService,
        disk_budget: DiskBudgetManager,
        admission_allowed: Callable[[], bool],
        additional_roots: tuple[Path, ...] = (),
    ) -> None:
        self._database = database
        self._identity = library_root
        self._root = library_root.canonical_path
        self._inputs = {self._root: library_root}
        for path in additional_roots:
            if path.is_dir() and _plain(path):
                identity = DataRootIdentity.capture(path)
                root = identity.canonical_path
                if not any(root.is_relative_to(existing) for existing in self._inputs):
                    self._inputs[root] = identity
        self._builder = builder
        self._covers = covers
        self._disk = disk_budget
        self._allowed = admission_allowed
        self._reader = ReaderAssetService(
            database,
            data_root_identity=database.data_root_identity,
            library_root_identity=library_root,
            limits=ReaderAssetLimits(
                max_cbz_bytes=MAX_CBZ_BYTES_V1, max_page_bytes=_MAX_PAGE_BYTES, chunk_bytes=65_536
            ),
        )
        self._policy = ImageValidationPolicy(
            max_bytes=_MAX_PAGE_BYTES, max_width=40_000, max_height=40_000, max_pixels=100_000_000
        )
        self._seen: dict[Path, tuple[tuple[str, int, int, int], ...]] = {}
        self._done: dict[Path, tuple[tuple[str, int, int, int], ...]] = {}
        self._errors: dict[Path, str] = {}
        self._folder_errors: dict[Path, str] = {}
        self._pending: dict[UUID, float] = {}
        self._metadata_attempts: dict[UUID, int] = {}
        self._metadata_review: set[UUID] = set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._scanning = False
        self._imported = 0
        for relative in (IMPORT_FOLDER_NAME, "chapters"):
            try:
                self._directory(relative, library=True)
            except (OSError, DataRootIdentityError, StorageGateClosedError) as error:
                logger.warning(
                    "local_import.folder_unavailable", extra={"cause_type": type(error).__name__}
                )

    def _folder_problem(self, path: Path, identity: DataRootIdentity | None = None) -> str | None:
        try:
            (identity or self._identity).assert_current()
            if (
                not _plain(path)
                or not path.is_dir()
                or not path.resolve().is_relative_to((identity or self._identity).canonical_path)
            ):
                raise OSError("folder unavailable")
            # Read access is checked without modifying the user's files.
            with os.scandir(path) as entries:
                next(entries, None)
        except OSError, DataRootIdentityError:
            return (
                "La carpeta no está disponible. Comprueba que siga en esta ubicación "
                "y que puedas abrirla desde el Explorador."
            )
        return None

    def open_folder(self, kind: str) -> None:
        relative = {"library": "", "inbox": IMPORT_FOLDER_NAME, "downloads": "chapters"}[kind]
        path = self._root / relative
        if kind == "inbox" and not path.exists():
            path = self._directory(IMPORT_FOLDER_NAME, library=True)
        if self._folder_problem(path):
            raise OSError("library folder unavailable")
        if os.name != "nt":
            raise NotImplementedError("folder opening requires Windows")
        os.startfile(str(path), "explore")

    def status(self) -> dict[str, object]:
        return {
            "folder": str(self._root),
            "folders": [
                {"kind": kind, "path": str(path), "problem": self._folder_problem(path, identity)}
                for kind, path, identity in (
                    ("library", self._root, self._identity),
                    ("inbox", self._root / IMPORT_FOLDER_NAME, self._identity),
                    ("downloads", self._root / "chapters", self._identity),
                    *(
                        ("legacy", root, identity)
                        for root, identity in self._inputs.items()
                        if root != self._root
                    ),
                )
            ],
            "scanning": self._scanning,
            "imported_chapters": self._imported,
            "pending_metadata": len(self._pending),
            "metadata_review": len(self._metadata_review),
            "errors": [
                {
                    "file": self._input_label(path),
                    "message": message,
                    **(
                        {"chapter_id": chapter_id}
                        if (chapter_id := self._replacement_chapter(path))
                        else {}
                    ),
                }
                for path, message in list((self._folder_errors | self._errors).items())[:20]
            ],
        }

    def _replacement_chapter(self, path: Path) -> str | None:
        """Keep the rejected original visible, but offer an unambiguous readable copy."""
        try:
            title, label, _ = self._identity_for(path, {})
        except ValueError, OSError:
            return None
        number = chapter_number(label)
        if number is None:
            return None
        with self._database.read_session() as session:
            series_ids = [
                row.series_id
                for row in session.scalars(select(SeriesRow))
                if _title_key(title)
                in {_title_key(row.canonical_title), _title_key(row.display_title)}
            ]
            if len(series_ids) != 1:
                return None
            matches = [
                row
                for row in session.scalars(
                    select(ChapterRow).where(ChapterRow.series_id == series_ids[0])
                )
                if chapter_number(row.display_label) == number
                and row.chapter_variant is None
                and row.volume_number is None
                and row.chapter_type in (None, "CHAPTER")
                and self._chapter_available(session, UUID(row.chapter_id))
            ]
            return matches[0].chapter_id if len(matches) == 1 else None

    def retry_failed(self) -> None:
        for path in self._errors:
            self._done.pop(path, None)
        self._pending = dict.fromkeys(self._pending, 0)
        self._metadata_attempts.clear()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "local_import.scan_failed", extra={"cause_type": type(error).__name__}
                )
            await asyncio.sleep(5)

    def _input_root(self, path: Path) -> Path:
        for root, identity in self._inputs.items():
            if path.is_relative_to(root) and path.resolve().is_relative_to(root):
                identity.assert_current()
                return root
        raise ValueError("Ruta fuera de las carpetas de importación")

    def _input_label(self, path: Path) -> str:
        root = next(root for root in self._inputs if path.is_relative_to(root))
        relative = path.relative_to(root).as_posix()
        return relative if root == self._root else f"{root.name}/{relative}"

    def _fingerprint(self, path: Path) -> tuple[tuple[str, int, int, int], ...]:
        root = self._input_root(path)
        if not _plain(path):
            raise ValueError("Ruta fuera de la biblioteca")
        files = (
            sorted(
                (
                    child
                    for child in path.iterdir()
                    if child.suffix.lower() in _IMAGES or child.name == "ComicInfo.xml"
                ),
                key=lambda child: _natural(child.name),
            )
            if path.is_dir()
            else [path]
        )
        if not files or len(files) > _MAX_FILES:
            raise ValueError("Número de páginas no admitido")
        result: list[tuple[str, int, int, int]] = []
        for file in files:
            if not _plain(file) or not file.is_file() or not file.resolve().is_relative_to(root):
                raise ValueError("La ruta debe ser un archivo local normal")
            info = file.stat()
            result.append((file.name, info.st_ino, info.st_size, info.st_mtime_ns))
        return tuple(result)

    def _discover(self) -> dict[Path, tuple[tuple[str, int, int, int], ...]]:
        self._identity.assert_current()
        self._folder_errors.clear()
        found: dict[Path, tuple[tuple[str, int, int, int], ...]] = {}
        for root in self._inputs:
            if self._folder_problem(root, self._inputs[root]):
                continue
            found.update(self._discover_root(root))
        return found

    def _discover_root(self, root: Path) -> dict[Path, tuple[tuple[str, int, int, int], ...]]:
        found: dict[Path, tuple[tuple[str, int, int, int], ...]] = {}

        def unreadable(error: OSError) -> None:
            path = Path(error.filename) if error.filename else root
            if path.is_relative_to(root):
                self._folder_errors[path] = (
                    "No se pudo leer esta carpeta. Comprueba sus permisos "
                    "y que sus archivos estén disponibles en este equipo."
                )

        for directory, subdirs, files in os.walk(root, followlinks=False, onerror=unreadable):
            parent = Path(directory)
            subdirs[:] = sorted(
                (
                    name
                    for name in subdirs
                    if not name.startswith(".")
                    and not (parent == root and name.casefold() in {"chapters", "exports"})
                    and _plain(parent / name)
                ),
                key=_natural,
            )
            if len(parent.relative_to(root).parts) >= 16:
                subdirs.clear()
            candidates = [
                parent / name
                for name in files
                if Path(name).suffix.lower() in _ARCHIVES and not name.startswith(".")
            ]
            if parent != root and any(Path(name).suffix.lower() in _IMAGES for name in files):
                candidates.append(parent)
            for candidate in sorted(candidates, key=lambda path: _natural(str(path))):
                try:
                    found[candidate] = self._fingerprint(candidate)
                except OSError, ValueError:
                    continue
                if len(found) >= _MAX_FILES:
                    return found
        return found

    async def scan_once(self) -> None:
        async with self._lock:
            if not self._allowed():
                return
            self._scanning = True
            try:
                found = await _completed_io(self._discover)
                await _completed_io(self._recheck_imports)
                for path, fingerprint in found.items():
                    # ponytail: two snapshots; use a copy-complete marker for paused transfers.
                    if self._seen.get(path) != fingerprint or self._done.get(path) == fingerprint:
                        continue
                    if not self._allowed():
                        break
                    try:
                        series_id, created = await _completed_io(
                            lambda path=path, fingerprint=fingerprint: self._import(
                                path, fingerprint
                            )
                        )
                        self._imported += int(created)
                        self._errors.pop(path, None)
                        if series_id not in self._metadata_review:
                            self._pending.setdefault(series_id, 0)
                            if self._pending[series_id] == 0:
                                await self._fill_metadata(series_id)
                    except Exception as error:
                        self._errors[path] = (
                            str(error)
                            if isinstance(error, LocalImportPageError)
                            else (
                                "No se pudo validar. Comprueba que la copia terminó y que "
                                "el archivo contiene imágenes PNG, JPEG o WebP."
                            )
                        )
                        logger.warning(
                            "local_import.file_rejected", extra={"cause_type": type(error).__name__}
                        )
                    self._done[path] = fingerprint
                self._seen = found
                self._done = {path: value for path, value in self._done.items() if path in found}
                self._errors = {
                    path: value for path, value in self._errors.items() if path in found
                }
                eligible = [
                    (key, retry)
                    for key, retry in self._pending.items()
                    if time.monotonic() >= retry
                ]
                for series_id, _ in eligible[:5]:
                    await self._fill_metadata(series_id)
                for series_id in tuple(self._metadata_review):
                    if await _completed_io(
                        lambda series_id=series_id: self._metadata_ready(series_id)
                    ):
                        self._metadata_review.discard(series_id)
            finally:
                self._scanning = False

    async def _fill_metadata(self, series_id: UUID) -> None:
        if not self._allowed():
            return
        self._pending[series_id] = time.monotonic() + 60
        attempts = self._metadata_attempts.get(series_id, 0) + 1
        self._metadata_attempts[series_id] = attempts
        finished = False
        needs_review = True
        try:
            if not await _completed_io(lambda: self._metadata_ready(series_id)):
                await self._covers.ensure_cover(series_id)
            needs_review = not await _completed_io(lambda: self._metadata_ready(series_id))
            finished = True
        except Exception as error:
            finished = attempts >= 3
            logger.warning(
                "local_import.metadata_pending",
                extra={
                    "series_id": str(series_id),
                    "cause_type": type(error).__name__,
                },
            )
        if finished:
            self._pending.pop(series_id, None)
            self._metadata_attempts.pop(series_id, None)
            if needs_review:
                self._metadata_review.add(series_id)

    def _metadata_ready(self, series_id: UUID) -> bool:
        repository = MetadataRepository()
        with self._database.read_session() as session:
            row = session.get(SeriesRow, str(series_id))
            if row is None:
                return True
            values = resolve_series_metadata(
                series_from_row(row),
                repository.list_series_sources(session, series_id),
                repository.list_overrides(session, series_id),
            )
            return all(
                item.value is not None
                for item in values
                if item.field
                in {
                    MetadataField.AUTHOR,
                    MetadataField.DESCRIPTION,
                    MetadataField.GENRES,
                    MetadataField.COVER,
                }
            )

    def _identity_for(
        self, path: Path, metadata: dict[str, str]
    ) -> tuple[str, str, tuple[int, ...]]:
        relative = path.relative_to(self._input_root(path))
        if path.is_relative_to(self._root / IMPORT_FOLDER_NAME):
            relative = path.relative_to(self._root / IMPORT_FOLDER_NAME)
        name = path.name if path.is_dir() else path.stem
        match = _CHAPTER.search(name)
        title = metadata.get("Series") or (
            relative.parts[0]
            if len(relative.parts) > 1
            else name[: match.start()]
            if match and match.start()
            else name
        )
        title = " ".join(title.strip(" ._-").split())[:2_000]
        if not title:
            raise ValueError("El nombre de la carpeta debe identificar el manga")
        number = metadata.get("Number") or (
            match.group(1) if match else name if re.fullmatch(r"\d+(?:[.,]\d+)?", name) else None
        )
        if number and re.fullmatch(r"\d+(?:[.,]\d+)?", number):
            # Free Manga groups thousands with commas: 001,364 means chapter 1364.
            if re.fullmatch(r"\d{1,3},\d{3}", number):
                number = str(int(number.replace(",", "")))
            parts = tuple(int(value) for value in re.split(r"[.,]", number))
            return title, f"Capítulo {number}", parts
        return title, metadata.get("Title", name)[:2_000], (0,)

    def _directory(self, relative: str, *, library: bool = False) -> Path:
        return ensure_managed_directory(
            relative,
            data_root_identity=self._identity if library else self._database.data_root_identity,
            storage_gate=self._database.storage_gate,
            component_name="local_import",
            trace_id=create_trace_id(),
        )

    def _chapter_available(self, session: Session, chapter_id: UUID) -> bool:
        linked = ChapterCbzAssetRepository().get_for_chapter(session, chapter_id)
        return linked is not None and self._reader.asset_available(linked[1])

    def _recheck_imports(self) -> None:
        with self._database.read_session() as session:
            rows = session.execute(
                select(ChapterSourceRow.remote_url, AssetRow)
                .join(SourceRow, SourceRow.source_id == ChapterSourceRow.source_id)
                .join(
                    ChapterCbzAssetRow, ChapterCbzAssetRow.chapter_id == ChapterSourceRow.chapter_id
                )
                .join(AssetRow, AssetRow.asset_id == ChapterCbzAssetRow.asset_id)
                .where(SourceRow.connector_id == _LOCAL_SOURCE)
            )
            missing = {
                reference.split("#", 1)[0]
                for reference, asset in rows
                if reference and not self._reader.asset_available(asset_from_row(asset))
            }
        self._done = {
            path: value for path, value in self._done.items() if path.as_uri() not in missing
        }

    def _import(
        self, path: Path, fingerprint: tuple[tuple[str, int, int, int], ...]
    ) -> tuple[UUID, bool]:
        if self._fingerprint(path) != fingerprint:
            raise ValueError("La copia todavía está cambiando")
        # ponytail: stat-based cache; rehash if edits preserving inode/size/mtime must be supported.
        input_reference = (
            path.as_uri() + "#" + hashlib.sha256(repr(fingerprint).encode()).hexdigest()
        )
        with self._database.read_session() as session:
            cached = session.scalar(
                select(ChapterRow)
                .join(ChapterSourceRow, ChapterSourceRow.chapter_id == ChapterRow.chapter_id)
                .join(SourceRow, SourceRow.source_id == ChapterSourceRow.source_id)
                .join(ChapterCbzAssetRow, ChapterCbzAssetRow.chapter_id == ChapterRow.chapter_id)
                .where(
                    SourceRow.connector_id == _LOCAL_SOURCE,
                    ChapterSourceRow.remote_url == input_reference,
                )
            )
            if cached is not None and self._chapter_available(session, UUID(cached.chapter_id)):
                return UUID(cached.series_id), False
        job_id = uuid4()
        staged = self._directory(f"temp/local-imports/{job_id}")
        page_paths: list[Path] = []
        archive: zipfile.ZipFile | None = None
        archive_stream = None
        destination: Path | None = None
        built = False
        committed = False
        reserved = False
        try:
            if self._fingerprint(path) != fingerprint:
                raise ValueError("Ruta no admitida")
            input_root = self._input_root(path)
            expected = {
                name: (inode, size, modified) for name, inode, size, modified in fingerprint
            }

            def open_original(file: Path):
                if not _plain(file) or not file.resolve().is_relative_to(input_root):
                    raise ValueError("Ruta no admitida")
                stream = file.open("rb")
                info = os.fstat(stream.fileno())
                if (info.st_ino, info.st_size, info.st_mtime_ns) != expected[file.name]:
                    stream.close()
                    raise ValueError("La copia todavía está cambiando")
                return stream

            if path.is_dir():
                names = sorted(
                    (child.name for child in path.iterdir() if child.suffix.lower() in _IMAGES),
                    key=_natural,
                )
                metadata = {}
                if (path / "ComicInfo.xml").is_file():
                    with open_original(path / "ComicInfo.xml") as stream:
                        metadata = {
                            child.tag: child.text.strip()
                            for child in _xml(stream.read(_MAX_XML_BYTES + 1))
                            if child.text
                        }
                sizes = [(path / name).stat().st_size for name in names]
            else:
                archive_stream = open_original(path)
                archive = zipfile.ZipFile(archive_stream)
                names, metadata = _archive_pages(archive, epub=path.suffix.lower() == ".epub")
                sizes = [archive.getinfo(name).file_size for name in names]
            if not 0 < len(names) <= _MAX_FILES or any(
                not 0 < size <= _MAX_PAGE_BYTES for size in sizes
            ):
                raise ValueError("Tamaño o número de páginas no admitido")
            total = sum(sizes)
            if total + 4_194_304 > MAX_CBZ_BYTES_V1:
                raise ValueError("El manga supera el tamaño admitido")
            self._disk.reserve_bounded_operation(
                job_id, page_bytes=total, cbz_bytes=total + 4_194_304
            )
            reserved = True
            images: list[ValidatedImage] = []
            pages: list[ValidatedPage] = []
            for ordinal, (name, expected_size) in enumerate(zip(names, sizes, strict=True), 1):
                extension = PurePosixPath(name).suffix.lower()
                if extension not in _IMAGES:
                    raise ValueError("Formato de página no admitido")
                page = staged / f"{ordinal:06d}{extension}"
                page_paths.append(page)
                incoming = archive.open(name) if archive is not None else open_original(path / name)
                count = 0
                with incoming, page.open("xb") as outgoing:
                    while chunk := incoming.read(65_536):
                        count += len(chunk)
                        if count > expected_size:
                            raise ValueError("La página cambió durante la copia")
                        self._disk.write_reserved(
                            job_id,
                            category="pages",
                            requested_bytes=len(chunk),
                            write=lambda chunk=chunk: outgoing.write(chunk),
                        )
                if count != expected_size:
                    raise ValueError("Página incompleta")
                try:
                    image = validate_image_file(
                        page,
                        mimetypes.guess_type(name)[0] or "",
                        policy=self._policy,
                        job_id=str(job_id),
                    )
                except ImageValidationError as error:
                    raise LocalImportPageError(
                        f"No se pudo validar la página {ordinal}. "
                        "La imagen puede estar incompleta, dañada o superar los límites admitidos. "
                        "Vuelve a copiar o descargar el capítulo completo."
                    ) from error
                images.append(image)
                pages.append(ValidatedPage(ordinal, page, image.size_bytes, image.sha256))
            if self._fingerprint(path) != fingerprint:
                raise ValueError("La copia todavía está cambiando")
            title, label, sort_key = self._identity_for(path, metadata)
            content_id = hashlib.sha256(
                (_title_key(title) + "\n" + "\n".join(image.sha256 for image in images)).encode()
            ).hexdigest()
            reference = "local:v1:" + content_id
            with self._database.read_session() as session:
                existing = session.execute(
                    select(ChapterSourceRow.chapter_id)
                    .join(SourceRow, SourceRow.source_id == ChapterSourceRow.source_id)
                    .where(
                        SourceRow.connector_id == _LOCAL_SOURCE,
                        ChapterSourceRow.remote_id == reference,
                    )
                ).scalar_one_or_none()
                restore_chapter = None
                if existing is not None:
                    chapter = session.get(ChapterRow, existing)
                    assert chapter is not None
                    existing_series = UUID(chapter.series_id)
                    if not self._chapter_available(session, UUID(existing)):
                        restore_chapter = chapter_from_row(chapter)
                else:
                    existing_series = None
            if existing_series is not None and restore_chapter is None:
                with self._database.write_session() as session:
                    association = session.scalar(
                        select(ChapterSourceRow).where(
                            ChapterSourceRow.chapter_id == str(existing),
                            ChapterSourceRow.remote_id == reference,
                        )
                    )
                    assert association is not None
                    # ponytail: remember the latest copy; other copies are deduplicated by content.
                    association.remote_url = input_reference
                return existing_series, False
            with self._database.read_session() as session:
                rows = list(session.scalars(select(SeriesRow)))
                matches = [
                    row
                    for row in rows
                    if _title_key(title)
                    in {_title_key(row.canonical_title), _title_key(row.display_title)}
                ]
                if not matches:
                    base, year = _query_title(title)
                    matches = [
                        row
                        for row in rows
                        if _title_key(_query_title(row.canonical_title)[0]) == _title_key(base)
                        and (year is None or _query_title(row.canonical_title)[1] == year)
                    ]
                if restore_chapter is not None:
                    matches = [
                        row for row in rows if row.series_id == str(restore_chapter.series_id)
                    ]
                if len(matches) > 1:
                    raise ValueError(
                        "Hay varias ediciones con ese nombre; usa un nombre más específico"
                    )
                language = metadata.get("LanguageISO")
                series = (
                    series_from_row(matches[0])
                    if matches
                    else Series.new_local(
                        title,
                        title,
                        author=(metadata.get("Writer", "")[:2_000] or None),
                        description=(metadata.get("Summary", "")[:100_000] or None),
                        language=language[:64] if language and language != "und" else None,
                        genres=tuple(
                            value.strip()[:2_000] for value in metadata["Genre"].split(",")[:64]
                        )
                        if metadata.get("Genre")
                        else None,
                    )
                )
            chapter = restore_chapter or Chapter.new_local(
                series.series_id,
                label,
                label,
                chapter_sort_key=sort_key,
                chapter_major=sort_key[0] if label.startswith("Capítulo ") else None,
                chapter_minor=sort_key[1] if len(sort_key) > 1 else None,
                chapter_type="CHAPTER",
                language=series.language,
            )
            target_dir = self._directory(f"chapters/{series.series_id}", library=True)
            destination = target_dir / f"{chapter.chapter_id}-{job_id}.cbz"
            result = self._builder.build(
                destination=destination,
                metadata=ComicInfoMetadata(
                    title=label,
                    series=series.canonical_title,
                    number=label.removeprefix("Capítulo ")
                    if label.startswith("Capítulo ")
                    else None,
                    writer=series.author,
                    summary=series.description,
                    genres=series.genres,
                    language_iso=series.language,
                ),
                pages=tuple(pages),
                job_id=job_id,
            )
            built = True
            now = datetime.now(UTC)
            asset = Asset.new_local(
                AssetType.CBZ,
                destination.relative_to(self._root).as_posix(),
                result.sha256,
                result.size_bytes,
                verified_at=now,
            )
            with self._database.write_session() as session:
                source = session.scalar(
                    select(SourceRow).where(SourceRow.connector_id == _LOCAL_SOURCE)
                )
                if source is None:
                    source = source_to_row(
                        Source.new_local(
                            _LOCAL_SOURCE,
                            "1",
                            "Archivos locales",
                            False,
                            SourceHealthStatus.DISABLED,
                        )
                    )
                    session.add(source)
                    session.flush()
                if session.get(SeriesRow, str(series.series_id)) is None:
                    session.add(series_to_row(series))
                    session.flush()
                if restore_chapter is None:
                    session.add(chapter_to_row(chapter))
                    session.flush()
                    association = ChapterSource.new_local(
                        chapter.chapter_id,
                        UUID(source.source_id),
                        remote_id=reference,
                        remote_url=input_reference,
                        remote_label=label,
                        available=True,
                        last_seen_at=now,
                    )
                    association_row = chapter_source_to_row(association)
                    session.add(association_row)
                else:
                    association_row = session.scalar(
                        select(ChapterSourceRow).where(
                            ChapterSourceRow.chapter_id == str(chapter.chapter_id),
                            ChapterSourceRow.source_id == source.source_id,
                            ChapterSourceRow.remote_id == reference,
                        )
                    )
                    assert association_row is not None
                    association_row.remote_url = input_reference
                    old_link = session.get(ChapterCbzAssetRow, str(chapter.chapter_id))
                    if old_link is not None:
                        old_asset = session.get(AssetRow, old_link.asset_id)
                        if old_asset is not None and self._reader.asset_available(
                            asset_from_row(old_asset)
                        ):
                            raise ValueError("El capítulo ya se recuperó")
                        session.delete(old_link)
                session.flush()
                # This is a verified local import record, kept out of the visible download queue.
                session.add(
                    download_job_to_row(
                        DownloadJob(
                            job_id,
                            UUID(association_row.chapter_source_id),
                            DownloadJobStatus.COMPLETED,
                            DownloadPriority.NORMAL,
                            now,
                            now,
                        )
                    )
                )
                session.flush()
                for ordinal, image in enumerate(images, 1):
                    session.add(
                        DownloadPageRow(
                            job_id=str(job_id),
                            ordinal=ordinal,
                            remote_reference=f"{reference}:{ordinal}",
                            status="VALIDATED",
                            size_bytes=image.size_bytes,
                            sha256=image.sha256,
                            width=image.width,
                            height=image.height,
                            mime=image.mime,
                            attempts=0,
                        )
                    )
                session.add(asset_to_row(asset))
                session.flush()
                session.add(
                    chapter_cbz_asset_to_row(
                        ChapterCbzAsset.new_local(
                            chapter.chapter_id, asset.asset_id, job_id, len(images), indexed_at=now
                        )
                    )
                )
                session.add(
                    DownloadJobArchiveRow(
                        job_id=str(job_id), archived_at_us=datetime_to_epoch_us(now)
                    )
                )
            committed = True
            logger.info(
                "local_import.completed",
                extra={"series_id": str(series.series_id), "page_count": len(images)},
            )
            return series.series_id, True
        finally:
            if archive is not None:
                archive.close()
            if archive_stream is not None:
                archive_stream.close()
            try:
                if built and destination is not None and not committed:
                    self._identity.assert_current()
                    if _plain(destination) and destination.resolve().is_relative_to(self._root):
                        destination.unlink()
                self._database.data_root_identity.assert_current()
                for file in page_paths:
                    if _plain(file) and file.resolve().is_relative_to(staged):
                        file.unlink()
                staged.rmdir()
            except OSError:
                logger.warning("local_import.cleanup_pending")
            finally:
                if reserved:
                    self._disk.release_bounded_operation(job_id)
