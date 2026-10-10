from dataclasses import dataclass
from uuid import UUID

from app.domain.library_reading_status import LibraryReadingStatus
from app.domain.series import Series
from app.persistence.database import SQLiteDatabase
from app.persistence.repositories.series import SeriesRepository
from app.services.recent_views import DEFAULT_RECENT_LIMIT, validate_recent_limit

DEFAULT_LIBRARY_PAGE_SIZE = 100
MAX_LIBRARY_PAGE_SIZE = 500


@dataclass(frozen=True, slots=True)
class SeriesPage:
    items: tuple[Series, ...]
    total: int
    limit: int
    offset: int


class LibraryService:
    def __init__(
        self,
        database: SQLiteDatabase,
        series_repository: SeriesRepository | None = None,
    ) -> None:
        self._database = database
        self._series_repository = series_repository or SeriesRepository()

    def add_series(self, series: Series) -> None:
        with self._database.write_session() as session:
            self._series_repository.add(session, series)

    def get_series(self, series_id: UUID) -> Series | None:
        with self._database.read_session() as session:
            return self._series_repository.get(session, series_id)

    def list_series(
        self,
        *,
        limit: int = DEFAULT_LIBRARY_PAGE_SIZE,
        offset: int = 0,
        reading_status: LibraryReadingStatus | str | None = None,
    ) -> SeriesPage:
        if not 1 <= limit <= MAX_LIBRARY_PAGE_SIZE:
            raise ValueError("limit must be between 1 and 500")
        if offset < 0:
            raise ValueError("offset must not be negative")
        if reading_status is not None:
            try:
                reading_status = LibraryReadingStatus(reading_status)
            except ValueError as error:
                raise ValueError("reading_status is not supported") from error

        with self._database.read_session() as session:
            items, total = self._series_repository.list(
                session,
                limit=limit,
                offset=offset,
                reading_status=reading_status,
            )
        return SeriesPage(items=items, total=total, limit=limit, offset=offset)

    def list_recent(self, *, limit: int = DEFAULT_RECENT_LIMIT) -> tuple[Series, ...]:
        validate_recent_limit(limit)
        with self._database.read_session() as session:
            return self._series_repository.list_recent(session, limit=limit)
