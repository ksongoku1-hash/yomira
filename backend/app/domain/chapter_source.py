from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.timestamps import normalise_utc


@dataclass(frozen=True, slots=True)
class ChapterSource:
    chapter_source_id: UUID
    chapter_id: UUID
    source_id: UUID
    remote_id: str | None
    remote_url: str | None
    remote_label: str | None
    available: bool
    last_seen_at: datetime | None

    def __post_init__(self) -> None:
        for field_name in ("chapter_source_id", "chapter_id", "source_id"):
            if getattr(self, field_name).version != 4:
                raise ValueError(f"{field_name} must be a UUIDv4")

        if self.remote_id is not None and not self.remote_id:
            raise ValueError("remote_id must not be empty when provided")
        if self.remote_url is not None and not self.remote_url:
            raise ValueError("remote_url must not be empty when provided")
        if self.remote_id is None and self.remote_url is None:
            raise ValueError("remote_id or remote_url must be provided")
        if type(self.available) is not bool:
            raise ValueError("available must be a boolean")

        if self.last_seen_at is not None:
            object.__setattr__(
                self,
                "last_seen_at",
                normalise_utc(self.last_seen_at, "last_seen_at"),
            )

    @classmethod
    def new_local(
        cls,
        chapter_id: UUID,
        source_id: UUID,
        *,
        remote_id: str | None = None,
        remote_url: str | None = None,
        remote_label: str | None = None,
        available: bool,
        last_seen_at: datetime | None = None,
        chapter_source_id: UUID | None = None,
    ) -> ChapterSource:
        return cls(
            chapter_source_id=(
                chapter_source_id if chapter_source_id is not None else uuid4()
            ),
            chapter_id=chapter_id,
            source_id=source_id,
            remote_id=remote_id,
            remote_url=remote_url,
            remote_label=remote_label,
            available=available,
            last_seen_at=last_seen_at,
        )
