from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from app.domain.reader_progress import ReaderProgress
from app.persistence.database import SQLiteDatabase
from app.persistence.repositories.reader_progress import (
    ReaderProgressRepository,
    RecentReaderProgressRecord,
)
from app.services.reader_asset import ReaderChapter
from app.services.recent_views import DEFAULT_RECENT_LIMIT, validate_recent_limit


class ReaderChapterLookup(Protocol):
    def get_chapter(self, chapter_id: UUID) -> ReaderChapter | None: ...


class ReaderProgressChapterUnavailable(RuntimeError):
    """The chapter is not available through the verified reader asset service."""


class ReaderProgressPageOutOfRange(ValueError):
    """The requested zero-based page index is outside the published chapter."""


@dataclass(slots=True)
class ReaderProgressService:
    database: SQLiteDatabase
    chapters: ReaderChapterLookup
    repository: ReaderProgressRepository
    page_available: Callable[[UUID, int], bool] | None

    def __init__(
        self,
        database: SQLiteDatabase,
        chapters: ReaderChapterLookup,
        repository: ReaderProgressRepository | None = None,
        *,
        page_available: Callable[[UUID, int], bool] | None = None,
    ) -> None:
        self.database = database
        self.chapters = chapters
        self.repository = repository or ReaderProgressRepository()
        self.page_available = page_available

    def get(self, chapter_id: UUID) -> ReaderProgress | None:
        chapter = self._get_available_chapter(chapter_id)
        with self.database.read_session() as session:
            return self.repository.get(session, chapter.series_id, chapter.chapter_id)

    def list_recent(
        self,
        *,
        limit: int = DEFAULT_RECENT_LIMIT,
    ) -> tuple[RecentReaderProgressRecord, ...]:
        validate_recent_limit(limit)
        with self.database.read_session() as session:
            return self.repository.list_recent(session, limit=limit)

    def save(self, chapter_id: UUID, page_index: int) -> ReaderProgress:
        chapter = self._get_available_chapter(chapter_id)
        if type(page_index) is not int or not 0 <= page_index < chapter.page_count:
            raise ReaderProgressPageOutOfRange
        if self.page_available is not None and not self.page_available(chapter_id, page_index):
            raise ReaderProgressPageOutOfRange

        if chapter.page_count == 1:
            basis_points = 10_000
        else:
            denominator = chapter.page_count - 1
            numerator = page_index * 10_000
            # Nearest basis point, with exact ties rounded up. Integer-only.
            basis_points = (numerator + denominator // 2) // denominator
        progress = ReaderProgress.new_local(
            chapter.series_id,
            chapter.chapter_id,
            page_index,
            Decimal(basis_points) / Decimal(100),
        )
        with self.database.write_session() as session:
            self.repository.save(session, progress)
        return progress

    def _get_available_chapter(self, chapter_id: UUID) -> ReaderChapter:
        if type(chapter_id) is not UUID or chapter_id.version != 4:
            raise ValueError("chapter_id must be a UUIDv4")
        chapter = self.chapters.get_chapter(chapter_id)
        if (
            chapter is None
            or type(chapter.chapter_id) is not UUID
            or chapter.chapter_id != chapter_id
            or chapter.chapter_id.version != 4
            or type(chapter.series_id) is not UUID
            or chapter.series_id.version != 4
            or type(chapter.page_count) is not int
            or chapter.page_count < 1
        ):
            raise ReaderProgressChapterUnavailable
        return chapter
