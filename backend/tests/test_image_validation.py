from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID

import pytest
from PIL import Image, ImageFile

from app.persistence.filesystem_retry import retry_filesystem_operation
from app.services import image_validation
from app.services.image_validation import (
    ImageValidationError,
    ImageValidationErrorCode,
    ImageValidationPolicy,
    validate_image_file,
)

_APPROVED_PAGE_MAX_BYTES = 64 * 1024 * 1024
_APPROVED_MAX_DIMENSION = 40_000
_APPROVED_MAX_PIXELS = 100_000_000
_FAKE_SHA256 = "a" * 64


@pytest.fixture
def policy() -> ImageValidationPolicy:
    # Test-only bounds. Product limits remain explicitly injected until approved.
    return ImageValidationPolicy(
        max_bytes=32_768,
        max_width=256,
        max_height=256,
        max_pixels=32_768,
    )


def _image_bytes(image_format: str, size: tuple[int, int] = (8, 12)) -> bytes:
    image = Image.new("RGB", size, color=(30, 60, 90))
    output = BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def _install_fake_image_file_observations(
    monkeypatch: pytest.MonkeyPatch,
    *,
    size_bytes: int,
) -> None:
    identity = (1, 1, size_bytes, 1, 1)

    def fake_path_identity(_path: Path) -> tuple[int, int, int, int, int]:
        return identity

    def fake_file_identity(_stream: BinaryIO) -> tuple[int, int, int, int, int]:
        return identity

    def fake_hash_stream(
        _stream: BinaryIO,
        _max_bytes: int,
    ) -> tuple[int, str]:
        return size_bytes, _FAKE_SHA256

    monkeypatch.setattr(image_validation, "_path_identity", fake_path_identity)
    monkeypatch.setattr(image_validation, "_file_identity", fake_file_identity)
    monkeypatch.setattr(
        image_validation,
        "_hash_stream",
        fake_hash_stream,
    )

    def decode(
        _stream: BinaryIO,
        *,
        signature_format: str,
        policy: ImageValidationPolicy,
    ) -> tuple[str, int, int]:
        assert signature_format == "PNG"
        assert policy.max_width == _APPROVED_MAX_DIMENSION
        assert policy.max_height == _APPROVED_MAX_DIMENSION
        assert policy.max_pixels == _APPROVED_MAX_PIXELS
        return "PNG", 1, 1

    monkeypatch.setattr(image_validation, "_decode_and_validate", decode)


@pytest.mark.parametrize(
    ("pillow_format", "mime"),
    [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")],
)
def test_accepts_supported_image_and_returns_immutable_verified_metadata(
    tmp_path: Path,
    policy: ImageValidationPolicy,
    pillow_format: str,
    mime: str,
) -> None:
    content = _image_bytes(pillow_format)
    path = tmp_path / "download.part"
    path.write_bytes(content)

    result = validate_image_file(path, mime, policy=policy)

    assert result.format == pillow_format
    assert result.width == 8
    assert result.height == 12
    assert result.size_bytes == len(content)
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert result.mime == mime
    assert path.read_bytes() == content
    with pytest.raises(FrozenInstanceError):
        result.width = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("actual_format", "declared_mime"),
    [
        ("PNG", "image/webp"),
        ("PNG", "image/jpeg"),
        ("JPEG", "image/png"),
        ("JPEG", "image/webp"),
        ("WEBP", "image/png"),
        ("WEBP", "image/jpeg"),
    ],
)
def test_allowed_mime_disagreement_uses_verified_bytes_and_logs_after_handle_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: ImageValidationPolicy,
    actual_format: str,
    declared_mime: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    content = _image_bytes(actual_format)
    path = tmp_path / "page.part"
    path.write_bytes(content)
    opened_streams: list[BinaryIO] = []
    original_open = Path.open
    logger = logging.getLogger("yomira.image_validation")
    original_warning = logger.warning
    job_id = "8b25dcca-e824-4aeb-9e0d-06b3d3f3a932"

    def tracked_open(candidate: Path, *args: Any, **kwargs: Any) -> Any:
        stream = cast(BinaryIO, original_open(candidate, *args, **kwargs))
        if candidate == path and args and args[0] == "rb":
            opened_streams.append(stream)
        return stream

    def warning_after_close(message: object, *args: Any, **kwargs: Any) -> None:
        assert opened_streams and all(stream.closed for stream in opened_streams)
        original_warning(message, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(logger, "warning", warning_after_close)
    caplog.set_level(logging.WARNING, logger=logger.name)
    result = validate_image_file(path, declared_mime, policy=policy, job_id=job_id)

    assert result.format == actual_format
    assert result.mime == {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[
        actual_format
    ]
    assert result.size_bytes == len(content)
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert path.read_bytes() == content
    records = [record for record in caplog.records if record.name == logger.name]
    assert len(records) == 1
    assert records[0].__dict__["error_code"] == ImageValidationErrorCode.MIME_MISMATCH.value
    assert records[0].__dict__["stage"] == "mime_reconciled"
    assert records[0].__dict__["reason"] == f"{declared_mime}->{result.mime}"
    assert records[0].__dict__["retryable"] is False
    assert records[0].__dict__["job_id"] == job_id
    assert UUID(records[0].__dict__["trace_id"]).version == 4
    assert str(path) not in repr(records[0].__dict__)
    assert content.hex() not in repr(records[0].__dict__)


@pytest.mark.parametrize(
    "declared_mime", ["", "text/html", "image/avif", "application/octet-stream", "image/svg+xml"]
)
def test_rejects_missing_or_unsupported_mime(
    tmp_path: Path,
    policy: ImageValidationPolicy,
    declared_mime: str,
) -> None:
    path = tmp_path / "page.part"
    path.write_bytes(_image_bytes("PNG"))

    with pytest.raises(ImageValidationError) as caught:
        validate_image_file(path, declared_mime, policy=policy)

    assert caught.value.code is ImageValidationErrorCode.MIME_UNSUPPORTED


def test_accepts_mime_parameters_but_reports_canonical_mime(
    tmp_path: Path,
    policy: ImageValidationPolicy,
) -> None:
    path = tmp_path / "page.part"
    path.write_bytes(_image_bytes("PNG"))

    result = validate_image_file(path, " IMAGE/PNG ; charset=binary ", policy=policy)

    assert result.mime == "image/png"


def test_allowed_header_does_not_override_disagreement_between_signature_and_decoder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: ImageValidationPolicy,
) -> None:
    path = tmp_path / "page.part"
    path.write_bytes(_image_bytes("PNG"))

    class MisidentifiedImage:
        format = "WEBP"

        def __enter__(self) -> MisidentifiedImage:
            return self

        def __exit__(self, *_args: object) -> None:
            return

    def misidentify_image(*_args: Any, **_kwargs: Any) -> MisidentifiedImage:
        return MisidentifiedImage()

    monkeypatch.setattr(Image, "open", misidentify_image)
    with pytest.raises(ImageValidationError) as caught:
        validate_image_file(path, "image/webp", policy=policy)
    assert caught.value.code is ImageValidationErrorCode.SIGNATURE_INVALID


def test_rejects_html_with_image_mime_before_pillow_decode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: ImageValidationPolicy,
) -> None:
    path = tmp_path / "page.part"
    path.write_bytes(b"<html>not an image</html>")

    def fail_if_decode_is_attempted(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Pillow must not decode unrecognized magic bytes")

    monkeypatch.setattr(Image, "open", fail_if_decode_is_attempted)

    with pytest.raises(ImageValidationError) as caught:
        validate_image_file(path, "image/png", policy=policy)

    assert caught.value.code is ImageValidationErrorCode.SIGNATURE_INVALID


def test_rejects_oversize_before_calling_pillow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: ImageValidationPolicy,
) -> None:
    path = tmp_path / "page.part"
    path.write_bytes(b"x" * (policy.max_bytes + 1))

    def fail_if_decode_is_attempted(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("oversize data must be rejected before Pillow")

    monkeypatch.setattr(Image, "open", fail_if_decode_is_attempted)

    with pytest.raises(ImageValidationError) as caught:
        validate_image_file(path, "image/png", policy=policy)

    assert caught.value.code is ImageValidationErrorCode.BYTE_LIMIT


@pytest.mark.parametrize(
    ("size_bytes", "accepted"),
    [(_APPROVED_PAGE_MAX_BYTES, True), (_APPROVED_PAGE_MAX_BYTES + 1, False)],
)
def test_approved_page_byte_boundary_uses_file_metadata_without_large_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    size_bytes: int,
    accepted: bool,
) -> None:
    path = tmp_path / "synthetic-large-page.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00")
    _install_fake_image_file_observations(monkeypatch, size_bytes=size_bytes)
    page_policy = ImageValidationPolicy(
        max_bytes=_APPROVED_PAGE_MAX_BYTES,
        max_width=_APPROVED_MAX_DIMENSION,
        max_height=_APPROVED_MAX_DIMENSION,
        max_pixels=_APPROVED_MAX_PIXELS,
    )

    if accepted:
        result = validate_image_file(path, "image/png", policy=page_policy)
        assert result.size_bytes == _APPROVED_PAGE_MAX_BYTES
    else:
        with pytest.raises(ImageValidationError) as caught:
            validate_image_file(path, "image/png", policy=page_policy)
        assert caught.value.code is ImageValidationErrorCode.BYTE_LIMIT


@pytest.mark.parametrize(
    ("width", "height", "expected_code"),
    [
        (_APPROVED_MAX_DIMENSION, 1, None),
        (_APPROVED_MAX_DIMENSION + 1, 1, ImageValidationErrorCode.DIMENSION_LIMIT),
        (1, _APPROVED_MAX_DIMENSION, None),
        (1, _APPROVED_MAX_DIMENSION + 1, ImageValidationErrorCode.DIMENSION_LIMIT),
    ],
)
def test_approved_dimension_limits_accept_exact_and_reject_plus_one(
    width: int,
    height: int,
    expected_code: ImageValidationErrorCode | None,
) -> None:
    page_policy = ImageValidationPolicy(
        max_bytes=_APPROVED_PAGE_MAX_BYTES,
        max_width=_APPROVED_MAX_DIMENSION,
        max_height=_APPROVED_MAX_DIMENSION,
        max_pixels=_APPROVED_MAX_PIXELS,
    )

    if expected_code is None:
        assert image_validation._check_dimensions(  # pyright: ignore[reportPrivateUsage]
            width,
            height,
            policy=page_policy,
            decoded_pixels=0,
        ) == width * height
    else:
        with pytest.raises(ImageValidationError) as caught:
            image_validation._check_dimensions(  # pyright: ignore[reportPrivateUsage]
                width,
                height,
                policy=page_policy,
                decoded_pixels=0,
            )
        assert caught.value.code is expected_code


def test_approved_total_pixel_limit_accepts_exact_100_mpx() -> None:
    page_policy = ImageValidationPolicy(
        max_bytes=_APPROVED_PAGE_MAX_BYTES,
        max_width=_APPROVED_MAX_DIMENSION,
        max_height=_APPROVED_MAX_DIMENSION,
        max_pixels=_APPROVED_MAX_PIXELS,
    )

    assert image_validation._check_dimensions(  # pyright: ignore[reportPrivateUsage]
        _APPROVED_MAX_DIMENSION,
        2_500,
        policy=page_policy,
        decoded_pixels=0,
    ) == _APPROVED_MAX_PIXELS


def test_approved_total_pixel_limit_rejects_first_pixel_above_100_mpx() -> None:
    page_policy = ImageValidationPolicy(
        max_bytes=_APPROVED_PAGE_MAX_BYTES,
        max_width=_APPROVED_MAX_DIMENSION,
        max_height=_APPROVED_MAX_DIMENSION,
        max_pixels=_APPROVED_MAX_PIXELS,
    )

    with pytest.raises(ImageValidationError) as caught:
        image_validation._check_dimensions(  # pyright: ignore[reportPrivateUsage]
            _APPROVED_MAX_DIMENSION,
            2_500,
            policy=page_policy,
            decoded_pixels=1,
        )
    assert caught.value.code is ImageValidationErrorCode.PIXEL_LIMIT


def test_over_100_mpx_header_is_rejected_before_image_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HeaderOnlyImage:
        format = "PNG"
        size = (_APPROVED_MAX_DIMENSION, 2_501)

        def __init__(self) -> None:
            self.load_calls = 0
            self.verify_calls = 0

        def __enter__(self) -> HeaderOnlyImage:
            return self

        def __exit__(self, *_args: object) -> None:
            return

        def verify(self) -> None:
            self.verify_calls += 1

        def load(self) -> None:
            self.load_calls += 1

    header_image = HeaderOnlyImage()
    def return_header_only_image(
        *_args: Any,
        **_kwargs: Any,
    ) -> HeaderOnlyImage:
        return header_image

    monkeypatch.setattr(Image, "open", return_header_only_image)
    page_policy = ImageValidationPolicy(
        max_bytes=_APPROVED_PAGE_MAX_BYTES,
        max_width=_APPROVED_MAX_DIMENSION,
        max_height=_APPROVED_MAX_DIMENSION,
        max_pixels=_APPROVED_MAX_PIXELS,
    )

    with pytest.raises(ImageValidationError) as caught:
        image_validation._decode_and_validate(  # pyright: ignore[reportPrivateUsage]
            BytesIO(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00"),
            signature_format="PNG",
            policy=page_policy,
        )

    assert caught.value.code is ImageValidationErrorCode.PIXEL_LIMIT
    assert header_image.verify_calls == 0
    assert header_image.load_calls == 0


@pytest.mark.parametrize(
    ("size", "expected_code"),
    [
        ((257, 8), ImageValidationErrorCode.DIMENSION_LIMIT),
        ((8, 257), ImageValidationErrorCode.DIMENSION_LIMIT),
        ((200, 200), ImageValidationErrorCode.PIXEL_LIMIT),
    ],
)
def test_rejects_dimension_or_pixel_limit_before_full_decode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: ImageValidationPolicy,
    size: tuple[int, int],
    expected_code: ImageValidationErrorCode,
) -> None:
    path = tmp_path / "page.part"
    path.write_bytes(_image_bytes("PNG", size))

    def fail_if_decode_is_attempted(self: Image.Image) -> None:
        raise AssertionError("limits must be checked before pixel decoding")

    monkeypatch.setattr(Image.Image, "load", fail_if_decode_is_attempted)

    with pytest.raises(ImageValidationError) as caught:
        validate_image_file(path, "image/png", policy=policy)

    assert caught.value.code is expected_code


@pytest.mark.parametrize("declared_mime", ["image/jpeg", "image/webp"])
def test_rejects_truncated_jpeg(
    tmp_path: Path,
    policy: ImageValidationPolicy,
    declared_mime: str,
) -> None:
    content = _image_bytes("JPEG")
    path = tmp_path / "truncated.part"
    path.write_bytes(content[:-2])

    with pytest.raises(ImageValidationError) as caught:
        validate_image_file(path, declared_mime, policy=policy)

    assert caught.value.code is ImageValidationErrorCode.TRUNCATED


@pytest.mark.parametrize("declared_mime", ["image/jpeg", "image/webp"])
def test_rejects_corrupt_content_with_valid_jpeg_signature(
    tmp_path: Path,
    policy: ImageValidationPolicy,
    declared_mime: str,
) -> None:
    path = tmp_path / "corrupt.part"
    path.write_bytes(b"\xff\xd8\xffnot a complete JPEG")

    with pytest.raises(ImageValidationError) as caught:
        validate_image_file(path, declared_mime, policy=policy)

    assert caught.value.code is ImageValidationErrorCode.DECODE_FAILED


def test_decodes_all_frames_and_bounds_total_decoded_pixels(
    tmp_path: Path,
    policy: ImageValidationPolicy,
) -> None:
    first = Image.new("RGB", (8, 12), color=(30, 60, 90))
    second = Image.new("RGB", (8, 12), color=(90, 60, 30))
    output = BytesIO()
    first.save(output, format="WEBP", save_all=True, append_images=[second], duration=50)
    path = tmp_path / "animated.part"
    path.write_bytes(output.getvalue())

    result = validate_image_file(path, "image/webp", policy=policy)

    assert result.format == "WEBP"
    assert result.width == 8
    assert result.height == 12

    bounded_policy = ImageValidationPolicy(
        max_bytes=policy.max_bytes,
        max_width=policy.max_width,
        max_height=policy.max_height,
        max_pixels=100,
    )
    with pytest.raises(ImageValidationError) as caught:
        validate_image_file(path, "image/webp", policy=bounded_policy)

    assert caught.value.code is ImageValidationErrorCode.PIXEL_LIMIT


def test_fails_closed_if_shared_pillow_setting_accepts_truncated_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: ImageValidationPolicy,
) -> None:
    path = tmp_path / "page.part"
    path.write_bytes(_image_bytes("PNG"))
    original = ImageFile.LOAD_TRUNCATED_IMAGES
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)

    with pytest.raises(ImageValidationError) as caught:
        validate_image_file(path, "image/png", policy=policy)

    assert caught.value.code is ImageValidationErrorCode.DECODE_FAILED
    assert ImageFile.LOAD_TRUNCATED_IMAGES is True
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", original)


@pytest.mark.parametrize(
    "invalid_policy",
    [
        {"max_bytes": 0, "max_width": 1, "max_height": 1, "max_pixels": 1},
        {"max_bytes": True, "max_width": 1, "max_height": 1, "max_pixels": 1},
        {"max_bytes": 1, "max_width": 0, "max_height": 1, "max_pixels": 1},
        {"max_bytes": 1, "max_width": 1, "max_height": 1, "max_pixels": 0},
    ],
)
def test_policy_requires_positive_non_boolean_integer_limits(
    invalid_policy: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        ImageValidationPolicy(**invalid_policy)


def test_validation_does_not_mutate_pillow_process_globals(
    tmp_path: Path,
    policy: ImageValidationPolicy,
) -> None:
    path = tmp_path / "page.part"
    path.write_bytes(_image_bytes("PNG"))
    original_max_pixels = Image.MAX_IMAGE_PIXELS
    original_truncated_images = ImageFile.LOAD_TRUNCATED_IMAGES

    validate_image_file(path, "image/png", policy=policy)

    assert Image.MAX_IMAGE_PIXELS == original_max_pixels
    assert ImageFile.LOAD_TRUNCATED_IMAGES is original_truncated_images


def test_rejects_file_changed_while_decoding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: ImageValidationPolicy,
) -> None:
    path = tmp_path / "changing.part"
    content = _image_bytes("PNG")
    path.write_bytes(content)
    original_load = Image.Image.load
    changed = False

    def mutate_after_load(image: Image.Image) -> object:
        nonlocal changed
        result = original_load(image)
        if not changed:
            changed = True
            path.write_bytes(content[:-1])
        return result

    monkeypatch.setattr(Image.Image, "load", mutate_after_load)

    with pytest.raises(ImageValidationError) as caught:
        validate_image_file(path, "image/png", policy=policy)

    assert caught.value.code is ImageValidationErrorCode.SOURCE_CHANGED


class _Win32LockError(OSError):
    def __init__(self, winerror: int) -> None:
        super().__init__("simulated sharing violation")
        self.winerror = winerror


@pytest.mark.parametrize("winerror", [32, 33, 36])
@pytest.mark.parametrize("stage", ["open", "read", "decode"])
def test_transient_windows_read_lock_restarts_validation_from_closed_handle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: ImageValidationPolicy,
    stage: str,
    winerror: int,
) -> None:
    content = _image_bytes("PNG")
    path = tmp_path / "locked.part"
    path.write_bytes(content)
    expected_hash = hashlib.sha256(content).hexdigest()
    original_open = Path.open
    original_hash_stream = cast(
        Callable[[BinaryIO, int], tuple[int, str]],
        image_validation._hash_stream,  # pyright: ignore[reportPrivateUsage]
    )
    original_load = Image.Image.load
    opened_streams: list[BinaryIO] = []
    opened_images: list[Image.Image] = []
    sleeps: list[float] = []
    injected = False
    original_retry = retry_filesystem_operation

    def no_wait_retry(operation: Callable[[], Any], **kwargs: Any) -> Any:
        return original_retry(  # type: ignore[arg-type]
            operation, sleep=lambda delay: sleeps.append(delay), **kwargs
        )

    def tracked_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        if self == path and args and args[0] == "rb":
            if opened_streams:
                assert opened_streams[-1].closed, (
                    "the prior validation handle must close before retry"
                )
                assert all(getattr(image, "fp", None) is None for image in opened_images), (
                    "the prior Pillow image handles must close before retry"
                )
            if stage == "open" and not injected:
                injected = True
                raise _Win32LockError(winerror)
            stream = cast(BinaryIO, original_open(self, *args, **kwargs))
            opened_streams.append(stream)
            return stream
        return cast(BinaryIO, original_open(self, *args, **kwargs))

    def transient_hash(stream: BinaryIO, max_bytes: int) -> tuple[int, str]:
        nonlocal injected
        if stage == "read" and not injected:
            injected = True
            raise _Win32LockError(winerror)
        return original_hash_stream(stream, max_bytes)  # type: ignore[arg-type]

    def transient_load(image: Image.Image, *args: Any, **kwargs: Any) -> object:
        nonlocal injected
        opened_images.append(image)
        if stage == "decode" and not injected:
            injected = True
            raise _Win32LockError(winerror)
        return original_load(image, *args, **kwargs)

    monkeypatch.setattr(
        image_validation,
        "retry_filesystem_operation",
        no_wait_retry,
        raising=False,
    )
    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(image_validation, "_hash_stream", transient_hash)
    monkeypatch.setattr(Image.Image, "load", transient_load)

    result = validate_image_file(path, "image/png", policy=policy)

    assert result.sha256 == expected_hash
    assert result.size_bytes == len(content)
    assert injected
    assert len(opened_streams) == (1 if stage == "open" else 2)
    assert sleeps == [0.1]
    assert path.read_bytes() == content


def test_transient_read_lock_stops_after_four_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: ImageValidationPolicy,
) -> None:
    path = tmp_path / "persistently-locked.part"
    path.write_bytes(_image_bytes("PNG"))
    original_open = Path.open
    original_retry = retry_filesystem_operation
    opened_streams: list[BinaryIO] = []
    opened_images: list[Image.Image] = []
    sleeps: list[float] = []
    load_calls = 0

    def no_wait_retry(operation: Callable[[], Any], **kwargs: Any) -> Any:
        return original_retry(  # type: ignore[arg-type]
            operation, sleep=lambda delay: sleeps.append(delay), **kwargs
        )

    def tracked_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == path and args and args[0] == "rb":
            if opened_streams:
                assert opened_streams[-1].closed
                assert all(getattr(image, "fp", None) is None for image in opened_images)
            stream = cast(BinaryIO, original_open(self, *args, **kwargs))
            opened_streams.append(stream)
            return stream
        return cast(BinaryIO, original_open(self, *args, **kwargs))

    def persistent_lock(
        image: Image.Image,
        *args: Any,
        **kwargs: Any,
    ) -> object:
        nonlocal load_calls
        load_calls += 1
        opened_images.append(image)
        raise _Win32LockError(32)

    monkeypatch.setattr(
        image_validation,
        "retry_filesystem_operation",
        no_wait_retry,
        raising=False,
    )
    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(Image.Image, "load", persistent_lock)

    with pytest.raises(ImageValidationError) as caught:
        validate_image_file(path, "image/png", policy=policy)

    assert caught.value.code is ImageValidationErrorCode.FILE_UNAVAILABLE
    assert load_calls == 5  # Initial attempt plus the four ADR-0087 retries.
    assert len(opened_streams) == 5
    assert sleeps == [0.1, 0.2, 0.4, 0.8]
    assert opened_streams[-1].closed
    assert all(getattr(image, "fp", None) is None for image in opened_images)


def test_transient_read_lock_does_not_retry_after_unverified_file_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: ImageValidationPolicy,
) -> None:
    path = tmp_path / "unverified-close.part"
    path.write_bytes(_image_bytes("PNG"))
    original_open = Path.open
    original_retry = retry_filesystem_operation
    handles: list[object] = []
    sleeps: list[float] = []

    class ReadProxy:
        def __init__(self, stream: BinaryIO, *, fail_read: bool) -> None:
            self._stream = stream
            self._fail_read = fail_read
            self.closed = False
            handles.append(self)

        def __enter__(self) -> ReadProxy:
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def read(self, size: int = -1) -> bytes:
            if self._fail_read:
                self._fail_read = False
                self._stream.read(5)
                raise _Win32LockError(32)
            return self._stream.read(size)

        def close(self) -> None:
            raise _Win32LockError(32)

        def cleanup(self) -> None:
            self._stream.close()
            self.closed = True

        def __getattr__(self, name: str) -> object:
            return getattr(self._stream, name)

    def tracked_open(candidate: Path, *args: Any, **kwargs: Any) -> Any:
        stream = cast(BinaryIO, original_open(candidate, *args, **kwargs))
        if candidate == path and args and args[0] == "rb":
            return ReadProxy(stream, fail_read=True)
        return stream

    def no_wait_retry(operation: Callable[[], Any], **kwargs: Any) -> Any:
        return original_retry(  # type: ignore[arg-type]
            operation, sleep=lambda delay: sleeps.append(delay), **kwargs
        )

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(image_validation, "retry_filesystem_operation", no_wait_retry)

    try:
        with pytest.raises(ImageValidationError) as caught:
            validate_image_file(path, "image/png", policy=policy)
    finally:
        for handle in handles:
            handle.cleanup()  # type: ignore[attr-defined]

    assert caught.value.code is ImageValidationErrorCode.FILE_UNAVAILABLE
    assert len(handles) == 1
    assert sleeps == []


def test_transient_lock_during_initial_path_stat_retries_safely(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: ImageValidationPolicy,
) -> None:
    content = _image_bytes("PNG")
    path = tmp_path / "initial-stat-lock.part"
    path.write_bytes(content)
    original_path_identity = cast(
        Callable[[Path], tuple[int, int, int, int, int]],
        image_validation._path_identity,  # pyright: ignore[reportPrivateUsage]
    )
    original_retry = retry_filesystem_operation
    identity_calls = 0
    sleeps: list[float] = []

    def no_wait_retry(operation: Callable[[], Any], **kwargs: Any) -> Any:
        return original_retry(  # type: ignore[arg-type]
            operation, sleep=lambda delay: sleeps.append(delay), **kwargs
        )

    def transient_first_stat(candidate: Path) -> tuple[int, int, int, int, int]:
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 1:
            raise _Win32LockError(33)
        return original_path_identity(candidate)

    monkeypatch.setattr(
        image_validation,
        "retry_filesystem_operation",
        no_wait_retry,
        raising=False,
    )
    monkeypatch.setattr(image_validation, "_path_identity", transient_first_stat)

    result = validate_image_file(path, "image/png", policy=policy)

    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert result.size_bytes == len(content)
    assert identity_calls >= 4
    assert sleeps == [0.1]


@pytest.mark.parametrize("winerror", [32, 33, 36])
def test_transient_lock_during_unchanged_proof_retries_within_shared_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: ImageValidationPolicy,
    winerror: int,
) -> None:
    content = _image_bytes("PNG")
    path = tmp_path / "proof-stat-lock.part"
    path.write_bytes(content)
    original_open = Path.open
    original_path_identity = cast(
        Callable[[Path], tuple[int, int, int, int, int]],
        image_validation._path_identity,  # pyright: ignore[reportPrivateUsage]
    )
    original_retry = retry_filesystem_operation
    original_load = Image.Image.load
    opened_streams: list[BinaryIO] = []
    opened_images: list[Image.Image] = []
    sleeps: list[float] = []
    decode_failed_once = False
    proof_failed_once = False

    def no_wait_retry(operation: Callable[[], Any], **kwargs: Any) -> Any:
        return original_retry(  # type: ignore[arg-type]
            operation, sleep=lambda delay: sleeps.append(delay), **kwargs
        )

    def tracked_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == path and args and args[0] == "rb":
            if opened_streams:
                assert opened_streams[-1].closed
                assert all(getattr(image, "fp", None) is None for image in opened_images)
            stream = cast(BinaryIO, original_open(self, *args, **kwargs))
            opened_streams.append(stream)
            return stream
        return cast(BinaryIO, original_open(self, *args, **kwargs))

    def transient_proof_stat(candidate: Path) -> tuple[int, int, int, int, int]:
        nonlocal proof_failed_once
        if decode_failed_once and not proof_failed_once:
            assert opened_streams[-1].closed
            assert all(getattr(image, "fp", None) is None for image in opened_images)
            proof_failed_once = True
            raise _Win32LockError(winerror)
        return original_path_identity(candidate)

    def transient_decode(image: Image.Image, *args: Any, **kwargs: Any) -> object:
        nonlocal decode_failed_once
        opened_images.append(image)
        if not decode_failed_once:
            decode_failed_once = True
            raise _Win32LockError(32)
        return original_load(image, *args, **kwargs)

    monkeypatch.setattr(
        image_validation,
        "retry_filesystem_operation",
        no_wait_retry,
        raising=False,
    )
    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(image_validation, "_path_identity", transient_proof_stat)
    monkeypatch.setattr(Image.Image, "load", transient_decode)

    result = validate_image_file(path, "image/png", policy=policy)

    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert proof_failed_once
    assert len(opened_streams) == 2
    assert opened_streams[0].closed and opened_streams[1].closed
    assert all(getattr(image, "fp", None) is None for image in opened_images)
    assert sleeps == [0.1, 0.2]
    assert sum(sleeps) <= 1.5
    monkeypatch.setattr(Image.Image, "load", original_load)


def test_retry_log_includes_only_sanitized_job_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: ImageValidationPolicy,
    caplog: pytest.LogCaptureFixture,
) -> None:
    content = _image_bytes("PNG")
    path = tmp_path / "logged-image.part"
    path.write_bytes(content)
    original_load = Image.Image.load
    original_retry = retry_filesystem_operation
    sleeps: list[float] = []
    load_failed_once = False
    job_id = str(UUID("8b25dcca-e824-4aeb-9e0d-06b3d3f94932"))

    def no_wait_retry(operation: Callable[[], Any], **kwargs: Any) -> Any:
        return original_retry(  # type: ignore[arg-type]
            operation, sleep=lambda delay: sleeps.append(delay), **kwargs
        )

    def transient_decode(image: Image.Image, *args: Any, **kwargs: Any) -> object:
        nonlocal load_failed_once
        if not load_failed_once:
            load_failed_once = True
            raise _Win32LockError(32)
        return original_load(image, *args, **kwargs)

    caplog.set_level(logging.WARNING, logger="yomira.filesystem_retry")
    monkeypatch.setattr(
        image_validation,
        "retry_filesystem_operation",
        no_wait_retry,
        raising=False,
    )
    monkeypatch.setattr(Image.Image, "load", transient_decode)

    result = validate_image_file(path, "image/png", policy=policy, job_id=job_id)

    retry_records = [
        record
        for record in caplog.records
        if record.name == "yomira.filesystem_retry"
        and record.msg == "filesystem_lock_retry"
    ]
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert len(retry_records) == 1
    assert retry_records[0].__dict__["job_id"] == job_id
    assert str(path) not in repr(retry_records[0].__dict__)
    assert content.hex() not in repr(retry_records[0].__dict__)
    assert sleeps == [0.1]


def test_permanent_decoder_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: ImageValidationPolicy,
) -> None:
    path = tmp_path / "permanent.part"
    path.write_bytes(_image_bytes("PNG"))
    original_load = Image.Image.load
    original_retry = retry_filesystem_operation
    load_calls = 0
    sleeps: list[float] = []

    def no_wait_retry(operation: Callable[[], Any], **kwargs: Any) -> Any:
        return original_retry(  # type: ignore[arg-type]
            operation, sleep=lambda delay: sleeps.append(delay), **kwargs
        )

    def permanent_error(image: Image.Image, *args: Any, **kwargs: Any) -> object:
        nonlocal load_calls
        load_calls += 1
        raise OSError("permanent decoder read failure")

    monkeypatch.setattr(
        image_validation,
        "retry_filesystem_operation",
        no_wait_retry,
        raising=False,
    )
    monkeypatch.setattr(Image.Image, "load", permanent_error)

    with pytest.raises(ImageValidationError) as caught:
        validate_image_file(path, "image/png", policy=policy)

    assert caught.value.code is ImageValidationErrorCode.DECODE_FAILED
    assert load_calls == 1
    assert sleeps == []
    monkeypatch.setattr(Image.Image, "load", original_load)
