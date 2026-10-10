"""Lossless data contracts for source connector search operations."""

from dataclasses import dataclass

DEFAULT_SEARCH_PAGE_BUDGET = 3
MAX_SEARCH_PAGE_BUDGET = 20


def _is_string(value: object) -> bool:
    return isinstance(value, str)


@dataclass(frozen=True, slots=True)
class ConnectorSearchQuery:
    """Exact query text passed from the caller to a connector."""

    query: str
    page_budget: int = DEFAULT_SEARCH_PAGE_BUDGET

    def __post_init__(self) -> None:
        if not _is_string(self.query):
            raise TypeError("query must be a string")
        if type(self.page_budget) is not int:
            raise TypeError("page_budget must be an integer")
        if not 1 <= self.page_budget <= MAX_SEARCH_PAGE_BUDGET:
            raise ValueError("page_budget must be from 1 to 20")


@dataclass(frozen=True, slots=True)
class ConnectorSearchResult:
    """Remote series reference, optional title, cover, and source display label."""

    remote_id: str | None
    remote_url: str | None
    remote_title: str | None
    cover_url: str | None = None
    source_label: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("remote_id", self.remote_id),
            ("remote_url", self.remote_url),
        ):
            if value is not None and not _is_string(value):
                raise TypeError(f"{field_name} must be a string or None")
            if value is not None and not value:
                raise ValueError(f"{field_name} must not be empty when provided")
        if self.remote_id is None and self.remote_url is None:
            raise ValueError("remote_id or remote_url must be provided")
        if self.remote_title is not None and not _is_string(self.remote_title):
            raise TypeError("remote_title must be a string or None")
        if self.cover_url is not None and not _is_string(self.cover_url):
            raise TypeError("cover_url must be a string or None")
        if self.source_label is not None and not _is_string(self.source_label):
            raise TypeError("source_label must be a string or None")
        if self.source_label is not None and (
            not self.source_label.strip() or len(self.source_label) > 200
        ):
            raise ValueError("source_label must be nonempty and at most 200 characters")
