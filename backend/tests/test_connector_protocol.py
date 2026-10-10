import asyncio
from collections.abc import Mapping

from app.connectors import (
    ConnectorCapability,
    ConnectorChapter,
    ConnectorMetadata,
    ConnectorPageReference,
    ConnectorSearchQuery,
    ConnectorSearchResult,
    ConnectorSeriesMetadata,
    PageFetchRequest,
)
from app.connectors.protocol import SourceConnector
from app.domain.source import SourceHealthStatus


class LocalFixtureConnector:
    metadata = ConnectorMetadata(
        connector_id="local-fixture",
        connector_version="test-v1",
        capabilities=frozenset(
            {
                ConnectorCapability.SEARCH,
                ConnectorCapability.METADATA,
                ConnectorCapability.CHAPTER_LIST,
                ConnectorCapability.PAGE_LIST,
            }
        ),
    )

    async def health_check(self) -> SourceHealthStatus:
        return SourceHealthStatus.HEALTHY

    async def search(
        self,
        query: ConnectorSearchQuery,
    ) -> tuple[ConnectorSearchResult, ...]:
        return (
            ConnectorSearchResult(
                remote_id=query.query,
                remote_url=None,
                remote_title=query.query,
            ),
        )

    async def get_series(
        self,
        remote_series_reference: str,
    ) -> Mapping[str, object]:
        return {"title": remote_series_reference}

    async def get_chapters(
        self,
        remote_series_reference: str,
    ) -> tuple[ConnectorChapter, ...]:
        return (_fixture_chapter(),)

    async def get_chapter(self, remote_chapter_reference: str) -> ConnectorChapter:
        return _fixture_chapter()

    async def get_pages(
        self,
        remote_chapter_reference: str,
    ) -> tuple[ConnectorPageReference, ...]:
        return (
            ConnectorPageReference(f"{remote_chapter_reference}/page-2"),
            ConnectorPageReference(f"{remote_chapter_reference}/page-1"),
            ConnectorPageReference(f"{remote_chapter_reference}/page-1"),
        )

    def resolve_page(self, page_reference: ConnectorPageReference) -> PageFetchRequest:
        return PageFetchRequest(
            f"https://pages.example.test/{page_reference.remote_reference}"
        )

    def normalize_metadata(
        self,
        raw_metadata: Mapping[str, object],
    ) -> ConnectorSeriesMetadata:
        title = raw_metadata.get("title")
        return ConnectorSeriesMetadata(title=title if isinstance(title, str) else None)


def test_source_connector_accepts_a_structural_local_fixture() -> None:
    connector: SourceConnector[
        str,
        str,
        Mapping[str, object],
    ] = LocalFixtureConnector()

    async def exercise() -> None:
        assert await connector.health_check() is SourceHealthStatus.HEALTHY
        assert await connector.search(ConnectorSearchQuery("manga")) == (
            ConnectorSearchResult("manga", None, "manga"),
        )
        raw_metadata = await connector.get_series("series-1")
        assert raw_metadata == {"title": "series-1"}
        assert await connector.get_chapters("series-1") == (_fixture_chapter(),)
        assert await connector.get_chapter("chapter-1") == _fixture_chapter()
        assert await connector.get_pages("chapter-1") == (
            ConnectorPageReference("chapter-1/page-2"),
            ConnectorPageReference("chapter-1/page-1"),
            ConnectorPageReference("chapter-1/page-1"),
        )

    asyncio.run(exercise())
    assert connector.normalize_metadata({"title": "Example"}) == (
        ConnectorSeriesMetadata(title="Example")
    )
    assert connector.metadata.connector_id == "local-fixture"


def _fixture_chapter() -> ConnectorChapter:
    return ConnectorChapter(
        remote_id="chapter-1",
        remote_url=None,
        remote_label="1",
        chapter_raw_label="1",
        chapter_major=1,
        chapter_minor=None,
        chapter_variant=None,
        chapter_type=None,
        chapter_sort_key=(1, 0),
        volume_number=None,
        language=None,
        display_label="1",
    )
