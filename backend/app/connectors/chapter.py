"""Lossless chapter and remote-reference data contract for source connectors."""

from dataclasses import dataclass
from typing import cast


def _is_string(value: object) -> bool:
    return isinstance(value, str)


def _is_optional_string(value: object) -> bool:
    return value is None or _is_string(value)


def _is_integer(value: object) -> bool:
    return type(value) is int


def _is_integer_tuple(value: object) -> bool:
    if not isinstance(value, tuple):
        return False
    items = cast(tuple[object, ...], value)
    return all(_is_integer(item) for item in items)


@dataclass(frozen=True, slots=True)
class ConnectorChapter:
    """Remote chapter identity plus its exact logical chapter representation."""

    remote_id: str | None
    remote_url: str | None
    remote_label: str | None
    chapter_raw_label: str
    chapter_major: int | None
    chapter_minor: int | None
    chapter_variant: str | None
    chapter_type: str | None
    chapter_sort_key: tuple[int, ...]
    volume_number: int | None
    language: str | None
    display_label: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("remote_id", self.remote_id),
            ("remote_url", self.remote_url),
        ):
            if not _is_optional_string(value):
                raise TypeError(f"{field_name} must be a string or None")
            if value is not None and not value:
                raise ValueError(f"{field_name} must not be empty when provided")
        if self.remote_id is None and self.remote_url is None:
            raise ValueError("remote_id or remote_url must be provided")

        for field_name, value in (
            ("remote_label", self.remote_label),
            ("chapter_variant", self.chapter_variant),
            ("chapter_type", self.chapter_type),
            ("language", self.language),
        ):
            if not _is_optional_string(value):
                raise TypeError(f"{field_name} must be a string or None")

        for field_name, value in (
            ("chapter_raw_label", self.chapter_raw_label),
            ("display_label", self.display_label),
        ):
            if not _is_string(value):
                raise TypeError(f"{field_name} must be a string")
            if not value:
                raise ValueError(f"{field_name} must not be empty")

        for field_name, value in (
            ("chapter_major", self.chapter_major),
            ("chapter_minor", self.chapter_minor),
            ("volume_number", self.volume_number),
        ):
            if value is not None and not _is_integer(value):
                raise ValueError(f"{field_name} must be an integer or None")

        if not _is_integer_tuple(self.chapter_sort_key):
            raise ValueError("chapter_sort_key must be a tuple of integers")
