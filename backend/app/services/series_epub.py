"""Manual, serialized EPUB preparation with one current book per library series."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

from sqlalchemy import func, select

from app.domain.series import Series
from app.persistence.database import SQLiteDatabase
from app.persistence.managed_directories import ensure_managed_directory
from app.persistence.models.asset import AssetRow
from app.persistence.models.chapter_cbz_asset import ChapterCbzAssetRow
from app.persistence.models.download_page import DownloadPageRow
from app.persistence.windows_paths import DataRootIdentity
from app.services.epub_builder import (
    EPUB_FORMAT_VERSION,
    MAX_EPUB_BYTES,
    MAX_EPUB_PAGES,
    EpubBuilder,
    EpubChapter,
)
from app.services.library import LibraryService
from app.services.reader_asset import ReaderAssetService
from app.tracing import create_trace_id

logger = logging.getLogger("yomira.series_epub")
_OPF = "{http://www.idpf.org/2007/opf}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    series: Series
    chapters: tuple[EpubChapter, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _Preparation:
    completed_pages: int = 0
    building: bool = True
    error: str | None = None
    chapter_count: int = 0
    page_count: int = 0


class SeriesEpubService:
    def __init__(
        self,
        *,
        database: SQLiteDatabase,
        reader: ReaderAssetService,
        library: LibraryService,
        library_root_identity: DataRootIdentity,
        builder: EpubBuilder,
    ) -> None:
        self._database = database
        self._reader = reader
        self._library = library
        self._identity = library_root_identity
        self._builder = builder
        self._states: dict[UUID, _Preparation] = {}
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._queue = asyncio.Lock()
        self._closing = threading.Event()

    def _snapshot(self, series_id: UUID) -> _Snapshot:
        series = self._library.get_series(series_id)
        if series is None:
            raise LookupError("No se encontró este manga en la biblioteca.")
        ids: list[UUID] = []
        while True:
            page = self._reader.list_chapters_for_series(series_id, limit=500, offset=len(ids))
            ids.extend(chapter.chapter_id for chapter in page.items)
            if len(ids) > 10_000:
                raise ValueError("El manga supera el límite de capítulos de un EPUB.")
            if len(ids) >= page.total or not page.items:
                break
        records = self._reader.get_ready_chapter_records_by_ids(tuple(ids)) if ids else ()
        sources: dict[str, tuple[str, int]] = {}
        if ids:
            with self._database.read_session() as session:
                rows = session.execute(
                    select(
                        ChapterCbzAssetRow.chapter_id,
                        AssetRow.sha256,
                        func.sum(DownloadPageRow.size_bytes),
                    )
                    .join(AssetRow, AssetRow.asset_id == ChapterCbzAssetRow.asset_id)
                    .join(
                        DownloadPageRow,
                        DownloadPageRow.job_id == ChapterCbzAssetRow.download_job_id,
                    )
                    .where(
                        ChapterCbzAssetRow.chapter_id.in_([str(chapter_id) for chapter_id in ids])
                    )
                    .group_by(ChapterCbzAssetRow.chapter_id, AssetRow.sha256)
                ).all()
                for chapter_id, sha, size in rows:
                    if size is None:
                        raise ValueError("Faltan páginas de un capítulo descargado.")
                    sources[chapter_id] = (sha, size)
        if any(str(record.chapter_id) not in sources for record in records):
            raise ValueError("Los capítulos cambiaron. Actualiza la ficha y vuelve a intentarlo.")
        chapters = tuple(
            EpubChapter(
                record.chapter_id,
                record.display_label,
                record.page_count,
                sources[str(record.chapter_id)][0],
                sources[str(record.chapter_id)][1],
            )
            for record in records
        )
        if sum(chapter.page_count for chapter in chapters) > MAX_EPUB_PAGES:
            raise ValueError("El manga supera el límite de páginas de un EPUB.")
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "format": EPUB_FORMAT_VERSION,
                    "series": str(series_id),
                    "title": series.display_title,
                    "author": series.author,
                    "language": series.language,
                    "chapters": [
                        (
                            str(chapter.chapter_id),
                            chapter.label,
                            chapter.page_count,
                            chapter.asset_sha256,
                        )
                        for chapter in chapters
                    ],
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return _Snapshot(series, chapters, fingerprint)

    def _assert_path(self, path: Path, *, file: bool = False) -> os.stat_result:
        self._identity.assert_current()
        relative = path.relative_to(self._identity.canonical_path)
        if not relative.parts or relative.parts[0] != "EPUB":
            raise ValueError("EPUB path is outside its managed folder")
        current = self._identity.canonical_path
        info = current.lstat()
        root_device = info.st_dev
        for index, part in enumerate(relative.parts):
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE:
                raise ValueError("EPUB path crosses a link")
            if info.st_dev != root_device or (
                file and index == len(relative.parts) - 1 and info.st_nlink != 1
            ):
                raise ValueError("EPUB path crosses a volume or hardlink")
            expected_file = file and index == len(relative.parts) - 1
            if not (stat.S_ISREG(info.st_mode) if expected_file else stat.S_ISDIR(info.st_mode)):
                raise ValueError("EPUB path type is invalid")
        return info

    def _open(self, path: Path) -> BinaryIO:
        expected = self._assert_path(path, file=True)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            actual = os.fstat(descriptor)
            self._identity.assert_current()
            if (actual.st_dev, actual.st_ino, actual.st_size, actual.st_mtime_ns) != (
                expected.st_dev,
                expected.st_ino,
                expected.st_size,
                expected.st_mtime_ns,
            ):
                raise ValueError("EPUB changed while opening")
            stream = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        return stream

    def _file(self, series_id: UUID) -> Path | None:
        folder = self._identity.canonical_path / "EPUB" / str(series_id)
        try:
            self._assert_path(folder)
        except FileNotFoundError:
            return None
        files = list(folder.glob("*.epub"))
        if len(files) > 1:
            raise ValueError("Hay más de un EPUB en la carpeta de este manga.")
        if files:
            self._assert_path(files[0], file=True)
        return files[0] if files else None

    def _inspect(self, series_id: UUID) -> tuple[Path, str, int, int] | None:
        path = self._file(series_id)
        if path is None:
            return None
        info = self._assert_path(path, file=True)
        if not 0 < info.st_size <= MAX_EPUB_BYTES:
            raise ValueError("El tamaño del EPUB no es válido.")
        with self._open(path) as stream, zipfile.ZipFile(stream) as archive:
            if len(archive.infolist()) > MAX_EPUB_PAGES * 2 + 20:
                raise ValueError("EPUB con demasiados recursos")
            member = archive.getinfo("EPUB/package.opf")
            if member.file_size > 64 * 1024 * 1024:
                raise ValueError("EPUB metadata exceeds its bounded size")
            payload = archive.read(member)
            if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
                raise ValueError("EPUB metadata must not contain entities")
            package = ET.fromstring(payload)
            metadata = package.find(f"{_OPF}metadata")
            if metadata is None or metadata.findtext(f"{_DC}identifier") != f"urn:uuid:{series_id}":
                raise ValueError("El EPUB no pertenece a este manga.")
            values = {
                element.get("property"): element.text for element in metadata.findall(f"{_OPF}meta")
            }
            fingerprint = values.get("yomira:fingerprint") or ""
            chapter_count = int(values.get("yomira:chapter-count") or "0")
            page_count = int(values.get("yomira:page-count") or "0")
            if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None or not (
                1 <= chapter_count <= 10_000 and 1 <= page_count <= MAX_EPUB_PAGES
            ):
                raise ValueError("Los datos del EPUB no son válidos.")
        return path, fingerprint, chapter_count, page_count

    def status(self, series_id: UUID) -> dict[str, object]:
        snapshot = self._snapshot(series_id)
        state = self._states.get(series_id)
        error = state.error if state else None
        try:
            book = self._inspect(series_id)
        except OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError:
            book = None
            error = error or "El EPUB no se pudo comprobar. Puedes volver a crearlo."
        status = (
            "building"
            if state and state.building
            else "failed"
            if error
            else (
                "not_created"
                if book is None
                else "ready"
                if book[1] == snapshot.fingerprint
                else "outdated"
            )
        )
        chapter_count = state.chapter_count if state and state.building else len(snapshot.chapters)
        page_count = (
            state.page_count
            if state and state.building
            else sum(chapter.page_count for chapter in snapshot.chapters)
        )
        return {
            "status": status,
            "filename": book[0].name if book else None,
            "chapter_count": chapter_count,
            "page_count": page_count,
            "completed_pages": min(page_count, state.completed_pages) if state else 0,
            "size_bytes": book[0].stat().st_size if book else None,
            "error": error,
            "has_file": book is not None,
        }

    async def create(self, series_id: UUID) -> dict[str, object]:
        if self._closing.is_set():
            raise RuntimeError("La aplicación se está cerrando.")
        if series_id in self._tasks:
            return await asyncio.to_thread(self.status, series_id)
        snapshot = await asyncio.to_thread(self._snapshot, series_id)
        if self._closing.is_set():
            raise RuntimeError("La aplicación se está cerrando.")
        if not snapshot.chapters:
            raise ValueError("Descarga al menos un capítulo antes de crear el EPUB.")
        if series_id not in self._tasks:
            self._states[series_id] = _Preparation(
                chapter_count=len(snapshot.chapters),
                page_count=sum(chapter.page_count for chapter in snapshot.chapters),
            )
            self._tasks[series_id] = asyncio.create_task(
                self._run(snapshot), name=f"epub-{series_id}"
            )
        return await asyncio.to_thread(self.status, series_id)

    def _build(self, snapshot: _Snapshot, progress: Callable[[int, int], None]) -> None:
        series_id = snapshot.series.series_id
        directory = ensure_managed_directory(
            f"EPUB/{series_id}",
            data_root_identity=self._identity,
            storage_gate=self._database.storage_gate,
            component_name="series_epub",
            job_id=uuid4(),
            trace_id=create_trace_id(),
        )
        for abandoned in directory.glob(".*.epub.tmp"):
            if re.fullmatch(r"\.[0-9a-f-]{36}\.epub\.tmp", abandoned.name):
                self._assert_path(abandoned, file=True)
                with self._database.storage_gate.mutation():
                    abandoned.unlink()
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", snapshot.series.display_title).strip(" .")[
            :72
        ]
        filename = filename.encode("utf-8")[:240].decode("utf-8", "ignore").strip(" .")
        if not filename or filename.split(".")[0].upper() in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }:
            filename = "Manga"
        existing = self._inspect(series_id)
        destination = existing[0] if existing else directory / f"{filename}.epub"
        self._builder.build(
            series_id=series_id,
            title=snapshot.series.display_title,
            author=snapshot.series.author,
            language=snapshot.series.language,
            chapters=snapshot.chapters,
            fingerprint=snapshot.fingerprint,
            temporary_path=directory / f".{uuid4()}.epub.tmp",
            destination_path=destination,
            on_progress=progress,
            cancelled=self._closing.is_set,
        )

    async def _run(self, snapshot: _Snapshot) -> None:
        series_id = snapshot.series.series_id
        loop = asyncio.get_running_loop()

        def progress(done: int, _total: int) -> None:
            def update() -> None:
                state = self._states.get(series_id)
                if state and state.building:
                    self._states[series_id] = replace(state, completed_pages=done)

            loop.call_soon_threadsafe(update)

        try:
            async with self._queue:
                if self._closing.is_set():
                    return
                await asyncio.to_thread(self._build, snapshot, progress)
            self._states[series_id] = _Preparation(
                completed_pages=sum(chapter.page_count for chapter in snapshot.chapters),
                building=False,
            )
        except Exception:
            logger.exception("epub.preparation_failed", extra={"series_id": str(series_id)})
            self._states[series_id] = _Preparation(
                building=False,
                error=(
                    "No se pudo preparar el EPUB. Comprueba el espacio disponible "
                    "y los capítulos descargados; "
                    "puedes reintentar. El libro anterior se conserva."
                ),
            )
        finally:
            self._tasks.pop(series_id, None)

    def open_download(self, series_id: UUID) -> tuple[BinaryIO, str, int]:
        if self._library.get_series(series_id) is None:
            raise LookupError("No se encontró este manga.")
        book = self._inspect(series_id)
        if book is None:
            raise LookupError("Crea primero el EPUB de este manga.")
        stream = self._open(book[0])
        return stream, book[0].name, os.fstat(stream.fileno()).st_size

    async def close(self) -> None:
        self._closing.set()
        await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)
