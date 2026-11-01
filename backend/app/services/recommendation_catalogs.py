"""Bounded, public discovery feeds; no library history is sent upstream."""

import json
import re
from typing import Literal, cast

from app.connectors.errors import SourceConnectorOperation
from app.connectors.http_client import ConnectorHttpClient

PublicCatalog = Literal["AniList", "MyAnimeList"]
CATALOG_ORIGINS = {"AniList": "https://graphql.anilist.co", "MyAnimeList": "https://api.jikan.moe"}
ANILIST_QUERY = """{ Page(page:1,perPage:24) {
  media(type:MANGA,sort:TRENDING_DESC,isAdult:false) {
    id isAdult title { english romaji } synonyms genres coverImage { large }
  }
} }"""


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Invalid public catalog object")
    return cast(dict[str, object], value)


def _list(value: object, limit: int = 128) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("Invalid public catalog list")
    values = cast(list[object], value)
    if len(values) > limit:
        raise ValueError("Invalid public catalog list")
    return values


def _texts(value: object) -> list[str]:
    values = _list(value)
    if any(not isinstance(v, str) or len(v) > 2000 for v in values):
        raise ValueError("Invalid public catalog text")
    return [str(v).strip() for v in values if v and len(str(v).strip()) <= 200]


def _cover(value: object, provider: PublicCatalog) -> str | None:
    pattern = (
        r"https://s4\.anilist\.co/file/anilistcdn/media/manga/cover/(?:medium|large)/[A-Za-z0-9_.-]+"
        if provider == "AniList"
        else r"https://cdn\.myanimelist\.net/images/manga/[0-9]+/[0-9]+\.(?:jpg|jpeg|png|webp)"
    )
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


async def load_catalog(
    provider: PublicCatalog, client: ConnectorHttpClient
) -> list[dict[str, object]]:
    operation = SourceConnectorOperation.SEARCH
    if provider == "AniList":
        response = await client.post_json(
            CATALOG_ORIGINS[provider], {"query": ANILIST_QUERY}, operation=operation
        )
    else:
        response = await client.get_json(
            CATALOG_ORIGINS[provider] + "/v4/top/manga?filter=bypopularity&limit=24",
            operation=operation,
        )
    if response.status_code != 200:
        raise ValueError("Public catalog unavailable")
    payload = _object(json.loads(response.body))
    rows = (
        _list(_object(_object(payload.get("data")).get("Page")).get("media"), 24)
        if provider == "AniList"
        else _list(payload.get("data"), 24)
    )
    output: list[dict[str, object]] = []
    seen: set[int] = set()
    for row in rows:
        item = _object(row)
        identifier = item.get("id" if provider == "AniList" else "mal_id")
        if type(identifier) is not int or not 0 < identifier < 2**31 or identifier in seen:
            raise ValueError("Invalid public catalog ID")
        seen.add(identifier)
        if provider == "AniList":
            if item.get("isAdult") is not False:
                continue
            title_data = _object(item.get("title"))
            titles = _texts([title_data.get("english") or "", title_data.get("romaji") or ""])
            titles += _texts(item.get("synonyms", []))
            genres = _texts(item.get("genres", []))
            cover = _cover(_object(item.get("coverImage", {})).get("large"), provider)
        else:
            if _list(item.get("explicit_genres", [])):
                continue
            titles = _texts([item.get("title_english") or "", item.get("title") or ""])
            titles += _texts(item.get("title_synonyms", []))
            genres = _texts([_object(g).get("name") for g in _list(item.get("genres", []), 64)])
            cover = _cover(
                _object(_object(item.get("images", {})).get("jpg", {})).get("image_url"), provider
            )
        titles = list(dict.fromkeys(titles))
        if not titles:
            raise ValueError("Missing public catalog title")
        output.append(
            {
                "remote_id": f"{'anilist' if provider == 'AniList' else 'jikan'}:{identifier}",
                "title": titles[0],
                "aliases": titles[1:7],
                "genres": genres,
                "cover_url": cover,
            }
        )
    return output
