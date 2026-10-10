from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase

SERIES_ID_UUID4_CHECK = (
    "typeof(series_id) = 'text' "
    "AND length(series_id) = 36 "
    "AND length(replace(series_id, '-', '')) = 32 "
    "AND substr(series_id, 9, 1) = '-' "
    "AND substr(series_id, 14, 1) = '-' "
    "AND substr(series_id, 19, 1) = '-' "
    "AND substr(series_id, 24, 1) = '-' "
    "AND substr(series_id, 15, 1) = '4' "
    "AND substr(series_id, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND series_id = lower(series_id) "
    "AND series_id NOT GLOB '*[^0-9a-f-]*'"
)
GENRES_JSON_CHECK = (
    "genres_json IS NULL OR (typeof(genres_json) = 'text' AND "
    "CASE WHEN json_valid(genres_json) = 1 THEN json_type(genres_json) = 'array' ELSE 0 END)"
)


class SeriesRow(PersistenceBase):
    __tablename__ = "series"
    __table_args__ = (
        CheckConstraint(SERIES_ID_UUID4_CHECK, name="series_id_uuid4"),
        CheckConstraint("length(canonical_title) > 0", name="canonical_title_nonempty"),
        CheckConstraint("length(display_title) > 0", name="display_title_nonempty"),
        CheckConstraint(
            "typeof(created_at_us) = 'integer' AND typeof(updated_at_us) = 'integer'",
            name="timestamps_integer",
        ),
        CheckConstraint("updated_at_us >= created_at_us", name="timestamps_ordered"),
        CheckConstraint(GENRES_JSON_CHECK, name="genres_json_array"),
        Index("ix_series_canonical_title", "canonical_title"),
        Index("ix_series_updated_at_us", "updated_at_us"),
        Index(
            "ix_series_created_recent",
            text("created_at_us DESC"),
            text("series_id ASC"),
        ),
    )

    series_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    canonical_title: Mapped[str] = mapped_column(Text(), nullable=False)
    display_title: Mapped[str] = mapped_column(Text(), nullable=False)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str | None] = mapped_column(Text(), nullable=True)
    language: Mapped[str | None] = mapped_column(Text(), nullable=True)
    author: Mapped[str | None] = mapped_column(Text(), nullable=True)
    genres_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    cover_asset_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("assets.asset_id", ondelete="RESTRICT", name="fk_series_cover_asset_id_assets"),
        nullable=True,
    )
    created_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    updated_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
