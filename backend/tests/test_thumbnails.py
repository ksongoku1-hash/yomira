from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest
from PIL import Image

from app.errors import ErrorCode
from app.services.thumbnails import (
    DEFAULT_CACHE_MAX_BYTES,
    DEFAULT_CACHE_MAX_ENTRIES,
    MAX_THUMBNAIL_ICC_BYTES,
    Thumbnail,
    ThumbnailProcessingError,
    ThumbnailService,
    ThumbnailVariant,
)

SOURCE_SHA256 = "a" * 64


def _png_image(
    size: tuple[int, int] = (1000, 1500),
    *,
    mode: str = "RGBA",
) -> Image.Image:
    image = Image.new(mode, size, (25, 60, 120, 0) if mode == "RGBA" else 120)
    if mode == "RGBA":
        image.putpixel((size[0] // 2, size[1] // 2), (240, 30, 10, 255))

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    image.close()
    buffer.seek(0)
    opened = Image.open(buffer)
    opened.load()
    return opened


@pytest.mark.parametrize(
    ("variant", "expected_size"),
    [
        (ThumbnailVariant.SMALL, (160, 240)),
        (ThumbnailVariant.MEDIUM, (320, 480)),
        (ThumbnailVariant.LARGE, (640, 960)),
    ],
)
def test_generates_bounded_lossless_webp_without_changing_source(
    variant: ThumbnailVariant,
    expected_size: tuple[int, int],
) -> None:
    source = _png_image()
    original_pixels = source.tobytes()
    service = ThumbnailService()

    thumbnail = service.generate(source, SOURCE_SHA256, variant)

    assert source.size == (1000, 1500)
    assert source.tobytes() == original_pixels
    assert thumbnail.variant is variant
    assert thumbnail.width <= expected_size[0]
    assert thumbnail.height <= expected_size[1]
    assert (thumbnail.width, thumbnail.height) == expected_size
    assert thumbnail.mime_type == "image/webp"
    assert thumbnail.sha256 == hashlib.sha256(thumbnail.content).hexdigest()
    assert len(thumbnail.cache_key) == 64

    with Image.open(BytesIO(thumbnail.content)) as result:
        assert result.format == "WEBP"
        assert result.mode == "RGBA"
        assert result.size == expected_size
        transparent_pixel = result.getpixel((0, 0))
        center_pixel = result.getpixel((expected_size[0] // 2, expected_size[1] // 2))
        assert isinstance(transparent_pixel, tuple)
        assert isinstance(center_pixel, tuple)
        assert transparent_pixel[3] == 0
        assert center_pixel[3] > 0


def test_preserves_aspect_ratio_and_never_upscales() -> None:
    source = _png_image((1200, 800))
    service = ThumbnailService()

    landscape = service.generate(source, SOURCE_SHA256, ThumbnailVariant.SMALL)
    small_source = _png_image((80, 120))
    no_upscale = service.generate(small_source, "b" * 64, ThumbnailVariant.LARGE)

    assert (landscape.width, landscape.height) == (160, 107)
    assert (no_upscale.width, no_upscale.height) == (80, 120)


def test_small_source_round_trips_losslessly_with_transparency() -> None:
    source = _png_image((40, 60))

    thumbnail = ThumbnailService().generate(
        source,
        SOURCE_SHA256,
        ThumbnailVariant.MEDIUM,
    )

    with Image.open(BytesIO(thumbnail.content)) as result:
        assert result.tobytes() == source.tobytes()


def test_cache_is_keyed_by_source_hash_variant_and_generator() -> None:
    source = _png_image((40, 60))
    service = ThumbnailService()

    first = service.generate(source, SOURCE_SHA256, ThumbnailVariant.SMALL)
    cached = service.generate(source, SOURCE_SHA256, ThumbnailVariant.SMALL)
    changed_source = service.generate(source, "b" * 64, ThumbnailVariant.SMALL)
    changed_variant = service.generate(source, SOURCE_SHA256, ThumbnailVariant.MEDIUM)

    assert cached is first
    assert changed_source.cache_key != first.cache_key
    assert changed_variant.cache_key != first.cache_key


def test_memory_cache_evicts_oldest_entry_at_configured_bound() -> None:
    source = _png_image((40, 60))
    service = ThumbnailService(cache_max_entries=1)

    first = service.generate(source, SOURCE_SHA256, ThumbnailVariant.SMALL)
    service.generate(source, SOURCE_SHA256, ThumbnailVariant.MEDIUM)
    recomputed = service.generate(source, SOURCE_SHA256, ThumbnailVariant.SMALL)

    assert recomputed is not first
    assert recomputed.content == first.content


def test_memory_cache_does_not_retain_entries_over_byte_budget() -> None:
    source = _png_image((40, 60))
    service = ThumbnailService(cache_max_bytes=1)

    first = service.generate(source, SOURCE_SHA256, ThumbnailVariant.SMALL)
    recomputed = service.generate(source, SOURCE_SHA256, ThumbnailVariant.SMALL)

    assert recomputed is not first
    assert recomputed.content == first.content


def test_cache_configuration_cannot_exceed_reviewed_resource_bounds() -> None:
    with pytest.raises(ValueError, match="32 MiB"):
        ThumbnailService(cache_max_bytes=DEFAULT_CACHE_MAX_BYTES + 1)
    with pytest.raises(ValueError, match="64"):
        ThumbnailService(cache_max_entries=DEFAULT_CACHE_MAX_ENTRIES + 1)


def test_concurrent_requests_for_same_key_share_one_cached_result() -> None:
    source = _png_image((400, 600))
    service = ThumbnailService()

    def generate_one(_: int) -> Thumbnail:
        return service.generate(source, SOURCE_SHA256, ThumbnailVariant.SMALL)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(generate_one, range(16)))

    assert all(result is results[0] for result in results)


def test_rejects_invalid_source_hash_without_weakening_cache_identity() -> None:
    source = _png_image((40, 60))

    with pytest.raises(ValueError, match="source_sha256"):
        ThumbnailService().generate(source, "not-a-sha256", ThumbnailVariant.SMALL)


def test_rejects_non_pillow_input_before_processing() -> None:
    with pytest.raises(TypeError, match="Pillow Image"):
        ThumbnailService().generate(object(), SOURCE_SHA256, ThumbnailVariant.SMALL)


def test_rejects_unprepared_image_mode_with_stable_error_code() -> None:
    source = _png_image((40, 60), mode="L")

    with pytest.raises(ThumbnailProcessingError) as raised:
        ThumbnailService().generate(source, SOURCE_SHA256, ThumbnailVariant.SMALL)

    assert raised.value.code is ErrorCode.IMG_THUMBNAIL_FAILED


def test_rejects_unbounded_icc_profile_without_changing_source() -> None:
    source = _png_image((40, 60))
    source.info["icc_profile"] = b"x" * (MAX_THUMBNAIL_ICC_BYTES + 1)
    original_pixels = source.tobytes()

    with pytest.raises(ThumbnailProcessingError):
        ThumbnailService().generate(source, SOURCE_SHA256, ThumbnailVariant.SMALL)

    assert source.tobytes() == original_pixels


def test_wraps_encoder_failure_without_exposing_internal_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _png_image((40, 60))

    def fail_save(self: Image.Image, *args: object, **kwargs: object) -> None:
        raise OSError("private encoder detail")

    monkeypatch.setattr(Image.Image, "save", fail_save)

    with pytest.raises(ThumbnailProcessingError) as raised:
        ThumbnailService().generate(source, SOURCE_SHA256, ThumbnailVariant.SMALL)

    assert raised.value.code is ErrorCode.IMG_THUMBNAIL_FAILED
    assert str(raised.value) == "Thumbnail generation failed"
    assert "private encoder detail" not in str(raised.value)
