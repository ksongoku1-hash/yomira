from dataclasses import FrozenInstanceError

import pytest

from app.connectors.search import ConnectorSearchQuery, ConnectorSearchResult


def test_search_query_preserves_text_without_implicit_normalization() -> None:
    query = ConnectorSearchQuery(query="  A title\n")

    assert query.query == "  A title\n"


def test_search_query_does_not_reject_empty_query_without_specification_rule() -> None:
    assert ConnectorSearchQuery(query="").query == ""


def test_search_result_preserves_an_empty_remote_title() -> None:
    result = ConnectorSearchResult(
        remote_id="remote-id",
        remote_url=None,
        remote_title="",
    )

    assert result.remote_title == ""


@pytest.mark.parametrize(
    ("remote_id", "remote_url"),
    [
        ("remote-id", None),
        (None, "https://example.invalid/series/42"),
        ("remote-id", "https://example.invalid/series/42"),
    ],
)
def test_search_result_accepts_a_remote_id_or_url_without_normalizing(
    remote_id: str | None,
    remote_url: str | None,
) -> None:
    result = ConnectorSearchResult(
        remote_id=remote_id,
        remote_url=remote_url,
        remote_title="  Remote title\n",
    )

    assert result.remote_id == remote_id
    assert result.remote_url == remote_url
    assert result.remote_title == "  Remote title\n"


@pytest.mark.parametrize(
    ("remote_id", "remote_url", "message"),
    [
        (None, None, "remote_id or remote_url must be provided"),
        ("", "https://example.invalid/series/42", "remote_id must not be empty"),
        ("remote-id", "", "remote_url must not be empty"),
    ],
)
def test_search_result_rejects_missing_or_empty_present_references(
    remote_id: str | None,
    remote_url: str | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ConnectorSearchResult(
            remote_id=remote_id,
            remote_url=remote_url,
            remote_title=None,
        )


def test_search_contract_values_are_immutable() -> None:
    query = ConnectorSearchQuery(query="manga")
    result = ConnectorSearchResult(
        remote_id="42",
        remote_url=None,
        remote_title="Example",
    )

    with pytest.raises(FrozenInstanceError):
        query.query = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.remote_id = "changed"  # type: ignore[misc]
