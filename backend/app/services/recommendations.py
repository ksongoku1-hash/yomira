"""A daily, bounded public ranking, cached independently of the personal library."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal
from uuid import UUID

from pydantic import UUID4, BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.connectors.errors import SourceConnectorError
from app.connectors.mangadex import MangaDexConnector

Text = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f]+$")]
_DAY = timedelta(days=1)
_STALE = timedelta(days=7)
_COOLDOWN = timedelta(minutes=15)
_MAX_CACHE_BYTES = 160 * 1024


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remote_id: UUID4
    title: Text
    aliases: list[Text] = Field(max_length=6)
    genres: list[Text] = Field(max_length=64)
    cover_url: str | None = Field(
        default=None,
        pattern=(
            r"^https://uploads\.mangadex\.org/covers/[0-9a-f-]{36}/[0-9a-f-]{36}"
            r"\.(?:jpg|jpeg|png|webp)$"
        ),
    )


class SavedRecommendations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["MangaDex"] = "MangaDex"
    updated_at: datetime
    data: list[Recommendation] = Field(max_length=24)

    @field_validator("updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Cache timestamps require a timezone")
        return value


class RecommendationMeta(BaseModel):
    provider: Literal["MangaDex"] = "MangaDex"
    criterion: Literal["followed_count"] = "followed_count"
    status: Literal["ready", "stale", "unavailable"]
    updated_at: datetime | None = None
    retry_at: datetime | None = None


class RecommendationFeed(BaseModel):
    data: list[Recommendation]
    meta: RecommendationMeta


class RecommendationsService:
    def __init__(self, cache_path: Path, *, clock: Callable[[], datetime] | None = None):
        self._cache_path = cache_path
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self._retry_at: datetime | None = None
        self._saved: SavedRecommendations | None = None
        try:
            if cache_path.is_file() and not cache_path.is_symlink():
                with cache_path.open("rb") as file:
                    content = file.read(_MAX_CACHE_BYTES + 1)
                if len(content) <= _MAX_CACHE_BYTES:
                    saved = SavedRecommendations.model_validate_json(content)
                    if self._clock() - _STALE <= saved.updated_at <= self._clock():
                        self._saved = saved
        except OSError, ValidationError, ValueError:
            pass

    async def get(self, connector: MangaDexConnector) -> RecommendationFeed:
        async with self._lock:
            now = self._clock()
            if self._saved is not None and now - _DAY <= self._saved.updated_at <= now:
                return self._response(now)
            if self._retry_at is not None and now < self._retry_at:
                return self._response(now)
            try:
                async with asyncio.timeout(8):
                    candidates = await connector.popular()
                saved = SavedRecommendations(
                    updated_at=now,
                    data=[
                        Recommendation(
                            remote_id=UUID(item.remote_id),
                            title=item.title[:200],
                            aliases=list(
                                dict.fromkeys(
                                    title
                                    for title in item.aliases
                                    if title != item.title and 0 < len(title) <= 200
                                )
                            )[:6],
                            genres=[genre[:200] for genre in item.metadata.genres or ()],
                            cover_url=item.metadata.cover_url,
                        )
                        for item in candidates
                    ],
                )
            except SourceConnectorError, TimeoutError, ValidationError, ValueError:
                self._retry_at = now + _COOLDOWN
                return self._response(now)
            self._saved = saved
            self._retry_at = None
            try:
                content = saved.model_dump_json().encode("utf-8")
                if len(content) <= _MAX_CACHE_BYTES:
                    self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                    with NamedTemporaryFile(
                        dir=self._cache_path.parent, suffix=".tmp", delete=False
                    ) as file:
                        file.write(content)
                        temporary = Path(file.name)
                    temporary.replace(self._cache_path)
            except OSError:
                pass
            return self._response(now)

    def _response(self, now: datetime) -> RecommendationFeed:
        saved = self._saved
        if saved is None or saved.updated_at < now - _STALE or saved.updated_at > now:
            return RecommendationFeed(
                data=[], meta=RecommendationMeta(status="unavailable", retry_at=self._retry_at)
            )
        return RecommendationFeed(
            data=saved.data,
            meta=RecommendationMeta(
                status="stale" if saved.updated_at < now - _DAY else "ready",
                updated_at=saved.updated_at,
                retry_at=self._retry_at,
            ),
        )
