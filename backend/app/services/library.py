from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from app.domain.library_reading_status import LibraryReadingStatus
from app.domain.library_sort import LibrarySort
from app.domain.series import Series
from app.persistence.database import SQLiteDatabase
from app.persistence.models.series_source import SeriesSourceRow
from app.persistence.models.source import SourceRow
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


@dataclass(frozen=True, slots=True)
class LibraryReadingSource:
    series_source_id: UUID
    connector_id: str
    connector_version: str
    remote_series_id: str
    display_name: str


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

    def list_reading_sources(self, series_id: UUID) -> tuple[LibraryReadingSource, ...]:
        with self._database.read_session() as session:
            rows = session.execute(
                select(SeriesSourceRow, SourceRow)
                .join(SourceRow, SourceRow.source_id == SeriesSourceRow.source_id)
                .where(SeriesSourceRow.series_id == str(series_id), SourceRow.enabled.is_(True))
                .order_by(SeriesSourceRow.series_source_id)
            ).all()
            return tuple(
                LibraryReadingSource(
                    UUID(link.series_source_id),
                    source.connector_id,
                    source.connector_version,
                    link.remote_id or link.remote_url,
                    source.display_name,
                )
                for link, source in rows
                if link.remote_id or link.remote_url
            )

    def list_series(
        self,
        *,
        limit: int = DEFAULT_LIBRARY_PAGE_SIZE,
        offset: int = 0,
        reading_status: LibraryReadingStatus | str | None = None,
        query: str = "",
        sort: LibrarySort | str | None = None,
    ) -> SeriesPage:
        if not 1 <= limit <= MAX_LIBRARY_PAGE_SIZE:
            raise ValueError("limit must be between 1 and 500")
        if offset < 0:
            raise ValueError("offset must not be negative")
        if len(query) > 200:
            raise ValueError("query must be a string of at most 200 characters")
        if sort is not None:
            sort = LibrarySort(sort)
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
                query=query.strip(),
                sort=sort,
            )
        return SeriesPage(items=items, total=total, limit=limit, offset=offset)

    def list_recent(self, *, limit: int = DEFAULT_RECENT_LIMIT) -> tuple[Series, ...]:
        validate_recent_limit(limit)
        with self._database.read_session() as session:
            return self._series_repository.list_recent(session, limit=limit)
