from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.domain.timestamps import normalise_utc


@dataclass(frozen=True, slots=True)
class Series:
    series_id: UUID
    canonical_title: str
    display_title: str
    description: str | None
    status: str | None
    language: str | None
    cover_asset_id: UUID | None
    created_at: datetime
    updated_at: datetime
    author: str | None = None
    genres: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.series_id.version != 4:
            raise ValueError("series_id must be a UUIDv4")
        if self.cover_asset_id is not None and self.cover_asset_id.version != 4:
            raise ValueError("cover_asset_id must be a UUIDv4")
        if not self.canonical_title:
            raise ValueError("canonical_title must not be empty")
        if not self.display_title:
            raise ValueError("display_title must not be empty")

        created_at = normalise_utc(self.created_at, "created_at")
        updated_at = normalise_utc(self.updated_at, "updated_at")
        if updated_at < created_at:
            raise ValueError("updated_at must not precede created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        if self.genres is not None:
            if any(type(genre) is not str for genre in self.genres):
                raise ValueError("genres must contain only strings")
            object.__setattr__(self, "genres", tuple(self.genres))

    @classmethod
    def new_local(
        cls,
        canonical_title: str,
        display_title: str,
        *,
        description: str | None = None,
        status: str | None = None,
        language: str | None = None,
        author: str | None = None,
        genres: tuple[str, ...] | None = None,
        series_id: UUID | None = None,
        now: datetime | None = None,
    ) -> Series:
        created_at = now if now is not None else datetime.now(UTC)
        return cls(
            series_id=series_id if series_id is not None else uuid4(),
            canonical_title=canonical_title,
            display_title=display_title,
            description=description,
            status=status,
            language=language,
            cover_asset_id=None,
            created_at=created_at,
            updated_at=created_at,
            author=author,
            genres=genres,
        )
