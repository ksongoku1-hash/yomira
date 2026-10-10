"""Recover a failed FMD chapter through another verified public source."""

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.chapter import ConnectorChapter
from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.registry import ConnectorRegistry
from app.connectors.search import ConnectorSearchQuery
from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.domain.chapter import chapter_number as _chapter_number
from app.domain.chapter_source import ChapterSource
from app.domain.download_job import DownloadJob, DownloadJobStatus, DownloadPriority
from app.domain.series_source import SeriesSource
from app.errors import ErrorCode
from app.fmd2_bridge import (
    FMD2_PUBLIC_SOURCES,
    fmd2_chapter_location,
    fmd2_series_source,
)
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.download_job import download_job_to_row
from app.persistence.mappers.series_source import series_source_to_row
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.download_job_archive import DownloadJobArchiveRow
from app.persistence.models.series_source import SeriesSourceRow
from app.persistence.models.source import SourceRow
from app.services.download_worker import DownloadWorkerResult

logger = logging.getLogger("yomira.chapter_fallback")
_CONNECTOR_ID = "yomira.fmd2.webtoons"
_SOURCE_FAILURES = frozenset(
    {
        ErrorCode.SRC_CONNECTOR_FAILED.value,
        ErrorCode.SRC_SCHEMA_CHANGED.value,
        "IMG_DECODE_FAILED",
        "IMG_MIME_MISMATCH",
        "IMG_MIME_UNSUPPORTED",
        "IMG_SIGNATURE_INVALID",
        "IMG_TRUNCATED",
    }
)


def _title_key(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "").casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def _authors(value: str) -> set[tuple[str, ...]]:
    return {
        tuple(sorted(_title_key(author).split()))
        for author in re.split(r"[/;,]", value)
        if _title_key(author)
    }


@dataclass(frozen=True)
class _FailedChapter:
    job_id: UUID
    chapter_id: UUID
    series_id: UUID
    source_id: UUID
    title: str
    author: str | None
    language: str
    number: Decimal
    tried_sources: frozenset[str]


class ChapterFallbackService:
    def __init__(self, database: SQLiteDatabase, registry: ConnectorRegistry) -> None:
        self._database = database
        self._registry = registry

    @staticmethod
    def eligible(result: DownloadWorkerResult) -> bool:
        return (
            result.job.status is DownloadJobStatus.FAILED
            and result.job.cancel_requested_at is None
            and result.error_code in _SOURCE_FAILURES
        )

    async def recover(self, result: DownloadWorkerResult) -> bool:
        if not self.eligible(result) or self._database.storage_gate.closed:
            return False
        context = await asyncio.to_thread(self._context, result.job.job_id)
        if context is None:
            return False
        connector = self._registry.resolve(_CONNECTOR_ID)
        try:
            async with asyncio.timeout(120):
                results = await connector.search(ConnectorSearchQuery(context.title))
                candidates: dict[str, set[str]] = {}
                for item in results:
                    if item.remote_id is None or _title_key(item.remote_title) != _title_key(
                        context.title
                    ):
                        continue
                    source = fmd2_series_source(item.remote_id)
                    if (
                        source in context.tried_sources
                        or FMD2_PUBLIC_SOURCES[source][2] != context.language
                    ):
                        continue
                    candidates.setdefault(source, set()).add(item.remote_id)
                for source, references in candidates.items():
                    # Multiple editions with the same title need a human choice.
                    if len(references) != 1:
                        continue
                    reference = next(iter(references))
                    try:
                        async with asyncio.timeout(20):
                            metadata = connector.normalize_metadata(
                                await connector.get_series(reference)
                            )
                            if (
                                _title_key(metadata.title) != _title_key(context.title)
                                or metadata.language != context.language
                                or (
                                    context.author
                                    and metadata.author
                                    and not (_authors(context.author) & _authors(metadata.author))
                                )
                            ):
                                continue
                            chapters = await connector.get_chapters(reference)
                    except SourceConnectorError, TimeoutError:
                        continue
                    matches = [
                        chapter
                        for chapter in chapters
                        if _chapter_number(chapter.display_label) == context.number
                        and chapter.language == context.language
                        and chapter.chapter_variant is None
                        and chapter.chapter_type is None
                        and chapter.volume_number is None
                    ]
                    if len(matches) != 1 or matches[0].remote_id is None:
                        continue
                    recovered = await asyncio.to_thread(
                        self._enqueue,
                        context,
                        reference,
                        metadata,
                        matches[0],
                    )
                    if recovered:
                        logger.info(
                            "chapter.fallback.queued",
                            extra={
                                "job_id": str(context.job_id),
                                "stage": "alternate_source",
                                "source_label": FMD2_PUBLIC_SOURCES[source][0],
                            },
                        )
                        return True
        except SourceConnectorError, TimeoutError:
            logger.info(
                "chapter.fallback.unavailable",
                extra={
                    "job_id": str(context.job_id),
                    "stage": "alternate_source",
                },
            )
        return False

    @staticmethod
    def _attempts(
        session: Session,
        chapter_id: UUID,
    ) -> tuple[tuple[DownloadJobRow, str | None], ...]:
        return tuple(
            (job, reference)
            for job, reference in session.execute(
                select(DownloadJobRow, ChapterSourceRow.remote_id)
                .join(
                    ChapterSourceRow,
                    ChapterSourceRow.chapter_source_id == DownloadJobRow.chapter_source_id,
                )
                .where(ChapterSourceRow.chapter_id == str(chapter_id))
            ).all()
        )

    def _context(self, job_id: UUID) -> _FailedChapter | None:
        with self._database.read_session() as session:
            row = session.execute(
                select(DownloadJobRow, ChapterSourceRow, ChapterRow, SourceRow)
                .join(
                    ChapterSourceRow,
                    ChapterSourceRow.chapter_source_id == DownloadJobRow.chapter_source_id,
                )
                .join(ChapterRow, ChapterRow.chapter_id == ChapterSourceRow.chapter_id)
                .join(SourceRow, SourceRow.source_id == ChapterSourceRow.source_id)
                .where(DownloadJobRow.job_id == str(job_id))
            ).one_or_none()
            if row is None:
                return None
            job, source, chapter, connector = row
            number = _chapter_number(chapter.display_label)
            if (
                connector.connector_id != _CONNECTOR_ID
                or source.remote_id is None
                or job.status != "FAILED"
                or job.cancel_requested_at_us is not None
                or session.get(DownloadJobArchiveRow, str(job_id)) is not None
                or number is None
                or not chapter.language
                or chapter.chapter_variant is not None
                or chapter.chapter_type is not None
                or chapter.volume_number is not None
            ):
                return None
            manga_ref, _, _ = fmd2_chapter_location(source.remote_id)
            series_source = session.scalar(
                select(SeriesSourceRow).where(
                    SeriesSourceRow.source_id == source.source_id,
                    SeriesSourceRow.remote_id == manga_ref,
                    SeriesSourceRow.series_id == chapter.series_id,
                )
            )
            if series_source is None or not series_source.remote_title:
                return None
            attempts = self._attempts(session, UUID(chapter.chapter_id))
            if any(
                other.status not in {"FAILED", "CANCELLED"}
                or other.cancel_requested_at_us is not None
                for other, _ in attempts
            ):
                return None
            tried = frozenset(
                fmd2_chapter_location(reference)[1]
                for _, reference in attempts
                if reference is not None
            )
            return _FailedChapter(
                job_id,
                UUID(chapter.chapter_id),
                UUID(chapter.series_id),
                UUID(source.source_id),
                series_source.remote_title,
                series_source.remote_author,
                chapter.language,
                number,
                tried,
            )

    def _enqueue(
        self,
        context: _FailedChapter,
        remote_series_id: str,
        metadata: ConnectorSeriesMetadata,
        chapter: ConnectorChapter,
    ) -> bool:
        assert chapter.remote_id is not None
        manga_ref, website, _ = fmd2_chapter_location(chapter.remote_id)
        if manga_ref != remote_series_id:
            raise SourceConnectorError(
                code=ErrorCode.SRC_SCHEMA_CHANGED,
                operation=SourceConnectorOperation.GET_CHAPTERS,
            )
        observed_at = datetime.now(UTC)
        with self._database.write_session() as session:
            original = session.get(DownloadJobRow, str(context.job_id))
            if (
                original is None
                or original.status != "FAILED"
                or original.cancel_requested_at_us is not None
                or session.get(DownloadJobArchiveRow, str(context.job_id)) is not None
            ):
                return False
            for other, reference in self._attempts(session, context.chapter_id):
                if (
                    other.status not in {"FAILED", "CANCELLED"}
                    or other.cancel_requested_at_us is not None
                    or (reference and fmd2_chapter_location(reference)[1] == website)
                ):
                    return False
            series_source = session.scalar(
                select(SeriesSourceRow).where(
                    SeriesSourceRow.source_id == str(context.source_id),
                    SeriesSourceRow.remote_id == remote_series_id,
                )
            )
            if series_source is not None and series_source.series_id != str(context.series_id):
                return False
            existing = session.scalar(
                select(ChapterSourceRow).where(
                    ChapterSourceRow.source_id == str(context.source_id),
                    ChapterSourceRow.remote_id == chapter.remote_id,
                )
            )
            if existing is not None and existing.chapter_id != str(context.chapter_id):
                return False
            if series_source is None:
                session.add(
                    series_source_to_row(
                        SeriesSource.new_local(
                            context.series_id,
                            context.source_id,
                            remote_id=remote_series_id,
                            remote_title=metadata.title,
                            remote_author=metadata.author,
                            remote_cover_url=metadata.cover_url,
                            remote_description=metadata.description,
                            remote_genres=metadata.genres,
                            remote_status=metadata.status,
                            remote_language=metadata.language,
                            last_checked_at=observed_at,
                        )
                    )
                )
            if existing is None:
                alternative = ChapterSource.new_local(
                    context.chapter_id,
                    context.source_id,
                    remote_id=chapter.remote_id,
                    remote_url=chapter.remote_url,
                    remote_label=chapter.remote_label,
                    available=True,
                    last_seen_at=observed_at,
                )
                session.add(chapter_source_to_row(alternative))
                session.flush()
                chapter_source_id = alternative.chapter_source_id
            else:
                chapter_source_id = UUID(existing.chapter_source_id)
            session.add(
                download_job_to_row(
                    DownloadJob.new_local(
                        chapter_source_id,
                        priority=DownloadPriority.HIGH,
                        now=observed_at,
                    )
                )
            )
            session.add(
                DownloadJobArchiveRow(
                    job_id=str(context.job_id),
                    archived_at_us=datetime_to_epoch_us(observed_at),
                )
            )
            return True
