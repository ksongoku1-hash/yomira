from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.domain.asset import Asset
from app.domain.chapter_cbz_asset import ChapterCbzAsset
from app.persistence.mappers.asset import asset_from_row
from app.persistence.mappers.chapter import chapter_from_row
from app.persistence.mappers.chapter_cbz_asset import (
    chapter_cbz_asset_from_row,
    chapter_cbz_asset_to_row,
)
from app.persistence.models.asset import AssetRow
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_cbz_asset import ChapterCbzAssetRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.download_manifest_state import DownloadManifestStateRow
from app.persistence.models.download_page import DownloadPageRow


@dataclass(frozen=True, slots=True)
class ReaderChapterRecord:
    chapter_id: UUID
    series_id: UUID
    display_label: str
    chapter_raw_label: str
    chapter_sort_key: tuple[int, ...]
    page_count: int


@dataclass(frozen=True, slots=True)
class ReaderPageRecord:
    link: ChapterCbzAsset
    asset: Asset
    page_ordinal: int
    page_size_bytes: int
    page_sha256: str
    page_mime: str


class ChapterCbzAssetRepository:
    def add(self, session: Session, link: ChapterCbzAsset) -> None:
        session.add(chapter_cbz_asset_to_row(link))

    def get_for_chapter(
        self,
        session: Session,
        chapter_id: UUID,
    ) -> tuple[ChapterCbzAsset, Asset] | None:
        row = session.execute(
            select(ChapterCbzAssetRow, AssetRow)
            .join(AssetRow, AssetRow.asset_id == ChapterCbzAssetRow.asset_id)
            .where(ChapterCbzAssetRow.chapter_id == str(chapter_id))
        ).first()
        if row is None:
            return None
        link_row, asset_row = row
        return chapter_cbz_asset_from_row(link_row), asset_from_row(asset_row)

    def list_ready_chapters(
        self,
        session: Session,
        series_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> tuple[tuple[ReaderChapterRecord, ...], int]:
        predicate = (
            ChapterRow.series_id == str(series_id),
            AssetRow.asset_type == "CBZ",
            DownloadJobRow.status == "COMPLETED",
            DownloadJobRow.cancel_requested_at_us.is_(None),
            DownloadJobRow.recovery_blocked.is_(False),
            ChapterSourceRow.chapter_id == ChapterCbzAssetRow.chapter_id,
            or_(
                DownloadManifestStateRow.job_id.is_(None),
                DownloadManifestStateRow.publication_pending.is_(False),
            ),
        )
        total = int(
            session.execute(
                select(func.count())
                .select_from(ChapterCbzAssetRow)
                .join(ChapterRow, ChapterRow.chapter_id == ChapterCbzAssetRow.chapter_id)
                .join(AssetRow, AssetRow.asset_id == ChapterCbzAssetRow.asset_id)
                .join(DownloadJobRow, DownloadJobRow.job_id == ChapterCbzAssetRow.download_job_id)
                .join(
                    ChapterSourceRow,
                    ChapterSourceRow.chapter_source_id == DownloadJobRow.chapter_source_id,
                )
                .outerjoin(
                    DownloadManifestStateRow,
                    DownloadManifestStateRow.job_id == DownloadJobRow.job_id,
                )
                .where(*predicate)
            ).scalar_one()
        )
        rows = session.execute(
            select(ChapterRow, ChapterCbzAssetRow)
            .join(ChapterCbzAssetRow, ChapterCbzAssetRow.chapter_id == ChapterRow.chapter_id)
            .join(AssetRow, AssetRow.asset_id == ChapterCbzAssetRow.asset_id)
            .join(DownloadJobRow, DownloadJobRow.job_id == ChapterCbzAssetRow.download_job_id)
            .join(
                ChapterSourceRow,
                ChapterSourceRow.chapter_source_id == DownloadJobRow.chapter_source_id,
            )
            .outerjoin(
                DownloadManifestStateRow,
                DownloadManifestStateRow.job_id == DownloadJobRow.job_id,
            )
            .where(*predicate)
            .order_by(
                func.json_extract(ChapterRow.chapter_sort_key, "$[0]").asc(),
                func.json_extract(ChapterRow.chapter_sort_key, "$[1]").asc(),
                func.json_extract(ChapterRow.chapter_sort_key, "$[2]").asc(),
                ChapterRow.chapter_id.asc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()
        items = tuple(
            ReaderChapterRecord(
                chapter_id=UUID(chapter.chapter_id),
                series_id=UUID(chapter.series_id),
                display_label=chapter.display_label,
                chapter_raw_label=chapter.chapter_raw_label,
                chapter_sort_key=chapter_from_row(chapter).chapter_sort_key,
                page_count=link.page_count,
            )
            for chapter, link in rows
        )
        return items, total

    def list_ready_chapters_by_ids(
        self,
        session: Session,
        chapter_ids: tuple[UUID, ...],
    ) -> tuple[ReaderChapterRecord, ...]:
        if not chapter_ids:
            return ()
        predicates = (
            ChapterRow.chapter_id.in_(tuple(str(chapter_id) for chapter_id in chapter_ids)),
            AssetRow.asset_type == "CBZ",
            DownloadJobRow.status == "COMPLETED",
            DownloadJobRow.cancel_requested_at_us.is_(None),
            DownloadJobRow.recovery_blocked.is_(False),
            ChapterSourceRow.chapter_id == ChapterCbzAssetRow.chapter_id,
            or_(
                DownloadManifestStateRow.job_id.is_(None),
                DownloadManifestStateRow.publication_pending.is_(False),
            ),
        )
        rows = session.execute(
            select(ChapterRow, ChapterCbzAssetRow)
            .join(ChapterCbzAssetRow, ChapterCbzAssetRow.chapter_id == ChapterRow.chapter_id)
            .join(AssetRow, AssetRow.asset_id == ChapterCbzAssetRow.asset_id)
            .join(DownloadJobRow, DownloadJobRow.job_id == ChapterCbzAssetRow.download_job_id)
            .join(
                ChapterSourceRow,
                ChapterSourceRow.chapter_source_id == DownloadJobRow.chapter_source_id,
            )
            .outerjoin(
                DownloadManifestStateRow,
                DownloadManifestStateRow.job_id == DownloadJobRow.job_id,
            )
            .where(*predicates)
            .order_by(
                func.json_extract(ChapterRow.chapter_sort_key, "$[0]").asc(),
                func.json_extract(ChapterRow.chapter_sort_key, "$[1]").asc(),
                func.json_extract(ChapterRow.chapter_sort_key, "$[2]").asc(),
                ChapterRow.chapter_id.asc(),
            )
        ).all()
        return tuple(
            ReaderChapterRecord(
                chapter_id=UUID(chapter.chapter_id),
                series_id=UUID(chapter.series_id),
                display_label=chapter.display_label,
                chapter_raw_label=chapter.chapter_raw_label,
                chapter_sort_key=chapter_from_row(chapter).chapter_sort_key,
                page_count=link.page_count,
            )
            for chapter, link in rows
        )

    def get_reader_chapter(
        self,
        session: Session,
        chapter_id: UUID,
    ) -> ReaderChapterRecord | None:
        row = session.execute(
            select(ChapterRow, ChapterCbzAssetRow)
            .join(ChapterCbzAssetRow, ChapterCbzAssetRow.chapter_id == ChapterRow.chapter_id)
            .join(AssetRow, AssetRow.asset_id == ChapterCbzAssetRow.asset_id)
            .join(DownloadJobRow, DownloadJobRow.job_id == ChapterCbzAssetRow.download_job_id)
            .join(
                ChapterSourceRow,
                ChapterSourceRow.chapter_source_id == DownloadJobRow.chapter_source_id,
            )
            .outerjoin(
                DownloadManifestStateRow,
                DownloadManifestStateRow.job_id == DownloadJobRow.job_id,
            )
            .where(
                ChapterRow.chapter_id == str(chapter_id),
                AssetRow.asset_type == "CBZ",
                DownloadJobRow.status == "COMPLETED",
                DownloadJobRow.cancel_requested_at_us.is_(None),
                DownloadJobRow.recovery_blocked.is_(False),
                ChapterSourceRow.chapter_id == ChapterCbzAssetRow.chapter_id,
                or_(
                    DownloadManifestStateRow.job_id.is_(None),
                    DownloadManifestStateRow.publication_pending.is_(False),
                ),
            )
        ).first()
        if row is None:
            return None
        chapter, link = row
        return ReaderChapterRecord(
            chapter_id=UUID(chapter.chapter_id),
            series_id=UUID(chapter.series_id),
            display_label=chapter.display_label,
            chapter_raw_label=chapter.chapter_raw_label,
            chapter_sort_key=chapter_from_row(chapter).chapter_sort_key,
            page_count=link.page_count,
        )

    def get_reader_page(
        self,
        session: Session,
        chapter_id: UUID,
        page_number: int,
    ) -> ReaderPageRecord | None:
        row = session.execute(
            select(ChapterCbzAssetRow, AssetRow, DownloadPageRow)
            .join(AssetRow, AssetRow.asset_id == ChapterCbzAssetRow.asset_id)
            .join(DownloadJobRow, DownloadJobRow.job_id == ChapterCbzAssetRow.download_job_id)
            .join(
                ChapterSourceRow,
                ChapterSourceRow.chapter_source_id == DownloadJobRow.chapter_source_id,
            )
            .outerjoin(
                DownloadManifestStateRow,
                DownloadManifestStateRow.job_id == DownloadJobRow.job_id,
            )
            .join(
                DownloadPageRow,
                DownloadPageRow.job_id == ChapterCbzAssetRow.download_job_id,
            )
            .where(
                ChapterCbzAssetRow.chapter_id == str(chapter_id),
                ChapterCbzAssetRow.page_count >= page_number,
                DownloadPageRow.ordinal == page_number,
                DownloadPageRow.status == "VALIDATED",
                AssetRow.asset_type == "CBZ",
                DownloadJobRow.status == "COMPLETED",
                DownloadJobRow.cancel_requested_at_us.is_(None),
                DownloadJobRow.recovery_blocked.is_(False),
                ChapterSourceRow.chapter_id == ChapterCbzAssetRow.chapter_id,
                or_(
                    DownloadManifestStateRow.job_id.is_(None),
                    DownloadManifestStateRow.publication_pending.is_(False),
                ),
            )
        ).first()
        if row is None:
            return None
        link_row, asset_row, page_row = row
        if (
            page_row.size_bytes is None
            or page_row.sha256 is None
            or page_row.mime is None
        ):
            return None
        return ReaderPageRecord(
            link=chapter_cbz_asset_from_row(link_row),
            asset=asset_from_row(asset_row),
            page_ordinal=page_row.ordinal,
            page_size_bytes=page_row.size_bytes,
            page_sha256=page_row.sha256,
            page_mime=page_row.mime,
        )

    def list_reader_pages(
        self,
        session: Session,
        chapter_id: UUID,
    ) -> tuple[ReaderPageRecord, ...]:
        rows = session.execute(
            select(ChapterCbzAssetRow, AssetRow, DownloadPageRow)
            .join(AssetRow, AssetRow.asset_id == ChapterCbzAssetRow.asset_id)
            .join(DownloadJobRow, DownloadJobRow.job_id == ChapterCbzAssetRow.download_job_id)
            .join(
                ChapterSourceRow,
                ChapterSourceRow.chapter_source_id == DownloadJobRow.chapter_source_id,
            )
            .outerjoin(
                DownloadManifestStateRow,
                DownloadManifestStateRow.job_id == DownloadJobRow.job_id,
            )
            .join(
                DownloadPageRow,
                DownloadPageRow.job_id == ChapterCbzAssetRow.download_job_id,
            )
            .where(
                ChapterCbzAssetRow.chapter_id == str(chapter_id),
                DownloadPageRow.status == "VALIDATED",
                AssetRow.asset_type == "CBZ",
                DownloadJobRow.status == "COMPLETED",
                DownloadJobRow.cancel_requested_at_us.is_(None),
                DownloadJobRow.recovery_blocked.is_(False),
                ChapterSourceRow.chapter_id == ChapterCbzAssetRow.chapter_id,
                or_(
                    DownloadManifestStateRow.job_id.is_(None),
                    DownloadManifestStateRow.publication_pending.is_(False),
                ),
            )
            .order_by(DownloadPageRow.ordinal.asc())
        ).all()
        pages: list[ReaderPageRecord] = []
        for link_row, asset_row, page_row in rows:
            if (
                page_row.size_bytes is None
                or page_row.sha256 is None
                or page_row.mime is None
            ):
                return ()
            pages.append(
                ReaderPageRecord(
                    link=chapter_cbz_asset_from_row(link_row),
                    asset=asset_from_row(asset_row),
                    page_ordinal=page_row.ordinal,
                    page_size_bytes=page_row.size_bytes,
                    page_sha256=page_row.sha256,
                    page_mime=page_row.mime,
                )
            )
        return tuple(pages)
