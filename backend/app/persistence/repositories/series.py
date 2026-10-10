from uuid import UUID

from sqlalchemy import and_, exists, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.domain.library_reading_status import LibraryReadingStatus
from app.domain.series import Series
from app.persistence.mappers.series import series_from_row, series_to_row
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.reader_progress import ReaderProgressRow
from app.persistence.models.series import SeriesRow

PERCENTAGE_COMPLETE_BASIS_POINTS = 10_000


class SeriesRepository:
    def add(self, session: Session, series: Series) -> None:
        session.add(series_to_row(series))

    def get(self, session: Session, series_id: UUID) -> Series | None:
        row = session.get(SeriesRow, str(series_id))
        return series_from_row(row) if row is not None else None

    def update(self, session: Session, series: Series) -> bool:
        existing = session.get(SeriesRow, str(series.series_id))
        if existing is None:
            return False

        row = series_to_row(series)
        existing.canonical_title = row.canonical_title
        existing.display_title = row.display_title
        existing.description = row.description
        existing.status = row.status
        existing.language = row.language
        existing.author = row.author
        existing.genres_json = row.genres_json
        existing.cover_asset_id = row.cover_asset_id
        existing.updated_at_us = row.updated_at_us
        return True

    def list(
        self,
        session: Session,
        *,
        limit: int,
        offset: int,
        reading_status: LibraryReadingStatus | None = None,
    ) -> tuple[tuple[Series, ...], int]:
        reading_clause = _reading_status_clause(reading_status)
        rows_query = select(SeriesRow).order_by(
            SeriesRow.canonical_title.asc(), SeriesRow.series_id.asc()
        )
        total_query = select(func.count()).select_from(SeriesRow)
        if reading_clause is not None:
            rows_query = rows_query.where(reading_clause)
            total_query = total_query.where(reading_clause)

        rows = session.scalars(rows_query.limit(limit).offset(offset)).all()
        total = session.execute(total_query).scalar_one()
        return tuple(series_from_row(row) for row in rows), int(total)

    def list_recent(self, session: Session, *, limit: int) -> tuple[Series, ...]:
        rows = session.scalars(
            select(SeriesRow)
            .from_statement(
                text("SELECT * FROM series ORDER BY created_at_us DESC, series_id ASC LIMIT :limit")
            )
            .params(limit=limit)
        ).all()
        return tuple(series_from_row(row) for row in rows)


def _reading_status_clause(
    reading_status: LibraryReadingStatus | None,
) -> ColumnElement[bool] | None:
    if reading_status is None:
        return None

    has_chapter = exists(
        select(1).select_from(ChapterRow).where(ChapterRow.series_id == SeriesRow.series_id)
    )
    has_progress = exists(
        select(1)
        .select_from(ReaderProgressRow)
        .where(ReaderProgressRow.series_id == SeriesRow.series_id)
    )
    has_partial_progress = exists(
        select(1)
        .select_from(ReaderProgressRow)
        .where(
            ReaderProgressRow.series_id == SeriesRow.series_id,
            ReaderProgressRow.percentage_basis_points > 0,
            ReaderProgressRow.percentage_basis_points < PERCENTAGE_COMPLETE_BASIS_POINTS,
        )
    )
    has_unfinished_chapter = exists(
        select(1)
        .select_from(ChapterRow)
        .where(
            ChapterRow.series_id == SeriesRow.series_id,
            ~exists(
                select(1)
                .select_from(ReaderProgressRow)
                .where(
                    ReaderProgressRow.series_id == ChapterRow.series_id,
                    ReaderProgressRow.chapter_id == ChapterRow.chapter_id,
                    ReaderProgressRow.percentage_basis_points == PERCENTAGE_COMPLETE_BASIS_POINTS,
                )
            ),
        )
    )
    is_completed = and_(has_chapter, ~has_unfinished_chapter)

    if reading_status is LibraryReadingStatus.READING:
        return has_partial_progress
    if reading_status is LibraryReadingStatus.COMPLETED:
        return is_completed
    return and_(has_progress, ~has_partial_progress, ~is_completed)
