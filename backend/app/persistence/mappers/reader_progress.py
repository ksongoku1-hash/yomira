from decimal import Decimal
from uuid import UUID

from app.domain.reader_progress import BASIS_POINTS_PER_PERCENT, ReaderProgress
from app.persistence.mappers.timestamps import datetime_to_epoch_us, epoch_us_to_datetime
from app.persistence.models.reader_progress import ReaderProgressRow


def reader_progress_to_row(progress: ReaderProgress) -> ReaderProgressRow:
    return ReaderProgressRow(
        series_id=str(progress.series_id),
        chapter_id=str(progress.chapter_id),
        page_index=progress.page_index,
        percentage_basis_points=progress.percentage_basis_points,
        updated_at_us=datetime_to_epoch_us(progress.updated_at),
    )


def reader_progress_from_row(row: ReaderProgressRow) -> ReaderProgress:
    percentage = Decimal(row.percentage_basis_points) / BASIS_POINTS_PER_PERCENT
    return ReaderProgress(
        series_id=UUID(row.series_id),
        chapter_id=UUID(row.chapter_id),
        page_index=row.page_index,
        percentage=percentage,
        updated_at=epoch_us_to_datetime(row.updated_at_us),
    )
