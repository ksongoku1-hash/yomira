from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase

CHAPTER_ID_UUID4_CHECK = (
    "typeof(chapter_id) = 'text' "
    "AND length(chapter_id) = 36 "
    "AND length(replace(chapter_id, '-', '')) = 32 "
    "AND substr(chapter_id, 9, 1) = '-' "
    "AND substr(chapter_id, 14, 1) = '-' "
    "AND substr(chapter_id, 19, 1) = '-' "
    "AND substr(chapter_id, 24, 1) = '-' "
    "AND substr(chapter_id, 15, 1) = '4' "
    "AND substr(chapter_id, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND chapter_id = lower(chapter_id) "
    "AND chapter_id NOT GLOB '*[^0-9a-f-]*'"
)
CHAPTER_SORT_KEY_JSON_CHECK = (
    "typeof(chapter_sort_key) = 'text' "
    "AND CASE WHEN json_valid(chapter_sort_key) = 1 "
    "THEN json_type(chapter_sort_key) = 'array' ELSE 0 END"
)


class ChapterRow(PersistenceBase):
    __tablename__ = "chapters"
    __table_args__ = (
        CheckConstraint(CHAPTER_ID_UUID4_CHECK, name="chapter_id_uuid4"),
        CheckConstraint("length(chapter_raw_label) > 0", name="chapter_raw_label_nonempty"),
        CheckConstraint("length(display_label) > 0", name="display_label_nonempty"),
        CheckConstraint(
            "chapter_major IS NULL OR typeof(chapter_major) = 'integer'",
            name="chapter_major_integer",
        ),
        CheckConstraint(
            "chapter_minor IS NULL OR typeof(chapter_minor) = 'integer'",
            name="chapter_minor_integer",
        ),
        CheckConstraint(
            "volume_number IS NULL OR typeof(volume_number) = 'integer'",
            name="volume_number_integer",
        ),
        CheckConstraint(CHAPTER_SORT_KEY_JSON_CHECK, name="chapter_sort_key_json_array"),
        Index("ix_chapters_series_id", "series_id"),
        Index("ux_chapters_series_chapter_id", "series_id", "chapter_id", unique=True),
    )

    chapter_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    series_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("series.series_id", ondelete="RESTRICT", name="fk_chapters_series_id"),
        nullable=False,
    )
    chapter_raw_label: Mapped[str] = mapped_column(Text(), nullable=False)
    chapter_major: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    chapter_minor: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    chapter_variant: Mapped[str | None] = mapped_column(Text(), nullable=True)
    chapter_type: Mapped[str | None] = mapped_column(Text(), nullable=True)
    chapter_sort_key: Mapped[str] = mapped_column(Text(), nullable=False)
    volume_number: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    language: Mapped[str | None] = mapped_column(Text(), nullable=True)
    display_label: Mapped[str] = mapped_column(Text(), nullable=False)
