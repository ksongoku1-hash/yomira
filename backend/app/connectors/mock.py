"""Deterministic, offline connector fixture for SDK verification."""

import ipaddress
from collections.abc import Mapping
from types import MappingProxyType
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

from app.connectors.chapter import ConnectorChapter
from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.metadata import ConnectorCapability, ConnectorMetadata
from app.connectors.pages import ConnectorPageReference, PageFetchRequest
from app.connectors.search import ConnectorSearchQuery, ConnectorSearchResult
from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.domain.source import SourceHealthStatus
from app.errors import ErrorCode

_METADATA = ConnectorMetadata(
    connector_id="yomira.mock",
    connector_version="1.0.0",
    capabilities=frozenset(
        {
            ConnectorCapability.SEARCH,
            ConnectorCapability.METADATA,
            ConnectorCapability.CHAPTER_LIST,
            ConnectorCapability.PAGE_LIST,
        }
    ),
)
_RAW_SERIES_METADATA: Mapping[str, object] = MappingProxyType(
    {
        "title": "Mock Fixture Series",
        "author": "Yomira Test Fixture",
        "genres": ("test", "fixture"),
    }
)
_CHAPTER = ConnectorChapter(
    remote_id="mock-chapter-1",
    remote_url=None,
    remote_label="1",
    chapter_raw_label="1",
    chapter_major=1,
    chapter_minor=None,
    chapter_variant=None,
    chapter_type=None,
    chapter_sort_key=(1, 0),
    volume_number=None,
    language="es",
    display_label="Capítulo 1",
)
_PAGES = (
    ConnectorPageReference("mock-chapter-1/page-2"),
    ConnectorPageReference("mock-chapter-1/page-1"),
    ConnectorPageReference("mock-chapter-1/page-1"),
)


class MockConnector:
    """One immutable synthetic series; performs no network or filesystem I/O."""

    def __init__(self, *, page_server_base_url: str | None = None) -> None:
        if page_server_base_url is not None:
            if type(page_server_base_url) is not str or not page_server_base_url:
                raise ValueError("page_server_base_url must be an HTTP(S) origin or None")
            try:
                parsed = urlsplit(page_server_base_url)
                hostname = parsed.hostname
                port = parsed.port
            except ValueError:
                raise ValueError("page_server_base_url must be an HTTP(S) origin") from None
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or hostname is None
                or parsed.username is not None
                or parsed.password is not None
                or "@" in parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.netloc.endswith(":")
                or (port is not None and not 1 <= port <= 65535)
            ):
                raise ValueError("page_server_base_url must be an HTTP(S) origin")
            try:
                address = ipaddress.ip_address(hostname)
            except ValueError:
                address = None
            is_loopback = hostname.lower().rstrip(".") == "localhost" or (
                address is not None and address.is_loopback
            )
            if parsed.scheme.lower() == "http" and not is_loopback:
                raise ValueError("HTTP page fixtures must use a loopback origin")
            self._page_server_base_url = page_server_base_url.rstrip("/")
        else:
            self._page_server_base_url = None

    @property
    def metadata(self) -> ConnectorMetadata:
        return _METADATA

    async def health_check(self) -> SourceHealthStatus:
        return SourceHealthStatus.HEALTHY

    async def search(
        self,
        query: ConnectorSearchQuery,
    ) -> tuple[ConnectorSearchResult, ...]:
        if query.query != "fixture":
            return ()
        return (
            ConnectorSearchResult(
                remote_id="mock-series-1",
                remote_url=None,
                remote_title="Mock Fixture Series",
            ),
        )

    async def get_series(
        self,
        remote_series_reference: str,
    ) -> Mapping[str, object]:
        if remote_series_reference != "mock-series-1":
            raise _unknown_reference(SourceConnectorOperation.GET_SERIES)
        return _RAW_SERIES_METADATA

    async def get_chapters(
        self,
        remote_series_reference: str,
    ) -> tuple[ConnectorChapter, ...]:
        if remote_series_reference != "mock-series-1":
            raise _unknown_reference(SourceConnectorOperation.GET_CHAPTERS)
        return (_CHAPTER,)

    async def get_chapter(self, remote_chapter_reference: str) -> ConnectorChapter:
        if remote_chapter_reference != "mock-chapter-1":
            raise _unknown_reference(SourceConnectorOperation.GET_CHAPTER)
        return _CHAPTER

    async def get_pages(
        self,
        remote_chapter_reference: str,
    ) -> tuple[ConnectorPageReference, ...]:
        if remote_chapter_reference != "mock-chapter-1":
            raise _unknown_reference(SourceConnectorOperation.GET_PAGES)
        return _PAGES

    async def acquire_pages(
        self,
        _job_id: UUID,
        remote_chapter_reference: str,
        _chapter_title: str,
    ) -> tuple[ConnectorPageReference, ...]:
        return await self.get_pages(remote_chapter_reference)

    def resolve_page(self, page_reference: ConnectorPageReference) -> PageFetchRequest:
        if type(page_reference) is not ConnectorPageReference:
            raise TypeError("page_reference must be a ConnectorPageReference")
        known_references = {page.remote_reference for page in _PAGES}
        if page_reference.remote_reference not in known_references:
            raise _unknown_reference(SourceConnectorOperation.RESOLVE_PAGE)
        if self._page_server_base_url is None:
            raise SourceConnectorError(
                code=ErrorCode.SRC_CAPABILITY_UNSUPPORTED,
                operation=SourceConnectorOperation.RESOLVE_PAGE,
            )
        return PageFetchRequest(
            url=(
                f"{self._page_server_base_url}/api/v1/pages/"
                f"{page_reference.remote_reference}"
            )
        )

    def normalize_metadata(
        self,
        raw_metadata: Mapping[str, object],
    ) -> ConnectorSeriesMetadata:
        return ConnectorSeriesMetadata(
            title=_optional_string(raw_metadata.get("title")),
            cover_url=_optional_string(raw_metadata.get("cover_url")),
            author=_optional_string(raw_metadata.get("author")),
            description=_optional_string(raw_metadata.get("description")),
            genres=_string_tuple(raw_metadata.get("genres")),
            status=_optional_string(raw_metadata.get("status")),
            language=_optional_string(raw_metadata.get("language")),
        )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, tuple):
        return None
    items = cast(tuple[object, ...], value)
    if not all(isinstance(item, str) for item in items):
        return None
    return cast(tuple[str, ...], items)


def _unknown_reference(operation: SourceConnectorOperation) -> SourceConnectorError:
    return SourceConnectorError(
        code=ErrorCode.SRC_CONNECTOR_FAILED,
        operation=operation,
        retryable=False,
    )
