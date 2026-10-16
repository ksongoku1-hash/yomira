"""Core-owned page access shared by acquisition consumers."""

from typing import cast
from uuid import UUID

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.metadata import ConnectorCapability
from app.connectors.pages import ConnectorPageAvailability, ConnectorPageReference
from app.connectors.protocol import AcquisitionProvider, OnlinePageProvider, ProgressivePageProvider
from app.connectors.registry import ConnectorRegistry, RegisteredSourceConnector
from app.domain.job_manifest import MAX_MANIFEST_PAGE_COUNT
from app.errors import ErrorCode
from app.services.download_transport import NetworkOrigin


class SourcePageService:
    def __init__(self, registry: ConnectorRegistry) -> None:
        if type(registry) is not ConnectorRegistry:
            raise TypeError("registry must be a ConnectorRegistry")
        self._registry = registry

    def resolve(self, connector_id: str, connector_version: str) -> RegisteredSourceConnector:
        metadata = self._registry.get_metadata(connector_id)
        if metadata.connector_version != connector_version:
            raise SourceConnectorError(
                code=ErrorCode.SRC_SCHEMA_CHANGED,
                operation=SourceConnectorOperation.GET_CHAPTER,
            )
        return self._registry.resolve(
            connector_id,
            required_capability=ConnectorCapability.PAGE_LIST,
        )

    async def acquire_pages(
        self,
        connector: RegisteredSourceConnector,
        acquisition_id: UUID,
        remote_chapter_reference: str,
        chapter_title: str,
    ) -> tuple[ConnectorPageReference, ...]:
        if type(acquisition_id) is not UUID or acquisition_id.version != 4:
            raise ValueError("acquisition_id must be a UUIDv4")
        references = tuple(
            await cast(AcquisitionProvider, connector).acquire_pages(
                acquisition_id,
                remote_chapter_reference,
                chapter_title,
            )
        )
        if (
            not references
            or len(references) > MAX_MANIFEST_PAGE_COUNT
            or any(type(reference) is not ConnectorPageReference for reference in references)
        ):
            raise ValueError("Connector page list exceeds or violates the V1 contract.")
        return references

    async def open_online_pages(
        self, connector: RegisteredSourceConnector, session_id: UUID, reference: str
    ) -> tuple[ConnectorPageReference, ...]:
        if type(session_id) is not UUID or session_id.version != 4:
            raise ValueError("session_id must be a UUIDv4")
        if not isinstance(connector, OnlinePageProvider):
            raise SourceConnectorError(
                code=ErrorCode.SRC_CAPABILITY_UNSUPPORTED,
                operation=SourceConnectorOperation.GET_PAGES,
            )
        pages = tuple(await connector.open_online_pages(session_id, reference))
        if (
            not 1 <= len(pages) <= MAX_MANIFEST_PAGE_COUNT
            or any(type(page) is not ConnectorPageReference for page in pages)
            or len(set(pages)) != len(pages)
        ):
            raise ValueError("Online page plan violates the V1 contract")
        return pages

    async def get_ready_pages(
        self,
        connector: RegisteredSourceConnector,
        acquisition_id: UUID,
        previous: ConnectorPageAvailability | None = None,
    ) -> ConnectorPageAvailability:
        if type(acquisition_id) is not UUID or acquisition_id.version != 4:
            raise ValueError("acquisition_id must be a UUIDv4")
        if not isinstance(connector, ProgressivePageProvider):
            raise SourceConnectorError(
                code=ErrorCode.SRC_CAPABILITY_UNSUPPORTED,
                operation=SourceConnectorOperation.GET_PAGES,
            )
        snapshot = await connector.get_ready_pages(acquisition_id, previous)
        if type(snapshot) is not ConnectorPageAvailability:
            raise ValueError("Connector readiness violates the V1 contract.")
        if previous is not None and previous.total_pages and (
            snapshot.generation != previous.generation
            or snapshot.total_pages != previous.total_pages
            or not set(previous.pages).issubset(snapshot.pages)
            or (previous.is_complete and not snapshot.is_complete)
        ):
            raise ValueError("Connector readiness regressed or changed its page plan.")
        return snapshot

    def download_origins(self, connector_id: str) -> frozenset[NetworkOrigin]:
        origins = frozenset(
            NetworkOrigin.from_url(origin)
            for origin in self._registry.get_download_origins(connector_id)
        )
        if not origins:
            raise SourceConnectorError(
                code=ErrorCode.SRC_CAPABILITY_UNSUPPORTED,
                operation=SourceConnectorOperation.RESOLVE_PAGE,
            )
        return origins
