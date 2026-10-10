from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.domain.timestamps import normalise_utc


@dataclass(frozen=True, slots=True)
class ChapterCbzAsset:
    chapter_id: UUID
    asset_id: UUID
    download_job_id: UUID
    page_count: int
    indexed_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("chapter_id", "asset_id", "download_job_id"):
            value = getattr(self, field_name)
            if type(value) is not UUID or value.version != 4:
                raise ValueError(f"{field_name} must be a UUIDv4")
        if type(self.page_count) is not int or self.page_count <= 0:
            raise ValueError("page_count must be a positive integer")
        object.__setattr__(self, "indexed_at", normalise_utc(self.indexed_at, "indexed_at"))

    @classmethod
    def new_local(
        cls,
        chapter_id: UUID,
        asset_id: UUID,
        download_job_id: UUID,
        page_count: int,
        *,
        indexed_at: datetime | None = None,
    ) -> ChapterCbzAsset:
        return cls(
            chapter_id=chapter_id,
            asset_id=asset_id,
            download_job_id=download_job_id,
            page_count=page_count,
            indexed_at=indexed_at if indexed_at is not None else datetime.now(UTC),
        )
