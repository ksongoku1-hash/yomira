from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.timestamps import normalise_utc


@dataclass(frozen=True, slots=True)
class SeriesSource:
    series_source_id: UUID
    series_id: UUID
    source_id: UUID
    remote_id: str | None
    remote_url: str | None
    remote_title: str | None
    last_checked_at: datetime | None
    remote_cover_url: str | None = None
    remote_author: str | None = None
    remote_description: str | None = None
    remote_genres: tuple[str, ...] | None = None
    remote_status: str | None = None
    remote_language: str | None = None
    source_display_name: str | None = None
    source_connector_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("series_source_id", "series_id", "source_id"):
            if getattr(self, field_name).version != 4:
                raise ValueError(f"{field_name} must be a UUIDv4")

        if self.remote_id is not None and not self.remote_id:
            raise ValueError("remote_id must not be empty when provided")
        if self.remote_url is not None and not self.remote_url:
            raise ValueError("remote_url must not be empty when provided")
        if self.remote_id is None and self.remote_url is None:
            raise ValueError("remote_id or remote_url must be provided")

        if self.last_checked_at is not None:
            object.__setattr__(
                self,
                "last_checked_at",
                normalise_utc(self.last_checked_at, "last_checked_at"),
            )
        if self.remote_genres is not None:
            if any(type(genre) is not str for genre in self.remote_genres):
                raise ValueError("remote_genres must contain only strings")
            object.__setattr__(self, "remote_genres", tuple(self.remote_genres))

    @classmethod
    def new_local(
        cls,
        series_id: UUID,
        source_id: UUID,
        *,
        remote_id: str | None = None,
        remote_url: str | None = None,
        remote_title: str | None = None,
        last_checked_at: datetime | None = None,
        series_source_id: UUID | None = None,
        remote_cover_url: str | None = None,
        remote_author: str | None = None,
        remote_description: str | None = None,
        remote_genres: tuple[str, ...] | None = None,
        remote_status: str | None = None,
        remote_language: str | None = None,
    ) -> SeriesSource:
        return cls(
            series_source_id=(series_source_id if series_source_id is not None else uuid4()),
            series_id=series_id,
            source_id=source_id,
            remote_id=remote_id,
            remote_url=remote_url,
            remote_title=remote_title,
            last_checked_at=last_checked_at,
            remote_cover_url=remote_cover_url,
            remote_author=remote_author,
            remote_description=remote_description,
            remote_genres=remote_genres,
            remote_status=remote_status,
            remote_language=remote_language,
        )
