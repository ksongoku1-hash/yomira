"""Static contract required of source connector implementations."""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.connectors.chapter import ConnectorChapter
from app.connectors.metadata import ConnectorMetadata
from app.connectors.pages import ConnectorPageAvailability, ConnectorPageReference, PageFetchRequest
from app.connectors.search import ConnectorSearchQuery, ConnectorSearchResult
from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.domain.source import SourceHealthStatus


class SourceConnector[
    SeriesReferenceT,
    ChapterReferenceT,
    RawMetadataT,
](Protocol):
    """Structural contract for retrieving and normalizing remote source data."""

    @property
    def metadata(self) -> ConnectorMetadata:
        """Expose the connector's immutable identity and declared capabilities."""
        ...

    async def health_check(self) -> SourceHealthStatus:
        """Return the connector's currently observed health status."""
        ...

    async def search(
        self,
        query: ConnectorSearchQuery,
    ) -> Sequence[ConnectorSearchResult]:
        """Search the remote source using a connector-specific query."""
        ...

    async def get_series(
        self,
        remote_series_reference: SeriesReferenceT,
    ) -> RawMetadataT:
        """Fetch raw connector-specific series metadata for local normalization."""
        ...

    async def get_chapters(
        self,
        remote_series_reference: SeriesReferenceT,
    ) -> Sequence[ConnectorChapter]:
        """List chapters belonging to a source-specific series reference."""
        ...

    async def get_chapter(
        self,
        remote_chapter_reference: ChapterReferenceT,
    ) -> ConnectorChapter:
        """Retrieve one chapter using a source-specific reference."""
        ...

    async def get_pages(
        self,
        remote_chapter_reference: ChapterReferenceT,
    ) -> Sequence[ConnectorPageReference]:
        """List pages belonging to a source-specific chapter reference."""
        ...

    def resolve_page(self, page_reference: ConnectorPageReference) -> PageFetchRequest:
        """Resolve one opaque page reference without performing network or file I/O."""
        ...

    def normalize_metadata(self, raw_metadata: RawMetadataT) -> ConnectorSeriesMetadata:
        """Normalize connector-specific metadata without persisting it."""
        ...


class AcquisitionProvider(Protocol):
    """Required acquisition contract for connectors advertising PAGE_LIST."""

    async def acquire_pages(
        self,
        job_id: UUID,
        remote_chapter_reference: str,
        chapter_title: str,
    ) -> Sequence[ConnectorPageReference]:
        """Acquire one chapter under the stable Yomira job ID and expose its raw pages."""
        ...


@runtime_checkable
class OnlinePageProvider(Protocol):
    async def open_online_pages(
        self, session_id: UUID, remote_chapter_reference: str
    ) -> Sequence[ConnectorPageReference]:
        """Discover a page plan without fetching or writing chapter images."""
        ...

    async def close_online_pages(self, session_id: UUID) -> None:
        """Release the source's in-memory plan; no download is created."""
        ...


@runtime_checkable
class ProgressivePageProvider(Protocol):
    async def get_ready_pages(
        self,
        job_id: UUID,
        previous: ConnectorPageAvailability | None = None,
    ) -> ConnectorPageAvailability:
        """Observe an existing acquisition; availability does not imply core validation."""
        ...
