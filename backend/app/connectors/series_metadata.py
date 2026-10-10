"""Normalized series metadata values returned by source connectors."""

from dataclasses import dataclass
from typing import cast


def _is_string(value: object) -> bool:
    return isinstance(value, str)


def _is_optional_string(value: object) -> bool:
    return value is None or _is_string(value)


def _is_string_tuple(value: object) -> bool:
    if not isinstance(value, tuple):
        return False
    genres = cast(tuple[object, ...], value)
    return all(_is_string(genre) for genre in genres)


@dataclass(frozen=True, slots=True)
class ConnectorSeriesMetadata:
    """Source-owned metadata separated from local series and user overrides."""

    title: str | None = None
    cover_url: str | None = None
    author: str | None = None
    description: str | None = None
    genres: tuple[str, ...] | None = None
    status: str | None = None
    language: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("title", self.title),
            ("cover_url", self.cover_url),
            ("author", self.author),
            ("description", self.description),
            ("status", self.status),
            ("language", self.language),
        ):
            if not _is_optional_string(value):
                raise TypeError(f"{field_name} must be a string or None")
        if self.genres is not None and not _is_string_tuple(self.genres):
            raise TypeError("genres must be a tuple of strings or None")
