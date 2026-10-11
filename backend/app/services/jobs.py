"""Durable job creation from a validated connector chapter selection."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import case, exists, func, select
from sqlalchemy.orm import Session, aliased

from app.connectors.chapter import ConnectorChapter
from app.connectors.metadata import ConnectorMetadata
from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.domain.chapter import Chapter, chapter_number
from app.domain.chapter_source import ChapterSource
from app.domain.download_job import (
    DownloadJob,
    DownloadJobStatus,
    DownloadPriority,
)
from app.domain.job_cancellation import JobCancellationNotAllowed
from app.domain.metadata import MetadataField, resolve_series_metadata
from app.domain.series import Series
from app.domain.series_source import SeriesSource
from app.domain.source import Source, SourceHealthStatus
from app.fmd2_bridge import FMD2_METADATA, fmd2_stable_chapter_reference
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.asset import asset_from_row
from app.persistence.mappers.chapter import chapter_from_row, chapter_to_row
from app.persistence.mappers.chapter_source import chapter_source_to_row
from app.persistence.mappers.download_job import download_job_from_row, download_job_to_row
from app.persistence.mappers.json_fields import encode_optional_string_tuple
from app.persistence.mappers.series import series_from_row, series_to_row
from app.persistence.mappers.series_source import series_source_to_row
from app.persistence.mappers.source import source_from_row, source_to_row
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.models.asset import AssetRow
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_cbz_asset import ChapterCbzAssetRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.download_job_archive import DownloadJobArchiveRow
from app.persistence.models.download_page import DownloadPageRow
from app.persistence.models.series import SeriesRow
from app.persistence.models.series_source import SeriesSourceRow
from app.persistence.models.source import SourceRow
from app.persistence.repositories.chapter_cbz_asset import ChapterCbzAssetRepository
from app.persistence.repositories.download_job import (
    DownloadJobRepository,
    StaleJobStateError,
)
from app.persistence.repositories.metadata import MetadataRepository
from app.persistence.windows_paths import DataRootIdentity
from app.services.reader_asset import (
    V1_MAX_CBZ_BYTES,
    V1_MAX_PAGE_BYTES,
    V1_STREAM_CHUNK_BYTES,
    ReaderAssetLimits,
    ReaderAssetService,
)
from app.services.series_matching import matching_library_series

DEFAULT_DOWNLOAD_JOB_PAGE_SIZE = 50
MAX_DOWNLOAD_JOB_PAGE_SIZE = 100
MAX_DOWNLOAD_JOB_OFFSET = 10_000_000
MAX_SELECTED_CHAPTERS = 100
_TERMINAL_RETRYABLE_STATUSES = frozenset({DownloadJobStatus.FAILED, DownloadJobStatus.CANCELLED})
_ARCHIVABLE_STATUSES = frozenset(
    {DownloadJobStatus.COMPLETED, DownloadJobStatus.FAILED, DownloadJobStatus.CANCELLED}
)


class InvalidChapterSelection(ValueError):
    """A requested remote chapter was not present in the connector response."""


class InconsistentConnectorSelection(RuntimeError):
    """Connector lineage cannot be persisted without risking a false association."""


class AmbiguousLibrarySeries(ValueError):
    """Several compatible entries require an explicit edition choice."""


class JobRetryNotAllowed(ValueError):
    """Only an available failed chapter without cancellation can be retried."""


class JobArchiveNotAllowed(ValueError):
    """Only a finalized job can be removed from the visible queue."""


@dataclass(frozen=True, slots=True)
class DownloadJobSummary:
    job: DownloadJob
    series_id: UUID
    chapter_id: UUID
    series_title: str
    chapter_label: str
    created: bool


@dataclass(frozen=True, slots=True)
class LibrarySeriesImportSummary:
    series_id: UUID
    series_title: str
    created: bool


@dataclass(frozen=True, slots=True)
class DownloadJobPage:
    items: tuple[DownloadJobSummary, ...]
    total: int
    limit: int
    offset: int
    states: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    page_progress: tuple[dict[str, object], ...] = ()


class DownloadJobService:
    def __init__(
        self, database: SQLiteDatabase, *, library_root_identity: DataRootIdentity | None = None
    ) -> None:
        self._database = database
        self._reader = ReaderAssetService(
            database,
            data_root_identity=database.data_root_identity,
            library_root_identity=library_root_identity,
            limits=ReaderAssetLimits(
                max_cbz_bytes=V1_MAX_CBZ_BYTES,
                max_page_bytes=V1_MAX_PAGE_BYTES,
                chunk_bytes=V1_STREAM_CHUNK_BYTES,
            ),
        )

    def import_library_series(
        self,
        *,
        connector: ConnectorMetadata,
        remote_series_id: str,
        series_metadata: ConnectorSeriesMetadata,
        now: datetime | None = None,
    ) -> LibrarySeriesImportSummary:
        """Import source metadata atomically without creating chapters or jobs."""
        if type(connector) is not ConnectorMetadata:
            raise TypeError("connector must be ConnectorMetadata")
        if type(remote_series_id) is not str or not 1 <= len(remote_series_id) <= 512:
            raise ValueError("remote_series_id must contain between 1 and 512 characters")
        if type(series_metadata) is not ConnectorSeriesMetadata:
            raise TypeError("series_metadata must be ConnectorSeriesMetadata")
        observed_at = now if now is not None else datetime.now(UTC)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        with self._database.write_session() as session:
            source = self._get_or_create_source(session, connector, observed_at)
            series, created = self._get_or_create_series(
                session,
                source=source,
                remote_series_id=remote_series_id,
                metadata=series_metadata,
                observed_at=observed_at,
            )
            repository = MetadataRepository()
            resolved = resolve_series_metadata(
                series,
                repository.list_series_sources(session, series.series_id),
                repository.list_overrides(session, series.series_id),
            )
            title = next(item.value for item in resolved if item.field is MetadataField.TITLE)
            summary = LibrarySeriesImportSummary(
                series_id=series.series_id,
                series_title=title if isinstance(title, str) and title else series.canonical_title,
                created=created,
            )
        return summary

    def retry_job(
        self,
        job_id: UUID,
        *,
        now: datetime | None = None,
    ) -> DownloadJobSummary | None:
        """Prioritize the latest retry and keep the superseded attempt in history."""
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        observed_at = now if now is not None else datetime.now(UTC)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        with self._database.write_session() as session:
            current = DownloadJobRepository().get(session, job_id)
            if current is None:
                return None
            if (
                not (
                    current.status is DownloadJobStatus.FAILED
                    or (
                        current.status is DownloadJobStatus.INTERRUPTED and current.recovery_blocked
                    )
                )
                or current.cancel_requested_at is not None
            ):
                raise JobRetryNotAllowed()
            chapter_source = session.get(ChapterSourceRow, str(current.chapter_source_id))
            if chapter_source is None:
                raise InconsistentConnectorSelection()
            source = session.get(SourceRow, chapter_source.source_id)
            if source is None:
                raise InconsistentConnectorSelection()
            if not chapter_source.available or not source.enabled:
                raise JobRetryNotAllowed()
            retried, created = self._get_or_create_job(
                session,
                chapter_source_id=current.chapter_source_id,
                priority=DownloadPriority.HIGH,
                observed_at=observed_at,
                allow_cancelled_retry=False,
            )
            if retried.status is DownloadJobStatus.CANCELLED or (
                retried.status is not DownloadJobStatus.COMPLETED
                and retried.cancel_requested_at is not None
            ):
                raise JobRetryNotAllowed()
            if (
                retried.status is DownloadJobStatus.QUEUED
                and retried.priority is not DownloadPriority.HIGH
            ):
                row = session.get(DownloadJobRow, str(retried.job_id))
                assert row is not None
                row.priority = DownloadPriority.HIGH.value
                row.updated_at_us = max(row.updated_at_us, datetime_to_epoch_us(observed_at))
                retried = download_job_from_row(row)
            if (
                retried.job_id != current.job_id
                and session.get(DownloadJobArchiveRow, str(current.job_id)) is None
            ):
                session.add(
                    DownloadJobArchiveRow(
                        job_id=str(current.job_id),
                        archived_at_us=datetime_to_epoch_us(observed_at),
                    )
                )
            return replace(self._summary_for_job(session, retried), created=created)

    def cancel_job(
        self,
        job_id: UUID,
        *,
        now: datetime | None = None,
    ) -> DownloadJobSummary | None:
        """Persist cancellation intent and return the current durable job view."""
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        observed_at = now if now is not None else datetime.now(UTC)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        repository = DownloadJobRepository()
        for _attempt in range(3):
            with self._database.read_session() as session:
                current = repository.get(session, job_id)
                if current is None:
                    return None
                if (
                    current.status is DownloadJobStatus.CANCELLED
                    or current.cancel_requested_at is not None
                ):
                    return self._summary_for_job(session, current)

            try:
                with self._database.write_session() as session:
                    latest = repository.get(session, job_id)
                    if latest is None:
                        return None
                    changed = repository.request_cancellation(
                        session,
                        current_job=latest,
                        requested_at=max(observed_at, latest.updated_at),
                    )
            except StaleJobStateError:
                # A worker transition or another cancellation won the CAS.
                # Re-read the full snapshot and retry only against its new stage.
                continue

            if changed is None:
                return None
            with self._database.read_session() as session:
                durable = repository.get(session, job_id)
                if durable is None:
                    return None
                return self._summary_for_job(session, durable)

        with self._database.read_session() as session:
            durable = repository.get(session, job_id)
            if durable is None:
                return None
            if (
                durable.status is DownloadJobStatus.CANCELLED
                or durable.cancel_requested_at is not None
            ):
                return self._summary_for_job(session, durable)
            if durable.status in {
                DownloadJobStatus.COMPLETED,
                DownloadJobStatus.FAILED,
                DownloadJobStatus.INTERRUPTED,
            }:
                raise JobCancellationNotAllowed(durable.status)
            raise StaleJobStateError(durable, durable)

    def archive_job(self, job_id: UUID, *, now: datetime | None = None) -> bool | None:
        """Hide a finalized job from the default queue without deleting its evidence."""
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        observed_at = now if now is not None else datetime.now(UTC)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        with self._database.write_session() as session:
            current = DownloadJobRepository().get(session, job_id)
            if current is None:
                return None
            cancellation_pending = (
                current.cancel_requested_at is not None
                and current.status is not DownloadJobStatus.CANCELLED
            )
            can_archive = current.status in _ARCHIVABLE_STATUSES or (
                current.status is DownloadJobStatus.INTERRUPTED and current.recovery_blocked
            )
            if not can_archive or cancellation_pending:
                raise JobArchiveNotAllowed()
            if session.get(DownloadJobArchiveRow, str(job_id)) is None:
                session.add(
                    DownloadJobArchiveRow(
                        job_id=str(job_id),
                        archived_at_us=datetime_to_epoch_us(observed_at),
                    )
                )
        return True

    def restore_job(self, job_id: UUID) -> bool | None:
        """Restore a previously archived job; never mutate its execution state."""
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        with self._database.write_session() as session:
            if DownloadJobRepository().get(session, job_id) is None:
                return None
            archived = session.get(DownloadJobArchiveRow, str(job_id))
            if archived is not None:
                session.delete(archived)
        return True

    def create_selected_jobs(
        self,
        *,
        connector: ConnectorMetadata,
        remote_series_id: str,
        series_metadata: ConnectorSeriesMetadata,
        chapters: Sequence[ConnectorChapter],
        selected_remote_ids: Sequence[str],
        priority: DownloadPriority = DownloadPriority.NORMAL,
        now: datetime | None = None,
    ) -> tuple[DownloadJobSummary, ...]:
        if type(connector) is not ConnectorMetadata:
            raise TypeError("connector must be ConnectorMetadata")
        if type(remote_series_id) is not str or not remote_series_id:
            raise ValueError("remote_series_id must not be empty")
        if type(series_metadata) is not ConnectorSeriesMetadata:
            raise TypeError("series_metadata must be ConnectorSeriesMetadata")
        if type(priority) is not DownloadPriority:
            raise ValueError("priority must be a DownloadPriority")
        if not 1 <= len(selected_remote_ids) <= MAX_SELECTED_CHAPTERS:
            raise ValueError("selected_remote_ids must contain between 1 and 100 IDs")
        if len(set(selected_remote_ids)) != len(selected_remote_ids):
            raise ValueError("selected_remote_ids must be unique")

        chapters_by_remote_id: dict[str, ConnectorChapter] = {}
        for chapter in chapters:
            if type(chapter) is not ConnectorChapter:
                raise TypeError("chapters must contain ConnectorChapter values")
            if chapter.remote_id is None:
                continue
            if chapter.remote_id in chapters_by_remote_id:
                raise InconsistentConnectorSelection()
            chapters_by_remote_id[chapter.remote_id] = chapter

        selected_chapters: list[ConnectorChapter] = []
        for remote_id in selected_remote_ids:
            if type(remote_id) is not str or not remote_id:
                raise ValueError("selected remote IDs must be nonempty strings")
            chapter = chapters_by_remote_id.get(remote_id)
            if chapter is None:
                raise InvalidChapterSelection()
            selected_chapters.append(chapter)

        observed_at = now if now is not None else datetime.now(UTC)
        results: list[DownloadJobSummary] = []
        with self._database.write_session() as session:
            source = self._get_or_create_source(session, connector, observed_at)
            series, _ = self._get_or_create_series(
                session,
                source=source,
                remote_series_id=remote_series_id,
                metadata=series_metadata,
                observed_at=observed_at,
            )

            local_ready = self._ready_chapters(session, series.series_id, series_metadata.language)
            saved_chapters: dict[str, ChapterSourceRow] = {}
            if connector.connector_id == FMD2_METADATA.connector_id:
                rows = session.scalars(
                    select(ChapterSourceRow)
                    .join(ChapterRow, ChapterRow.chapter_id == ChapterSourceRow.chapter_id)
                    .where(
                        ChapterSourceRow.source_id == str(source.source_id),
                        ChapterRow.series_id == str(series.series_id),
                    )
                )
                for saved in rows:
                    if saved.remote_id is not None:
                        key = fmd2_stable_chapter_reference(saved.remote_id)
                        previous = saved_chapters.get(key)
                        if previous is None or self._chapter_available(session, saved.chapter_id):
                            saved_chapters[key] = saved
            local_by_number = {chapter_number(item.display_label): item for item in local_ready}
            remote_numbers = Counter(chapter_number(item.display_label) for item in chapters)
            for remote_chapter in selected_chapters:
                number = chapter_number(remote_chapter.display_label)
                existing = local_by_number.get(number)
                if (
                    existing is not None
                    and remote_numbers[number] == 1
                    and remote_chapter.chapter_variant is None
                    and remote_chapter.volume_number is None
                    and remote_chapter.chapter_type in (None, "CHAPTER")
                    and (
                        not existing.language
                        or not remote_chapter.language
                        or existing.language == remote_chapter.language
                    )
                ):
                    linked_asset = ChapterCbzAssetRepository().get_for_chapter(
                        session, existing.chapter_id
                    )
                    assert linked_asset is not None
                    job_row = session.get(DownloadJobRow, str(linked_asset[0].download_job_id))
                    assert job_row is not None
                    results.append(self._summary_for_job(session, download_job_from_row(job_row)))
                    continue
                chapter, chapter_source = self._get_or_create_chapter(
                    session,
                    series_id=series.series_id,
                    source_id=source.source_id,
                    remote_chapter=remote_chapter,
                    observed_at=observed_at,
                    saved_chapters=saved_chapters,
                )
                job, created = self._get_or_create_job(
                    session,
                    chapter_source_id=chapter_source.chapter_source_id,
                    priority=priority,
                    observed_at=observed_at,
                )
                results.append(
                    DownloadJobSummary(
                        job=job,
                        series_id=series.series_id,
                        chapter_id=chapter.chapter_id,
                        series_title=series.display_title,
                        chapter_label=chapter.display_label,
                        created=created,
                    )
                )
        return tuple(results)

    def list_jobs(
        self,
        *,
        limit: int = DEFAULT_DOWNLOAD_JOB_PAGE_SIZE,
        offset: int = 0,
        archived: bool = False,
    ) -> DownloadJobPage:
        if type(limit) is not int or not 1 <= limit <= MAX_DOWNLOAD_JOB_PAGE_SIZE:
            raise ValueError("limit must be between 1 and 100")
        if type(offset) is not int or not 0 <= offset <= MAX_DOWNLOAD_JOB_OFFSET:
            raise ValueError("offset must be between 0 and 10000000")
        if type(archived) is not bool:
            raise ValueError("archived must be a boolean")

        is_archived = exists().where(DownloadJobArchiveRow.job_id == DownloadJobRow.job_id)
        archive_filter = is_archived if archived else ~is_archived

        statement = (
            select(
                DownloadJobRow,
                SeriesRow.series_id,
                ChapterRow.chapter_id,
                SeriesRow.display_title,
                ChapterRow.display_label,
            )
            .join(
                ChapterSourceRow,
                ChapterSourceRow.chapter_source_id == DownloadJobRow.chapter_source_id,
            )
            .join(ChapterRow, ChapterRow.chapter_id == ChapterSourceRow.chapter_id)
            .join(SeriesRow, SeriesRow.series_id == ChapterRow.series_id)
            .where(archive_filter)
            .limit(limit)
            .offset(offset)
        )
        with self._database.read_session() as session:
            active_statuses = (
                "FETCHING_METADATA",
                "DOWNLOADING",
                "VALIDATING",
                "PROCESSING",
                "PACKAGING",
                "VERIFYING",
                "INDEXING",
            )
            active_series = session.execute(
                select(ChapterRow.series_id)
                .join(ChapterSourceRow, ChapterSourceRow.chapter_id == ChapterRow.chapter_id)
                .join(
                    DownloadJobRow,
                    DownloadJobRow.chapter_source_id == ChapterSourceRow.chapter_source_id,
                )
                .where(DownloadJobRow.status.in_(active_statuses))
                .order_by(DownloadJobRow.created_at_us.asc())
                .limit(1)
            ).scalar_one_or_none()
            status_order = case(
                (DownloadJobRow.status.in_(active_statuses), 0),
                (DownloadJobRow.status == "QUEUED", 1),
                else_=2,
            )
            statement = statement.order_by(
                status_order,
                case((status_order == 2, DownloadJobRow.created_at_us)).desc(),
                *DownloadJobRepository.queue_order(UUID(active_series) if active_series else None),
            )
            rows = session.execute(statement).all()
            states = {status.value: 0 for status in DownloadJobStatus}
            states.update(
                {
                    status: count
                    for status, count in session.execute(
                        select(DownloadJobRow.status, func.count())
                        .where(archive_filter)
                        .group_by(DownloadJobRow.status)
                    )
                }
            )
            progress = session.execute(
                select(
                    DownloadPageRow.job_id,
                    func.count(),
                    func.sum(case((DownloadPageRow.status == "VALIDATED", 1), else_=0)),
                )
                .where(DownloadPageRow.job_id.in_([row[0].job_id for row in rows]))
                .group_by(DownloadPageRow.job_id)
            ).all()

        items = tuple(
            DownloadJobSummary(
                job=download_job_from_row(row[0]),
                series_id=UUID(row[1]),
                chapter_id=UUID(row[2]),
                series_title=row[3],
                chapter_label=row[4],
                created=False,
            )
            for row in rows
        )
        return DownloadJobPage(
            items=items,
            total=sum(states.values()),
            limit=limit,
            offset=offset,
            states=states,
            page_progress=tuple(
                {"job_id": job_id, "known_pages": int(count), "validated_pages": int(validated)}
                for job_id, count, validated in progress
            ),
        )

    def chapter_availability(self, connector_id: str, remote_series_id: str) -> dict[str, object]:
        """Compare only the edition explicitly linked to this source's series identity."""
        with self._database.read_session() as session:
            linked = session.execute(
                select(SeriesSourceRow.series_id, SeriesSourceRow.remote_language)
                .join(SourceRow, SourceRow.source_id == SeriesSourceRow.source_id)
                .where(
                    SourceRow.connector_id == connector_id,
                    SeriesSourceRow.remote_id == remote_series_id,
                )
            ).one_or_none()
            if linked is None:
                return {
                    "linked": False,
                    "ready_remote_ids": [],
                    "ready_labels": [],
                    "busy_remote_ids": [],
                }
            series_id, language = linked
            # Source catalogs are bounded to 10,000 entries; match the reader's verified predicate.
            ready, _ = ChapterCbzAssetRepository().list_ready_chapters(
                session, UUID(series_id), limit=10_000, offset=0
            )
            ready_ids = [str(item.chapter_id) for item in ready]
            present = self._present_chapters(session, ready_ids)
            ready_ids = [value for value in ready_ids if value in present]
            local_ready = self._ready_chapters(session, UUID(series_id), language)
            remote_ids = list(
                session.scalars(
                    select(ChapterSourceRow.remote_id)
                    .join(SourceRow, SourceRow.source_id == ChapterSourceRow.source_id)
                    .where(
                        SourceRow.connector_id == connector_id,
                        ChapterSourceRow.chapter_id.in_(ready_ids),
                    )
                )
            )
            job_source = aliased(ChapterSourceRow)
            busy = list(
                session.scalars(
                    select(ChapterSourceRow.remote_id)
                    .join(SourceRow, SourceRow.source_id == ChapterSourceRow.source_id)
                    .join(ChapterRow, ChapterRow.chapter_id == ChapterSourceRow.chapter_id)
                    .join(job_source, job_source.chapter_id == ChapterSourceRow.chapter_id)
                    .join(
                        DownloadJobRow,
                        DownloadJobRow.chapter_source_id == job_source.chapter_source_id,
                    )
                    .where(
                        SourceRow.connector_id == connector_id,
                        ChapterRow.series_id == series_id,
                        DownloadJobRow.status.not_in(
                            ("COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED")
                        ),
                        DownloadJobRow.cancel_requested_at_us.is_(None),
                    )
                )
            )

            def reference(value: str) -> str:
                return (
                    fmd2_stable_chapter_reference(value)
                    if connector_id == FMD2_METADATA.connector_id
                    else value
                )

            return {
                "linked": True,
                "ready_remote_ids": list(
                    dict.fromkeys(reference(value) for value in remote_ids if value)
                ),
                "ready_labels": [item.display_label for item in local_ready],
                "busy_remote_ids": list(dict.fromkeys(reference(value) for value in busy if value)),
            }

    def _present_chapters(self, session: Session, chapter_ids: list[str]) -> set[str]:
        rows = session.execute(
            select(ChapterCbzAssetRow.chapter_id, AssetRow)
            .join(AssetRow, AssetRow.asset_id == ChapterCbzAssetRow.asset_id)
            .where(ChapterCbzAssetRow.chapter_id.in_(chapter_ids))
        )
        return {
            chapter_id
            for chapter_id, asset in rows
            if self._reader.asset_available(asset_from_row(asset))
        }

    def _chapter_available(self, session: Session, chapter_id: str) -> bool:
        return chapter_id in self._present_chapters(session, [chapter_id])

    def _ready_chapters(
        self, session: Session, series_id: UUID, language: str | None
    ) -> tuple[Chapter, ...]:
        ready, _ = ChapterCbzAssetRepository().list_ready_chapters(
            session, series_id, limit=10_000, offset=0
        )
        present = self._present_chapters(session, [str(item.chapter_id) for item in ready])
        rows = session.scalars(select(ChapterRow).where(ChapterRow.chapter_id.in_(present))).all()
        counts = Counter(chapter_number(row.display_label) for row in rows)
        return tuple(
            chapter_from_row(row)
            for row in rows
            if chapter_number(row.display_label) is not None
            and counts[chapter_number(row.display_label)] == 1
            and row.chapter_variant is None
            and row.volume_number is None
            and row.chapter_type in (None, "CHAPTER")
            and (not row.language or not language or row.language == language)
        )

    @staticmethod
    def _summary_for_job(session: Session, job: DownloadJob) -> DownloadJobSummary:
        row = session.execute(
            select(
                SeriesRow.series_id,
                ChapterRow.chapter_id,
                SeriesRow.display_title,
                ChapterRow.display_label,
            )
            .join(ChapterSourceRow, ChapterSourceRow.chapter_id == ChapterRow.chapter_id)
            .join(SeriesRow, SeriesRow.series_id == ChapterRow.series_id)
            .where(ChapterSourceRow.chapter_source_id == str(job.chapter_source_id))
        ).one_or_none()
        if row is None:
            raise InconsistentConnectorSelection()
        return DownloadJobSummary(
            job=job,
            series_id=UUID(row[0]),
            chapter_id=UUID(row[1]),
            series_title=row[2],
            chapter_label=row[3],
            created=False,
        )

    @staticmethod
    def _get_or_create_source(
        session: Session,
        connector: ConnectorMetadata,
        observed_at: datetime,
    ) -> Source:
        row = session.scalar(
            select(SourceRow).where(SourceRow.connector_id == connector.connector_id)
        )
        if row is not None:
            return source_from_row(row)

        source = Source.new_local(
            connector.connector_id,
            connector.connector_version,
            connector.connector_id,
            True,
            SourceHealthStatus.HEALTHY,
            now=observed_at,
        )
        session.add(source_to_row(source))
        session.flush()
        return source

    @staticmethod
    def _get_or_create_series(
        session: Session,
        *,
        source: Source,
        remote_series_id: str,
        metadata: ConnectorSeriesMetadata,
        observed_at: datetime,
    ) -> tuple[Series, bool]:
        source_row = session.scalar(
            select(SeriesSourceRow).where(
                SeriesSourceRow.source_id == str(source.source_id),
                SeriesSourceRow.remote_id == remote_series_id,
            )
        )
        if source_row is not None:
            row = session.get(SeriesRow, source_row.series_id)
            if row is None:
                raise InconsistentConnectorSelection()
            source_row.remote_title = metadata.title
            source_row.remote_cover_url = metadata.cover_url
            source_row.remote_author = metadata.author
            source_row.remote_description = metadata.description
            source_row.remote_genres_json = encode_optional_string_tuple(metadata.genres)
            source_row.remote_status = metadata.status
            source_row.remote_language = metadata.language
            source_row.last_checked_at_us = datetime_to_epoch_us(observed_at)
            return series_from_row(row), False

        title = metadata.title if metadata.title else remote_series_id
        matches = matching_library_series(
            session, title=title, author=metadata.author, language=metadata.language
        )
        if len(matches) > 1:
            raise AmbiguousLibrarySeries(
                "Hay varias fichas compatibles en la biblioteca; "
                "especifica la edición antes de añadir capítulos."
            )
        created = not matches
        series = (
            series_from_row(matches[0])
            if matches
            else Series.new_local(title, title, now=observed_at)
        )
        if created:
            session.add(series_to_row(series))
            session.flush()
        series_source = SeriesSource.new_local(
            series.series_id,
            source.source_id,
            remote_id=remote_series_id,
            remote_title=metadata.title,
            remote_cover_url=metadata.cover_url,
            remote_author=metadata.author,
            remote_description=metadata.description,
            remote_genres=metadata.genres,
            remote_status=metadata.status,
            remote_language=metadata.language,
            last_checked_at=observed_at,
        )
        session.add(series_source_to_row(series_source))
        session.flush()
        return series, created

    @staticmethod
    def _get_or_create_chapter(
        session: Session,
        *,
        series_id: UUID,
        source_id: UUID,
        remote_chapter: ConnectorChapter,
        observed_at: datetime,
        saved_chapters: dict[str, ChapterSourceRow],
    ) -> tuple[Chapter, ChapterSource]:
        if remote_chapter.remote_id is None:
            raise InvalidChapterSelection()
        source_row = session.scalar(
            select(ChapterSourceRow).where(
                ChapterSourceRow.source_id == str(source_id),
                ChapterSourceRow.remote_id == remote_chapter.remote_id,
            )
        )
        if source_row is None and saved_chapters:
            source_row = saved_chapters.get(fmd2_stable_chapter_reference(remote_chapter.remote_id))
        if source_row is not None:
            row = session.get(ChapterRow, source_row.chapter_id)
            if row is None or row.series_id != str(series_id):
                raise InconsistentConnectorSelection()
            source_row.available = True
            source_row.last_seen_at_us = datetime_to_epoch_us(observed_at)
            chapter_source = ChapterSource(
                chapter_source_id=UUID(source_row.chapter_source_id),
                chapter_id=UUID(source_row.chapter_id),
                source_id=UUID(source_row.source_id),
                remote_id=source_row.remote_id,
                remote_url=source_row.remote_url,
                remote_label=source_row.remote_label,
                available=source_row.available,
                last_seen_at=observed_at,
            )
            return chapter_from_row(row), chapter_source

        chapter = Chapter.new_local(
            series_id,
            remote_chapter.chapter_raw_label,
            remote_chapter.display_label,
            chapter_sort_key=remote_chapter.chapter_sort_key,
            chapter_major=remote_chapter.chapter_major,
            chapter_minor=remote_chapter.chapter_minor,
            chapter_variant=remote_chapter.chapter_variant,
            chapter_type=remote_chapter.chapter_type,
            volume_number=remote_chapter.volume_number,
            language=remote_chapter.language,
        )
        session.add(chapter_to_row(chapter))
        session.flush()
        chapter_source = ChapterSource.new_local(
            chapter.chapter_id,
            source_id,
            remote_id=remote_chapter.remote_id,
            remote_url=remote_chapter.remote_url,
            remote_label=remote_chapter.remote_label,
            available=True,
            last_seen_at=observed_at,
        )
        session.add(chapter_source_to_row(chapter_source))
        session.flush()
        return chapter, chapter_source

    def _get_or_create_job(
        self,
        session: Session,
        *,
        chapter_source_id: UUID,
        priority: DownloadPriority,
        observed_at: datetime,
        allow_cancelled_retry: bool = True,
    ) -> tuple[DownloadJob, bool]:
        chapter_source = session.get(ChapterSourceRow, str(chapter_source_id))
        if chapter_source is None:
            raise InconsistentConnectorSelection()
        # A recovered chapter keeps its ID; reuse its completed or ongoing alternate attempt.
        siblings = session.scalars(
            select(DownloadJobRow)
            .join(
                ChapterSourceRow,
                ChapterSourceRow.chapter_source_id == DownloadJobRow.chapter_source_id,
            )
            .where(
                ChapterSourceRow.chapter_id == chapter_source.chapter_id,
                DownloadJobRow.status.not_in(("FAILED", "CANCELLED", "INTERRUPTED")),
            )
            .order_by(DownloadJobRow.updated_at_us.desc(), DownloadJobRow.job_id.desc())
        )
        for sibling in siblings:
            if sibling.status != "COMPLETED" or self._chapter_available(
                session, chapter_source.chapter_id
            ):
                return download_job_from_row(sibling), False
        latest_row = session.scalar(
            select(DownloadJobRow)
            .where(DownloadJobRow.chapter_source_id == str(chapter_source_id))
            .order_by(DownloadJobRow.created_at_us.desc(), DownloadJobRow.job_id.desc())
            .limit(1)
        )
        if latest_row is not None:
            latest = download_job_from_row(latest_row)
            retryable = (
                latest.status in _TERMINAL_RETRYABLE_STATUSES
                or (latest.status is DownloadJobStatus.INTERRUPTED and latest.recovery_blocked)
                or (
                    latest.status is DownloadJobStatus.COMPLETED
                    and not self._chapter_available(session, chapter_source.chapter_id)
                )
            )
            if not retryable or (
                not allow_cancelled_retry
                and (
                    latest.status is DownloadJobStatus.CANCELLED
                    or latest.cancel_requested_at is not None
                )
            ):
                return latest, False
            # Preserve latest-job ordering even if the local clock moves backward.
            observed_at = max(observed_at, latest.updated_at + timedelta(microseconds=1))

        link = session.get(ChapterCbzAssetRow, chapter_source.chapter_id)
        if link is not None and not self._chapter_available(session, chapter_source.chapter_id):
            session.delete(link)
            session.flush()
        job = DownloadJob.new_local(
            chapter_source_id,
            priority=priority,
            now=observed_at,
        )
        session.add(download_job_to_row(job))
        session.flush()
        return job, True
