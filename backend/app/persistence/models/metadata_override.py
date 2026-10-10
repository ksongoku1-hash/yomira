from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase

METADATA_OVERRIDE_FIELDS = (
    "'title', 'cover', 'author', 'description', 'genres', 'status', 'language'"
)


class MetadataOverrideRow(PersistenceBase):
    __tablename__ = "metadata_overrides"
    __table_args__ = (
        CheckConstraint(
            f"field_name IN ({METADATA_OVERRIDE_FIELDS})",
            name="field_name_supported",
        ),
        CheckConstraint(
            "typeof(created_at_us) = 'integer' AND typeof(updated_at_us) = 'integer' "
            "AND updated_at_us >= created_at_us",
            name="timestamps_ordered_integer",
        ),
        CheckConstraint(
            "(field_name = 'cover' AND value_json IS NULL) OR "
            "(field_name <> 'cover' AND cover_asset_id IS NULL AND value_json IS NOT NULL "
            "AND json_valid(value_json) = 1 AND CASE WHEN field_name = 'genres' "
            "THEN json_type(value_json) IN ('array', 'null') "
            "ELSE json_type(value_json) IN ('text', 'null') END)",
            name="value_matches_field",
        ),
        Index("ix_metadata_overrides_field_name", "field_name"),
    )

    series_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("series.series_id", ondelete="RESTRICT", name="fk_metadata_overrides_series_id"),
        primary_key=True,
    )
    field_name: Mapped[str] = mapped_column(String(32), primary_key=True)
    value_json: Mapped[str | None] = mapped_column(Text(), nullable=True)
    cover_asset_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "assets.asset_id",
            ondelete="RESTRICT",
            name="fk_metadata_overrides_cover_asset_id",
        ),
        nullable=True,
    )
    created_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    updated_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
