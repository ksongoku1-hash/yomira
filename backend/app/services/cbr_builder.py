"""Build a bounded, verified Bigme-oriented CBR from canonical chapter CBZs."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import disk_usage
from threading import Lock
from typing import BinaryIO, cast
from uuid import UUID, uuid4

from PIL import Image, ImageFile

from app.domain.asset import Asset, AssetType, validate_relative_path
from app.errors import ErrorCode
from app.persistence.atomic_file_publisher import AtomicFilePublisher
from app.persistence.database import SQLiteDatabase
from app.persistence.disk_budget import (
    MINIMUM_RESERVED_BYTES,
    DiskBudgetManager,
    DiskBudgetUnavailableError,
    DiskUsageProvider,
)
from app.persistence.filesystem_retry import RetryingFileStream
from app.persistence.managed_directories import ensure_managed_directory
from app.persistence.mappers.asset import asset_to_row
from app.persistence.storage_gate import is_disk_full_os_error
from app.persistence.windows_paths import DataRootIdentity
from app.services.library import LibraryService
from app.services.rar5_store_writer import (
    MAX_CBR_BYTES,
    MAX_CBR_PAGE_COUNT,
    Rar5StoreFile,
    Rar5StoreWriter,
    Rar5WriteError,
    Rar5WriteResult,
)
from app.services.reader_asset import (
    ReaderAssetError,
    ReaderAssetNotFound,
    ReaderAssetService,
    ReaderPageRecord,
)
from app.tracing import activate_trace_id, create_trace_id, reset_trace_id

logger = logging.getLogger("yomira.cbr_export")

MAX_CBR_TEMP_PNG_BYTES = MAX_CBR_BYTES
MAX_CBR_IMAGE_WIDTH = 40_000
MAX_CBR_IMAGE_HEIGHT = 40_000
MAX_CBR_IMAGE_PIXELS = 100_000_000
_RGBA_HASH_BAND_ROWS = 64
_SUPPORTED_SOURCE_FORMATS = ("JPEG", "PNG", "WEBP")
_MIME_TO_FORMAT = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}
_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


class CbrExportFailure(RuntimeError):
    """Sanitized, traceable failure while constructing a CBR asset."""

    def __init__(
        self,
        code: ErrorCode,
        stage: str,
        *,
        trace_id: str,
        retryable: bool,
        status_code: int,
        cause_type: str | None = None,
    ) -> None:
        self.code = code
        self.stage = stage
        self.trace_id = trace_id
        self.retryable = retryable
        self.status_code = status_code
        self.cause_type = cause_type
        super().__init__("The selected chapters could not be assembled into a verified CBR.")


@dataclass(frozen=True, slots=True)
class CbrExportResult:
    asset: Asset
    filename: str
    series_title: str
    chapter_labels: tuple[str, ...]
    page_count: int


class _PngWriteBudget:
    def __init__(
        self,
        disk_budget: DiskBudgetManager,
        operation_id: UUID,
        *,
        max_bytes: int,
    ) -> None:
        self._disk_budget = disk_budget
        self._operation_id = operation_id
        self._max_bytes = max_bytes
        self.bytes_written = 0

    def write(self, stream: RetryingFileStream, value: object) -> int:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError("PNG encoder writes must be bytes-like")
        payload = bytes(cast(bytes | bytearray | memoryview[int], value))
        if not payload:
            return 0
        if self.bytes_written + len(payload) > self._max_bytes:
            raise OSError("The aggregate CBR PNG staging limit was exceeded.")
        written = self._disk_budget.write_reserved(
            self._operation_id,
            category="pages",
            requested_bytes=len(payload),
            write=lambda: stream.write(payload),
        )
        if written != len(payload):
            raise OSError("The PNG staging write was incomplete.")
        self.bytes_written += written
        return written


class _PngOutput:
    """Sequential, reservation-metered file adapter accepted by Pillow."""

    def __init__(self, stream: RetryingFileStream, budget: _PngWriteBudget) -> None:
        self._stream = stream
        self._budget = budget

    def write(self, value: object) -> int:
        return self._budget.write(self._stream, value)

    def tell(self) -> int:
        return self._stream.tell()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if offset == 0 and whence == os.SEEK_CUR:
            return self._stream.tell()
        raise OSError("CBR PNG output must be sequential.")

    def flush(self) -> None:
        self._stream.flush()

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False


class CbrExportService:
    """Assemble selected, complete chapters without mutating their canonical CBZs."""

    def __init__(
        self,
        *,
        database: SQLiteDatabase,
        reader: ReaderAssetService,
        library: LibraryService,
        disk_budget: DiskBudgetManager,
        library_root_identity: DataRootIdentity,
        publisher: AtomicFilePublisher,
        disk_usage_provider: DiskUsageProvider = disk_usage,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if type(database) is not SQLiteDatabase:
            raise TypeError("database must be a SQLiteDatabase")
        if database is not disk_budget.database:
            raise ValueError("database and disk budget must share one instance")
        if type(reader) is not ReaderAssetService:
            raise TypeError("reader must be a ReaderAssetService")
        if type(library) is not LibraryService:
            raise TypeError("library must be a LibraryService")
        if type(library_root_identity) is not DataRootIdentity:
            raise TypeError("library_root_identity must be a DataRootIdentity")
        if type(publisher) is not AtomicFilePublisher:
            raise TypeError("publisher must be an AtomicFilePublisher")
        if publisher.data_root_identity is not library_root_identity:
            raise ValueError("publisher must use the configured library root identity")
        if not callable(disk_usage_provider) or not callable(sleep):
            raise TypeError("disk_usage_provider and sleep must be callable")
        self._database = database
        self._reader = reader
        self._library = library
        self._disk_budget = disk_budget
        self._identity = library_root_identity
        self._publisher = publisher
        self._disk_usage_provider = disk_usage_provider
        self._sleep = sleep
        self._operation_lock = Lock()

    def create(
        self,
        *,
        series_id: UUID,
        chapter_ids: tuple[UUID, ...],
    ) -> CbrExportResult:
        trace_id = create_trace_id()
        trace_token = activate_trace_id(trace_id)
        operation_id = uuid4()
        stage_directory: Path | None = None
        reserved = False
        lock_acquired = False
        try:
            self._operation_lock.acquire()
            lock_acquired = True
            if type(series_id) is not UUID or series_id.version != 4:
                raise self._failure(
                    ErrorCode.EXP_SOURCE_INVALID,
                    "series_id_validation",
                    trace_id=trace_id,
                    status_code=422,
                )
            if (
                type(chapter_ids) is not tuple
                or not 1 <= len(chapter_ids) <= MAX_CBR_PAGE_COUNT
                or any(
                    type(chapter_id) is not UUID or chapter_id.version != 4
                    for chapter_id in chapter_ids
                )
                or len(set(chapter_ids)) != len(chapter_ids)
            ):
                raise self._failure(
                    ErrorCode.EXP_SOURCE_INVALID,
                    "chapter_selection_validation",
                    trace_id=trace_id,
                    status_code=422,
                )

            series = self._library.get_series(series_id)
            if series is None:
                raise self._failure(
                    ErrorCode.EXP_SOURCE_INVALID,
                    "series_not_found",
                    trace_id=trace_id,
                    status_code=404,
                )
            records = self._reader.get_ready_chapter_records_by_ids(chapter_ids)
            if any(record.series_id != series_id for record in records):
                raise self._failure(
                    ErrorCode.EXP_SOURCE_INVALID,
                    "chapter_series_mismatch",
                    trace_id=trace_id,
                    status_code=422,
                )
            expected_page_count = sum(record.page_count for record in records)
            if not 1 <= expected_page_count <= MAX_CBR_PAGE_COUNT:
                raise self._failure(
                    ErrorCode.EXP_SOURCE_INVALID,
                    "page_count_limit",
                    trace_id=trace_id,
                    status_code=422,
                )

            self._disk_budget.reserve_bounded_operation(
                operation_id,
                page_bytes=MAX_CBR_TEMP_PNG_BYTES,
                cbz_bytes=MAX_CBR_BYTES,
            )
            reserved = True
            self._check_library_volume_capacity()

            stage_directory = ensure_managed_directory(
                f"exports/cbr/.staging/{operation_id}",
                data_root_identity=self._identity,
                storage_gate=self._database.storage_gate,
                component_name="cbr_export",
                job_id=operation_id,
                trace_id=trace_id,
            )
            output_directory = ensure_managed_directory(
                f"exports/cbr/{series_id}",
                data_root_identity=self._identity,
                storage_gate=self._database.storage_gate,
                component_name="cbr_export",
                job_id=operation_id,
                trace_id=trace_id,
            )

            files: list[Rar5StoreFile] = []
            write_budget = _PngWriteBudget(
                self._disk_budget,
                operation_id,
                max_bytes=MAX_CBR_TEMP_PNG_BYTES,
            )
            for chapter in records:
                chapter_page_count = 0
                pages = self._reader.iter_verified_chapter_pages(chapter.chapter_id)
                try:
                    for page, payload in pages:
                        chapter_page_count += 1
                        if chapter_page_count > chapter.page_count:
                            raise self._failure(
                                ErrorCode.EXP_HASH_MISMATCH,
                                "source_page_count",
                                trace_id=trace_id,
                                status_code=409,
                            )
                        png_path = stage_directory / f"page-{len(files) + 1:05d}.png"
                        png_size, png_sha256 = self._convert_verified_page_to_png(
                            page,
                            payload,
                            png_path,
                            operation_id=operation_id,
                            trace_id=trace_id,
                            write_budget=write_budget,
                        )
                        member_name = f"{len(files) + 1:05d}_" + self._page_member_name(
                            chapter.chapter_raw_label,
                            chapter.chapter_id,
                            page.page_ordinal,
                        )
                        files.append(
                            Rar5StoreFile(
                                member_name=member_name,
                                source_path=png_path,
                                size_bytes=png_size,
                                sha256=png_sha256,
                            )
                        )
                finally:
                    pages.close()
                if chapter_page_count != chapter.page_count:
                    raise self._failure(
                        ErrorCode.EXP_HASH_MISMATCH,
                        "source_page_count",
                        trace_id=trace_id,
                        status_code=409,
                    )

            if len(files) != expected_page_count:
                raise self._failure(
                    ErrorCode.EXP_HASH_MISMATCH,
                    "aggregate_page_count",
                    trace_id=trace_id,
                    status_code=409,
                )

            filename = self._archive_filename(
                series.canonical_title,
                records[0].chapter_raw_label,
                records[-1].chapter_raw_label,
                operation_id,
            )
            destination = output_directory / filename
            archive_temporary = stage_directory / "archive.tmp"
            writer = Rar5StoreWriter(
                publisher=self._publisher,
                data_root_identity=self._identity,
                max_archive_bytes=MAX_CBR_BYTES,
                storage_gate=self._database.storage_gate,
                sleep=self._sleep,
            )
            result_holder: list[Rar5WriteResult] = []

            def write_archive() -> int:
                result = writer.write(
                    tuple(files),
                    temporary_path=archive_temporary,
                    destination_path=destination,
                    operation_id=operation_id,
                )
                result_holder.append(result)
                return result.size_bytes

            self._disk_budget.write_reserved(
                operation_id,
                category="cbz",
                requested_bytes=MAX_CBR_BYTES,
                write=write_archive,
            )
            if len(result_holder) != 1:
                raise self._failure(
                    ErrorCode.EXP_HASH_MISMATCH,
                    "archive_publish_result",
                    trace_id=trace_id,
                    status_code=409,
                )
            write_result = result_holder[0]
            relative_path = self._relative_path(write_result.path)
            verified_at = datetime.now(UTC)
            asset = Asset.new_local(
                AssetType.CBR,
                relative_path,
                write_result.sha256,
                write_result.size_bytes,
                created_at=verified_at,
                verified_at=verified_at,
            )
            with self._database.write_session() as session:
                session.add(asset_to_row(asset))

            labels = tuple(record.display_label for record in records)
            logger.info(
                "cbr_export.completed",
                extra={
                    "asset_id": str(asset.asset_id),
                    "series_id": str(series_id),
                    "chapter_count": len(records),
                    "page_count": write_result.page_count,
                    "size_bytes": write_result.size_bytes,
                    "sha256": write_result.sha256,
                    "stage": "complete",
                    "trace_id": trace_id,
                },
            )
            return CbrExportResult(
                asset=asset,
                filename=filename,
                series_title=series.canonical_title,
                chapter_labels=labels,
                page_count=write_result.page_count,
            )
        except CbrExportFailure as error:
            self._log_failure(error)
            raise
        except DiskBudgetUnavailableError as error:
            failure = self._failure(
                ErrorCode.SYS_DISK_BUDGET_EXCEEDED,
                "disk_preflight",
                trace_id=trace_id,
                retryable=True,
                status_code=507,
                cause_type=type(error).__name__,
            )
            self._log_failure(failure)
            raise failure from None
        except ReaderAssetNotFound as error:
            failure = self._failure(
                ErrorCode.EXP_SOURCE_INVALID,
                "chapter_selection",
                trace_id=trace_id,
                status_code=404,
                cause_type=type(error).__name__,
            )
            self._log_failure(failure)
            raise failure from None
        except ReaderAssetError as error:
            failure = self._failure(
                ErrorCode.EXP_HASH_MISMATCH,
                "source_cbz_verification",
                trace_id=trace_id,
                status_code=409,
                cause_type=type(error).__name__,
            )
            self._log_failure(failure)
            raise failure from None
        except Rar5WriteError as error:
            failure = self._failure(
                error.error_code,
                error.stage,
                trace_id=error.trace_id,
                retryable=False,
                status_code=(
                    409
                    if error.error_code
                    in {ErrorCode.EXP_HASH_MISMATCH, ErrorCode.EXP_DESTINATION_CONFLICT}
                    else 500
                ),
                cause_type=error.cause_type or type(error).__name__,
            )
            self._log_failure(failure)
            raise failure from None
        except Exception as error:
            code = (
                ErrorCode.EXP_DISK_FULL
                if isinstance(error, OSError) and is_disk_full_os_error(error)
                else ErrorCode.DB_CONNECTION_FAILED
                if type(error).__module__.startswith("sqlalchemy")
                else ErrorCode.EXP_COPY_FAILED
            )
            failure = self._failure(
                code,
                "build",
                trace_id=trace_id,
                retryable=code in {ErrorCode.EXP_DISK_FULL, ErrorCode.DB_CONNECTION_FAILED},
                status_code=(
                    507
                    if code is ErrorCode.EXP_DISK_FULL
                    else 503
                    if code is ErrorCode.DB_CONNECTION_FAILED
                    else 500
                ),
                cause_type=type(error).__name__,
            )
            self._log_failure(failure)
            raise failure from None
        finally:
            try:
                if stage_directory is not None:
                    self._remove_stage_directory(stage_directory, trace_id=trace_id)
            finally:
                try:
                    if reserved:
                        try:
                            self._disk_budget.release_bounded_operation(operation_id)
                        except Exception as error:
                            logger.error(
                                "cbr_export.reservation_release_failed",
                                extra={
                                    "stage": "reservation_release",
                                    "trace_id": trace_id,
                                    "cause_type": type(error).__name__,
                                },
                            )
                finally:
                    if lock_acquired:
                        self._operation_lock.release()
                    reset_trace_id(trace_token)

    def recover_stale_staging(self) -> int:
        """Remove only abandoned, UUID-owned CBR staging directories."""
        trace_id = create_trace_id()
        trace_token = activate_trace_id(trace_id)
        staging_root = self._identity.canonical_path / "exports" / "cbr" / ".staging"
        current = self._identity.canonical_path
        cleaned = 0
        try:
            self._identity.assert_current()
            for component in ("exports", "cbr", ".staging"):
                current = current / component
                try:
                    info = current.lstat()
                except FileNotFoundError:
                    return 0
                attributes = getattr(info, "st_file_attributes", 0)
                if (
                    stat.S_ISLNK(info.st_mode)
                    or attributes & _REPARSE_POINT_ATTRIBUTE
                    or not stat.S_ISDIR(info.st_mode)
                ):
                    logger.warning(
                        "cbr_export.staging_recovery_skipped",
                        extra={
                            "stage": "staging_recovery",
                            "trace_id": trace_id,
                            "cause_type": "UnsafeStagingRoot",
                        },
                    )
                    return 0

            for entry in staging_root.iterdir():
                try:
                    operation_id = UUID(entry.name)
                except ValueError:
                    continue
                if operation_id.version != 4 or str(operation_id) != entry.name:
                    continue
                self._remove_stage_directory(entry, trace_id=trace_id)
                if not entry.exists():
                    cleaned += 1
            if cleaned:
                logger.info(
                    "cbr_export.staging_recovery_completed",
                    extra={
                        "stage": "staging_recovery",
                        "trace_id": trace_id,
                        "cleaned_count": cleaned,
                    },
                )
            return cleaned
        except Exception as error:
            logger.warning(
                "cbr_export.staging_recovery_failed",
                extra={
                    "stage": "staging_recovery",
                    "trace_id": trace_id,
                    "cause_type": type(error).__name__,
                },
            )
            return cleaned
        finally:
            reset_trace_id(trace_token)

    def _check_library_volume_capacity(self) -> None:
        snapshot = self._disk_budget.snapshot()
        if snapshot.volume_serial == self._identity.volume_serial:
            return
        usage = self._disk_usage_provider(self._identity.canonical_path)
        free_bytes = getattr(usage, "free", None)
        required_bytes = MAX_CBR_TEMP_PNG_BYTES + MAX_CBR_BYTES + MINIMUM_RESERVED_BYTES
        if type(free_bytes) is not int or free_bytes < required_bytes:
            raise DiskBudgetUnavailableError(
                "LIBRARY_VOLUME_RESERVATION_UNAVAILABLE",
                free_bytes=free_bytes if type(free_bytes) is int else None,
                required_bytes=required_bytes,
            )

    def _convert_verified_page_to_png(
        self,
        page: ReaderPageRecord,
        payload: bytes,
        destination: Path,
        *,
        operation_id: UUID,
        trace_id: str,
        write_budget: _PngWriteBudget,
    ) -> tuple[int, str]:
        if type(payload) is not bytes or len(payload) != page.page_size_bytes:
            raise self._failure(
                ErrorCode.EXP_HASH_MISMATCH,
                "source_page_size",
                trace_id=trace_id,
                status_code=409,
            )
        if ImageFile.LOAD_TRUNCATED_IMAGES:
            raise self._failure(
                ErrorCode.EXP_SOURCE_INVALID,
                "truncated_image_policy",
                trace_id=trace_id,
                status_code=422,
            )

        rgba: Image.Image | None = None
        try:
            with Image.open(io.BytesIO(payload), formats=_SUPPORTED_SOURCE_FORMATS) as image:
                if image.format != _MIME_TO_FORMAT.get(page.page_mime):
                    raise ValueError("validated page format does not match its MIME")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("animated pages cannot be represented as one CBR PNG")
                width, height = image.size
                if (
                    width <= 0
                    or height <= 0
                    or width > MAX_CBR_IMAGE_WIDTH
                    or height > MAX_CBR_IMAGE_HEIGHT
                    or width * height > MAX_CBR_IMAGE_PIXELS
                ):
                    raise ValueError("page dimensions exceed the approved CBR limit")
                image.load()
                rgba = image.convert("RGBA")
            expected_pixel_sha256 = self._hash_rgba_pixels(rgba)
        except Exception as error:
            if rgba is not None:
                rgba.close()
            raise self._failure(
                ErrorCode.EXP_SOURCE_INVALID,
                "image_decode",
                trace_id=trace_id,
                status_code=422,
                cause_type=type(error).__name__,
            ) from None
        stream: RetryingFileStream | None = None
        try:
            self._identity.assert_current()
            stream = RetryingFileStream.create(
                destination,
                component="cbr_export",
                stage="png_staging",
                trace_id=trace_id,
                job_id=str(operation_id),
                validate_path=self._identity.assert_current,
                sleep=self._sleep,
                storage_gate=self._database.storage_gate,
            )
            output = _PngOutput(stream, write_budget)
            if page.page_mime == "image/png" and payload[24] == 8:
                # Keep only 8-bit PNGs; other inputs use the reader's RGBA8 profile.
                write_budget.write(stream, payload)
            else:
                rgba.save(cast(BinaryIO, output), format="PNG", optimize=False)
            stream.sync()
            stream.close()
            stream = None
            size_bytes, sha256 = self._hash_managed_file(destination)
            if size_bytes <= 0:
                raise ValueError("encoded PNG is empty")
            with destination.open("rb") as encoded_stream:
                with Image.open(encoded_stream, formats=("PNG",)) as decoded:
                    if decoded.size != rgba.size or getattr(decoded, "n_frames", 1) != 1:
                        raise self._failure(
                            ErrorCode.EXP_HASH_MISMATCH,
                            "png_dimensions",
                            trace_id=trace_id,
                            status_code=409,
                        )
                    decoded.load()
                    decoded_rgba = decoded.convert("RGBA")
                    try:
                        if self._hash_rgba_pixels(decoded_rgba) != expected_pixel_sha256:
                            raise self._failure(
                                ErrorCode.EXP_HASH_MISMATCH,
                                "png_pixels",
                                trace_id=trace_id,
                                status_code=409,
                            )
                    finally:
                        decoded_rgba.close()
            return size_bytes, sha256
        except CbrExportFailure:
            raise
        except Exception as error:
            code = (
                ErrorCode.EXP_DISK_FULL
                if isinstance(error, OSError) and is_disk_full_os_error(error)
                else ErrorCode.EXP_COPY_FAILED
            )
            raise self._failure(
                code,
                "png_conversion",
                trace_id=trace_id,
                retryable=code is ErrorCode.EXP_DISK_FULL,
                status_code=507 if code is ErrorCode.EXP_DISK_FULL else 500,
                cause_type=type(error).__name__,
            ) from None
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception as error:
                    logger.warning(
                        "cbr_export.png_stream_close_failed",
                        extra={
                            "stage": "png_stream_close",
                            "trace_id": trace_id,
                            "cause_type": type(error).__name__,
                        },
                    )
            rgba.close()

    @staticmethod
    def _hash_rgba_pixels(image: Image.Image) -> str:
        if image.mode != "RGBA":
            raise ValueError("pixel hashing requires RGBA input")
        width, height = image.size
        digest = hashlib.sha256()
        for top in range(0, height, _RGBA_HASH_BAND_ROWS):
            band = image.crop((0, top, width, min(height, top + _RGBA_HASH_BAND_ROWS)))
            try:
                digest.update(band.tobytes())
            finally:
                band.close()
        return digest.hexdigest()

    def _hash_managed_file(self, path: Path) -> tuple[int, str]:
        self._assert_regular_file(path)
        digest = hashlib.sha256()
        size_bytes = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1_048_576):
                digest.update(chunk)
                size_bytes += len(chunk)
                if size_bytes > MAX_CBR_TEMP_PNG_BYTES:
                    raise ValueError("aggregate PNG staging limit exceeded")
        self._identity.assert_current()
        return size_bytes, digest.hexdigest()

    def _assert_regular_file(self, path: Path) -> None:
        self._identity.assert_current()
        root = self._identity.canonical_path
        relative = path.relative_to(root)
        current = root
        for index, component in enumerate(relative.parts):
            current = current / component
            info = current.lstat()
            attributes = getattr(info, "st_file_attributes", 0)
            if stat.S_ISLNK(info.st_mode) or attributes & _REPARSE_POINT_ATTRIBUTE:
                raise ValueError("managed CBR path traverses a reparse point")
            if index < len(relative.parts) - 1:
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("managed CBR parent is not a directory")
            elif not stat.S_ISREG(info.st_mode):
                raise ValueError("managed CBR page is not a regular file")

    def _remove_stage_directory(self, path: Path, *, trace_id: str) -> None:
        try:
            self._identity.assert_current()
            relative = path.relative_to(self._identity.canonical_path)
            if len(relative.parts) != 4 or relative.parts[:3] != ("exports", "cbr", ".staging"):
                raise ValueError("CBR stage directory is outside its reserved location")
            current = self._identity.canonical_path
            for index, component in enumerate(relative.parts):
                current = current / component
                info = current.lstat()
                attributes = getattr(info, "st_file_attributes", 0)
                if stat.S_ISLNK(info.st_mode) or attributes & _REPARSE_POINT_ATTRIBUTE:
                    raise ValueError("CBR stage path contains a reparse point")
                if index < len(relative.parts) - 1:
                    if not stat.S_ISDIR(info.st_mode):
                        raise ValueError("CBR stage parent is not a directory")
                elif not stat.S_ISDIR(info.st_mode):
                    raise ValueError("CBR stage directory identity is unsafe")
            for child in path.iterdir():
                child_info = child.lstat()
                child_attributes = getattr(child_info, "st_file_attributes", 0)
                if (
                    stat.S_ISLNK(child_info.st_mode)
                    or child_attributes & _REPARSE_POINT_ATTRIBUTE
                    or not stat.S_ISREG(child_info.st_mode)
                    or not (
                        child.name == "archive.tmp"
                        or re.fullmatch(r"page-\d{5}\.png", child.name)
                    )
                ):
                    raise ValueError("CBR stage contains an unmanaged entry")
            shutil.rmtree(path)
        except FileNotFoundError:
            return
        except Exception as error:
            logger.warning(
                "cbr_export.staging_cleanup_failed",
                extra={
                    "stage": "staging_cleanup",
                    "trace_id": trace_id,
                    "cause_type": type(error).__name__,
                },
            )

    def _relative_path(self, path: Path) -> str:
        if not path.is_absolute():
            raise ValueError("published CBR path must be absolute")
        relative = path.relative_to(self._identity.canonical_path)
        value = "/".join(relative.parts)
        validate_relative_path(value)
        return value

    @staticmethod
    def _safe_label(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
        cleaned = cleaned.strip(" ._-")[:48]
        return cleaned or "sin-etiqueta"

    @classmethod
    def _page_member_name(cls, label: str, chapter_id: UUID, ordinal: int) -> str:
        return f"Capitulo_{cls._safe_label(label)}_{chapter_id.hex}_Pagina_{ordinal:05d}.png"

    @classmethod
    def _archive_filename(
        cls,
        title: str,
        first_chapter: str,
        last_chapter: str,
        operation_id: UUID,
    ) -> str:
        safe_title = cls._safe_label(title)
        first_label = cls._safe_label(first_chapter)
        last_label = cls._safe_label(last_chapter)
        return f"{safe_title} - Capitulos {first_label}-{last_label} - {operation_id.hex[:8]}.cbr"

    @staticmethod
    def _failure(
        code: ErrorCode,
        stage: str,
        *,
        trace_id: str,
        retryable: bool = False,
        status_code: int,
        cause_type: str | None = None,
    ) -> CbrExportFailure:
        return CbrExportFailure(
            code,
            stage,
            trace_id=trace_id,
            retryable=retryable,
            status_code=status_code,
            cause_type=cause_type,
        )

    @staticmethod
    def _log_failure(error: CbrExportFailure) -> None:
        logger.error(
            "cbr_export.failed",
            extra={
                "error_code": error.code.value,
                "retryable": error.retryable,
                "stage": error.stage,
                "cause_type": error.cause_type,
                "trace_id": error.trace_id,
            },
        )
