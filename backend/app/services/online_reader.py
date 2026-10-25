"""On-demand online pages with validated memory and disposable disk caches."""

import asyncio
import hashlib
import logging
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from threading import RLock
from uuid import UUID, uuid4

from sqlalchemy import select

from app.connectors.mangadex import MangaDexConnector
from app.connectors.pages import ConnectorPageReference
from app.connectors.protocol import OnlinePageProvider
from app.connectors.registry import RegisteredSourceConnector
from app.fmd2_bridge import FMD2_METADATA, fmd2_chapter_location
from app.persistence.database import SQLiteDatabase
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.source import SourceRow
from app.services.download_transport import DownloadTransport
from app.services.image_validation import (
    ImageValidationPolicy,
    ValidatedImage,
    validate_image_bytes,
)
from app.services.online_image_cache import OnlineImageCache
from app.services.reader_asset import (
    ReaderAssetConflict,
    ReaderAssetError,
    ReaderAssetNotFound,
    ReaderChapter,
)
from app.services.reader_session import (
    CachedReaderPageStream,
    ReaderSessionStatus,
    _owned_io,  # pyright: ignore[reportPrivateUsage]
)
from app.services.source_pages import SourcePageService

ONLINE_IMAGE_CACHE_BYTES = 128 * 1024 * 1024
ONLINE_CHAPTER_LIMIT = 16


@dataclass(slots=True)
class _OnlineChapter:
    session_id: UUID
    chapter: ReaderChapter
    connector: RegisteredSourceConnector
    cache_namespace: str
    reference: str = ""
    pages: tuple[ConnectorPageReference, ...] = ()
    seen: set[int] = field(default_factory=lambda: set[int]())
    state: str = "acquiring"
    error_code: str | None = None
    released: bool = True
    cache: OrderedDict[int, tuple[bytes, ValidatedImage]] = field(
        default_factory=lambda: OrderedDict[int, tuple[bytes, ValidatedImage]]()
    )
    cached_bytes: int = 0


class OnlineReaderSessionService:
    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        source_pages: SourcePageService,
        transport: DownloadTransport,
        image_policy: ImageValidationPolicy,
        disk_cache: OnlineImageCache | None = None,
    ) -> None:
        self._database = database
        self._sources = source_pages
        self._transport = transport
        self._image_policy = image_policy
        self._disk_cache = disk_cache
        self._current: _OnlineChapter | None = None
        self._chapters: OrderedDict[UUID, _OnlineChapter] = OrderedDict()
        self._operations = asyncio.Lock()
        self._direct_slots = asyncio.Semaphore(2)
        self._direct_tasks: dict[tuple[UUID, int], asyncio.Task[tuple[bytes, ValidatedImage]]] = {}
        self._pins = 0
        self._pins_lock = RLock()
        self._closed = False

    def status(self, chapter_id: UUID) -> ReaderSessionStatus:
        current = self._chapters.get(chapter_id)
        if current is None or current.chapter.chapter_id != chapter_id:
            raise ReaderAssetNotFound("The online chapter is not open.")
        return ReaderSessionStatus(
            current.session_id,
            chapter_id,
            current.state,
            len(current.pages),
            tuple(sorted(current.seen)),
            current.error_code,
        )

    def get_chapter(self, chapter_id: UUID) -> ReaderChapter | None:
        current = self._chapters.get(chapter_id)
        if (
            current is None
            or current.chapter.chapter_id != chapter_id
            or not current.pages
            or current.state != "complete"
        ):
            return None
        return current.chapter

    async def _release(self) -> None:
        current = self._current
        if current is not None and not current.released:
            assert isinstance(current.connector, OnlinePageProvider)
            try:
                await _owned_io(current.connector.close_online_pages(current.session_id))
            except Exception:
                current.state = "interrupted"
                current.error_code = "READER_RELEASE_PENDING"
                raise ReaderAssetError("The source reader could not be closed safely.") from None
            current.released = True

    async def open(
        self, chapter_id: UUID, *, restart: bool = False, prepare: bool = False
    ) -> ReaderSessionStatus:
        if type(chapter_id) is not UUID or chapter_id.version != 4:
            raise ValueError("chapter_id must be a UUIDv4")
        if prepare and restart:
            raise ReaderAssetConflict("Preparation cannot renew an active chapter.")
        existing = self._chapters.get(chapter_id)
        if (
            prepare
            and existing is not None
            and not isinstance(existing.connector, MangaDexConnector)
        ):
            raise ReaderAssetConflict("This source cannot be prepared in the background.")
        if (
            not self._closed
            and not restart
            and existing is not None
            and existing.chapter.chapter_id == chapter_id
            and existing.state == "complete"
        ):
            self._chapters.move_to_end(chapter_id)
            return self.status(chapter_id)
        async with self._operations:
            if self._closed:
                raise ReaderAssetError("The online reader is stopping.")
            existing = self._chapters.get(chapter_id)
            if (
                prepare
                and existing is not None
                and not isinstance(existing.connector, MangaDexConnector)
            ):
                raise ReaderAssetConflict("This source cannot be prepared in the background.")
            if (
                existing is not None
                and existing.chapter.chapter_id == chapter_id
                and existing.state == "complete"
                and not restart
            ):
                self._chapters.move_to_end(chapter_id)
                return self.status(chapter_id)
            with self._database.read_session() as session:
                chapter = session.get(ChapterRow, str(chapter_id))
                source = session.execute(
                    select(ChapterSourceRow, SourceRow)
                    .join(SourceRow, SourceRow.source_id == ChapterSourceRow.source_id)
                    .where(
                        ChapterSourceRow.chapter_id == str(chapter_id),
                        ChapterSourceRow.available.is_(True),
                        SourceRow.enabled.is_(True),
                    )
                    .order_by(ChapterSourceRow.chapter_source_id)
                ).first()
                if chapter is None or source is None:
                    raise ReaderAssetNotFound("No available source for this chapter.")
                chapter_source, source_row = source
                try:
                    connector = self._sources.resolve(
                        source_row.connector_id, source_row.connector_version
                    )
                except Exception:
                    raise ReaderAssetError("The chapter source is unavailable.") from None
                if prepare and not isinstance(connector, MangaDexConnector):
                    raise ReaderAssetConflict("This source cannot be prepared in the background.")
                if not isinstance(connector, OnlinePageProvider):
                    raise ReaderAssetConflict("This source does not yet support disk-free reading.")
                reference = chapter_source.remote_id or chapter_source.remote_url
                if not reference:
                    raise ReaderAssetNotFound("The chapter reference is unavailable.")
                chapter_language = chapter.language
                current = _OnlineChapter(
                    uuid4(),
                    ReaderChapter(chapter_id, UUID(chapter.series_id), chapter.display_label, 0),
                    connector,
                    hashlib.sha256(
                        f"online-images-v1\0{chapter_id}\0{source_row.connector_id}\0"
                        f"{source_row.connector_version}\0{reference}".encode()
                    ).hexdigest(),
                )
            if connector.metadata.connector_id == FMD2_METADATA.connector_id:
                manga_ref, website, chapter_url = fmd2_chapter_location(reference)
                if website == "424747b0135c4240a902eb9cfcb11db9" and chapter_url.startswith(
                    "/go/rds/"
                ):
                    catalog = await connector.get_chapters(manga_ref)
                    matches = [
                        item
                        for item in catalog
                        if item.display_label == current.chapter.display_label
                        and item.language == chapter_language
                        and item.remote_id is not None
                    ]
                    if len(matches) != 1:
                        raise ReaderAssetNotFound(
                            "The saved chapter has no unique current source reference."
                        )
                    reference = matches[0].remote_id
                    assert reference is not None
            current.reference = reference
            self._chapters[chapter_id] = current
            self._chapters.move_to_end(chapter_id)
            while len(self._chapters) > ONLINE_CHAPTER_LIMIT:
                self._chapters.popitem(last=False)
            try:
                if restart and isinstance(current.connector, MangaDexConnector):
                    await _owned_io(current.connector.invalidate_page_plan(reference))
                if restart and self._disk_cache is not None:
                    await _owned_io(
                        asyncio.to_thread(self._disk_cache.invalidate, current.cache_namespace)
                    )
                await self._activate(current)
                current.state = "complete"
            except asyncio.CancelledError:
                current.state = "interrupted"
                current.error_code = "READER_INTERRUPTED"
                if self._current is current:
                    await self._release()
                raise
            except Exception as error:
                logging.getLogger("yomira.reader.online").warning(
                    "reader.online.open_failed",
                    extra={
                        "cause_type": type(error).__name__,
                        "error_code": getattr(error, "code", None),
                        "operation": getattr(error, "operation", None),
                    },
                )
                current.state = "failed"
                current.error_code = "READER_SOURCE_FAILED"
                if self._current is current:
                    await self._release()
                raise ReaderAssetError("The source could not open this chapter online.") from None
            return self.status(chapter_id)

    async def _activate(self, current: _OnlineChapter) -> None:
        direct = isinstance(current.connector, MangaDexConnector)
        if direct and current.pages:
            return
        if not direct:
            if self._current is current and not current.released:
                return
            await self._release()
            self._current = current
            current.released = False
        try:
            pages = await _owned_io(
                self._sources.open_online_pages(
                    current.connector, current.session_id, current.reference
                )
            )
            if current.pages and len(pages) != len(current.pages):
                raise ReaderAssetConflict("The source changed the chapter page count.")
            current.pages = pages
            current.chapter = ReaderChapter(
                current.chapter.chapter_id,
                current.chapter.series_id,
                current.chapter.display_label,
                len(pages),
            )
        except BaseException:
            current.state = "interrupted"
            current.error_code = "READER_SOURCE_FAILED"
            if self._current is current:
                await self._release()
            raise

    def _reserve_stream(self) -> Callable[[], None]:
        with self._pins_lock:
            if self._pins >= 2:
                raise ReaderAssetError("Reader streaming capacity is temporarily occupied.")
            self._pins += 1

        def release() -> None:
            with self._pins_lock:
                self._pins -= 1

        return release

    def _cached_stream(self, chapter_id: UUID, page_number: int) -> CachedReaderPageStream | None:
        current = self._chapters.get(chapter_id)
        if (
            self._closed
            or current is None
            or current.chapter.chapter_id != chapter_id
            or current.state != "complete"
            or type(page_number) is not int
        ):
            return None
        cached = current.cache.get(page_number)
        if cached is None:
            return None
        release = self._reserve_stream()
        current.cache.move_to_end(page_number)
        self._chapters.move_to_end(chapter_id)
        body, image = cached
        return CachedReaderPageStream(body, image.mime, image.sha256, release)

    def _available_chapter(self, chapter_id: UUID, page_number: int) -> _OnlineChapter:
        current = self._chapters.get(chapter_id)
        if self._closed or current is None or current.state != "complete":
            raise ReaderAssetNotFound("The online chapter is unavailable.")
        if type(page_number) is not int or not 1 <= page_number <= len(current.pages):
            raise ReaderAssetNotFound("Reader page is outside the plan.")
        self._chapters.move_to_end(chapter_id)
        return current

    async def _read_page(
        self, current: _OnlineChapter, page_number: int
    ) -> tuple[bytes, ValidatedImage, bool]:
        cached = current.cache.get(page_number)
        if cached is not None:
            return cached[0], cached[1], False
        disk = (
            None
            if self._disk_cache is None
            else await _owned_io(
                asyncio.to_thread(
                    self._disk_cache.get, current.cache_namespace, page_number, len(current.pages)
                )
            )
        )
        if disk is not None:
            try:
                image = await _owned_io(
                    asyncio.to_thread(
                        validate_image_bytes, disk[0], disk[1], policy=self._image_policy
                    )
                )
                return disk[0], image, False
            except ValueError:
                pass
        await self._activate(current)
        result = await self._transport.fetch_memory(
            current.connector.resolve_page(current.pages[page_number - 1]),
            session_id=current.session_id,
            ordinal=page_number,
            allowed_origins=self._sources.download_origins(current.connector.metadata.connector_id),
        )
        image = await _owned_io(
            asyncio.to_thread(
                validate_image_bytes, result.body, result.content_type, policy=self._image_policy
            )
        )
        return result.body, image, True

    async def _remember_page(
        self,
        current: _OnlineChapter,
        page_number: int,
        body: bytes,
        image: ValidatedImage,
        fresh: bool,
    ) -> None:
        if fresh and self._disk_cache is not None:
            await _owned_io(
                asyncio.to_thread(
                    self._disk_cache.put,
                    current.cache_namespace,
                    page_number,
                    len(current.pages),
                    body,
                    image.mime,
                )
            )
        previous = current.cache.pop(page_number, None)
        if previous is not None:
            current.cached_bytes -= len(previous[0])
        if len(body) <= ONLINE_IMAGE_CACHE_BYTES:
            current.cache[page_number] = (body, image)
            current.cached_bytes += len(body)
            while (
                sum(item.cached_bytes for item in self._chapters.values())
                > ONLINE_IMAGE_CACHE_BYTES
            ):
                victim = next(item for item in self._chapters.values() if item.cache)
                _page, (evicted, _image) = victim.cache.popitem(last=False)
                victim.cached_bytes -= len(evicted)
        current.seen.add(page_number)

    async def _direct_page(
        self, current: _OnlineChapter, page_number: int
    ) -> tuple[bytes, ValidatedImage]:
        async with self._direct_slots:
            if self._available_chapter(current.chapter.chapter_id, page_number) is not current:
                raise ReaderAssetConflict("The reader session changed before loading this page.")
            try:
                body, image, fresh = await self._read_page(current, page_number)
                async with self._operations:
                    if (
                        self._available_chapter(current.chapter.chapter_id, page_number)
                        is not current
                    ):
                        raise ReaderAssetConflict(
                            "The reader session changed while loading this page."
                        )
                    await self._remember_page(current, page_number, body, image, fresh)
                return body, image
            except ReaderAssetError, asyncio.CancelledError:
                raise
            except Exception:
                raise ReaderAssetError("The page could not be retrieved and verified.") from None

    async def open_page(self, chapter_id: UUID, page_number: int) -> CachedReaderPageStream:
        cached = self._cached_stream(chapter_id, page_number)
        if cached is not None:
            return cached
        current = self._available_chapter(chapter_id, page_number)
        if isinstance(current.connector, MangaDexConnector):
            key = (current.session_id, page_number)
            task = self._direct_tasks.get(key)
            if task is None:
                if len(self._direct_tasks) >= ONLINE_CHAPTER_LIMIT:
                    raise ReaderAssetError("Reader image requests are temporarily occupied.")
                task = asyncio.create_task(self._direct_page(current, page_number))
                self._direct_tasks[key] = task

                def finished(done: asyncio.Task[tuple[bytes, ValidatedImage]]) -> None:
                    self._direct_tasks.pop(key, None)
                    with suppress(Exception, asyncio.CancelledError):
                        done.exception()

                task.add_done_callback(finished)
            body, image = await asyncio.shield(task)
            release = self._reserve_stream()
            return CachedReaderPageStream(body, image.mime, image.sha256, release)
        async with self._operations:
            current = self._available_chapter(chapter_id, page_number)
            release = self._reserve_stream()
            try:
                body, image, fresh = await self._read_page(current, page_number)
                await self._remember_page(current, page_number, body, image, fresh)
                return CachedReaderPageStream(body, image.mime, image.sha256, release)
            except ReaderAssetError, asyncio.CancelledError:
                release()
                raise
            except Exception:
                release()
                raise ReaderAssetError("The page could not be retrieved and verified.") from None

    async def pause(self, chapter_id: UUID) -> ReaderSessionStatus:
        async with self._operations:
            self.status(chapter_id)
            current = self._chapters[chapter_id]
            if self._current is current:
                await self._release()
            current.state = "interrupted"
            current.seen.clear()
            current.cache.clear()
            current.cached_bytes = 0
            return self.status(chapter_id)

    async def close(self) -> None:
        self._closed = True
        await asyncio.gather(*list(self._direct_tasks.values()), return_exceptions=True)
        async with self._operations:
            await self._release()
            self._current = None
            self._chapters.clear()
