"""Public discovery metadata. Never imports a work or changes reading progress."""

from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Request

from app.connectors.mangadex import MANGADEX_METADATA, MangaDexConnector
from app.runtime import RuntimeState
from app.services.recommendation_catalogs import CATALOG_ORIGINS
from app.services.recommendations import RecommendationFeed

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])


@router.get("", response_model=RecommendationFeed)
async def recommendations(
    request: Request, provider: Literal["MangaDex", "AniList", "MyAnimeList"] = "MangaDex"
) -> RecommendationFeed:
    runtime = cast(RuntimeState, request.app.state.runtime)
    if runtime.recommendations_service is None or runtime.connector_registry is None:
        raise HTTPException(503, "Discovery service is not initialized.")
    if provider != "MangaDex":
        if runtime.connector_http_session is None:
            raise HTTPException(503, "Discovery transport is unavailable.")
        client = runtime.connector_http_session.scoped_client(
            frozenset({CATALOG_ORIGINS[provider]})
        )
        return await runtime.recommendations_service.get_public(provider, client)
    connector = runtime.connector_registry.resolve(MANGADEX_METADATA.connector_id)
    if not isinstance(connector, MangaDexConnector):
        raise HTTPException(503, "Discovery provider is unavailable.")
    return await runtime.recommendations_service.get(connector)
