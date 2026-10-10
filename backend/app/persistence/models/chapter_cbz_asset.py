from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase

_CHAPTER_CBZ_UUID_CHECK_TEMPLATE = (
    "typeof({column}) = 'text' "
    "AND length({column}) = 36 "
    "AND length(replace({column}, '-', '')) = 32 "
    "AND substr({column}, 9, 1) = '-' "
    "AND substr({column}, 14, 1) = '-' "
    "AND substr({column}, 19, 1) = '-' "
    "AND substr({column}, 24, 1) = '-' "
    "AND substr({column}, 15, 1) = '4' "
    "AND substr({column}, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND {column} = lower({column}) "
    "AND {column} NOT GLOB '*[^0-9a-f-]*'"
)


class ChapterCbzAssetRow(PersistenceBase):
    __tablename__ = "chapter_cbz_assets"
    __table_args__ = (
        CheckConstraint(
            _CHAPTER_CBZ_UUID_CHECK_TEMPLATE.format(column="chapter_id"),
            name="chapter_id_uuid4",
        ),
        CheckConstraint(
            _CHAPTER_CBZ_UUID_CHECK_TEMPLATE.format(column="asset_id"),
            name="asset_id_uuid4",
        ),
        CheckConstraint(
            _CHAPTER_CBZ_UUID_CHECK_TEMPLATE.format(column="download_job_id"),
            name="download_job_id_uuid4",
        ),
        CheckConstraint(
            "typeof(page_count) = 'integer' AND page_count > 0",
            name="page_count_positive_integer",
        ),
        CheckConstraint("typeof(indexed_at_us) = 'integer'", name="indexed_at_integer"),
        UniqueConstraint("asset_id", name="uq_chapter_cbz_assets_asset_id"),
        UniqueConstraint("download_job_id", name="uq_chapter_cbz_assets_download_job_id"),
    )

    chapter_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "chapters.chapter_id",
            ondelete="RESTRICT",
            name="fk_chapter_cbz_assets_chapter",
        ),
        primary_key=True,
    )
    asset_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "assets.asset_id",
            ondelete="RESTRICT",
            name="fk_chapter_cbz_assets_asset",
        ),
        nullable=False,
    )
    download_job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "download_jobs.job_id",
            ondelete="RESTRICT",
            name="fk_chapter_cbz_assets_download_job",
        ),
        nullable=False,
    )
    page_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    indexed_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
