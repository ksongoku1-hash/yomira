"""Strict, non-transforming validation for downloaded JPEG, PNG, and WEBP files."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, NoReturn
from uuid import UUID

from PIL import Image, ImageFile

from app.persistence.filesystem_retry import (
    WIN32_TRANSIENT_LOCK_ERRORS,
    close_read_handle,
    native_winerror,
    retry_filesystem_operation,
)
from app.persistence.filesystem_retry import (
    _RetryBudget as FilesystemRetryBudget,  # pyright: ignore[reportPrivateUsage]
)
from app.tracing import create_trace_id, current_trace_id

logger = logging.getLogger("yomira.image_validation")


class ImageValidationErrorCode(StrEnum):
    """Stable local reasons for rejecting an image without changing shared error types."""

    BYTE_LIMIT = "IMG_BYTE_LIMIT"
    DECODE_FAILED = "IMG_DECODE_FAILED"
    DIMENSION_LIMIT = "IMG_DIMENSION_LIMIT"
    FILE_UNAVAILABLE = "IMG_FILE_UNAVAILABLE"
    MIME_MISMATCH = "IMG_MIME_MISMATCH"
    MIME_UNSUPPORTED = "IMG_MIME_UNSUPPORTED"
    PIXEL_LIMIT = "IMG_PIXEL_LIMIT"
    SIGNATURE_INVALID = "IMG_SIGNATURE_INVALID"
    SOURCE_CHANGED = "IMG_SOURCE_CHANGED"
    TRUNCATED = "IMG_TRUNCATED"


class ImageValidationError(ValueError):
    """A safe-to-report validation failure with no path or payload in its message."""

    def __init__(self, code: ImageValidationErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ImageValidationPolicy:
    """Explicit resource bounds supplied by composition; there are no product defaults."""

    max_bytes: int
    max_width: int
    max_height: int
    max_pixels: int

    def __post_init__(self) -> None:
        for name in ("max_bytes", "max_width", "max_height", "max_pixels"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    """Immutable metadata for the exact original bytes that passed validation."""

    format: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    mime: str


_FORMAT_TO_MIME: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_SUPPORTED_FORMATS = tuple(_FORMAT_TO_MIME)
_HASH_CHUNK_BYTES = 1024 * 1024


def _raise(code: ImageValidationErrorCode) -> NoReturn:
    raise ImageValidationError(code)


def _normalized_mime(content_type: str) -> str:
    if type(content_type) is not str:
        _raise(ImageValidationErrorCode.MIME_UNSUPPORTED)
    mime = content_type.split(";", maxsplit=1)[0].strip().lower()
    if mime not in _FORMAT_TO_MIME.values():
        _raise(ImageValidationErrorCode.MIME_UNSUPPORTED)
    return mime


def _safe_job_id(job_id: str | None) -> str | None:
    if type(job_id) is not str:
        return None
    try:
        parsed = UUID(job_id)
    except ValueError:
        return None
    canonical = str(parsed)
    return canonical if canonical == job_id.lower() else None


def _format_from_signature(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "WEBP"
    return None


def _hash_stream(stream: BinaryIO, max_bytes: int) -> tuple[int, str]:
    stream.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(_HASH_CHUNK_BYTES):
        size += len(chunk)
        if size > max_bytes:
            _raise(ImageValidationErrorCode.BYTE_LIMIT)
        digest.update(chunk)
    return size, digest.hexdigest()


def _file_identity(stream: BinaryIO) -> tuple[int, int, int, int, int]:
    information = os.fstat(stream.fileno())
    if not stat.S_ISREG(information.st_mode):
        _raise(ImageValidationErrorCode.FILE_UNAVAILABLE)
    return _identity_from_stat(information)


def _path_identity(path: Path) -> tuple[int, int, int, int, int]:
    information = path.stat()
    if not stat.S_ISREG(information.st_mode):
        _raise(ImageValidationErrorCode.FILE_UNAVAILABLE)
    return _identity_from_stat(information)


def _identity_from_stat(
    information: os.stat_result,
) -> tuple[int, int, int, int, int]:
    device = information.st_dev
    file_id = information.st_ino
    size = information.st_size
    modified_ns = getattr(information, "st_mtime_ns", None)
    changed_ns = getattr(information, "st_ctime_ns", None)
    if (
        type(device) is not int
        or type(file_id) is not int
        or file_id == 0
        or type(size) is not int
        or type(modified_ns) is not int
        or type(changed_ns) is not int
    ):
        _raise(ImageValidationErrorCode.FILE_UNAVAILABLE)
    return device, file_id, size, modified_ns, changed_ns


def _check_dimensions(
    width: int,
    height: int,
    *,
    policy: ImageValidationPolicy,
    decoded_pixels: int,
) -> int:
    if width <= 0 or height <= 0:
        _raise(ImageValidationErrorCode.DECODE_FAILED)
    if width > policy.max_width or height > policy.max_height:
        _raise(ImageValidationErrorCode.DIMENSION_LIMIT)

    frame_pixels = width * height
    if decoded_pixels + frame_pixels > policy.max_pixels:
        _raise(ImageValidationErrorCode.PIXEL_LIMIT)

    return frame_pixels


def _decode_and_validate(
    stream: BinaryIO,
    *,
    signature_format: str,
    policy: ImageValidationPolicy,
) -> tuple[str, int, int]:
    if ImageFile.LOAD_TRUNCATED_IMAGES:
        # Pillow would otherwise accept partial raster data. Fail closed because
        # changing this process-wide switch here could race with other callers.
        _raise(ImageValidationErrorCode.DECODE_FAILED)

    try:
        stream.seek(0)
        with Image.open(stream, formats=_SUPPORTED_FORMATS) as image:
            actual_format = image.format
            if actual_format not in _FORMAT_TO_MIME:
                _raise(ImageValidationErrorCode.SIGNATURE_INVALID)
            if actual_format != signature_format:
                _raise(ImageValidationErrorCode.SIGNATURE_INVALID)

            width, height = image.size
            _check_dimensions(width, height, policy=policy, decoded_pixels=0)
            # verify() checks container structure and checksums. Reopen afterward
            # because Pillow invalidates the decoder state when verify is called.
            image.verify()

        stream.seek(0)
        with Image.open(stream, formats=_SUPPORTED_FORMATS) as image:
            if image.format != signature_format:
                _raise(ImageValidationErrorCode.SIGNATURE_INVALID)
            first_width, first_height = image.size
            frame_count = getattr(image, "n_frames", 1)
            if frame_count <= 0:
                _raise(ImageValidationErrorCode.DECODE_FAILED)

            decoded_pixels = 0
            for frame_index in range(frame_count):
                image.seek(frame_index)
                frame_width, frame_height = image.size
                frame_pixels = _check_dimensions(
                    frame_width,
                    frame_height,
                    policy=policy,
                    decoded_pixels=decoded_pixels,
                )
                image.load()
                decoded_pixels += frame_pixels

        return signature_format, first_width, first_height
    except ImageValidationError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        ValueError,
        EOFError,
    ) as error:
        if native_winerror(error) in WIN32_TRANSIENT_LOCK_ERRORS:
            # The caller closes this file handle before the bounded retry starts
            # validation over from byte zero.
            raise
        message = str(error).lower()
        if "truncated" in message or "truncat" in message or "end of file" in message:
            _raise(ImageValidationErrorCode.TRUNCATED)
        _raise(ImageValidationErrorCode.DECODE_FAILED)


def _validate_image_attempt(
    path: Path,
    *,
    policy: ImageValidationPolicy,
    expected_identity: tuple[int, int, int, int, int],
    expected_hash: list[tuple[int, str] | None],
    trace_id: str,
    job_id: str | None,
) -> ValidatedImage:
    opened_stream = path.open("rb")
    try:
        stream = opened_stream.__enter__()
    except BaseException as enter_error:
        close_error = close_read_handle(
            opened_stream,
            component="image_validation",
            stage="read_decode_enter_close",
            trace_id=trace_id,
            job_id=job_id,
        )
        try:
            definitely_closed = opened_stream.closed is True
        except BaseException:
            definitely_closed = False
        if not definitely_closed:
            failure = close_error or OSError("Image read handle close is unverified")
            raise failure from enter_error
        close_is_transient = (
            isinstance(close_error, OSError)
            and native_winerror(close_error) in WIN32_TRANSIENT_LOCK_ERRORS
        )
        enter_is_transient = (
            isinstance(enter_error, OSError)
            and native_winerror(enter_error) in WIN32_TRANSIENT_LOCK_ERRORS
        )
        if close_error is not None and enter_is_transient and not close_is_transient:
            raise close_error from enter_error
        raise

    result: ValidatedImage | None = None
    operation_error: BaseException | None = None
    try:
        result = _validate_image_stream(
            path,
            stream,
            policy=policy,
            expected_identity=expected_identity,
            expected_hash=expected_hash,
        )
    except BaseException as error:
        operation_error = error

    close_error = close_read_handle(
        stream,
        component="image_validation",
        stage="read_decode_close",
        trace_id=trace_id,
        job_id=job_id,
    )
    try:
        definitely_closed = stream.closed is True
    except BaseException:
        definitely_closed = False
    if not definitely_closed:
        raise close_error or OSError("Image read handle close is unverified")

    close_is_transient = (
        isinstance(close_error, OSError)
        and native_winerror(close_error) in WIN32_TRANSIENT_LOCK_ERRORS
    )
    operation_is_transient = (
        isinstance(operation_error, OSError)
        and native_winerror(operation_error) in WIN32_TRANSIENT_LOCK_ERRORS
    )
    if close_error is not None and (
        operation_error is None or (operation_is_transient and not close_is_transient)
    ):
        raise close_error
    if operation_error is not None:
        raise operation_error
    if close_error is not None:
        raise close_error
    if result is None:
        raise OSError("Image validation produced no stable result")
    return result


def _validate_image_stream(
    path: Path,
    stream: BinaryIO,
    *,
    policy: ImageValidationPolicy,
    expected_identity: tuple[int, int, int, int, int],
    expected_hash: list[tuple[int, str] | None],
) -> ValidatedImage:
    identity_before = _file_identity(stream)
    # On Windows, path.stat() and fstat() can report ctime with different
    # precision. Compare the stable file ID, size, and mtime across APIs;
    # compare ctime against its own path.stat() baseline and within fstat().
    if (
        identity_before[:4] != expected_identity[:4]
        or _path_identity(path) != expected_identity
    ):
        _raise(ImageValidationErrorCode.SOURCE_CHANGED)
    size_on_disk = identity_before[2]
    if size_on_disk > policy.max_bytes:
        _raise(ImageValidationErrorCode.BYTE_LIMIT)

    stream.seek(0)
    signature_format = _format_from_signature(stream.read(12))
    if signature_format is None:
        _raise(ImageValidationErrorCode.SIGNATURE_INVALID)
    hashed_size, sha256 = _hash_stream(stream, policy.max_bytes)
    if expected_hash[0] is not None and expected_hash[0] != (hashed_size, sha256):
        _raise(ImageValidationErrorCode.SOURCE_CHANGED)
    expected_hash[0] = (hashed_size, sha256)
    if hashed_size != size_on_disk or _file_identity(stream) != identity_before:
        _raise(ImageValidationErrorCode.SOURCE_CHANGED)

    actual_format, width, height = _decode_and_validate(
        stream,
        signature_format=signature_format,
        policy=policy,
    )

    final_size, final_hash = _hash_stream(stream, policy.max_bytes)
    if (
        final_size != hashed_size
        or final_hash != sha256
        or _file_identity(stream) != identity_before
        or _path_identity(path) != expected_identity
    ):
        _raise(ImageValidationErrorCode.SOURCE_CHANGED)

    return ValidatedImage(
        format=actual_format,
        width=width,
        height=height,
        size_bytes=hashed_size,
        sha256=sha256,
        mime=_FORMAT_TO_MIME[actual_format],
    )


def validate_image_file(
    path: Path,
    content_type: str,
    *,
    policy: ImageValidationPolicy,
    job_id: str | None = None,
) -> ValidatedImage:
    """Validate bounded image bytes, retrying only proven transient Windows locks.

    Each attempt closes its handle before a retry. The retry proof requires the
    path's file ID, size, mtime, and ctime to remain unchanged; successful output
    also requires a final hash identical to the initial hash.
    An allowed HTTP MIME is required; the verified format determines output MIME.
    """

    mime = _normalized_mime(content_type)
    expected_identity: list[tuple[int, int, int, int, int] | None] = [None]
    expected_hash: list[tuple[int, str] | None] = [None]
    trace_id = current_trace_id() or create_trace_id()
    safe_job_id = _safe_job_id(job_id)
    # The shared budget spans both validation retries and transient failures
    # while proving that the input path remained unchanged.
    retry_budget = FilesystemRetryBudget()

    def proof_stat_is_unchanged() -> bool:
        try:
            current_identity = _path_identity(path)
        except (OSError, ImageValidationError):
            return False
        baseline = expected_identity[0]
        if baseline is None:
            # The initial stat is read-only. If it itself hit a transient lock,
            # this stable snapshot becomes the first source baseline; the retry
            # helper will verify it again after its bounded wait.
            expected_identity[0] = current_identity
            return True
        return current_identity == baseline

    def source_is_unchanged() -> bool:
        try:
            current_identity = retry_filesystem_operation(
                lambda: _path_identity(path),
                unchanged=proof_stat_is_unchanged,
                component="image_validation",
                stage="unchanged_proof",
                trace_id=trace_id,
                job_id=safe_job_id,
                retry_budget=retry_budget,
            )
        except (OSError, ImageValidationError):
            return False
        baseline = expected_identity[0]
        if baseline is None:
            expected_identity[0] = current_identity
            return True
        return current_identity == baseline

    def validate_from_current_identity() -> ValidatedImage:
        current_identity = _path_identity(path)
        baseline = expected_identity[0]
        if baseline is None:
            baseline = current_identity
            expected_identity[0] = baseline
        elif current_identity != baseline:
            _raise(ImageValidationErrorCode.SOURCE_CHANGED)
        if current_identity[2] > policy.max_bytes:
            _raise(ImageValidationErrorCode.BYTE_LIMIT)
        return _validate_image_attempt(
            path,
            policy=policy,
            expected_identity=current_identity,
            expected_hash=expected_hash,
            trace_id=trace_id,
            job_id=safe_job_id,
        )

    try:
        result = retry_filesystem_operation(
            validate_from_current_identity,
            unchanged=source_is_unchanged,
            component="image_validation",
            stage="read_decode",
            trace_id=trace_id,
            job_id=safe_job_id,
            retry_budget=retry_budget,
        )
        if result.mime != mime:
            logger.warning(
                "image_validation.mime_reconciled",
                extra={
                    "error_code": ImageValidationErrorCode.MIME_MISMATCH.value,
                    "retryable": False,
                    "stage": "mime_reconciled",
                    "reason": f"{mime}->{result.mime}",
                    "trace_id": trace_id,
                    "job_id": safe_job_id,
                },
            )
        return result
    except ImageValidationError:
        raise
    except OSError as error:
        if native_winerror(error) in WIN32_TRANSIENT_LOCK_ERRORS:
            if expected_identity[0] is not None and not source_is_unchanged():
                _raise(ImageValidationErrorCode.SOURCE_CHANGED)
        _raise(ImageValidationErrorCode.FILE_UNAVAILABLE)
