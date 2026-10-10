"""Conservative, source-provenanced fallback to the MangaDex public catalog."""

import asyncio
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.mangadex import (
    MANGADEX_METADATA,
    MangaDexCandidate,
    MangaDexConnector,
    MangaDexLookup,
)
from app.connectors.metadata import ConnectorCapability, ConnectorMetadata
from app.connectors.registry import ConnectorRegistry
from app.connectors.search import ConnectorSearchQuery
from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.domain.metadata import resolve_series_metadata
from app.domain.series_source import SeriesSource
from app.domain.source import Source, SourceHealthStatus
from app.errors import ErrorCode
from app.fmd2_bridge import FMD2_METADATA
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.series_source import series_source_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.series_source import SeriesSourceRow
from app.persistence.models.source import SourceRow
from app.persistence.repositories.metadata import MetadataRepository
from app.persistence.repositories.series import SeriesRepository
from app.tracing import current_trace_id

logger = logging.getLogger("yomira.metadata.external")
_YEAR = re.compile(r"\s*\(([0-9]{4})\)\s*$")
_CATALOG_ID = MANGADEX_METADATA.connector_id
_NEGATIVE_TTL_SECONDS = 600
_MAX_NEGATIVE_LOOKUPS = 256


class MetadataAssociationConflict(RuntimeError):
    def __init__(self) -> None:
        super().__init__("La ficha externa ya está vinculada; se conservan las asociaciones.")


def _title_key(title: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", title).casefold().split())


def _query_title(title: str) -> tuple[str, int | None]:
    match = _YEAR.search(title)
    if match is None:
        return title.strip(), None
    return title[: match.start()].strip(), int(match.group(1))


def match_candidate(title: str, candidate: MangaDexCandidate) -> bool:
    query, year = _query_title(title)
    return (
        bool(query)
        and (year is None or candidate.year == year)
        and any(
            _title_key(query) == _title_key(value)
            for value in (candidate.title, *candidate.aliases)
        )
    )


class ExternalMetadataService:
    def __init__(self, database: SQLiteDatabase, registry: ConnectorRegistry) -> None:
        self._database = database
        self._registry = registry
        self._series = SeriesRepository()
        self._metadata = MetadataRepository()
        # ponytail: 256 negative lookups for ten minutes; manual search bypasses cooldown.
        self._negative: OrderedDict[UUID, float] = OrderedDict()
        # ponytail: share 256 title matches across search cards for ten minutes.
        self._matches: OrderedDict[str, tuple[float, MangaDexCandidate | None]] = OrderedDict()

    def _enabled(self) -> bool:
        if _CATALOG_ID not in self._registry.connector_ids:
            return False
        with self._database.read_session() as session:
            rows = tuple(
                session.scalars(select(SourceRow).where(SourceRow.connector_id == _CATALOG_ID))
            )
            return not rows or (len(rows) == 1 and rows[0].enabled)

    def _connector(self) -> MangaDexConnector:
        if not self._enabled():
            raise SourceConnectorError(
                code=ErrorCode.SRC_CIRCUIT_OPEN, operation=SourceConnectorOperation.GET_SERIES
            )
        connector = self._registry.resolve(
            _CATALOG_ID, required_capability=ConnectorCapability.METADATA
        )
        if not isinstance(connector, MangaDexConnector):
            raise TypeError("Catalog registration must provide MangaDexConnector")
        return connector

    def _missing_title(self, series_id: UUID) -> str | None:
        with self._database.read_session() as session:
            series = self._series.get(session, series_id)
            if series is None:
                return None
            sources = self._metadata.list_series_sources(session, series_id)
            if any(s.source_connector_id == _CATALOG_ID for s in sources):
                return None
            resolutions = resolve_series_metadata(
                series, sources, self._metadata.list_overrides(session, series_id)
            )
            if not any(
                r.value is None
                and not r.has_user_override
                and r.authority is None
                and not r.source_conflict
                for r in resolutions
            ):
                return None
            return series.canonical_title

    async def find_candidates(self, series_id: UUID) -> MangaDexLookup:
        with self._database.read_session() as session:
            series = self._series.get(session, series_id)
        if series is None:
            raise LookupError("Local series was not found")
        query, _year = _query_title(series.canonical_title)
        return await self._connector().lookup(ConnectorSearchQuery(query))

    async def ensure_missing(self, series_id: UUID) -> bool:
        if not self._enabled() or self._negative.get(series_id, 0) > time.monotonic():
            return False
        title = self._missing_title(series_id)
        if title is None:
            return False
        self._negative[series_id] = time.monotonic() + _NEGATIVE_TTL_SECONDS
        self._negative.move_to_end(series_id)
        if len(self._negative) > _MAX_NEGATIVE_LOOKUPS:
            self._negative.popitem(last=False)
        try:
            candidate = await self._matching_candidate(title)
            if candidate is None:
                logger.info(
                    "metadata.external_match_unresolved",
                    extra={
                        "series_id": str(series_id),
                        "stage": "external_lookup",
                        "trace_id": current_trace_id(),
                    },
                )
                return False
            return self.save_candidate(series_id, candidate)
        except (SourceConnectorError, MetadataAssociationConflict, ValueError) as error:
            logger.warning(
                "metadata.external_fallback_unavailable",
                extra={
                    "series_id": str(series_id),
                    "stage": "external_lookup",
                    "trace_id": current_trace_id(),
                    "error_code": error.code.value
                    if isinstance(error, SourceConnectorError)
                    else ErrorCode.API_REQUEST_REJECTED.value,
                    "cause_type": type(error).__name__,
                },
            )
            return False

    async def _matching_candidate(self, title: str) -> MangaDexCandidate | None:
        key = _title_key(title)
        now = time.monotonic()
        cached = self._matches.get(key)
        if cached is not None and cached[0] > now:
            self._matches.move_to_end(key)
            return cached[1]
        self._matches[key] = (now + _NEGATIVE_TTL_SECONDS, None)
        self._matches.move_to_end(key)
        if len(self._matches) > _MAX_NEGATIVE_LOOKUPS:
            self._matches.popitem(last=False)
        query, _year = _query_title(title)
        lookup = await self._connector().lookup(ConnectorSearchQuery(query))
        matches = tuple(c for c in lookup.candidates if match_candidate(title, c))
        if lookup.total > 100 or len(lookup.candidates) != lookup.total or len(matches) != 1:
            return None
        self._matches[key] = (now + _NEGATIVE_TTL_SECONDS, matches[0])
        return matches[0]

    async def cover_for_title(self, title: str) -> str | None:
        """Preview a unique catalog cover without creating a library association."""
        if not title.strip() or len(title) > 200 or not self._enabled():
            return None
        try:
            candidate = await self._matching_candidate(title)
            return candidate.metadata.cover_url if candidate is not None else None
        except SourceConnectorError, ValueError:
            return None

    async def ensure_source_missing(self, series_id: UUID) -> bool:
        """Link a unique exact source ficha only for an unbound local import."""
        connector_id = FMD2_METADATA.connector_id
        if connector_id not in self._registry.connector_ids:
            return False
        with self._database.read_session() as session:
            series = self._series.get(session, series_id)
            associations = self._metadata.list_series_sources(session, series_id)
            local = session.scalar(
                select(ChapterRow.chapter_id)
                .join(ChapterSourceRow, ChapterSourceRow.chapter_id == ChapterRow.chapter_id)
                .join(SourceRow, SourceRow.source_id == ChapterSourceRow.source_id)
                .where(
                    ChapterRow.series_id == str(series_id),
                    SourceRow.connector_id == "yomira.local.import",
                )
                .limit(1)
            )
        if (
            series is None
            or local is None
            or series.cover_asset_id is not None
            or any(source.source_connector_id == connector_id for source in associations)
        ):
            return False
        title, year = _query_title(series.canonical_title)
        # Source fichas do not provide a reliable year to distinguish dated editions.
        if year is not None or not 1 <= len(title) <= 200:
            return False
        connector = self._registry.resolve(
            connector_id, required_capability=ConnectorCapability.METADATA
        )
        try:
            async with asyncio.timeout(45):
                matches = {
                    item.remote_id: item
                    for item in await connector.search(ConnectorSearchQuery(title, page_budget=1))
                    if item.remote_id
                    and item.remote_title
                    and _title_key(item.remote_title) == _title_key(title)
                }
                if len(matches) != 1:
                    return False
                remote_id, result = next(iter(matches.items()))
                metadata = connector.normalize_metadata(await connector.get_series(remote_id))
                if (
                    not metadata.title
                    or _title_key(metadata.title) != _title_key(title)
                    or (series.language and metadata.language != series.language)
                    or (
                        series.author
                        and (
                            not metadata.author
                            or _title_key(metadata.author) != _title_key(series.author)
                        )
                    )
                ):
                    return False
                return self._save_metadata(
                    series_id, FMD2_METADATA, remote_id, result.remote_url, metadata
                )
        except SourceConnectorError, MetadataAssociationConflict, ValueError, TimeoutError:
            logger.info("metadata.source_match_unresolved", extra={"series_id": str(series_id)})
            return False

    async def apply(self, series_id: UUID, remote_id: str) -> None:
        connector = self._connector()
        raw = await connector.get_series(remote_id)
        metadata = connector.normalize_metadata(raw)
        candidate = MangaDexCandidate(
            remote_id=remote_id,
            title=metadata.title or "",
            aliases=(),
            year=None,
            metadata=metadata,
        )
        self.save_candidate(series_id, candidate)

    def save_candidate(self, series_id: UUID, candidate: MangaDexCandidate) -> bool:
        return self._save_metadata(
            series_id,
            MANGADEX_METADATA,
            candidate.remote_id,
            candidate.remote_url,
            candidate.metadata,
        )

    def _save_metadata(
        self,
        series_id: UUID,
        connector: ConnectorMetadata,
        remote_id: str,
        remote_url: str | None,
        metadata: ConnectorSeriesMetadata,
    ) -> bool:
        """Serialize identity binding with SQLite's writer; never replace a binding."""
        with self._database.write_session() as session:
            if self._series.get(session, series_id) is None:
                raise LookupError("Local series was not found")
            rows = tuple(
                session.scalars(
                    select(SourceRow).where(SourceRow.connector_id == connector.connector_id)
                )
            )
            if len(rows) > 1:
                raise MetadataAssociationConflict()
            if rows and not rows[0].enabled:
                raise SourceConnectorError(
                    code=ErrorCode.SRC_CIRCUIT_OPEN, operation=SourceConnectorOperation.GET_SERIES
                )
            if rows:
                source_id = UUID(rows[0].source_id)
            else:
                source = Source.new_local(
                    connector.connector_id,
                    connector.connector_version,
                    "MangaDex (metadatos)"
                    if connector == MANGADEX_METADATA
                    else "Free Manga (metadatos)",
                    True,
                    SourceHealthStatus.HEALTHY,
                )
                session.add(source_to_row(source))
                session.flush()
                source_id = source.source_id
            associations = self._metadata.list_series_sources(session, series_id)
            existing = next(
                (s for s in associations if s.source_connector_id == connector.connector_id), None
            )
            if existing is not None:
                if existing.remote_id != remote_id:
                    raise MetadataAssociationConflict()
                return False
            bound = session.scalar(
                select(SeriesSourceRow).where(
                    SeriesSourceRow.source_id == str(source_id),
                    SeriesSourceRow.remote_id == remote_id,
                )
            )
            if bound is not None:
                raise MetadataAssociationConflict()
            association = SeriesSource.new_local(
                series_id,
                source_id,
                remote_id=remote_id,
                remote_url=remote_url,
                remote_title=metadata.title,
                remote_cover_url=metadata.cover_url,
                remote_author=metadata.author,
                remote_description=metadata.description,
                remote_genres=metadata.genres,
                remote_status=metadata.status,
                remote_language=metadata.language,
                last_checked_at=datetime.now(UTC),
            )
            session.add(series_source_to_row(association))
        logger.info(
            "metadata.external_association_created",
            extra={
                "series_id": str(series_id),
                "source_id": str(source_id),
                "stage": "external_association",
                "trace_id": current_trace_id(),
            },
        )
        return True
