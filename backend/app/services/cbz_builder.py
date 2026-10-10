"""Deterministic CBZ construction and verification before delegated publication."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import stat
import time
import zipfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, NoReturn, Protocol, cast
from uuid import UUID
from xml.etree import ElementTree as ET

from PIL import Image

from app.domain.job_manifest import MAX_MANIFEST_PAGE_COUNT
from app.persistence.disk_budget import CBZ_RESERVATION_BYTES, DiskBudgetManager
from app.persistence.filesystem_retry import (
    ADR_0087_RETRY_DELAYS_SECONDS,
    WIN32_TRANSIENT_LOCK_ERRORS,
    RetryingFileStream,
    native_winerror,
)
from app.persistence.storage_gate import (
    SharedStorageGate,
    StorageGateClosedError,
    is_disk_full_os_error,
)
from app.tracing import create_trace_id, current_trace_id

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_COPY_CHUNK_SIZE = 64 * 1024
_SUPPORTED_IMAGE_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
# ADR-0088: approved maximum physical CBZ size.
MAX_CBZ_BYTES_V1 = CBZ_RESERVATION_BYTES
# ADR-0086's per-page byte ceiling is enforced again at the packaging boundary.
MAX_PAGE_BYTES_V1 = 67_108_864
_ZIP_LOCAL_HEADER_BYTES = 30
_ZIP_CENTRAL_HEADER_BYTES = 46
_ZIP_END_RECORD_BYTES = 22
logger = logging.getLogger("yomira.cbz_builder")
_current_job_id: ContextVar[str | None] = ContextVar("cbz_builder_job_id", default=None)


def _require_path(value: object, field_name: str) -> Path:
    """Validate a path at the object boundary without trusting annotations."""
    if not isinstance(value, Path):
        raise TypeError(f"{field_name} must be a pathlib Path")
    return value


class CBZBuildError(RuntimeError):
    """Sanitized stage identifier for a failed CBZ build or publication."""

    def __init__(self, stage: str, *, cause_type: str | None = None) -> None:
        self.stage = stage
        self.cause_type = cause_type
        super().__init__(f"CBZ construction failed during {stage}.")


class AtomicNoReplacePublisher(Protocol):
    """Same-volume atomic promotion adapter that fails if the target exists.

    Implementations must never replace a destination. A failure may have an
    ambiguous filesystem outcome; callers preserve both names and do not retry
    blindly unless the publisher proves the operation did not mutate them.
    """

    def publish_no_replace(self, source: Path, destination: Path) -> None:
        """Atomically publish source at destination without replacing a file."""


@dataclass(frozen=True, slots=True)
class ValidatedPage:
    """Immutable reference to one already-validated page on disk."""

    ordinal: int
    path: Path
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal <= 0:
            raise ValueError("ordinal must be a positive integer")
        object.__setattr__(self, "path", _require_path(self.path, "path"))
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")
        if type(self.sha256) is not str or _SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True, slots=True)
class ComicInfoMetadata:
    """Known ComicInfo fields; absent values remain absent in the generated XML."""

    title: str | None = None
    series: str | None = None
    number: str | None = None
    volume: str | None = None
    summary: str | None = None
    writer: str | None = None
    genres: tuple[str, ...] | None = None
    language_iso: str | None = None
    manga: str | None = None
    web: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "title",
            "series",
            "number",
            "volume",
            "summary",
            "writer",
            "language_iso",
            "manga",
            "web",
        ):
            value = getattr(self, field_name)
            if value is not None and type(value) is not str:
                raise TypeError(f"{field_name} must be a string or None")
        if self.genres is not None and (
            type(self.genres) is not tuple
            or any(type(genre) is not str for genre in self.genres)
        ):
            raise TypeError("genres must be a tuple of strings or None")


@dataclass(frozen=True, slots=True)
class CBZBuildResult:
    path: Path
    sha256: str
    size_bytes: int
    page_count: int


@dataclass(frozen=True, slots=True)
class _PreparedPage:
    page: ValidatedPage
    image_format: str
    member_name: str


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    """Identity and metadata used to reject retries after a source changes."""

    device: int
    file_id: int
    size_bytes: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> _FileSnapshot:
        device = getattr(info, "st_dev", None)
        file_id = getattr(info, "st_ino", None)
        modified_ns = getattr(info, "st_mtime_ns", None)
        changed_ns = getattr(info, "st_ctime_ns", None)
        if (
            type(device) is not int
            or type(file_id) is not int
            or type(modified_ns) is not int
            or type(changed_ns) is not int
        ):
            raise OSError("stable filesystem identity is unavailable")
        if device < 0 or file_id <= 0:
            raise OSError("stable filesystem identity is unavailable")
        return cls(
            device=device,
            file_id=file_id,
            size_bytes=info.st_size,
            modified_ns=modified_ns,
            changed_ns=changed_ns,
        )

    def same_open_file_version(self, other: _FileSnapshot) -> bool:
        """Compare fields whose meaning is consistent between path and handle stats."""
        return (
            self.device,
            self.file_id,
            self.size_bytes,
            self.modified_ns,
        ) == (
            other.device,
            other.file_id,
            other.size_bytes,
            other.modified_ns,
        )


@dataclass(slots=True)
class _LockRetryState:
    retries_used: int = 0
    handle_snapshot: _FileSnapshot | None = None
    exhaustion_logged: bool = False


def _retry_log(stage: str, error: OSError, retry_index: int) -> None:
    logger.warning(
        "cbz_filesystem_lock_retry",
        extra={
            "component": "cbz_builder",
            "stage": stage,
            "winerror": native_winerror(error),
            "retry": retry_index + 1,
            "job_id": _current_job_id.get(),
            "trace_id": current_trace_id() or create_trace_id(),
        },
    )


def _retry_exhausted_log(stage: str, error: OSError, retries_used: int) -> None:
    logger.error(
        "cbz_filesystem_lock_retries_exhausted",
        extra={
            "component": "cbz_builder",
            "stage": stage,
            "winerror": native_winerror(error),
            "attempt": retries_used + 1,
            "job_id": _current_job_id.get(),
            "trace_id": current_trace_id() or create_trace_id(),
        },
    )


def _safe_job_id(value: UUID | str | None) -> str | None:
    if value is None:
        return None
    if type(value) is UUID:
        parsed = value
    elif type(value) is str:
        try:
            parsed = UUID(value)
        except ValueError as error:
            raise ValueError("job_id must be a UUIDv4") from error
    else:
        raise TypeError("job_id must be a UUID, UUID string, or None")
    if parsed.version != 4:
        raise ValueError("job_id must be a UUIDv4")
    return str(parsed)


def _stream_is_closed(stream: BinaryIO) -> bool:
    try:
        closed = stream.closed
    except Exception:
        return False
    return type(closed) is bool and closed


def _close_read_stream(stream: BinaryIO, *, stage: str) -> OSError | None:
    """Close a read handle; return a transient error only when closure is proven."""
    close_error: BaseException | None = None
    try:
        stream.close()
    except BaseException as error:
        close_error = error
    if close_error is None and _stream_is_closed(stream):
        return None
    if (
        isinstance(close_error, OSError)
        and native_winerror(close_error) in WIN32_TRANSIENT_LOCK_ERRORS
        and _stream_is_closed(stream)
    ):
        return close_error

    logger.error(
        "cbz_read_handle_close_unverified",
        extra={
            "component": "cbz_builder",
            "stage": stage,
            "winerror": (
                native_winerror(close_error)
                if isinstance(close_error, OSError)
                else None
            ),
            "cause_type": type(close_error).__name__ if close_error else None,
            "job_id": _current_job_id.get(),
            "trace_id": current_trace_id() or create_trace_id(),
        },
    )
    raise CBZBuildError(
        "read_handle_close_unverified",
        cause_type=type(close_error).__name__ if close_error else None,
    ) from None


def _log_writer_close_failure(error: BaseException) -> None:
    logger.error(
        "cbz_writer_close_unverified",
        extra={
            "component": "cbz_builder",
            "stage": "cbz_staging_close",
            "winerror": native_winerror(error) if isinstance(error, OSError) else None,
            "cause_type": type(error).__name__,
            "job_id": _current_job_id.get(),
            "trace_id": current_trace_id() or create_trace_id(),
        },
    )


def _retry_os_stat(
    operation: Callable[[], os.stat_result],
    *,
    stage: str,
) -> os.stat_result:
    retries_used = 0
    while True:
        try:
            return operation()
        except OSError as error:
            if native_winerror(error) not in WIN32_TRANSIENT_LOCK_ERRORS:
                raise
            if retries_used >= len(ADR_0087_RETRY_DELAYS_SECONDS):
                _retry_exhausted_log(stage, error, retries_used)
                raise
            _retry_log(stage, error, retries_used)
            time.sleep(ADR_0087_RETRY_DELAYS_SECONDS[retries_used])
            retries_used += 1


def _retry_stat(
    path: Path,
    *,
    stage: str,
    follow_symlinks: bool = False,
) -> os.stat_result:
    operation = path.stat if follow_symlinks else path.lstat
    return _retry_os_stat(operation, stage=stage)


def _snapshot_matches(path: Path, snapshot: _FileSnapshot, *, stage: str) -> bool:
    try:
        observed = _FileSnapshot.from_stat(_retry_stat(path, stage=stage))
    except FileNotFoundError:
        return False
    return observed == snapshot


def _retry_after_lock(
    path: Path,
    snapshot: _FileSnapshot,
    error: OSError,
    *,
    stage: str,
    state: _LockRetryState,
    changed_stage: str,
) -> None:
    if native_winerror(error) not in WIN32_TRANSIENT_LOCK_ERRORS:
        raise error
    if not _snapshot_matches(path, snapshot, stage=f"{stage}_pre_retry_stat"):
        raise CBZBuildError(changed_stage)
    if state.retries_used >= len(ADR_0087_RETRY_DELAYS_SECONDS):
        if not state.exhaustion_logged:
            _retry_exhausted_log(stage, error, state.retries_used)
            state.exhaustion_logged = True
        raise error
    _retry_log(stage, error, state.retries_used)
    time.sleep(ADR_0087_RETRY_DELAYS_SECONDS[state.retries_used])
    state.retries_used += 1
    if not _snapshot_matches(path, snapshot, stage=f"{stage}_post_wait_stat"):
        raise CBZBuildError(changed_stage)


def _open_snapshot_stream(
    path: Path,
    snapshot: _FileSnapshot,
    *,
    stage: str,
    state: _LockRetryState,
    changed_stage: str,
    offset: int = 0,
) -> BinaryIO:
    while True:
        if not _snapshot_matches(path, snapshot, stage=f"{stage}_before_open_stat"):
            raise CBZBuildError(changed_stage)
        try:
            stream = path.open("rb")
        except OSError as error:
            _retry_after_lock(
                path,
                snapshot,
                error,
                stage=stage,
                state=state,
                changed_stage=changed_stage,
            )
            continue
        try:
            opened_snapshot = _FileSnapshot.from_stat(os.fstat(stream.fileno()))
            if not opened_snapshot.same_open_file_version(snapshot):
                raise CBZBuildError(changed_stage)
            if state.handle_snapshot is None:
                state.handle_snapshot = opened_snapshot
            elif opened_snapshot != state.handle_snapshot:
                raise CBZBuildError(changed_stage)
            if offset:
                stream.seek(offset, os.SEEK_SET)
            return stream
        except OSError as error:
            close_error = _close_read_stream(stream, stage=f"{stage}_open_close")
            if (
                close_error is not None
                and native_winerror(error) not in WIN32_TRANSIENT_LOCK_ERRORS
            ):
                raise error
            _retry_after_lock(
                path,
                snapshot,
                error,
                stage=stage,
                state=state,
                changed_stage=changed_stage,
            )
        except BaseException:
            stream.close()
            raise


class _RestartablePageReader:
    """Stream a stable page and resume a failed read from its last good offset."""

    def __init__(
        self,
        path: Path,
        snapshot: _FileSnapshot,
        *,
        stage: str,
        changed_stage: str,
    ) -> None:
        self._path = path
        self._snapshot = snapshot
        self._stage = stage
        self._changed_stage = changed_stage
        self._retry_state = _LockRetryState()
        self._stream: BinaryIO | None = None
        self._offset = 0

    def __enter__(self) -> _RestartablePageReader:
        self._stream = _open_snapshot_stream(
            self._path,
            self._snapshot,
            stage=self._stage,
            state=self._retry_state,
            changed_stage=self._changed_stage,
        )
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()

    def read(self, size: int = -1) -> bytes:
        while True:
            if self._stream is None:
                self._stream = _open_snapshot_stream(
                    self._path,
                    self._snapshot,
                    stage=self._stage,
                    state=self._retry_state,
                    changed_stage=self._changed_stage,
                    offset=self._offset,
                )
            try:
                chunk = self._stream.read(size)
            except OSError as error:
                try:
                    self.close()
                except OSError as close_error:
                    error.add_note(
                        f"Read handle close failed ({type(close_error).__name__})."
                    )
                    raise error from None
                _retry_after_lock(
                    self._path,
                    self._snapshot,
                    error,
                    stage=self._stage,
                    state=self._retry_state,
                    changed_stage=self._changed_stage,
                )
                self._stream = _open_snapshot_stream(
                    self._path,
                    self._snapshot,
                    stage=self._stage,
                    state=self._retry_state,
                    changed_stage=self._changed_stage,
                    offset=self._offset,
                )
                continue
            self._offset += len(chunk)
            return chunk

    def ensure_unchanged(self) -> None:
        if self._stream is not None:
            try:
                opened_snapshot = _FileSnapshot.from_stat(
                    os.fstat(self._stream.fileno())
                )
            except OSError as error:
                try:
                    self.close()
                except OSError as close_error:
                    error.add_note(
                        f"Read handle close failed ({type(close_error).__name__})."
                    )
                    raise error from None
                _retry_after_lock(
                    self._path,
                    self._snapshot,
                    error,
                    stage=f"{self._stage}_handle_final_stat",
                    state=self._retry_state,
                    changed_stage=self._changed_stage,
                )
                self._stream = _open_snapshot_stream(
                    self._path,
                    self._snapshot,
                    stage=f"{self._stage}_handle_final_stat",
                    state=self._retry_state,
                    changed_stage=self._changed_stage,
                    offset=self._offset,
                )
            else:
                if opened_snapshot != self._retry_state.handle_snapshot:
                    self.close()
                    raise CBZBuildError(self._changed_stage)
            self.close()
        if not _snapshot_matches(
            self._path,
            self._snapshot,
            stage=f"{self._stage}_final_stat",
        ):
            raise CBZBuildError(self._changed_stage)


class _SizeBoundedZipStream:
    """Seekable ZIP output wrapper that rejects writes beyond a byte budget."""

    def __init__(
        self,
        stream: BinaryIO,
        *,
        max_bytes: int,
        disk_budget_manager: DiskBudgetManager | None = None,
        job_id: UUID | None = None,
    ) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._disk_budget_manager = disk_budget_manager
        self._job_id = job_id

    @property
    def name(self) -> str:
        return str(self._stream.name)

    @property
    def closed(self) -> bool:
        return self._stream.closed

    def write(self, value: bytes) -> int:
        offset = self._stream.tell()
        try:
            before_size = os.fstat(self._stream.fileno()).st_size
        except OSError as error:
            raise CBZBuildError(
                "archive_size_check_failed",
                cause_type=type(error).__name__,
            ) from None
        projected_size = max(before_size, offset + len(value))
        if projected_size > self._max_bytes:
            raise CBZBuildError("archive_size_limit_during_write")

        def write_and_measure_growth() -> int:
            written = self._stream.write(value)
            if written != len(value):
                raise OSError("short write while building CBZ")
            after_size = os.fstat(self._stream.fileno()).st_size
            if after_size < before_size or after_size > self._max_bytes:
                raise CBZBuildError("archive_size_check_failed")
            return after_size - before_size

        if self._disk_budget_manager is None:
            write_and_measure_growth()
        else:
            if self._job_id is None:
                raise CBZBuildError("disk_budget_reservation_missing")
            self._disk_budget_manager.write_reserved(
                self._job_id,
                category="cbz",
                requested_bytes=projected_size - before_size,
                write=write_and_measure_growth,
            )
        return len(value)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()

    def seekable(self) -> bool:
        return self._stream.seekable()

    def writable(self) -> bool:
        return self._stream.writable()

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()


@contextmanager
def _write_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    force_zip64: bool = False,
) -> Generator[BinaryIO]:
    member = cast(BinaryIO, archive.open(info, mode="w", force_zip64=force_zip64))
    try:
        yield member
    except BaseException as error:
        try:
            member.close()
        except BaseException as close_error:
            error.add_note(
                "ZIP member finalization failed during exception cleanup "
                f"({type(close_error).__name__}); candidate preserved."
            )
        raise
    else:
        member.close()


class CBZBuilder:
    """Build, verify and delegate publication of one chapter CBZ.

    The service streams source page bytes into an exclusive `.cbz.tmp`, verifies
    the closed archive from disk, then calls an injected atomic no-replace
    publisher. It does not index the resulting artifact or access the database.
    Composition must pass the approved archive byte ceiling explicitly.
    """

    def __init__(
        self,
        *,
        publisher: AtomicNoReplacePublisher,
        max_archive_bytes: int,
        storage_gate: SharedStorageGate | None = None,
        disk_budget_manager: DiskBudgetManager | None = None,
    ) -> None:
        if not callable(getattr(publisher, "publish_no_replace", None)):
            raise TypeError("publisher must implement publish_no_replace")
        if type(max_archive_bytes) is not int:
            raise TypeError("max_archive_bytes must be an integer")
        if not 0 < max_archive_bytes <= MAX_CBZ_BYTES_V1:
            raise ValueError("max_archive_bytes must be within the approved V1 limit")
        if storage_gate is not None and type(storage_gate) is not SharedStorageGate:
            raise TypeError("storage_gate must be a SharedStorageGate or None")
        if disk_budget_manager is not None and type(disk_budget_manager) is not DiskBudgetManager:
            raise TypeError("disk_budget_manager must be a DiskBudgetManager or None")
        self._publisher = publisher
        self._max_archive_bytes = max_archive_bytes
        self.storage_gate = storage_gate
        self._disk_budget_manager = disk_budget_manager

    def build(
        self,
        *,
        destination: Path,
        metadata: ComicInfoMetadata,
        pages: tuple[ValidatedPage, ...],
        job_id: UUID | str | None = None,
    ) -> CBZBuildResult:
        """Build and verify a CBZ, attaching only a canonical UUIDv4 to logs."""
        token = _current_job_id.set(_safe_job_id(job_id))
        try:
            return self._build_with_job_context(
                destination=destination,
                metadata=metadata,
                pages=pages,
            )
        finally:
            _current_job_id.reset(token)

    def _build_with_job_context(
        self,
        *,
        destination: Path,
        metadata: ComicInfoMetadata,
        pages: tuple[ValidatedPage, ...],
    ) -> CBZBuildResult:
        """Build and verify a CBZ, then publish only through the injected adapter."""
        self._validate_arguments(destination=destination, metadata=metadata, pages=pages)
        temporary = destination.with_name(f"{destination.name}.tmp")
        self._assert_output_absent(destination, stage="destination_exists")
        self._assert_output_absent(temporary, stage="temporary_exists")
        prepared_pages = self._prepare_pages(pages, temporary=temporary)
        comic_info = self._comic_info_bytes(metadata, page_count=len(prepared_pages))
        expected_archive_size = self._estimate_archive_size(prepared_pages, comic_info)
        if expected_archive_size > self._max_archive_bytes:
            raise CBZBuildError("archive_size_limit_preflight")

        try:
            self._write_archive(
                temporary,
                prepared_pages,
                comic_info,
                max_archive_bytes=self._max_archive_bytes,
                storage_gate=self.storage_gate,
                disk_budget_manager=self._disk_budget_manager,
            )
        except CBZBuildError:
            raise
        except StorageGateClosedError:
            raise
        except Exception as error:
            self._fail("temporary_write_failed", error)

        self._verify_archive(
            temporary,
            prepared_pages,
            comic_info,
            expected_archive_size=expected_archive_size,
            max_archive_bytes=self._max_archive_bytes,
        )
        try:
            archive_sha256, archive_size = self._hash_regular_file(
                temporary,
                max_bytes=self._max_archive_bytes,
            )
        except CBZBuildError:
            raise
        except Exception as error:
            self._fail("temporary_hash_failed", error)

        try:
            self._publisher.publish_no_replace(temporary, destination)
        except StorageGateClosedError:
            raise
        except Exception as error:
            self._fail("publish_failed", error)

        try:
            if self._path_exists(temporary):
                raise CBZBuildError("published_source_retained")
            published_sha256, published_size = self._hash_regular_file(
                destination,
                max_bytes=self._max_archive_bytes,
            )
        except CBZBuildError:
            raise
        except Exception as error:
            self._fail("published_artifact_unavailable", error)
        if (published_sha256, published_size) != (archive_sha256, archive_size):
            raise CBZBuildError("published_artifact_mismatch")

        return CBZBuildResult(
            path=destination,
            sha256=published_sha256,
            size_bytes=published_size,
            page_count=len(prepared_pages),
        )

    @staticmethod
    def _validate_arguments(
        *,
        destination: Path,
        metadata: ComicInfoMetadata,
        pages: tuple[ValidatedPage, ...],
    ) -> None:
        destination = _require_path(destination, "destination")
        if destination.suffix.casefold() != ".cbz":
            raise CBZBuildError("invalid_destination")
        try:
            parent_info = _retry_stat(
                destination.parent,
                stage="cbz_destination_parent_stat",
                follow_symlinks=True,
            )
        except FileNotFoundError:
            raise CBZBuildError("destination_parent_missing") from None
        except OSError as error:
            raise CBZBuildError(
                "destination_parent_missing",
                cause_type=type(error).__name__,
            ) from None
        if not stat.S_ISDIR(parent_info.st_mode):
            raise CBZBuildError("destination_parent_missing")
        if type(metadata) is not ComicInfoMetadata:
            raise TypeError("metadata must be ComicInfoMetadata")
        if type(pages) is not tuple or any(type(page) is not ValidatedPage for page in pages):
            raise TypeError("pages must be a tuple of ValidatedPage values")
        if len(pages) > MAX_MANIFEST_PAGE_COUNT:
            raise CBZBuildError("page_count_limit_exceeded")
        if any(page.size_bytes > MAX_PAGE_BYTES_V1 for page in pages):
            raise CBZBuildError("page_size_limit_exceeded")
        ordinals = sorted(page.ordinal for page in pages)
        if ordinals != list(range(1, len(pages) + 1)):
            raise CBZBuildError("invalid_page_ordinals")

    @classmethod
    def _prepare_pages(
        cls,
        pages: tuple[ValidatedPage, ...],
        *,
        temporary: Path,
    ) -> tuple[_PreparedPage, ...]:
        prepared: list[_PreparedPage] = []
        temporary_key = os.path.normcase(os.path.abspath(temporary))
        for page in sorted(pages, key=lambda item: item.ordinal):
            if os.path.normcase(os.path.abspath(page.path)) == temporary_key:
                raise CBZBuildError("page_path_conflicts_with_temporary")
            try:
                info = _retry_stat(page.path, stage="cbz_prepare_page_stat")
            except Exception as error:
                cls._fail("page_source_unavailable", error)
            if cls._is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
                raise CBZBuildError("page_source_unavailable")
            if info.st_size != page.size_bytes:
                raise CBZBuildError("page_source_size_mismatch")
            try:
                snapshot = _FileSnapshot.from_stat(info)
                image_format = cls._read_image_format(page.path, snapshot)
            except Exception as error:
                if isinstance(error, CBZBuildError):
                    raise
                cls._fail("page_source_format_unavailable", error)
            if image_format not in _SUPPORTED_IMAGE_FORMATS:
                raise CBZBuildError("unsupported_page_format")
            extension = _SUPPORTED_IMAGE_FORMATS[image_format]
            member_name = f"{page.ordinal:03d}.{extension}"
            prepared.append(_PreparedPage(page, image_format, member_name))
        return tuple(prepared)

    @classmethod
    def _read_image_format(cls, path: Path, snapshot: _FileSnapshot) -> str | None:
        retry_state = _LockRetryState()
        while True:
            stream = _open_snapshot_stream(
                path,
                snapshot,
                stage="cbz_page_format_read",
                state=retry_state,
                changed_stage="page_source_changed",
            )
            try:
                with Image.open(stream) as image:
                    image_format = image.format
                opened_snapshot = _FileSnapshot.from_stat(os.fstat(stream.fileno()))
                if opened_snapshot != retry_state.handle_snapshot:
                    raise CBZBuildError("page_source_changed")
            except OSError as error:
                try:
                    stream.close()
                except OSError as close_error:
                    error.add_note(
                        f"Image source handle close failed ({type(close_error).__name__})."
                    )
                    raise error from None
                _retry_after_lock(
                    path,
                    snapshot,
                    error,
                    stage="cbz_page_format_read",
                    state=retry_state,
                    changed_stage="page_source_changed",
                )
                continue
            except BaseException:
                stream.close()
                raise
            stream.close()
            if not _snapshot_matches(
                path,
                snapshot,
                stage="cbz_page_format_final_stat",
            ):
                raise CBZBuildError("page_source_changed")
            return image_format

    @staticmethod
    def _estimate_archive_size(
        pages: tuple[_PreparedPage, ...],
        comic_info: bytes,
    ) -> int:
        """Return the exact ZIP size for stored entries with the V1 layout.

        V1 limits keep every entry below ZIP64 thresholds and use seekable output,
        UTF-8 names, no extra fields/comments, ZIP_STORED, and no data descriptors.
        Each entry therefore contributes one 30-byte local header, its name,
        payload, one 46-byte central header, and the same name. The final classic
        end record is 22 bytes.
        """
        comic_info_name_bytes = len(b"ComicInfo.xml")
        size = (
            len(comic_info)
            + _ZIP_LOCAL_HEADER_BYTES
            + comic_info_name_bytes
            + _ZIP_CENTRAL_HEADER_BYTES
            + comic_info_name_bytes
            + _ZIP_END_RECORD_BYTES
        )
        for page in pages:
            name_bytes = len(page.member_name.encode("utf-8"))
            size += (
                page.page.size_bytes
                + _ZIP_LOCAL_HEADER_BYTES
                + name_bytes
                + _ZIP_CENTRAL_HEADER_BYTES
                + name_bytes
            )
        return size

    @classmethod
    def _comic_info_bytes(
        cls,
        metadata: ComicInfoMetadata,
        *,
        page_count: int,
    ) -> bytes:
        values: tuple[tuple[str, str | None], ...] = (
            ("Title", metadata.title),
            ("Series", metadata.series),
            ("Number", metadata.number),
            ("Volume", metadata.volume),
            ("Summary", metadata.summary),
            ("Writer", metadata.writer),
            (
                "Genre",
                ", ".join(metadata.genres) if metadata.genres is not None else None,
            ),
            ("PageCount", str(page_count)),
            ("LanguageISO", metadata.language_iso),
            ("Manga", metadata.manga),
            ("Web", metadata.web),
        )
        root = ET.Element("ComicInfo")
        for tag, value in values:
            if value is None or value == "":
                continue
            ET.SubElement(root, tag).text = value
        try:
            return ET.tostring(
                root,
                encoding="utf-8",
                xml_declaration=True,
                short_empty_elements=True,
            )
        except Exception as error:
            cls._fail("comicinfo_serialization_failed", error)

    @staticmethod
    def _zip_info(member_name: str) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(member_name, date_time=_ZIP_TIMESTAMP)
        info.compress_type = zipfile.ZIP_STORED
        info.create_system = 0
        info.external_attr = 0
        info.internal_attr = 0
        info.flag_bits = 0
        info.extra = b""
        info.comment = b""
        return info

    @classmethod
    def _write_archive(
        cls,
        temporary: Path,
        pages: tuple[_PreparedPage, ...],
        comic_info: bytes,
        *,
        max_archive_bytes: int,
        storage_gate: SharedStorageGate | None = None,
        disk_budget_manager: DiskBudgetManager | None = None,
    ) -> None:
        raw_job_id = _current_job_id.get()
        if raw_job_id is None:
            job_id = None
        else:
            try:
                job_id = UUID(raw_job_id)
            except ValueError:
                raise CBZBuildError("disk_budget_reservation_missing") from None

        def write_archive_file() -> None:
            output = RetryingFileStream.create(
                temporary,
                component="cbz_builder",
                stage="cbz_staging",
                trace_id=current_trace_id() or create_trace_id(),
                job_id=_current_job_id.get(),
                storage_gate=storage_gate,
            )
            operation_error: BaseException | None = None
            try:
                bounded_output = _SizeBoundedZipStream(
                    cast(BinaryIO, output),
                    max_bytes=max_archive_bytes,
                    disk_budget_manager=disk_budget_manager,
                    job_id=job_id,
                )
                archive = zipfile.ZipFile(
                    cast(BinaryIO, bounded_output),
                    mode="w",
                    allowZip64=True,
                )
                try:
                    archive.comment = b""
                    comic_info_info = cls._zip_info("ComicInfo.xml")
                    comic_info_info.file_size = len(comic_info)
                    with _write_zip_member(archive, comic_info_info) as member:
                        member.write(comic_info)
                    for prepared in pages:
                        info = _retry_stat(
                            prepared.page.path,
                            stage="cbz_page_source_stat",
                        )
                        if (
                            cls._is_reparse_point(info)
                            or not stat.S_ISREG(info.st_mode)
                            or info.st_size != prepared.page.size_bytes
                        ):
                            raise CBZBuildError("page_source_changed")
                        snapshot = _FileSnapshot.from_stat(info)
                        member_info = cls._zip_info(prepared.member_name)
                        member_info.file_size = prepared.page.size_bytes
                        hasher = hashlib.sha256()
                        byte_count = 0
                        with _RestartablePageReader(
                            prepared.page.path,
                            snapshot,
                            stage="cbz_page_copy_read",
                            changed_stage="page_source_changed",
                        ) as source:
                            with _write_zip_member(
                                archive,
                                member_info,
                                force_zip64=prepared.page.size_bytes > zipfile.ZIP64_LIMIT,
                            ) as member:
                                while chunk := source.read(_COPY_CHUNK_SIZE):
                                    next_size = byte_count + len(chunk)
                                    if next_size > prepared.page.size_bytes:
                                        raise CBZBuildError("page_source_size_mismatch")
                                    if next_size > MAX_PAGE_BYTES_V1:
                                        raise CBZBuildError("page_size_limit_exceeded")
                                    member.write(chunk)
                                    hasher.update(chunk)
                                    byte_count = next_size
                            source.ensure_unchanged()
                        if byte_count != prepared.page.size_bytes:
                            raise CBZBuildError("page_source_size_mismatch")
                        if hasher.hexdigest() != prepared.page.sha256:
                            raise CBZBuildError("page_source_hash_mismatch")
                except BaseException as error:
                    operation_error = error
                try:
                    archive.close()
                except BaseException as archive_close_error:
                    if operation_error is None:
                        operation_error = archive_close_error
                    else:
                        operation_error.add_note(
                            "ZIP finalization failed during exception cleanup "
                            f"({type(archive_close_error).__name__}); candidate preserved."
                        )
                if operation_error is not None:
                    raise operation_error
                output.sync()
            except BaseException as error:
                operation_error = error

            try:
                output.close()
            except BaseException as close_error:
                if (
                    storage_gate is not None
                    and isinstance(close_error, OSError)
                    and is_disk_full_os_error(close_error)
                ):
                    storage_gate.close("FILESYSTEM_DISK_FULL")
                _log_writer_close_failure(close_error)
                if operation_error is None:
                    raise CBZBuildError(
                        "temporary_close_unverified",
                        cause_type=type(close_error).__name__,
                    ) from None
                operation_error.add_note(
                    "CBZ staging close was unverified "
                    f"({type(close_error).__name__}); candidate preserved."
                )
            if operation_error is not None:
                raise operation_error

        try:
            if storage_gate is None:
                write_archive_file()
            else:
                with storage_gate.mutation():
                    try:
                        write_archive_file()
                    except OSError as error:
                        if is_disk_full_os_error(error):
                            storage_gate.close("FILESYSTEM_DISK_FULL")
                        raise
        except StorageGateClosedError:
            raise
        except CBZBuildError:
            raise
        except Exception as error:
            cls._fail("temporary_write_failed", error)

    @classmethod
    def _verify_archive(
        cls,
        temporary: Path,
        pages: tuple[_PreparedPage, ...],
        comic_info: bytes,
        *,
        expected_archive_size: int,
        max_archive_bytes: int,
    ) -> None:
        expected_names = ["ComicInfo.xml", *(page.member_name for page in pages)]
        try:
            archive_info = _retry_stat(
                temporary,
                stage="cbz_archive_verification_stat",
            )
            if cls._is_reparse_point(archive_info) or not stat.S_ISREG(
                archive_info.st_mode
            ):
                raise CBZBuildError("temporary_archive_invalid")
            if archive_info.st_size > max_archive_bytes:
                raise CBZBuildError("archive_size_limit_on_disk")
            if archive_info.st_size != expected_archive_size:
                raise CBZBuildError("archive_size_estimate_mismatch")
            snapshot = _FileSnapshot.from_stat(archive_info)
        except CBZBuildError:
            raise
        except Exception as error:
            cls._fail("zip_verification_failed", error)

        retry_state = _LockRetryState()
        while True:
            try:
                cls._verify_archive_once(
                    temporary,
                    snapshot,
                    pages,
                    comic_info,
                    expected_names,
                    retry_state=retry_state,
                )
                return
            except CBZBuildError:
                raise
            except OSError as error:
                if native_winerror(error) not in WIN32_TRANSIENT_LOCK_ERRORS:
                    cls._fail("zip_verification_failed", error)
                try:
                    _retry_after_lock(
                        temporary,
                        snapshot,
                        error,
                        stage="cbz_zip_verification",
                        state=retry_state,
                        changed_stage="temporary_archive_changed",
                    )
                except CBZBuildError:
                    raise
                except OSError as terminal_error:
                    cls._fail("zip_verification_failed", terminal_error)
            except zipfile.BadZipFile as error:
                cls._fail("zip_verification_failed", error)
            except Exception as error:
                cls._fail("zip_verification_failed", error)

    @classmethod
    def _verify_archive_once(
        cls,
        temporary: Path,
        snapshot: _FileSnapshot,
        pages: tuple[_PreparedPage, ...],
        comic_info: bytes,
        expected_names: list[str],
        *,
        retry_state: _LockRetryState,
    ) -> None:
        stream = _open_snapshot_stream(
            temporary,
            snapshot,
            stage="cbz_zip_verification_open",
            state=retry_state,
            changed_stage="temporary_archive_changed",
        )
        operation_error: BaseException | None = None
        try:
            with zipfile.ZipFile(stream, mode="r") as archive:
                if archive.testzip() is not None:
                    raise CBZBuildError("zip_crc_failed")
                infos = archive.infolist()
                names = [info.filename for info in infos]
                if names != expected_names or len(set(names)) != len(names):
                    raise CBZBuildError("zip_structure_failed")
                cls._verify_comic_info(archive, comic_info)
                page_infos = infos[1:]
                if len(page_infos) != len(pages):
                    raise CBZBuildError("page_count_mismatch")
                for info, expected in zip(page_infos, pages, strict=True):
                    if info.file_size != expected.page.size_bytes:
                        raise CBZBuildError("page_size_mismatch")
                    cls._verify_page_hash(archive, expected)
                    cls._verify_page_decode(archive, expected)
        except BaseException as error:
            operation_error = error
        close_error = _close_read_stream(stream, stage="cbz_zip_verification_close")
        if operation_error is not None:
            raise operation_error
        if close_error is not None:
            raise close_error
        if not _snapshot_matches(
            temporary,
            snapshot,
            stage="cbz_zip_verification_final_stat",
        ):
            raise CBZBuildError("temporary_archive_changed")

    @classmethod
    def _verify_comic_info(
        cls,
        archive: zipfile.ZipFile,
        expected_bytes: bytes,
    ) -> None:
        try:
            root = ET.fromstring(archive.read("ComicInfo.xml"))
        except OSError as error:
            if native_winerror(error) in WIN32_TRANSIENT_LOCK_ERRORS:
                raise
            cls._fail("comicinfo_verification_failed", error)
        except Exception as error:
            cls._fail("comicinfo_verification_failed", error)
        if root.tag != "ComicInfo":
            raise CBZBuildError("comicinfo_verification_failed")
        try:
            expected_root = ET.fromstring(expected_bytes)
        except ET.ParseError as error:
            cls._fail("comicinfo_verification_failed", error)
        observed = [(child.tag, child.text) for child in root]
        expected = [(child.tag, child.text) for child in expected_root]
        if observed != expected:
            raise CBZBuildError("comicinfo_verification_failed")

    @classmethod
    def _verify_page_hash(
        cls,
        archive: zipfile.ZipFile,
        expected: _PreparedPage,
    ) -> None:
        hasher = hashlib.sha256()
        byte_count = 0
        try:
            with archive.open(expected.member_name, mode="r") as member:
                while chunk := member.read(_COPY_CHUNK_SIZE):
                    hasher.update(chunk)
                    byte_count += len(chunk)
        except OSError as error:
            if native_winerror(error) in WIN32_TRANSIENT_LOCK_ERRORS:
                raise
            cls._fail("page_hash_verification_failed", error)
        except Exception as error:
            cls._fail("page_hash_verification_failed", error)
        if byte_count != expected.page.size_bytes:
            raise CBZBuildError("page_size_mismatch")
        if hasher.hexdigest() != expected.page.sha256:
            raise CBZBuildError("page_hash_mismatch")

    @classmethod
    def _verify_page_decode(
        cls,
        archive: zipfile.ZipFile,
        expected: _PreparedPage,
    ) -> None:
        try:
            with archive.open(expected.member_name, mode="r") as member:
                with Image.open(member) as image:
                    if image.format != expected.image_format:
                        raise CBZBuildError("page_format_mismatch")
                    image.load()
        except CBZBuildError:
            raise
        except OSError as error:
            if native_winerror(error) in WIN32_TRANSIENT_LOCK_ERRORS:
                raise
            cls._fail("page_decode_failed", error)
        except Exception as error:
            cls._fail("page_decode_failed", error)

    @staticmethod
    def _hash_regular_file(path: Path, *, max_bytes: int) -> tuple[str, int]:
        info = _retry_stat(path, stage="cbz_regular_file_hash_stat")
        if CBZBuilder._is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
            raise OSError("artifact is not a regular file")
        if info.st_size > max_bytes:
            raise CBZBuildError("archive_size_limit_on_disk")
        snapshot = _FileSnapshot.from_stat(info)
        retry_state = _LockRetryState()
        while True:
            hasher = hashlib.sha256()
            byte_count = 0
            final_stream_snapshot: _FileSnapshot | None = None
            try:
                stream = _open_snapshot_stream(
                    path,
                    snapshot,
                    stage="cbz_regular_file_hash_read",
                    state=retry_state,
                    changed_stage="artifact_changed_while_hashing",
                )
                operation_error: BaseException | None = None
                try:
                    stream_info = os.fstat(stream.fileno())
                    if CBZBuilder._is_reparse_point(stream_info) or not stat.S_ISREG(
                        stream_info.st_mode
                    ):
                        raise OSError("artifact is not a regular file")
                    while chunk := stream.read(_COPY_CHUNK_SIZE):
                        if byte_count + len(chunk) > max_bytes:
                            raise CBZBuildError("archive_size_limit_on_disk")
                        hasher.update(chunk)
                        byte_count += len(chunk)
                    final_stream_snapshot = _FileSnapshot.from_stat(
                        os.fstat(stream.fileno())
                    )
                except BaseException as error:
                    operation_error = error
                close_error = _close_read_stream(
                    stream,
                    stage="cbz_regular_file_hash_close",
                )
                if operation_error is not None:
                    raise operation_error
                if close_error is not None:
                    raise close_error
                if final_stream_snapshot != retry_state.handle_snapshot or not _snapshot_matches(
                    path,
                    snapshot,
                    stage="cbz_regular_file_hash_final_stat",
                ):
                    raise CBZBuildError("artifact_changed_while_hashing")
                if byte_count != snapshot.size_bytes:
                    raise OSError("artifact size changed while hashing")
                return hasher.hexdigest(), byte_count
            except OSError as error:
                if native_winerror(error) not in WIN32_TRANSIENT_LOCK_ERRORS:
                    raise
                _retry_after_lock(
                    path,
                    snapshot,
                    error,
                    stage="cbz_regular_file_hash_read",
                    state=retry_state,
                    changed_stage="artifact_changed_while_hashing",
                )

    @classmethod
    def _assert_output_absent(cls, path: Path, *, stage: str) -> None:
        try:
            _retry_stat(path, stage="cbz_output_preflight_stat")
        except FileNotFoundError:
            return
        except Exception as error:
            cls._fail("output_preflight_failed", error)
        raise CBZBuildError(stage)

    @staticmethod
    def _path_exists(path: Path) -> bool:
        try:
            _retry_stat(path, stage="cbz_published_source_stat")
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _is_reparse_point(info: os.stat_result) -> bool:
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        attributes = getattr(info, "st_file_attributes", 0)
        return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_attribute)

    @staticmethod
    def _fail(stage: str, error: BaseException) -> NoReturn:
        raise CBZBuildError(stage, cause_type=type(error).__name__) from None
