"""Durable online plans and a bounded cache of original, verified image bytes."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from time import time_ns
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from app.services.online_reader import OnlineReaderSessionService

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.connectors.errors import SourceConnectorError
from app.connectors.fmd2 import FMD2_METADATA, Fmd2Connector
from app.connectors.pages import ConnectorPageAvailability, ConnectorPageReference
from app.connectors.protocol import ProgressivePageProvider
from app.connectors.registry import ConnectorRegistryError
from app.persistence.atomic_file_publisher import AtomicFilePublisher
from app.persistence.database import SQLiteDatabase
from app.persistence.disk_budget import DiskBudgetManager
from app.persistence.manifest_store import (
    ManifestStoreError,
    _assert_no_reparse,  # pyright: ignore[reportPrivateUsage]
    _directory_path_state,  # pyright: ignore[reportPrivateUsage]
    _read_manifest_bytes,  # pyright: ignore[reportPrivateUsage]
)
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.reader_session import ReaderSessionPageRow, ReaderSessionRow
from app.persistence.models.source import SourceRow
from app.services.download_transport import DownloadTransport
from app.services.image_validation import (
    ImageValidationError,
    ImageValidationPolicy,
    ValidatedImage,
    validate_image_file,
)
from app.services.reader_asset import (
    ReaderAssetConflict,
    ReaderAssetError,
    ReaderAssetNotFound,
    ReaderAssetService,
    ReaderChapter,
)
from app.services.reader_settings import ReaderSettingsService
from app.services.source_pages import SourcePageService

logger = logging.getLogger("yomira.reader_session")


async def _owned_io[T](operation: Awaitable[T]) -> T:
    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.exception()
        raise


def _uuid(value: UUID) -> str:
    if type(value) is not UUID or value.version != 4:
        raise ValueError("Reader identity must be a UUIDv4")
    return str(value)


@dataclass(frozen=True, slots=True)
class ReaderSessionStatus:
    session_id: UUID
    chapter_id: UUID
    state: str
    page_count: int
    ready_pages: tuple[int, ...]
    error_code: str | None


class CachedReaderPageStream:
    def __init__(self, body: bytes, mime: str, sha256: str, release: Callable[[], None]) -> None:
        self._body = body
        self._release: Callable[[], None] | None = release
        self.media_type = mime
        self.size_bytes = len(body)
        self.etag = f'"{sha256}"'
        self._lock = RLock()

    def iter_chunks(self) -> Iterator[bytes]:
        try:
            for offset in range(0, len(self._body), 65_536):
                yield self._body[offset : offset + 65_536]
        finally:
            self.close()

    def close(self) -> None:
        with self._lock:
            release, self._release = self._release, None
            self._body = b""
            if release is not None:
                release()


class ReaderSessionService:
    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        source_pages: SourcePageService,
        transport: DownloadTransport,
        disk_budget: DiskBudgetManager,
        publisher: AtomicFilePublisher,
        image_policy: ImageValidationPolicy,
    ) -> None:
        if disk_budget.database is not database:
            raise ValueError("Reader cache and budget must share the database")
        self._database = database
        self._root = database.data_root_identity.canonical_path
        self._identity = database.data_root_identity
        self._source_pages = source_pages
        self._transport = transport
        self._disk_budget = disk_budget
        self._publisher = publisher
        self._image_policy = image_policy
        self._settings = ReaderSettingsService(database)
        self._lock = RLock()
        self._producer_lock = asyncio.Lock()
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._active: set[UUID] = set()
        self._pins: dict[UUID, int] = {}
        self._closed = False
        self._restored: dict[UUID, Fmd2Connector] = {}
        self._cleanup_lock = asyncio.Lock()
        self._native_reservations: dict[UUID, UUID] = {}

    def _page_path(self, session_id: UUID, ordinal: int) -> Path:
        _uuid(session_id)
        if type(ordinal) is not int or not 1 <= ordinal <= 10_000:
            raise ValueError("Reader ordinal is outside the plan")
        return self._root / "cache" / "reader" / str(session_id) / f"{ordinal:05d}.page"

    def _guard(
        self,
        path: Path,
        *,
        directory: bool = False,
        missing: bool = False,
    ) -> os.stat_result | None:
        self._identity.assert_current()
        try:
            info = _assert_no_reparse(
                path,
                expected="directory" if directory else "file",
                allow_missing=missing,
                stage="reader_cache",
                identity=self._identity,
            )
        except ManifestStoreError:
            raise ReaderAssetConflict("Reader cache namespace could not be verified.") from None
        if info is not None and not directory and info.st_nlink != 1:
            raise ReaderAssetConflict("Reader cache file ownership is ambiguous.")
        return info

    def _make_directory(self, session_id: UUID) -> None:
        cursor = self._root
        for component in ("cache", "reader", _uuid(session_id)):
            cursor /= component
            _directory_path_state(
                self._identity,
                cursor,
                create=True,
                stage="reader_cache_directory",
                job_id=session_id,
                storage_gate=self._database.storage_gate,
            )

    def _live_row(self, session: Session, chapter_id: UUID) -> ReaderSessionRow | None:
        return session.scalar(
            select(ReaderSessionRow).where(
                ReaderSessionRow.chapter_id == _uuid(chapter_id),
                ReaderSessionRow.state != "evicted",
            )
        )

    def status(self, chapter_id: UUID) -> ReaderSessionStatus:
        with self._lock, self._database.write_session() as session:
            row = self._live_row(session, chapter_id)
            if row is None or row.state == "evicting":
                raise ReaderAssetNotFound("The reader session is unavailable.")
            row.last_access_us = time_ns() // 1000
            pages = tuple(
                session.scalars(
                    select(ReaderSessionPageRow.ordinal)
                    .where(
                        ReaderSessionPageRow.session_id == row.session_id,
                        ReaderSessionPageRow.state == "ready",
                    )
                    .order_by(ReaderSessionPageRow.ordinal)
                ).all()
            )
            return ReaderSessionStatus(
                UUID(row.session_id),
                chapter_id,
                row.state,
                row.page_count,
                pages,
                row.error_code,
            )

    async def open(self, chapter_id: UUID, *, restart: bool = False) -> ReaderSessionStatus:
        _uuid(chapter_id)
        if self._closed:
            raise ReaderAssetError("The reader session service is stopping.")
        with self._database.read_session() as session:
            existing = self._live_row(session, chapter_id)
        if existing is not None and not restart:
            return self.status(chapter_id)
        await self._reconcile_restored()
        if existing is not None:
            with self._database.read_session() as session:
                existing = self._live_row(session, chapter_id)
            if existing is None:
                return await self.open(chapter_id)
            if existing.state not in {"failed", "interrupted"} or not existing.native_released:
                raise ReaderAssetConflict("The reader attempt is still in use or unsettled.")
            with self._lock:
                self._evict(UUID(existing.session_id))
        with self._lock, self._database.write_session() as session:
            row = self._live_row(session, chapter_id)
            if row is None:
                chapter = session.get(ChapterRow, str(chapter_id))
                source = session.execute(
                    select(ChapterSourceRow, SourceRow)
                    .join(
                        SourceRow,
                        SourceRow.source_id == ChapterSourceRow.source_id,
                    )
                    .where(
                        ChapterSourceRow.chapter_id == str(chapter_id),
                        ChapterSourceRow.available.is_(True),
                        SourceRow.enabled.is_(True),
                    )
                    .order_by(ChapterSourceRow.chapter_source_id)
                ).first()
                if chapter is None or source is None:
                    raise ReaderAssetNotFound("No available source for the reader chapter.")
                chapter_source, source_row = source
                connector = self._source_pages.resolve(
                    source_row.connector_id,
                    source_row.connector_version,
                )
                if not isinstance(connector, ProgressivePageProvider):
                    raise ReaderAssetConflict("This source does not support progressive reading.")
                reference = chapter_source.remote_id or chapter_source.remote_url
                if not reference:
                    raise ReaderAssetNotFound("The chapter reference is unavailable.")
                sid = uuid4()
                row = ReaderSessionRow(
                    session_id=str(sid),
                    chapter_id=str(chapter_id),
                    chapter_source_id=chapter_source.chapter_source_id,
                    connector_id=source_row.connector_id,
                    connector_version=source_row.connector_version,
                    remote_reference=reference,
                    generation=None,
                    page_count=0,
                    state="acquiring",
                    error_code=None,
                    last_access_us=time_ns() // 1000,
                    native_released=False,
                )
                session.add(row)
                session.flush()
                title = chapter.display_label
            else:
                sid = UUID(row.session_id)
                title = ""
        if title:
            self._active.add(sid)
            self._tasks[sid] = asyncio.create_task(self._acquire(sid, title))
            self._tasks[sid].add_done_callback(lambda _task: self._tasks.pop(sid, None))
        return self.status(chapter_id)

    def get_chapter(self, chapter_id: UUID) -> ReaderChapter | None:
        with self._lock, self._database.read_session() as session:
            row = self._live_row(session, chapter_id)
            if row is None or row.state in {"evicting", "evicted"} or row.page_count == 0:
                return None
            first = session.scalar(
                select(ReaderSessionPageRow.ordinal)
                .where(
                    ReaderSessionPageRow.session_id == row.session_id,
                    ReaderSessionPageRow.state == "ready",
                )
                .limit(1)
            )
            chapter = session.get(ChapterRow, str(chapter_id))
            if first is None or chapter is None:
                return None
            return ReaderChapter(
                chapter_id, UUID(chapter.series_id), chapter.display_label, row.page_count
            )

    def open_page(self, chapter_id: UUID, page_number: int) -> CachedReaderPageStream:
        if type(page_number) is not int or page_number <= 0:
            raise ReaderAssetNotFound("Reader page is outside the plan.")
        with self._lock:
            if sum(self._pins.values()) >= 2:
                raise ReaderAssetError("Reader page streaming capacity is temporarily occupied.")
            with self._database.write_session() as session:
                row = self._live_row(session, chapter_id)
                if row is None or row.state in {"evicted", "evicting"}:
                    raise ReaderAssetNotFound("Reader page is unavailable.")
                sid = UUID(row.session_id)
                page = session.get(ReaderSessionPageRow, (row.session_id, page_number))
                if page is None or page.state != "ready":
                    raise ReaderAssetNotFound("Reader page is not verified yet.")
                row.last_access_us = time_ns() // 1000
                sha, mime, size = page.sha256, page.mime, page.size_bytes
            path = self._page_path(sid, page_number)
            self._guard(path)
            try:
                body = _read_manifest_bytes(
                    path, max_bytes=self._image_policy.max_bytes, job_id=sid
                )
            except ManifestStoreError, OSError:
                raise ReaderAssetConflict("Reader cache content could not be verified.") from None
            if mime is None or len(body) != size or hashlib.sha256(body).hexdigest() != sha:
                raise ReaderAssetConflict("Reader cache integrity verification failed.")
            self._pins[sid] = self._pins.get(sid, 0) + 1

            def release() -> None:
                with self._lock:
                    self._pins[sid] -= 1
                    if self._pins[sid] == 0:
                        del self._pins[sid]

            assert sha is not None
            return CachedReaderPageStream(body, mime, sha, release)

    def _files(self, sid: UUID) -> list[Path]:
        with self._database.read_session() as session:
            pages = session.scalars(
                select(ReaderSessionPageRow).where(
                    ReaderSessionPageRow.session_id == _uuid(sid),
                )
            ).all()
        files: list[Path] = []
        cache_directory = self._page_path(sid, 1).parent
        cache_info = self._guard(cache_directory, directory=True, missing=True)
        expected = {f"{page.ordinal:05d}.page" for page in pages}
        if cache_info is not None:
            with os.scandir(cache_directory) as entries:
                for entry in entries:
                    if entry.name not in expected:
                        raise ReaderAssetConflict("Unexpected reader cache artifact retained.")
                    self._guard(cache_directory / entry.name)
                    files.append(cache_directory / entry.name)
        temporary = self._root / "temp" / "jobs" / str(sid)
        if self._guard(temporary, directory=True, missing=True) is not None:
            with os.scandir(temporary) as entries:
                if {entry.name for entry in entries} - {"pages"}:
                    raise ReaderAssetConflict("Unexpected reader temporary artifact retained.")
            page_root = temporary / "pages"
            if self._guard(page_root, directory=True, missing=True) is not None:
                by_ordinal = {str(page.ordinal): page for page in pages}
                with os.scandir(page_root) as entries:
                    names = [entry.name for entry in entries]
                for name in names:
                    if name not in by_ordinal:
                        raise ReaderAssetConflict("Unexpected reader page directory retained.")
                    directory = page_root / name
                    self._guard(directory, directory=True)
                    expected_parts = {
                        f"attempt-{i}.part"
                        for i in range(
                            1,
                            by_ordinal[name].attempt + 1,
                        )
                    }
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if entry.name not in expected_parts:
                                raise ReaderAssetConflict(
                                    "Unexpected reader page attempt retained."
                                )
                            self._guard(directory / entry.name)
                            files.append(directory / entry.name)
        return files

    def _cache_bytes(self) -> int:
        with self._database.read_session() as session:
            ids = [UUID(value) for value in session.scalars(select(ReaderSessionRow.session_id))]
        reader_root = self._root / "cache" / "reader"
        if self._guard(reader_root, directory=True, missing=True) is not None:
            with os.scandir(reader_root) as entries:
                if {entry.name for entry in entries} - {str(sid) for sid in ids}:
                    raise ReaderAssetConflict("Unknown reader cache ownership retained.")
        total = 0
        for sid in ids:
            for path in self._files(sid):
                info = self._guard(path)
                assert info is not None
                total += info.st_size
        return total

    def _unlink(self, path: Path) -> None:
        before = self._guard(path, missing=True)
        if before is None:
            return
        with self._database.storage_gate.mutation():
            if self._guard(path) != before:
                raise ReaderAssetConflict("Reader artifact changed before cleanup.")
            path.unlink()

    def _evict(self, sid: UUID) -> None:
        if sid in self._active or self._pins.get(sid, 0):
            raise ReaderAssetConflict("Reader content is in use.")
        files = self._files(sid)
        with self._database.write_session() as session:
            row = session.get(ReaderSessionRow, _uuid(sid))
            assert row is not None
            row.state = "evicting"
        for path in files:
            self._unlink(path)
        with self._database.read_session() as session:
            ordinals = session.scalars(
                select(ReaderSessionPageRow.ordinal).where(
                    ReaderSessionPageRow.session_id == str(sid),
                )
            ).all()
        temporary = self._root / "temp" / "jobs" / str(sid)
        directories = [temporary / "pages" / str(ordinal) for ordinal in ordinals]
        directories += [temporary / "pages", temporary, self._page_path(sid, 1).parent]
        for directory in directories:
            info = self._guard(directory, directory=True, missing=True)
            if info is not None:
                with self._database.storage_gate.mutation():
                    if self._guard(directory, directory=True) != info:
                        raise ReaderAssetConflict("Reader directory changed before cleanup.")
                    directory.rmdir()
        with self._database.write_session() as session:
            session.execute(
                delete(ReaderSessionPageRow).where(
                    ReaderSessionPageRow.session_id == str(sid),
                )
            )
            row = session.get(ReaderSessionRow, str(sid))
            assert row is not None
            row.state = "evicted"
        # Keep the attempt identity for native reconciliation; it is not a library asset.

    def trim(self, *, needed_bytes: int = 0) -> int:
        # shortcut: evict idle chapters; use page windows for chapters larger than the quota.
        if type(needed_bytes) is not int or needed_bytes < 0:
            raise ValueError("Cache capacity must be nonnegative")
        with self._lock:
            limit = self._settings.get().cache_limit_mb * 1024 * 1024
            used = self._cache_bytes()
            with self._database.read_session() as session:
                candidates = [
                    UUID(value)
                    for value in session.scalars(
                        select(ReaderSessionRow.session_id)
                        .where(
                            ReaderSessionRow.state.not_in(("evicted", "acquiring")),
                        )
                        .order_by(ReaderSessionRow.last_access_us, ReaderSessionRow.session_id),
                    )
                ]
            for sid in candidates:
                if used + needed_bytes <= limit:
                    break
                if sid not in self._active and not self._pins.get(sid, 0):
                    self._evict(sid)
                    used = self._cache_bytes()
            if used + needed_bytes > limit:
                raise ReaderAssetError("Reader cache capacity is occupied.")
            return limit - used

    def recover(self) -> None:
        with self._lock:
            if self._active or self._pins:
                raise ReaderAssetConflict("Reader cache recovery requires quiescence.")
            with self._database.read_session() as session:
                rows = session.scalars(select(ReaderSessionRow)).all()
                pending = session.scalars(
                    select(ReaderSessionPageRow).where(
                        ReaderSessionPageRow.state == "publishing",
                    )
                ).all()
            for page in pending:
                sid = UUID(page.session_id)
                path = self._page_path(sid, page.ordinal)
                if self._guard(path, missing=True) is not None:
                    try:
                        image = validate_image_file(
                            path, page.mime or "", policy=self._image_policy
                        )
                    except ImageValidationError:
                        raise ReaderAssetConflict(
                            "Reader publication recovery is invalid."
                        ) from None
                    if (image.sha256, image.size_bytes, image.width, image.height) != (
                        page.sha256,
                        page.size_bytes,
                        page.width,
                        page.height,
                    ):
                        raise ReaderAssetConflict("Reader publication recovery changed content.")
                    with self._database.write_session() as session:
                        recovered = session.get(
                            ReaderSessionPageRow, (page.session_id, page.ordinal)
                        )
                        assert recovered is not None
                        recovered.state = "ready"
            for row in rows:
                sid = UUID(row.session_id)
                if row.state == "evicting":
                    self._evict(sid)
                elif row.state == "acquiring":
                    self._set_state(sid, "interrupted", "READER_INTERRUPTED")
                if not row.native_released and row.connector_id == FMD2_METADATA.connector_id:
                    try:
                        connector = self._source_pages.resolve(
                            row.connector_id,
                            FMD2_METADATA.connector_version,
                        )
                    except (SourceConnectorError, ConnectorRegistryError):
                        logger.warning(
                            "reader.recovery_source_unavailable", extra={"session_id": str(sid)}
                        )
                    else:
                        if isinstance(connector, Fmd2Connector):
                            connector.track_abandoned_acquisition(sid)
                            self._restored[sid] = connector
                for path in self._files(sid):
                    if path.suffix == ".part":
                        self._unlink(path)
            self.trim()

    async def _reconcile_restored(self) -> None:
        async with self._cleanup_lock:
            for sid, connector in tuple(self._restored.items()):
                if not await connector.connection_available():
                    raise ReaderAssetError("Previous reader acquisition cannot yet be reconciled.")
                try:
                    await _owned_io(connector.release_reader_staging(sid, restored=True))
                except SourceConnectorError, ValueError, OSError:
                    raise ReaderAssetError("Previous reader staging is still retained.") from None
                self._native_released(sid)
                reservation = self._native_reservations.pop(sid, None)
                if reservation is not None:
                    self._disk_budget.release_bounded_operation(reservation)
                del self._restored[sid]

    def _native_released(self, sid: UUID) -> None:
        with self._database.write_session() as session:
            row = session.get(ReaderSessionRow, _uuid(sid))
            assert row is not None
            row.native_released = True

    def _set_state(self, sid: UUID, state: str, error: str | None = None) -> None:
        with self._lock, self._database.write_session() as session:
            row = session.get(ReaderSessionRow, _uuid(sid))
            assert row is not None
            row.state, row.error_code = state, error

    def _persist_plan(self, sid: UUID, snapshot: ConnectorPageAvailability) -> None:
        if snapshot.total_pages == 0:
            return
        with self._lock, self._database.write_session() as session:
            row = session.get(ReaderSessionRow, _uuid(sid))
            assert row is not None
            if row.generation is not None and (
                row.generation != str(snapshot.generation) or row.page_count != snapshot.total_pages
            ):
                raise ReaderAssetConflict("Reader page plan changed.")
            row.generation, row.page_count = str(snapshot.generation), snapshot.total_pages
            for ordinal, reference in snapshot.pages:
                existing = session.get(ReaderSessionPageRow, (str(sid), ordinal))
                if existing is not None and existing.remote_reference != reference.remote_reference:
                    raise ReaderAssetConflict("Reader page reference changed.")
                if existing is None:
                    session.add(
                        ReaderSessionPageRow(
                            session_id=str(sid),
                            ordinal=ordinal,
                            remote_reference=reference.remote_reference,
                            attempt=1,
                            state="downloading",
                        )
                    )

    async def _retry_attempt(self, sid: UUID, ordinal: int, previous: int) -> int:
        with self._lock:
            self.trim(
                needed_bytes=min(
                    self._image_policy.max_bytes, self._settings.get().cache_limit_mb * 1024 * 1024
                )
            )
            with self._database.write_session() as session:
                page = session.get(ReaderSessionPageRow, (_uuid(sid), ordinal))
                if page is None or page.state != "downloading" or page.attempt != previous:
                    raise ReaderAssetConflict("Reader retry evidence changed.")
                page.attempt = previous + 1
                return page.attempt

    def _publication_intent(self, sid: UUID, ordinal: int, image: ValidatedImage) -> None:
        with self._lock, self._database.write_session() as session:
            page = session.get(ReaderSessionPageRow, (_uuid(sid), ordinal))
            assert page is not None and page.state == "downloading"
            page.state = "publishing"
            page.sha256, page.size_bytes, page.mime = image.sha256, image.size_bytes, image.mime
            page.width, page.height = image.width, image.height

    async def _acquire(self, sid: UUID, title: str) -> None:
        acquisition: asyncio.Task[tuple[ConnectorPageReference, ...]] | None = None
        native_reservation = uuid4()
        native_reserved = False
        connector = None
        completed = False
        try:
            async with self._producer_lock:
                with self._database.read_session() as session:
                    row = session.get(ReaderSessionRow, str(sid))
                    assert row is not None
                    connector = self._source_pages.resolve(row.connector_id, row.connector_version)
                    remote, connector_id = row.remote_reference, row.connector_id
                self._disk_budget.reserve_bounded_operation(
                    native_reservation,
                    page_bytes=512 * 1024 * 1024,
                )
                native_reserved = True
                self._native_reservations[sid] = native_reservation
                acquisition = asyncio.create_task(
                    self._source_pages.acquire_pages(connector, sid, remote, title)
                )
                previous = None
                while True:
                    if acquisition.done():
                        await acquisition
                    try:
                        snapshot = await self._source_pages.get_ready_pages(
                            connector, sid, previous
                        )
                    except SourceConnectorError as error:
                        if previous is not None or not error.retryable or acquisition.done():
                            raise
                        await asyncio.sleep(1)
                        continue
                    self._persist_plan(sid, snapshot)
                    previous = snapshot
                    for ordinal, reference in snapshot.pages:
                        with self._database.read_session() as session:
                            page = session.get(ReaderSessionPageRow, (str(sid), ordinal))
                            assert page is not None
                            if page.state == "ready":
                                continue
                            attempt = page.attempt
                        with self._lock:
                            limit = self._settings.get().cache_limit_mb * 1024 * 1024
                            room = self.trim(needed_bytes=min(limit, self._image_policy.max_bytes))
                            self._disk_budget.reserve_bounded_operation(sid, page_bytes=room)
                        try:
                            downloaded = await self._transport.download(
                                connector.resolve_page(reference),
                                job_id=sid,
                                ordinal=ordinal,
                                attempt=attempt,
                                reserve_next_attempt=self._retry_attempt,
                                allowed_origins=self._source_pages.download_origins(connector_id),
                            )
                            image = await _owned_io(
                                asyncio.to_thread(
                                    validate_image_file,
                                    downloaded.path,
                                    downloaded.content_type,
                                    policy=self._image_policy,
                                )
                            )
                            if (
                                image.sha256 != downloaded.sha256
                                or image.size_bytes != downloaded.size_bytes
                            ):
                                raise ReaderAssetConflict("Reader page changed after transport.")
                            self._publication_intent(sid, ordinal, image)
                            self._make_directory(sid)
                            await _owned_io(
                                asyncio.to_thread(
                                    self._publisher.publish_no_replace,
                                    downloaded.path,
                                    self._page_path(sid, ordinal),
                                    job_id=sid,
                                )
                            )
                            with self._database.write_session() as session:
                                page = session.get(ReaderSessionPageRow, (str(sid), ordinal))
                                assert page is not None
                                page.state = "ready"
                        finally:
                            self._disk_budget.release_bounded_operation(sid)
                    if snapshot.is_complete:
                        await acquisition
                        self._set_state(sid, "complete")
                        completed = True
                        break
                    await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            self._set_state(sid, "interrupted", "READER_INTERRUPTED")
            raise
        except Exception as error:
            self._set_state(sid, "failed", "READER_ACQUISITION_FAILED")
            logger.warning(
                "reader.session_failed",
                extra={
                    "session_id": str(sid),
                    "cause_type": type(error).__name__,
                    "error_code": "READER_ACQUISITION_FAILED",
                },
            )
        finally:
            if acquisition is not None and not acquisition.done():
                acquisition.cancel()
            if acquisition is not None:
                await asyncio.gather(acquisition, return_exceptions=True)
            settled = completed or connector is None or not isinstance(connector, Fmd2Connector)
            if isinstance(connector, Fmd2Connector) and not settled:
                settled = await connector.connection_available()
            if settled:
                try:
                    if isinstance(connector, Fmd2Connector):
                        await _owned_io(connector.release_reader_staging(sid, restored=True))
                    self._native_released(sid)
                except Exception as error:
                    settled = False
                    logger.warning(
                        "reader.native_cleanup_deferred",
                        extra={
                            "session_id": str(sid),
                            "cause_type": type(error).__name__,
                        },
                    )
            if native_reserved and settled:
                self._disk_budget.release_bounded_operation(native_reservation)
                self._native_reservations.pop(sid, None)
            elif isinstance(connector, Fmd2Connector):
                self._restored[sid] = connector
            with self._lock:
                self._active.discard(sid)
                try:
                    for path in self._files(sid):
                        if path.suffix == ".part":
                            self._unlink(path)
                except Exception as error:
                    logger.warning(
                        "reader.cache_cleanup_deferred",
                        extra={"session_id": str(sid), "cause_type": type(error).__name__},
                    )

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def retire_cache(self) -> None:
        """Remove only this prototype's owned image cache, preserving canonical progress."""
        if self._active or self._pins:
            raise ReaderAssetConflict("Reader cache retirement requires quiescence.")
        await self._reconcile_restored()
        with self._database.read_session() as session:
            ids = session.scalars(
                select(ReaderSessionRow.session_id).where(ReaderSessionRow.state != "evicted")
            ).all()
        for sid in ids:
            self._evict(UUID(sid))

    async def pause(self, chapter_id: UUID) -> ReaderSessionStatus:
        current = self.status(chapter_id)
        task = self._tasks.get(current.session_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.status(chapter_id)


@dataclass(frozen=True, slots=True)
class HybridReaderChapterLookup:
    local: ReaderAssetService
    online: ReaderSessionService | OnlineReaderSessionService

    def get_chapter(self, chapter_id: UUID) -> ReaderChapter | None:
        return self.local.get_chapter(chapter_id) or self.online.get_chapter(chapter_id)

    def page_available(self, chapter_id: UUID, page_index: int) -> bool:
        if self.local.get_chapter(chapter_id) is not None:
            return True
        return page_index + 1 in self.online.status(chapter_id).ready_pages
