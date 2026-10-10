from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from app.connectors.series_metadata import ConnectorSeriesMetadata


def test_connector_series_metadata_preserves_supported_source_fields() -> None:
    metadata = ConnectorSeriesMetadata(
        title="  Remote title\n",
        cover_url="../cover?id=7&size=large",
        author="Author",
        description="Description\nkept intact",
        genres=("Action", "Action", "Drama"),
        status="On hiatus",
        language="pt-BR",
    )

    assert metadata.title == "  Remote title\n"
    assert metadata.cover_url == "../cover?id=7&size=large"
    assert metadata.author == "Author"
    assert metadata.description == "Description\nkept intact"
    assert metadata.genres == ("Action", "Action", "Drama")
    assert metadata.status == "On hiatus"
    assert metadata.language == "pt-BR"


def test_connector_series_metadata_distinguishes_empty_from_absent() -> None:
    explicit_empty = ConnectorSeriesMetadata(title="", genres=())
    absent = ConnectorSeriesMetadata()

    assert explicit_empty.title == ""
    assert explicit_empty.genres == ()
    assert absent.title is None
    assert absent.genres is None
    assert explicit_empty != absent


@pytest.mark.parametrize(
    "field_name",
    ["title", "cover_url", "author", "description", "status", "language"],
)
def test_connector_series_metadata_rejects_non_text_values(field_name: str) -> None:
    with pytest.raises(TypeError, match=f"{field_name} must be a string or None"):
        _connector_series_metadata_with_invalid_text(field_name)


@pytest.mark.parametrize("genres", [[], ("Action", 7)])
def test_connector_series_metadata_rejects_mutable_or_invalid_genres(
    genres: object,
) -> None:
    invalid_genres = cast(tuple[str, ...] | None, genres)

    with pytest.raises(TypeError, match="genres must be a tuple of strings or None"):
        ConnectorSeriesMetadata(genres=invalid_genres)


def test_connector_series_metadata_is_immutable() -> None:
    metadata = ConnectorSeriesMetadata(title="Title", genres=("Action",))

    with pytest.raises(FrozenInstanceError):
        metadata.title = "Changed"  # type: ignore[misc]


def _connector_series_metadata_with_invalid_text(
    field_name: str,
) -> ConnectorSeriesMetadata:
    invalid_value = cast(str | None, 42)
    if field_name == "title":
        return ConnectorSeriesMetadata(title=invalid_value)
    if field_name == "cover_url":
        return ConnectorSeriesMetadata(cover_url=invalid_value)
    if field_name == "author":
        return ConnectorSeriesMetadata(author=invalid_value)
    if field_name == "description":
        return ConnectorSeriesMetadata(description=invalid_value)
    if field_name == "status":
        return ConnectorSeriesMetadata(status=invalid_value)
    if field_name == "language":
        return ConnectorSeriesMetadata(language=invalid_value)
    raise ValueError(f"unsupported test field: {field_name}")
