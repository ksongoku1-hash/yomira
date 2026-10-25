"""Public discovery metadata. Never imports a work or changes reading progress."""

from typing import cast

from fastapi import APIRouter, HTTPException, Request

from app.connectors.mangadex import MANGADEX_METADATA, MangaDexConnector
from app.runtime import RuntimeState
from app.services.recommendations import RecommendationFeed

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])


@router.get("", response_model=RecommendationFeed)
async def recommendations(request: Request) -> RecommendationFeed:
    runtime = cast(RuntimeState, request.app.state.runtime)
    if runtime.recommendations_service is None or runtime.connector_registry is None:
        raise HTTPException(503, "Discovery service is not initialized.")
    connector = runtime.connector_registry.resolve(MANGADEX_METADATA.connector_id)
    if not isinstance(connector, MangaDexConnector):
        raise HTTPException(503, "Discovery provider is unavailable.")
    return await runtime.recommendations_service.get(connector)
