from sqlalchemy import BigInteger, CheckConstraint, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase

ASSET_ID_UUID4_CHECK = (
    "typeof(asset_id) = 'text' "
    "AND length(asset_id) = 36 "
    "AND length(replace(asset_id, '-', '')) = 32 "
    "AND substr(asset_id, 9, 1) = '-' "
    "AND substr(asset_id, 14, 1) = '-' "
    "AND substr(asset_id, 19, 1) = '-' "
    "AND substr(asset_id, 24, 1) = '-' "
    "AND substr(asset_id, 15, 1) = '4' "
    "AND substr(asset_id, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND asset_id = lower(asset_id) "
    "AND asset_id NOT GLOB '*[^0-9a-f-]*'"
)
ASSET_TYPES = "'COVER', 'THUMBNAIL', 'CBZ', 'CBR', 'PAGE_CACHE', 'TEMP', 'EXPORT_COPY'"
ASSET_RELATIVE_PATH_CHECK = (
    "length(relative_path) > 0 "
    "AND substr(relative_path, 1, 1) <> '/' "
    "AND substr(relative_path, -1, 1) <> '/' "
    "AND instr(relative_path, char(92)) = 0 "
    "AND instr(relative_path, ':') = 0 "
    "AND instr(relative_path, '//') = 0 "
    "AND instr('/' || relative_path || '/', '/../') = 0 "
    "AND instr('/' || relative_path || '/', '/./') = 0 "
    "AND instr(relative_path, char(0)) = 0 "
    "AND relative_path NOT GLOB '*[<>:\"|?*]*'"
)
ASSET_SHA256_CHECK = (
    "typeof(sha256) = 'text' "
    "AND length(sha256) = 64 "
    "AND sha256 = lower(sha256) "
    "AND sha256 NOT GLOB '*[^0-9a-f]*'"
)


class AssetRow(PersistenceBase):
    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint(ASSET_ID_UUID4_CHECK, name="asset_id_uuid4"),
        CheckConstraint(f"asset_type IN ({ASSET_TYPES})", name="asset_type_supported"),
        CheckConstraint(ASSET_RELATIVE_PATH_CHECK, name="relative_path_safe"),
        CheckConstraint(ASSET_SHA256_CHECK, name="sha256_valid"),
        CheckConstraint(
            "typeof(size_bytes) = 'integer' AND size_bytes >= 0",
            name="size_bytes_nonnegative_integer",
        ),
        CheckConstraint(
            "typeof(created_at_us) = 'integer' "
            "AND (verified_at_us IS NULL OR typeof(verified_at_us) = 'integer')",
            name="timestamps_integer",
        ),
        Index("ix_assets_sha256", "sha256"),
    )

    asset_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_type: Mapped[str] = mapped_column(Text(), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text(), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    created_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    verified_at_us: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
