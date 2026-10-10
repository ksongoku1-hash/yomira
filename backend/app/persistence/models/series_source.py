from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase

SERIES_SOURCE_ID_UUID4_CHECK = (
    "typeof(series_source_id) = 'text' "
    "AND length(series_source_id) = 36 "
    "AND length(replace(series_source_id, '-', '')) = 32 "
    "AND substr(series_source_id, 9, 1) = '-' "
    "AND substr(series_source_id, 14, 1) = '-' "
    "AND substr(series_source_id, 19, 1) = '-' "
    "AND substr(series_source_id, 24, 1) = '-' "
    "AND substr(series_source_id, 15, 1) = '4' "
    "AND substr(series_source_id, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND series_source_id = lower(series_source_id) "
    "AND series_source_id NOT GLOB '*[^0-9a-f-]*'"
)
REMOTE_GENRES_JSON_CHECK = (
    "remote_genres_json IS NULL OR (typeof(remote_genres_json) = 'text' AND "
    "CASE WHEN json_valid(remote_genres_json) = 1 "
    "THEN json_type(remote_genres_json) = 'array' ELSE 0 END)"
)


class SeriesSourceRow(PersistenceBase):
    __tablename__ = "series_sources"
    __table_args__ = (
        CheckConstraint(SERIES_SOURCE_ID_UUID4_CHECK, name="series_source_id_uuid4"),
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
        CheckConstraint(
            "last_checked_at_us IS NULL OR typeof(last_checked_at_us) = 'integer'",
            name="last_checked_integer",
        ),
        CheckConstraint(REMOTE_GENRES_JSON_CHECK, name="remote_genres_json_array"),
        Index("ix_series_sources_series_id", "series_id"),
        Index("ix_series_sources_source_id", "source_id"),
        Index(
            "uq_series_sources_source_remote_id",
            "source_id",
            "remote_id",
            unique=True,
            sqlite_where=text("remote_id IS NOT NULL"),
        ),
        Index(
            "uq_series_sources_source_remote_url",
            "source_id",
            "remote_url",
            unique=True,
            sqlite_where=text("remote_url IS NOT NULL"),
        ),
    )

    series_source_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    series_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("series.series_id", ondelete="RESTRICT", name="fk_series_sources_series_id"),
        nullable=False,
    )
    source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sources.source_id", ondelete="RESTRICT", name="fk_series_sources_source_id"),
        nullable=False,
    )
    remote_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    remote_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    remote_title: Mapped[str | None] = mapped_column(Text(), nullable=True)
    remote_cover_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    remote_author: Mapped[str | None] = mapped_column(Text(), nullable=True)
    remote_description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    remote_genres_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    remote_status: Mapped[str | None] = mapped_column(Text(), nullable=True)
    remote_language: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_checked_at_us: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
