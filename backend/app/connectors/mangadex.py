"""Bounded public MangaDex metadata catalog; it cannot retrieve chapters or pages."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import NoReturn, cast
from urllib.parse import urlencode
from uuid import UUID

from app.connectors.chapter import ConnectorChapter
from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.http_client import ConnectorHttpClient
from app.connectors.metadata import ConnectorCapability, ConnectorMetadata
from app.connectors.pages import ConnectorPageReference, PageFetchRequest
from app.connectors.search import ConnectorSearchQuery, ConnectorSearchResult
from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.domain.source import SourceHealthStatus
from app.errors import ErrorCode
from app.version import APP_VERSION

_API = "https://api.mangadex.org"
_UPLOADS = "https://uploads.mangadex.org"
MANGADEX_NETWORK_ORIGINS = frozenset({_API, _UPLOADS})
MANGADEX_COVER_ONLY_ORIGINS = frozenset({_UPLOADS})
MANGADEX_USER_AGENT = f"Yomira/{APP_VERSION} (local manga metadata)"
MANGADEX_METADATA = ConnectorMetadata(
    connector_id="yomira.metadata.mangadex",
    connector_version="1.0.0",
    capabilities=frozenset({ConnectorCapability.SEARCH, ConnectorCapability.METADATA}),
)
_MAX_RESULTS = 100
# Related editions and spin-offs can exceed 100 links on one public series.
_MAX_RELATIONSHIPS = 256
_MAX_ALIASES = 256
_MAX_LOCALIZATIONS = 64
_MAX_TAGS = 64
_MAX_TEXT = 1024
_MAX_DESCRIPTION = 100_000
_SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "de",
        "del",
        "el",
        "la",
        "las",
        "los",
        "no",
        "of",
        "on",
        "the",
        "y",
    }
)
_COVER_FILE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r"\.(?:jpg|jpeg|png|webp)"
)
_COVER_URL = re.compile(
    r"https://uploads\.mangadex\.org/covers/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r"\.(?:jpg|jpeg|png|webp)"
)


@dataclass(frozen=True, slots=True)
class MangaDexCandidate:
    remote_id: str
    title: str
    aliases: tuple[str, ...]
    year: int | None
    metadata: ConnectorSeriesMetadata

    @property
    def remote_url(self) -> str:
        return f"https://mangadex.org/title/{self.remote_id}"


@dataclass(frozen=True, slots=True)
class MangaDexLookup:
    total: int
    candidates: tuple[MangaDexCandidate, ...]


class MangaDexConnector:
    """Searches public catalog data through the SDK's exact-origin, cookie-free client."""

    def __init__(self, http_client: ConnectorHttpClient) -> None:
        if not callable(getattr(http_client, "get_json", None)):
            raise TypeError("http_client must provide bounded get_json access")
        self._http_client = http_client

    @property
    def metadata(self) -> ConnectorMetadata:
        return MANGADEX_METADATA

    async def health_check(self) -> SourceHealthStatus:
        try:
            data = await self._get_json(
                f"{_API}/manga?limit=1",
                SourceConnectorOperation.HEALTH_CHECK,
            )
            if data.get("result") != "ok" or data.get("response") != "collection":
                return SourceHealthStatus.DEGRADED
        except SourceConnectorError:
            return SourceHealthStatus.DEGRADED
        return SourceHealthStatus.HEALTHY

    async def lookup(self, query: ConnectorSearchQuery) -> MangaDexLookup:
        if type(query) is not ConnectorSearchQuery:
            raise TypeError("query must be a ConnectorSearchQuery")
        if not query.query.strip() or len(query.query) > _MAX_TEXT:
            _raise(ErrorCode.SRC_CONNECTOR_FAILED, SourceConnectorOperation.SEARCH)
        params = urlencode(
            [
                ("title", query.query),
                ("limit", str(_MAX_RESULTS)),
                ("offset", "0"),
                ("includes[]", "cover_art"),
                ("includes[]", "author"),
            ]
        )
        operation = SourceConnectorOperation.SEARCH
        data = await self._get_json(f"{_API}/manga?{params}", operation)
        total = data.get("total")
        if (
            data.get("result") != "ok"
            or data.get("response") != "collection"
            or type(total) is not int
            or not 0 <= total <= 2**31 - 1
            or type(data.get("limit")) is not int
            or data.get("limit") != _MAX_RESULTS
            or type(data.get("offset")) is not int
            or data.get("offset") != 0
        ):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        items = _array(data.get("data"), _MAX_RESULTS, operation)
        if len(items) != min(total, _MAX_RESULTS):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        candidates = tuple(_candidate(item, operation) for item in items)
        if len({item.remote_id for item in candidates}) != len(candidates):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        return MangaDexLookup(total, candidates)

    async def search(self, query: ConnectorSearchQuery) -> tuple[ConnectorSearchResult, ...]:
        result = await self.lookup(query)
        return tuple(
            ConnectorSearchResult(
                item.remote_id,
                item.remote_url,
                item.title,
                cover_url=item.metadata.cover_url,
                source_label="MangaDex",
            )
            for item in result.candidates
            if _matches_search_title(query.query, item)
        )

    async def get_series(self, remote_series_reference: str) -> Mapping[str, object]:
        operation = SourceConnectorOperation.GET_SERIES
        if not _valid_uuid(remote_series_reference):
            _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation)
        params = urlencode([("includes[]", "cover_art"), ("includes[]", "author")])
        data = await self._get_json(f"{_API}/manga/{remote_series_reference}?{params}", operation)
        if data.get("result") != "ok" or data.get("response") != "entity":
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        candidate = _candidate(data.get("data"), operation)
        if candidate.remote_id != remote_series_reference:
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        return asdict(candidate.metadata)

    async def get_chapters(self, remote_series_reference: str) -> tuple[ConnectorChapter, ...]:
        _raise(ErrorCode.SRC_CAPABILITY_UNSUPPORTED, SourceConnectorOperation.GET_CHAPTERS)

    async def get_chapter(self, remote_chapter_reference: str) -> ConnectorChapter:
        _raise(ErrorCode.SRC_CAPABILITY_UNSUPPORTED, SourceConnectorOperation.GET_CHAPTER)

    async def get_pages(self, remote_chapter_reference: str) -> tuple[ConnectorPageReference, ...]:
        _raise(ErrorCode.SRC_CAPABILITY_UNSUPPORTED, SourceConnectorOperation.GET_PAGES)

    def resolve_page(self, page_reference: ConnectorPageReference) -> PageFetchRequest:
        _raise(ErrorCode.SRC_CAPABILITY_UNSUPPORTED, SourceConnectorOperation.RESOLVE_PAGE)

    def normalize_metadata(self, raw_metadata: Mapping[str, object]) -> ConnectorSeriesMetadata:
        operation = SourceConnectorOperation.NORMALIZE_METADATA
        raw = _object(raw_metadata, operation)
        cover = _text(raw.get("cover_url"), _MAX_TEXT, operation)
        if cover is not None and _COVER_URL.fullmatch(cover) is None:
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        genres = raw.get("genres")
        return ConnectorSeriesMetadata(
            title=_text(raw.get("title"), _MAX_TEXT, operation),
            cover_url=cover,
            author=_text(raw.get("author"), _MAX_TEXT, operation),
            description=_text(raw.get("description"), _MAX_DESCRIPTION, operation),
            genres=tuple(
                text
                for item in _array(genres, _MAX_TAGS, operation)
                if (text := _text(item, _MAX_TEXT, operation)) is not None
            )
            if genres is not None
            else None,
            status=_text(raw.get("status"), _MAX_TEXT, operation),
            language=_text(raw.get("language"), _MAX_TEXT, operation),
        )

    async def _get_json(
        self,
        url: str,
        operation: SourceConnectorOperation,
    ) -> Mapping[str, object]:
        response = await self._http_client.get_json(
            url,
            operation=operation,
            user_agent=MANGADEX_USER_AGENT,
        )
        if response.status_code != 200:
            _raise(
                ErrorCode.SRC_CONNECTOR_FAILED,
                operation,
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        if response.content_type != "application/json" or response.final_url != url:
            _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation)
        try:
            data: object = json.loads(
                response.body.decode("utf-8-sig", errors="strict"),
                parse_constant=_reject_constant,
                parse_float=_finite_float,
                object_pairs_hook=_unique_object,
            )
        except (UnicodeDecodeError, ValueError, RecursionError):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        return _object(data, operation)


def _raise(
    code: ErrorCode,
    operation: SourceConnectorOperation,
    *,
    retryable: bool = False,
) -> NoReturn:
    raise SourceConnectorError(code=code, operation=operation, retryable=retryable)


def _valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def _object(value: object, operation: SourceConnectorOperation) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    mapping = cast(Mapping[object, object], value)
    if len(mapping) > 128 or any(not isinstance(key, str) for key in mapping):
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    return cast(Mapping[str, object], mapping)


def _array(value: object, limit: int, operation: SourceConnectorOperation) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    sequence = cast(Sequence[object], value)
    if len(sequence) > limit:
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    return sequence


def _text(value: object, limit: int, operation: SourceConnectorOperation) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    return value.strip() or None


def _localized(
    value: object,
    limit: int,
    operation: SourceConnectorOperation,
) -> tuple[str, ...]:
    if value is None:
        return ()
    mapping = _object(value, operation)
    if len(mapping) > _MAX_LOCALIZATIONS:
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    cleaned: dict[str, str] = {}
    for language, text in mapping.items():
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", language):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        parsed = _text(text, limit, operation)
        if parsed is not None:
            cleaned[language] = parsed
    order = ["es", "en", *sorted(cleaned.keys() - {"es", "en"})]
    return tuple(dict.fromkeys(cleaned[key] for key in order if key in cleaned))


def _candidate(value: object, operation: SourceConnectorOperation) -> MangaDexCandidate:
    item = _object(value, operation)
    remote_id = item.get("id")
    if not _valid_uuid(remote_id) or item.get("type") != "manga":
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    remote_id = cast(str, remote_id)
    attrs = _object(item.get("attributes"), operation)
    titles = _localized(attrs.get("title"), _MAX_TEXT, operation)
    if not titles:
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    aliases = list(titles[1:])
    for alias in _array(attrs.get("altTitles", []), _MAX_ALIASES, operation):
        aliases.extend(_localized(alias, _MAX_TEXT, operation))
    aliases_tuple = tuple(dict.fromkeys(alias for alias in aliases if alias != titles[0]))
    if len(aliases_tuple) > _MAX_ALIASES:
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    year = attrs.get("year")
    if year is not None and (type(year) is not int or not 1 <= year <= 9999):
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    genres: list[str] = []
    for tag in _array(attrs.get("tags", []), _MAX_TAGS, operation):
        tag_attrs = _object(_object(tag, operation).get("attributes"), operation)
        labels = _localized(tag_attrs.get("name"), _MAX_TEXT, operation)
        if not labels:
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        genres.append(labels[0])
    authors: list[str] = []
    cover: str | None = None
    for relation in _array(item.get("relationships", []), _MAX_RELATIONSHIPS, operation):
        related = _object(relation, operation)
        if not _valid_uuid(related.get("id")) or not isinstance(related.get("type"), str):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        related_attrs = related.get("attributes")
        if related_attrs is None or related.get("type") not in {"author", "cover_art"}:
            continue
        related_attrs = _object(related_attrs, operation)
        if related["type"] == "author":
            name = _text(related_attrs.get("name"), _MAX_TEXT, operation)
            if name is not None:
                authors.append(name)
        else:
            filename = related_attrs.get("fileName")
            if not isinstance(filename, str) or _COVER_FILE.fullmatch(filename) is None or cover:
                _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
            cover = f"{_UPLOADS}/covers/{remote_id}/{filename}"
    author = ", ".join(dict.fromkeys(authors)) or None
    descriptions = _localized(attrs.get("description"), _MAX_DESCRIPTION, operation)
    return MangaDexCandidate(
        remote_id,
        titles[0],
        aliases_tuple,
        year,
        ConnectorSeriesMetadata(
            title=titles[0],
            cover_url=cover,
            author=_text(author, _MAX_TEXT, operation),
            description=descriptions[0] if descriptions else None,
            genres=tuple(dict.fromkeys(genres)) or None,
            status=_text(attrs.get("status"), _MAX_TEXT, operation),
            language=_text(attrs.get("originalLanguage"), _MAX_TEXT, operation),
        ),
    )


def _matches_search_title(query: str, candidate: MangaDexCandidate) -> bool:
    query_tokens = _search_title_tokens(query)
    titles = (candidate.title, *candidate.aliases)
    title_tokens = tuple(_search_title_tokens(title) for title in titles)
    if not query_tokens:
        return any(query.strip().casefold() == title.strip().casefold() for title in titles)
    return any(query_tokens <= tokens for tokens in title_tokens)


def _search_title_tokens(value: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    unaccented = "".join(char for char in normalized if not unicodedata.combining(char))
    tokens = re.findall(r"[^\W_]+", unaccented, flags=re.UNICODE)
    return frozenset(token for token in tokens if token not in _SEARCH_STOPWORDS)


def _reject_constant(value: str) -> NoReturn:
    raise ValueError("Nonfinite JSON value")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Nonfinite JSON value")
    return parsed


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs) or len(result) > 128:
        raise ValueError("Repeated or excessive JSON object keys")
    return result
