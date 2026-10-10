from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.domain.asset import Asset, AssetType
from app.domain.export_job import ExportJob, ExportJobStatus
from app.errors import ErrorCode
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.asset import asset_from_row
from app.persistence.models.asset import AssetRow
from app.persistence.models.chapter_cbz_asset import ChapterCbzAssetRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.download_manifest_state import DownloadManifestStateRow
from app.persistence.repositories.export_job import (
    ExportJobCursorPage,
    ExportJobPage,
    ExportJobRepository,
    ExportJobTransitionConflict,
)
from app.services.usb_export import MAX_EXPORT_BYTES, ExportFailure, ExportResult, ExportTarget
from app.tracing import activate_trace_id, create_trace_id, current_trace_id, reset_trace_id

logger = logging.getLogger("yomira.usb_export_history")

MAX_EXPORT_HISTORY_PAGE_SIZE = 100
DEFAULT_EXPORT_HISTORY_PAGE_SIZE = 100
MAX_EXPORT_IDEMPOTENCY_KEY_CHARS = 128


class ExportAssetUnavailable(RuntimeError):
    """The selected local library asset is not a verified CBZ or CBR."""


class Exporter(Protocol):
    def list_targets(self) -> tuple[ExportTarget, ...]: ...

    def export_asset(self, asset: Asset, *, target_id: str) -> ExportResult: ...


@dataclass(frozen=True, slots=True)
class _TargetMetadata:
    label: str
    file_system: str


class UsbExportJobService:
    """Durable orchestration around the verified USB copy service."""

    def __init__(
        self,
        *,
        database: SQLiteDatabase,
        exporter: Exporter,
        repository: ExportJobRepository | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not callable(getattr(exporter, "list_targets", None)) or not callable(
            getattr(exporter, "export_asset", None)
        ):
            raise TypeError("exporter must implement list_targets and export_asset")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._database = database
        self._exporter = exporter
        self._repository = repository or ExportJobRepository()
        self._clock = clock

        """List removable targets from the configured exporter/provider."""
    def progress_for_trace(self, trace_id: str) -> dict[str, object] | None:
        callback = getattr(self._exporter, "progress_for_trace", None)
        return cast(dict[str, object] | None, callback(trace_id)) if callable(callback) else None

    def list_targets(self) -> tuple[ExportTarget, ...]:
        """List removable targets from the configured exporter/provider."""
        return tuple(self._exporter.list_targets())

    def export_asset(
        self,
        asset_id: UUID,
        *,
        target_id: str,
        idempotency_key: str,
    ) -> ExportJob:
        asset = self._get_verified_export_asset(asset_id)
        job, created = self._create_or_get_job(
            asset,
            target_id=target_id,
            idempotency_key=idempotency_key,
        )
        if not created:
            return job

        exporting = self.mark_exporting(job.export_id)
        trace_token = activate_trace_id(exporting.trace_id)
        try:
            try:
                result = self._exporter.export_asset(asset, target_id=target_id)
            except ExportFailure as error:
                return self.mark_failed(
                    exporting.export_id,
                    error_code=_export_job_error_code(error.code).value,
                    failure_stage=error.stage,
                    retryable=error.retryable,
                    cause_type=error.cause_type or type(error).__name__,
                )
            except Exception as error:
                logger.error(
                    "usb_export.job_attempt_failed",
                    extra={
                        "error_code": ErrorCode.EXP_COPY_FAILED.value,
                        "retryable": False,
                        "stage": "export_attempt",
                        "cause_type": type(error).__name__,
                        "trace_id": exporting.trace_id,
                        "export_id": str(exporting.export_id),
                    },
                )
                return self.mark_failed(
                    exporting.export_id,
                    error_code=ErrorCode.EXP_COPY_FAILED.value,
                    failure_stage="export_attempt",
                    retryable=False,
                    cause_type=type(error).__name__,
                )

            if (
                result.asset_id != exporting.asset_id
                or result.target_id != exporting.target_id
                or result.sha256 != exporting.expected_sha256
                or result.size_bytes != exporting.expected_size_bytes
                or result.destination_name != exporting.destination_name
            ):
                return self.mark_failed(
                    exporting.export_id,
                    error_code=ErrorCode.EXP_HASH_MISMATCH.value,
                    failure_stage="result_verification",
                    retryable=False,
                    cause_type="ExportResultMismatch",
                )
            return self.mark_succeeded(
                exporting.export_id,
                status=result.status,
            )
        finally:
            reset_trace_id(trace_token)

    def export_chapter(
        self,
        chapter_id: UUID,
        *,
        target_id: str,
        idempotency_key: str,
    ) -> ExportJob:
        """Export only the verified CBZ currently indexed for a ready chapter."""
        if type(chapter_id) is not UUID or chapter_id.version != 4:
            raise ExportAssetUnavailable()
        with self._database.read_session() as session:
            asset_id = session.scalar(
                select(ChapterCbzAssetRow.asset_id)
                .join(AssetRow, AssetRow.asset_id == ChapterCbzAssetRow.asset_id)
                .join(
                    DownloadJobRow,
                    DownloadJobRow.job_id == ChapterCbzAssetRow.download_job_id,
                )
                .join(
                    ChapterSourceRow,
                    ChapterSourceRow.chapter_source_id == DownloadJobRow.chapter_source_id,
                )
                .outerjoin(
                    DownloadManifestStateRow,
                    DownloadManifestStateRow.job_id == DownloadJobRow.job_id,
                )
                .where(
                    ChapterCbzAssetRow.chapter_id == str(chapter_id),
                    AssetRow.asset_type == AssetType.CBZ.value,
                    DownloadJobRow.status == "COMPLETED",
                    DownloadJobRow.cancel_requested_at_us.is_(None),
                    DownloadJobRow.recovery_blocked.is_(False),
                    ChapterSourceRow.chapter_id == ChapterCbzAssetRow.chapter_id,
                    or_(
                        DownloadManifestStateRow.job_id.is_(None),
                        DownloadManifestStateRow.publication_pending.is_(False),
                    ),
                )
            )
        if asset_id is None:
            raise ExportAssetUnavailable()
        return self.export_asset(
            UUID(asset_id),
            target_id=target_id,
            idempotency_key=idempotency_key,
        )

    def create_queued_job(
        self,
        asset_id: UUID,
        *,
        target_id: str,
        idempotency_key: str,
    ) -> ExportJob:
        """Persist an export reservation without invoking the external writer."""
        asset = self._get_verified_export_asset(asset_id)
        job, _created = self._create_or_get_job(
            asset,
            target_id=target_id,
            idempotency_key=idempotency_key,
        )
        return job

    def mark_exporting(self, export_id: UUID) -> ExportJob:
        now = self._now()
        with self._database.write_session() as session:
            current = self._repository.get(session, export_id)
            if current is None or current.status is not ExportJobStatus.QUEUED:
                raise ExportJobTransitionConflict()
            started_at = max(now, current.created_at)
            updated = replace(
                current,
                status=ExportJobStatus.EXPORTING,
                updated_at=started_at,
                started_at=started_at,
            )
            return self._repository.save_transition(
                session,
                updated,
                expected_status=ExportJobStatus.QUEUED,
            )

    def mark_succeeded(self, export_id: UUID, *, status: str) -> ExportJob:
        try:
            target_status = ExportJobStatus(status)
        except ValueError as error:
            raise ValueError("success status must be EXPORTED or SKIPPED") from error
        if target_status not in {ExportJobStatus.EXPORTED, ExportJobStatus.SKIPPED}:
            raise ValueError("success status must be EXPORTED or SKIPPED")
        now = self._now()
        with self._database.write_session() as session:
            current = self._require_exporting_job(session, export_id)
            terminal_at = max(now, current.updated_at)
            completed = replace(
                current,
                status=target_status,
                updated_at=terminal_at,
                terminal_at=terminal_at,
            )
            job = self._repository.save_transition(
                session,
                completed,
                expected_status=ExportJobStatus.EXPORTING,
            )
        logger.info(
            "usb_export.job_terminal",
            extra={
                "export_id": str(job.export_id),
                "status": job.status.value,
                "stage": "complete",
                "trace_id": job.trace_id,
            },
        )
        return job

    def mark_failed(
        self,
        export_id: UUID,
        *,
        error_code: str,
        failure_stage: str,
        retryable: bool,
        cause_type: str,
    ) -> ExportJob:
        now = self._now()
        with self._database.write_session() as session:
            current = self._require_exporting_job(session, export_id)
            terminal_at = max(now, current.updated_at)
            failed = replace(
                current,
                status=ExportJobStatus.FAILED,
                updated_at=terminal_at,
                terminal_at=terminal_at,
                error_code=error_code,
                failure_stage=failure_stage[:64],
                retryable=retryable,
                cause_type=cause_type[:64] or "UnknownError",
            )
            job = self._repository.save_transition(
                session,
                failed,
                expected_status=ExportJobStatus.EXPORTING,
            )
        logger.error(
            "usb_export.job_terminal",
            extra={
                "export_id": str(job.export_id),
                "error_code": job.error_code,
                "retryable": job.retryable,
                "stage": job.failure_stage,
                "cause_type": job.cause_type,
                "trace_id": job.trace_id,
            },
        )
        return job

    def recover_after_restart(self) -> int:
        now = self._now()
        trace_id = create_trace_id()
        with self._database.write_session() as session:
            count = self._repository.interrupt_active(
                session,
                now=now,
            )
        if count:
            logger.warning(
                "usb_export.active_jobs_interrupted",
                extra={
                    "count": count,
                    "error_code": ErrorCode.SYS_STARTUP_FAILED.value,
                    "retryable": True,
                    "stage": "startup_recovery",
                    "trace_id": trace_id,
                },
            )
        return count

    def list_jobs(self, *, limit: int, offset: int) -> ExportJobPage:
        with self._database.read_session() as session:
            return self._repository.list_page(session, limit=limit, offset=offset)

    def list_jobs_after(
        self,
        *,
        limit: int,
        before: tuple[int, UUID] | None,
    ) -> ExportJobCursorPage:
        with self._database.read_session() as session:
            return self._repository.list_page_after(session, limit=limit, before=before)

    def _create_or_get_job(
        self,
        asset: Asset,
        *,
        target_id: str,
        idempotency_key: str,
    ) -> tuple[ExportJob, bool]:
        _validate_idempotency_key(idempotency_key)
        if not 1 <= len(target_id) <= 128:
            raise ValueError("target_id must contain between 1 and 128 characters")
        if any(ord(character) < 32 or ord(character) > 126 for character in target_id):
            raise ValueError("target_id must contain printable ASCII characters")

        idempotency_key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        fingerprint_bytes = json.dumps(
            {"asset_id": str(asset.asset_id), "target_id": target_id},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        request_fingerprint = hashlib.sha256(fingerprint_bytes).hexdigest()
        now = self._now()
        target_metadata = self._target_metadata(target_id)
        trace_id = current_trace_id() or create_trace_id()
        job = ExportJob(
            export_id=uuid4(),
            idempotency_key_hash=idempotency_key_hash,
            request_fingerprint=request_fingerprint,
            asset_id=asset.asset_id,
            target_id=target_id,
            target_label=target_metadata.label,
            file_system=target_metadata.file_system,
            destination_name=Path(asset.relative_path).name,
            expected_size_bytes=asset.size_bytes,
            expected_sha256=asset.sha256,
            status=ExportJobStatus.QUEUED,
            created_at=now,
            updated_at=now,
            started_at=None,
            terminal_at=None,
            trace_id=trace_id,
        )
        with self._database.write_session() as session:
            return self._repository.create_with_retention(session, job, now=now)

    def _target_metadata(self, target_id: str) -> _TargetMetadata:
        try:
            target = next(
                (item for item in self._exporter.list_targets() if item.target_id == target_id),
                None,
            )
        except Exception:
            # The export attempt will produce the authoritative traceable
            # device error after the durable reservation commits.
            target = None
        if target is None:
            return _TargetMetadata(label=target_id[:128], file_system="unknown")
        return _TargetMetadata(
            label=target.display_name[:128] or target.target_id[:128],
            file_system=target.file_system[:32] or "unknown",
        )

    def _get_verified_export_asset(self, asset_id: UUID) -> Asset:
        if type(asset_id) is not UUID or asset_id.version != 4:
            raise ExportAssetUnavailable()
        with self._database.read_session() as session:
            row = session.scalar(select(AssetRow).where(AssetRow.asset_id == str(asset_id)))
            if row is None:
                raise ExportAssetUnavailable()
            asset = asset_from_row(row)
        if (
            asset.asset_type not in {AssetType.CBZ, AssetType.CBR}
            or asset.verified_at is None
            or asset.size_bytes <= 0
            or asset.size_bytes > MAX_EXPORT_BYTES
        ):
            raise ExportAssetUnavailable()
        return asset

    def _require_exporting_job(self, session: Session, export_id: UUID) -> ExportJob:
        current = self._repository.get(session, export_id)
        if current is None or current.status is not ExportJobStatus.EXPORTING:
            raise ExportJobTransitionConflict()
        return current

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("clock must return an aware datetime")
        return value.astimezone(UTC)


def _validate_idempotency_key(value: str) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= MAX_EXPORT_IDEMPOTENCY_KEY_CHARS
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ValueError("Idempotency-Key must contain 1 to 128 visible ASCII characters")


def _export_job_error_code(error_code: ErrorCode) -> ErrorCode:
    if error_code is ErrorCode.LIB_PATH_UNAVAILABLE:
        return ErrorCode.EXP_SOURCE_INVALID
    if error_code.value.startswith("EXP_"):
        return error_code
    return ErrorCode.EXP_COPY_FAILED
