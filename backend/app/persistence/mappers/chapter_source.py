from uuid import UUID

from app.domain.chapter_source import ChapterSource
from app.persistence.mappers.timestamps import datetime_to_epoch_us, epoch_us_to_datetime
from app.persistence.models.chapter_source import ChapterSourceRow


def chapter_source_to_row(chapter_source: ChapterSource) -> ChapterSourceRow:
    return ChapterSourceRow(
        chapter_source_id=str(chapter_source.chapter_source_id),
        chapter_id=str(chapter_source.chapter_id),
        source_id=str(chapter_source.source_id),
        remote_id=chapter_source.remote_id,
        remote_url=chapter_source.remote_url,
        remote_label=chapter_source.remote_label,
        available=chapter_source.available,
        last_seen_at_us=(
            datetime_to_epoch_us(chapter_source.last_seen_at)
            if chapter_source.last_seen_at is not None
            else None
        ),
    )


def chapter_source_from_row(row: ChapterSourceRow) -> ChapterSource:
    return ChapterSource(
        chapter_source_id=UUID(row.chapter_source_id),
        chapter_id=UUID(row.chapter_id),
        source_id=UUID(row.source_id),
        remote_id=row.remote_id,
        remote_url=row.remote_url,
        remote_label=row.remote_label,
        available=row.available,
        last_seen_at=(
            epoch_us_to_datetime(row.last_seen_at_us)
            if row.last_seen_at_us is not None
            else None
        ),
    )
