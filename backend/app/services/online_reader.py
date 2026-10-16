"""On-demand online pages: no image files, disk quota, or download job."""

import asyncio
from dataclasses import dataclass, field
from threading import RLock
from uuid import UUID, uuid4

from sqlalchemy import select

from app.connectors.pages import ConnectorPageReference
from app.connectors.protocol import OnlinePageProvider
from app.connectors.registry import RegisteredSourceConnector
from app.persistence.database import SQLiteDatabase
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.source import SourceRow
from app.services.download_transport import DownloadTransport
from app.services.image_validation import ImageValidationPolicy, validate_image_bytes
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


@dataclass(slots=True)
class _OnlineChapter:
    session_id: UUID
    chapter: ReaderChapter
    connector: RegisteredSourceConnector
    pages: tuple[ConnectorPageReference, ...] = ()
    seen: set[int] = field(default_factory=lambda: set[int]())
    state: str = "acquiring"
    error_code: str | None = None
    released: bool = False


class OnlineReaderSessionService:
    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        source_pages: SourcePageService,
        transport: DownloadTransport,
        image_policy: ImageValidationPolicy,
    ) -> None:
        self._database = database
        self._sources = source_pages
        self._transport = transport
        self._image_policy = image_policy
        self._current: _OnlineChapter | None = None
        self._operations = asyncio.Lock()
        self._pins = 0
        self._pins_lock = RLock()
        self._closed = False

    def status(self, chapter_id: UUID) -> ReaderSessionStatus:
        current = self._current
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
        current = self._current
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

    async def open(self, chapter_id: UUID, *, restart: bool = False) -> ReaderSessionStatus:
        if type(chapter_id) is not UUID or chapter_id.version != 4:
            raise ValueError("chapter_id must be a UUIDv4")
        async with self._operations:
            if self._closed:
                raise ReaderAssetError("The online reader is stopping.")
            existing = self._current
            if existing is not None and existing.chapter.chapter_id == chapter_id and not restart:
                return self.status(chapter_id)
            await self._release()
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
                if not isinstance(connector, OnlinePageProvider):
                    raise ReaderAssetConflict("This source does not yet support disk-free reading.")
                reference = chapter_source.remote_id or chapter_source.remote_url
                if not reference:
                    raise ReaderAssetNotFound("The chapter reference is unavailable.")
                current = _OnlineChapter(
                    uuid4(),
                    ReaderChapter(chapter_id, UUID(chapter.series_id), chapter.display_label, 0),
                    connector,
                )
            self._current = current
            try:
                current.pages = await _owned_io(
                    self._sources.open_online_pages(connector, current.session_id, reference)
                )
                current.chapter = ReaderChapter(
                    chapter_id,
                    current.chapter.series_id,
                    current.chapter.display_label,
                    len(current.pages),
                )
                current.state = "complete"
            except asyncio.CancelledError:
                current.state = "interrupted"
                current.error_code = "READER_INTERRUPTED"
                await self._release()
                raise
            except Exception:
                current.state = "failed"
                current.error_code = "READER_SOURCE_FAILED"
                await self._release()
                raise ReaderAssetError("The source could not open this chapter online.") from None
            return self.status(chapter_id)

    async def open_page(self, chapter_id: UUID, page_number: int) -> CachedReaderPageStream:
        async with self._operations:
            current = self._current
            if (
                self._closed
                or current is None
                or current.chapter.chapter_id != chapter_id
                or current.state != "complete"
            ):
                raise ReaderAssetNotFound("The online chapter is unavailable.")
            if type(page_number) is not int or not 1 <= page_number <= len(current.pages):
                raise ReaderAssetNotFound("Reader page is outside the plan.")
            with self._pins_lock:
                if self._pins >= 2:
                    raise ReaderAssetError("Reader streaming capacity is temporarily occupied.")
                self._pins += 1

            def release() -> None:
                with self._pins_lock:
                    self._pins -= 1

            try:
                request = current.connector.resolve_page(current.pages[page_number - 1])
                result = await self._transport.fetch_memory(
                    request,
                    session_id=current.session_id,
                    ordinal=page_number,
                    allowed_origins=self._sources.download_origins(
                        current.connector.metadata.connector_id
                    ),
                )
                image = await _owned_io(
                    asyncio.to_thread(
                        validate_image_bytes,
                        result.body,
                        result.content_type,
                        policy=self._image_policy,
                    )
                )
            except asyncio.CancelledError:
                release()
                raise
            except Exception:
                release()
                raise ReaderAssetError("The page could not be retrieved and verified.") from None
            current.seen.add(page_number)

            return CachedReaderPageStream(result.body, image.mime, image.sha256, release)

    async def pause(self, chapter_id: UUID) -> ReaderSessionStatus:
        async with self._operations:
            self.status(chapter_id)
            await self._release()
            assert self._current is not None
            self._current.state = "interrupted"
            self._current.seen.clear()
            return self.status(chapter_id)

    async def close(self) -> None:
        async with self._operations:
            self._closed = True
            await self._release()
            self._current = None
