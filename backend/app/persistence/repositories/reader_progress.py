from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.domain.reader_progress import ReaderProgress
from app.persistence.mappers.reader_progress import (
    reader_progress_from_row,
    reader_progress_to_row,
)
from app.persistence.models.reader_progress import ReaderProgressRow


@dataclass(frozen=True, slots=True)
class RecentReaderProgressRecord:
    progress: ReaderProgress
    display_title: str
    display_label: str


class ReaderProgressRepository:
    def get(
        self,
        session: Session,
        series_id: UUID,
        chapter_id: UUID,
    ) -> ReaderProgress | None:
        row = session.get(
            ReaderProgressRow,
            (str(series_id), str(chapter_id)),
        )
        return reader_progress_from_row(row) if row is not None else None

    def save(self, session: Session, progress: ReaderProgress) -> None:
        row = reader_progress_to_row(progress)
        statement = insert(ReaderProgressRow).values(
            series_id=row.series_id,
            chapter_id=row.chapter_id,
            page_index=row.page_index,
            percentage_basis_points=row.percentage_basis_points,
            updated_at_us=row.updated_at_us,
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    ReaderProgressRow.series_id,
                    ReaderProgressRow.chapter_id,
                ],
                set_={
                    "page_index": statement.excluded.page_index,
                    "percentage_basis_points": (statement.excluded.percentage_basis_points),
                    "updated_at_us": statement.excluded.updated_at_us,
                },
            )
        )

    def list_recent(
        self,
        session: Session,
        *,
        limit: int,
    ) -> tuple[RecentReaderProgressRecord, ...]:
        rows = (
            session.execute(
                text(
                    "SELECT reader_progress.series_id, reader_progress.chapter_id, "
                    "reader_progress.page_index, reader_progress.percentage_basis_points, "
                    "reader_progress.updated_at_us, series.display_title, "
                    "chapters.display_label "
                    "FROM reader_progress "
                    "JOIN series ON series.series_id = reader_progress.series_id "
                    "JOIN chapters ON chapters.series_id = reader_progress.series_id "
                    "AND chapters.chapter_id = reader_progress.chapter_id "
                    "ORDER BY reader_progress.updated_at_us DESC, "
                    "reader_progress.series_id ASC, reader_progress.chapter_id ASC "
                    "LIMIT :limit"
                ),
                {"limit": limit},
            )
            .mappings()
            .all()
        )
        return tuple(
            RecentReaderProgressRecord(
                progress=reader_progress_from_row(
                    ReaderProgressRow(
                        series_id=row["series_id"],
                        chapter_id=row["chapter_id"],
                        page_index=row["page_index"],
                        percentage_basis_points=row["percentage_basis_points"],
                        updated_at_us=row["updated_at_us"],
                    )
                ),
                display_title=row["display_title"],
                display_label=row["display_label"],
            )
            for row in rows
        )
