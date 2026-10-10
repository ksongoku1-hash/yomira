"""Bounded, hash-keyed derivatives for already validated cover images."""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from threading import Lock
from types import MappingProxyType

from PIL import Image
from PIL import __version__ as pillow_version

from app.errors import ErrorCode

_SOURCE_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_GENERATOR_VERSION = "1"


class ThumbnailVariant(StrEnum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


THUMBNAIL_BOUNDS: Mapping[ThumbnailVariant, tuple[int, int]] = MappingProxyType(
    {
        ThumbnailVariant.SMALL: (160, 240),
        ThumbnailVariant.MEDIUM: (320, 480),
        ThumbnailVariant.LARGE: (640, 960),
    }
)
DEFAULT_CACHE_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_CACHE_MAX_ENTRIES = 64
MAX_THUMBNAIL_ICC_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Thumbnail:
    source_sha256: str
    variant: ThumbnailVariant
    cache_key: str
    sha256: str
    width: int
    height: int
    mime_type: str
    content: bytes


class ThumbnailProcessingError(Exception):
    """A sanitized processing failure; the source cover remains untouched."""

    code = ErrorCode.IMG_THUMBNAIL_FAILED

    def __init__(self) -> None:
        super().__init__("Thumbnail generation failed")


class _MemoryThumbnailCache:
    def __init__(self, *, max_bytes: int, max_entries: int) -> None:
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._items: OrderedDict[str, Thumbnail] = OrderedDict()
        self._size_bytes = 0

    def get(self, key: str) -> Thumbnail | None:
        item = self._items.get(key)
        if item is not None:
            self._items.move_to_end(key)
        return item

    def put(self, item: Thumbnail) -> None:
        item_size = len(item.content)
        previous = self._items.pop(item.cache_key, None)
        if previous is not None:
            self._size_bytes -= len(previous.content)

        if item_size > self._max_bytes:
            return

        while self._items and (
            len(self._items) >= self._max_entries or self._size_bytes + item_size > self._max_bytes
        ):
            _, evicted = self._items.popitem(last=False)
            self._size_bytes -= len(evicted.content)

        self._items[item.cache_key] = item
        self._size_bytes += item_size


class ThumbnailService:
    """Generate bounded WebP derivatives without filesystem or database access."""

    def __init__(
        self,
        *,
        cache_max_bytes: int = DEFAULT_CACHE_MAX_BYTES,
        cache_max_entries: int = DEFAULT_CACHE_MAX_ENTRIES,
    ) -> None:
        if type(cache_max_bytes) is not int or cache_max_bytes <= 0:
            raise ValueError("cache_max_bytes must be a positive integer")
        if type(cache_max_entries) is not int or cache_max_entries <= 0:
            raise ValueError("cache_max_entries must be a positive integer")
        if cache_max_bytes > DEFAULT_CACHE_MAX_BYTES:
            raise ValueError("cache_max_bytes cannot exceed 32 MiB")
        if cache_max_entries > DEFAULT_CACHE_MAX_ENTRIES:
            raise ValueError("cache_max_entries cannot exceed 64")
        self._cache = _MemoryThumbnailCache(
            max_bytes=cache_max_bytes,
            max_entries=cache_max_entries,
        )
        self._generation_lock = Lock()

    def get_cached(self, source_sha256: str, variant: ThumbnailVariant) -> Thumbnail | None:
        with self._generation_lock:
            return self._cache.get(self._cache_key(source_sha256, variant))

    def generate(
        self,
        image: object,
        source_sha256: str,
        variant: ThumbnailVariant | str,
    ) -> Thumbnail:
        if _SOURCE_HASH_PATTERN.fullmatch(source_sha256) is None:
            raise ValueError("source_sha256 must be 64 lowercase hexadecimal characters")
        try:
            resolved_variant = ThumbnailVariant(variant)
        except (TypeError, ValueError) as error:
            raise ValueError("variant must be small, medium, or large") from error

        if not isinstance(image, Image.Image):
            raise TypeError("image must be a Pillow Image")
        if image.mode not in {"RGB", "RGBA"} or image.width < 1 or image.height < 1:
            raise ThumbnailProcessingError
        try:
            is_first_frame = image.tell() == 0
        except EOFError as error:
            raise ThumbnailProcessingError from error
        if not is_first_frame:
            raise ThumbnailProcessingError

        cache_key = self._cache_key(source_sha256, resolved_variant)
        with self._generation_lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

            thumbnail = self._encode(image, source_sha256, resolved_variant, cache_key)
            self._cache.put(thumbnail)
            return thumbnail

    @staticmethod
    def _cache_key(source_sha256: str, variant: ThumbnailVariant) -> str:
        width, height = THUMBNAIL_BOUNDS[variant]
        identity = (
            f"yomira-thumbnail-{_GENERATOR_VERSION}\0{pillow_version}\0{source_sha256}"
            f"\0{variant.value}\0{width}x{height}\0WEBP\0lossless\0quality=80"
            "\0method=2\0exact=true\0LANCZOS\0reducing_gap=3.0"
        )
        return hashlib.sha256(identity.encode("ascii")).hexdigest()

    @staticmethod
    def _encode(
        image: Image.Image,
        source_sha256: str,
        variant: ThumbnailVariant,
        cache_key: str,
    ) -> Thumbnail:
        bound_width, bound_height = THUMBNAIL_BOUNDS[variant]
        scale = min(1.0, bound_width / image.width, bound_height / image.height)
        width = min(bound_width, max(1, int(image.width * scale + 0.5)))
        height = min(bound_height, max(1, int(image.height * scale + 0.5)))

        resized: Image.Image | None = None
        try:
            output_image = image
            if (width, height) != image.size:
                resized = image.resize(  # pyright: ignore[reportUnknownMemberType]
                    (width, height),
                    resample=Image.Resampling.LANCZOS,
                    reducing_gap=3.0,
                )
                output_image = resized

            icc_profile = image.info.get("icc_profile")
            if isinstance(icc_profile, bytes) and len(icc_profile) > MAX_THUMBNAIL_ICC_BYTES:
                raise ThumbnailProcessingError
            with BytesIO() as buffer:
                if isinstance(icc_profile, bytes):
                    output_image.save(
                        buffer,
                        "WEBP",
                        lossless=True,
                        quality=80,
                        method=2,
                        exact=True,
                        icc_profile=icc_profile,
                    )
                else:
                    output_image.save(
                        buffer,
                        "WEBP",
                        lossless=True,
                        quality=80,
                        method=2,
                        exact=True,
                    )
                content = buffer.getvalue()
        except (MemoryError, OSError, ValueError) as error:
            raise ThumbnailProcessingError from error
        finally:
            if resized is not None:
                resized.close()

        return Thumbnail(
            source_sha256=source_sha256,
            variant=variant,
            cache_key=cache_key,
            sha256=hashlib.sha256(content).hexdigest(),
            width=width,
            height=height,
            mime_type="image/webp",
            content=content,
        )
