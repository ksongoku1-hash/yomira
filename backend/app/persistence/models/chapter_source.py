from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase

CHAPTER_SOURCE_ID_UUID4_CHECK = (
    "typeof(chapter_source_id) = 'text' "
    "AND length(chapter_source_id) = 36 "
    "AND length(replace(chapter_source_id, '-', '')) = 32 "
    "AND substr(chapter_source_id, 9, 1) = '-' "
    "AND substr(chapter_source_id, 14, 1) = '-' "
    "AND substr(chapter_source_id, 19, 1) = '-' "
    "AND substr(chapter_source_id, 24, 1) = '-' "
    "AND substr(chapter_source_id, 15, 1) = '4' "
    "AND substr(chapter_source_id, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND chapter_source_id = lower(chapter_source_id) "
    "AND chapter_source_id NOT GLOB '*[^0-9a-f-]*'"
)


class ChapterSourceRow(PersistenceBase):
    __tablename__ = "chapter_sources"
    __table_args__ = (
        CheckConstraint(CHAPTER_SOURCE_ID_UUID4_CHECK, name="chapter_source_id_uuid4"),
        CheckConstraint(
            "remote_id IS NULL OR length(remote_id) > 0",
            name="remote_id_nonempty_when_present",
        ),
        CheckConstraint(
            "remote_url IS NULL OR length(remote_url) > 0",
            name="remote_url_nonempty_when_present",
        ),
        CheckConstraint(
            "(remote_id IS NOT NULL AND length(remote_id) > 0) "
            "OR (remote_url IS NOT NULL AND length(remote_url) > 0)",
            name="remote_reference_required",
        ),
        CheckConstraint("available IN (0, 1)", name="available_boolean"),
        CheckConstraint(
            "last_seen_at_us IS NULL OR typeof(last_seen_at_us) = 'integer'",
            name="last_seen_integer",
        ),
        Index("ix_chapter_sources_chapter_id", "chapter_id"),
        Index("ix_chapter_sources_source_id", "source_id"),
    )

    chapter_source_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chapter_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "chapters.chapter_id",
            ondelete="RESTRICT",
            name="fk_chapter_sources_chapter_id",
        ),
        nullable=False,
    )
    source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "sources.source_id",
            ondelete="RESTRICT",
            name="fk_chapter_sources_source_id",
        ),
        nullable=False,
    )
    remote_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    remote_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    remote_label: Mapped[str | None] = mapped_column(Text(), nullable=True)
    available: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    last_seen_at_us: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
