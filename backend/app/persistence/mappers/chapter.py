import json
from typing import cast
from uuid import UUID

from app.domain.chapter import Chapter
from app.persistence.models.chapter import ChapterRow


def chapter_to_row(chapter: Chapter) -> ChapterRow:
    return ChapterRow(
        chapter_id=str(chapter.chapter_id),
        series_id=str(chapter.series_id),
        chapter_raw_label=chapter.chapter_raw_label,
        chapter_major=chapter.chapter_major,
        chapter_minor=chapter.chapter_minor,
        chapter_variant=chapter.chapter_variant,
        chapter_type=chapter.chapter_type,
        chapter_sort_key=json.dumps(list(chapter.chapter_sort_key), separators=(",", ":")),
        volume_number=chapter.volume_number,
        language=chapter.language,
        display_label=chapter.display_label,
    )


def chapter_from_row(row: ChapterRow) -> Chapter:
    try:
        decoded_sort_key: object = json.loads(row.chapter_sort_key)
    except json.JSONDecodeError as error:
        raise ValueError("chapter_sort_key is not valid JSON") from error
    if not isinstance(decoded_sort_key, list):
        raise ValueError("chapter_sort_key must be a JSON array of integers")
    decoded_items = cast(list[object], decoded_sort_key)
    if any(type(value) is not int for value in decoded_items):
        raise ValueError("chapter_sort_key must be a JSON array of integers")
    sort_key = tuple(cast(int, value) for value in decoded_items)

    return Chapter(
        chapter_id=UUID(row.chapter_id),
        series_id=UUID(row.series_id),
        chapter_raw_label=row.chapter_raw_label,
        chapter_major=row.chapter_major,
        chapter_minor=row.chapter_minor,
        chapter_variant=row.chapter_variant,
        chapter_type=row.chapter_type,
        chapter_sort_key=sort_key,
        volume_number=row.volume_number,
        language=row.language,
        display_label=row.display_label,
    )
