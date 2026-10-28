"""A daily, bounded public ranking, cached independently of the personal library."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.connectors.errors import SourceConnectorError
from app.connectors.http_client import ConnectorHttpClient
from app.connectors.mangadex import MangaDexConnector
from app.services.recommendation_catalogs import PublicCatalog, load_catalog

Text = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f]+$")]
_DAY = timedelta(days=1)
_STALE = timedelta(days=7)
_COOLDOWN = timedelta(minutes=15)
_MAX_CACHE_BYTES = 160 * 1024


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remote_id: str = Field(
        pattern=r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}|(?:anilist|jikan):[1-9][0-9]{0,9})$"
    )
    title: Text
    aliases: list[Text] = Field(max_length=6)
    genres: list[Text] = Field(max_length=64)
    cover_url: str | None = Field(
        default=None,
        pattern=(
            r"^https://uploads\.mangadex\.org/covers/[0-9a-f-]{36}/[0-9a-f-]{36}"
            r"\.(?:jpg|jpeg|png|webp)$|^https://s4\.anilist\.co/file/anilistcdn/media/manga/cover/(?:medium|large)/[A-Za-z0-9_.-]+$|^https://cdn\.myanimelist\.net/images/manga/[0-9]+/[0-9]+\.(?:jpg|jpeg|png|webp)$"
        ),
    )


class SavedRecommendations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["MangaDex", "AniList", "MyAnimeList"] = "MangaDex"
    updated_at: datetime
    data: list[Recommendation] = Field(max_length=24)

    @field_validator("updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Cache timestamps require a timezone")
        return value


class RecommendationMeta(BaseModel):
    provider: Literal["MangaDex", "AniList", "MyAnimeList"] = "MangaDex"
    criterion: Literal["followed_count", "trending", "popularity"] = "followed_count"
    status: Literal["ready", "stale", "unavailable"]
    updated_at: datetime | None = None
    retry_at: datetime | None = None


class RecommendationFeed(BaseModel):
    data: list[Recommendation]
    meta: RecommendationMeta


class RecommendationsService:
    def __init__(
        self,
        cache_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        provider: Literal["MangaDex", "AniList", "MyAnimeList"] = "MangaDex",
    ):
        self._provider: Literal["MangaDex", "AniList", "MyAnimeList"] = provider
        self._other_feeds: dict[PublicCatalog, RecommendationsService] = {}
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
                    if (
                        saved.provider == provider
                        and self._clock() - _STALE <= saved.updated_at <= self._clock()
                    ):
                        self._saved = saved
        except OSError, ValidationError, ValueError:
            pass

    async def get(self, connector: MangaDexConnector) -> RecommendationFeed:
        async def fetch() -> list[Recommendation]:
            candidates = await connector.popular()
            return [
                Recommendation(
                    remote_id=item.remote_id,
                    title=item.title[:200],
                    aliases=list(
                        dict.fromkeys(
                            t for t in item.aliases if t != item.title and 0 < len(t) <= 200
                        )
                    )[:6],
                    genres=[g[:200] for g in item.metadata.genres or ()],
                    cover_url=item.metadata.cover_url,
                )
                for item in candidates
            ]

        return await self._get(fetch)

    async def get_public(
        self, provider: PublicCatalog, client: ConnectorHttpClient
    ) -> RecommendationFeed:
        if provider not in self._other_feeds:
            path = self._cache_path.with_stem(self._cache_path.stem + "-" + provider.lower())
            self._other_feeds[provider] = RecommendationsService(
                path, clock=self._clock, provider=provider
            )

        async def fetch() -> list[Recommendation]:
            return [
                Recommendation.model_validate(item) for item in await load_catalog(provider, client)
            ]

        return await self._other_feeds[provider]._get(fetch)

    async def _get(
        self, fetch: Callable[[], Awaitable[list[Recommendation]]]
    ) -> RecommendationFeed:
        async with self._lock:
            now = self._clock()
            if self._saved is not None and now - _DAY <= self._saved.updated_at <= now:
                return self._response(now)
            if self._retry_at is not None and now < self._retry_at:
                return self._response(now)
            try:
                async with asyncio.timeout(8):
                    items = await fetch()
                saved = SavedRecommendations(provider=self._provider, updated_at=now, data=items)
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
                data=[],
                meta=RecommendationMeta(
                    provider=self._provider,
                    criterion=self._criterion(),
                    status="unavailable",
                    retry_at=self._retry_at,
                ),
            )
        return RecommendationFeed(
            data=saved.data,
            meta=RecommendationMeta(
                provider=self._provider,
                criterion=self._criterion(),
                status="stale" if saved.updated_at < now - _DAY else "ready",
                updated_at=saved.updated_at,
                retry_at=self._retry_at,
            ),
        )

    def _criterion(self) -> Literal["followed_count", "trending", "popularity"]:
        if self._provider == "AniList":
            return "trending"
        return "popularity" if self._provider == "MyAnimeList" else "followed_count"
