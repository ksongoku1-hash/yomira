from uuid import UUID

from app.domain.chapter_cbz_asset import ChapterCbzAsset
from app.persistence.mappers.timestamps import datetime_to_epoch_us, epoch_us_to_datetime
from app.persistence.models.chapter_cbz_asset import ChapterCbzAssetRow


def chapter_cbz_asset_to_row(value: ChapterCbzAsset) -> ChapterCbzAssetRow:
    return ChapterCbzAssetRow(
        chapter_id=str(value.chapter_id),
        asset_id=str(value.asset_id),
        download_job_id=str(value.download_job_id),
        page_count=value.page_count,
        indexed_at_us=datetime_to_epoch_us(value.indexed_at),
    )


def chapter_cbz_asset_from_row(row: ChapterCbzAssetRow) -> ChapterCbzAsset:
    return ChapterCbzAsset(
        chapter_id=UUID(row.chapter_id),
        asset_id=UUID(row.asset_id),
        download_job_id=UUID(row.download_job_id),
        page_count=row.page_count,
        indexed_at=epoch_us_to_datetime(row.indexed_at_us),
    )
