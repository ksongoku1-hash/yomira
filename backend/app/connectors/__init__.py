"""Source connector contracts and SDK-facing interfaces."""

from app.connectors.chapter import ConnectorChapter
from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.http_client import (
    ConnectorHttpClient,
    ConnectorHttpResponse,
    ConnectorHttpSession,
)
from app.connectors.metadata import ConnectorCapability, ConnectorMetadata
from app.connectors.mock import MockConnector
from app.connectors.pages import ConnectorPageReference, PageFetchRequest
from app.connectors.protocol import SourceConnector
from app.connectors.registry import (
    ConnectorRegistration,
    ConnectorRegistry,
    ConnectorRegistryError,
    ConnectorRegistryErrorReason,
)
from app.connectors.sandbox import ConnectorHostContext
from app.connectors.search import ConnectorSearchQuery, ConnectorSearchResult
from app.connectors.series_metadata import ConnectorSeriesMetadata

__all__ = [
    "ConnectorCapability",
    "ConnectorChapter",
    "ConnectorHostContext",
    "ConnectorHttpClient",
    "ConnectorHttpResponse",
    "ConnectorHttpSession",
    "ConnectorMetadata",
    "MockConnector",
    "ConnectorPageReference",
    "PageFetchRequest",
    "ConnectorRegistration",
    "ConnectorRegistry",
    "ConnectorRegistryError",
    "ConnectorRegistryErrorReason",
    "ConnectorSearchQuery",
    "ConnectorSearchResult",
    "ConnectorSeriesMetadata",
    "SourceConnectorError",
    "SourceConnectorOperation",
    "SourceConnector",
]
