import asyncio
import inspect
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

import pytest

from app.connectors import (
    ConnectorCapability,
    ConnectorHostContext,
    ConnectorMetadata,
    ConnectorRegistration,
    ConnectorRegistry,
    ConnectorRegistryError,
    ConnectorRegistryErrorReason,
    ConnectorSearchQuery,
    ConnectorSearchResult,
    ConnectorSeriesMetadata,
    SourceConnector,
    SourceConnectorError,
    SourceConnectorOperation,
)
from app.connectors.mock import MockConnector
from app.domain.source import SourceHealthStatus
from app.errors import ErrorCode
from app.secrets.vault import WindowsDpapiSecretVault


@pytest.fixture
def connector() -> MockConnector:
    return MockConnector()


def test_mock_implements_generic_source_connector_protocol(connector: MockConnector) -> None:
    protocol_view: SourceConnector[str, str, Mapping[str, object]] = connector

    assert protocol_view.metadata is connector.metadata


def test_sdk_contract_exposes_declared_identity_capabilities_and_operations(
    connector: MockConnector,
) -> None:
    metadata = connector.metadata

    assert type(metadata) is ConnectorMetadata
    assert metadata.connector_id == "yomira.mock"
    assert metadata.connector_version == "1.0.0"
    assert metadata.capabilities == frozenset(
        {
            ConnectorCapability.SEARCH,
            ConnectorCapability.METADATA,
            ConnectorCapability.CHAPTER_LIST,
            ConnectorCapability.PAGE_LIST,
        }
    )
    assert ConnectorCapability.UPDATE_CHECK not in metadata.capabilities
    assert inspect.iscoroutinefunction(connector.health_check)
    assert inspect.iscoroutinefunction(connector.search)
    assert inspect.iscoroutinefunction(connector.get_series)
    assert inspect.iscoroutinefunction(connector.get_chapters)
    assert inspect.iscoroutinefunction(connector.get_chapter)
    assert inspect.iscoroutinefunction(connector.get_pages)
    assert callable(connector.normalize_metadata)


def test_sdk_contract_preserves_remote_values_order_and_duplicates(
    connector: MockConnector,
) -> None:
    async def exercise() -> None:
        assert await connector.health_check() is SourceHealthStatus.HEALTHY

        query = ConnectorSearchQuery("fixture")
        results = await connector.search(query)
        assert type(results) is tuple
        assert results == (
            ConnectorSearchResult(
                remote_id="mock-series-1",
                remote_url=None,
                remote_title="Mock Fixture Series",
            ),
        )
        assert await connector.search(ConnectorSearchQuery(" Fixture ")) == ()

        raw_metadata = await connector.get_series("mock-series-1")
        assert raw_metadata["title"] == "Mock Fixture Series"
        chapters = await connector.get_chapters("mock-series-1")
        assert type(chapters) is tuple
        assert len(chapters) == 1
        chapter = chapters[0]
        assert chapter == await connector.get_chapter("mock-chapter-1")
        assert chapter.chapter_raw_label == "1"
        assert chapter.chapter_sort_key == (1, 0)
        assert chapter.remote_id == "mock-chapter-1"

        pages = await connector.get_pages("mock-chapter-1")
        assert type(pages) is tuple
        assert tuple(page.remote_reference for page in pages) == (
            "mock-chapter-1/page-2",
            "mock-chapter-1/page-1",
            "mock-chapter-1/page-1",
        )

    asyncio.run(exercise())
    assert connector.normalize_metadata(
        {
            "title": "Mock Fixture Series",
            "author": "Yomira Test Fixture",
            "genres": ("test", "fixture"),
            "unrecognized": "ignored",
        }
    ) == ConnectorSeriesMetadata(
        title="Mock Fixture Series",
        author="Yomira Test Fixture",
        genres=("test", "fixture"),
    )


@pytest.mark.parametrize(
    ("operation", "reference"),
    [
        (SourceConnectorOperation.GET_SERIES, "unknown-series-secret"),
        (SourceConnectorOperation.GET_CHAPTERS, "unknown-series-secret"),
        (SourceConnectorOperation.GET_CHAPTER, "unknown-chapter-secret"),
        (SourceConnectorOperation.GET_PAGES, "unknown-chapter-secret"),
    ],
)
def test_sdk_contract_errors_are_typed_traced_and_sanitized(
    connector: MockConnector,
    operation: SourceConnectorOperation,
    reference: str,
) -> None:
    async def invoke_unknown_reference() -> None:
        if operation is SourceConnectorOperation.GET_SERIES:
            await connector.get_series(reference)
        elif operation is SourceConnectorOperation.GET_CHAPTERS:
            await connector.get_chapters(reference)
        elif operation is SourceConnectorOperation.GET_CHAPTER:
            await connector.get_chapter(reference)
        else:
            await connector.get_pages(reference)

    with pytest.raises(SourceConnectorError) as raised:
        asyncio.run(invoke_unknown_reference())

    error = raised.value
    assert error.code is ErrorCode.SRC_CONNECTOR_FAILED
    assert error.operation is operation
    assert error.retryable is False
    assert UUID(error.trace_id).version == 4
    assert reference not in str(error)


def test_sdk_mock_resolves_only_through_explicit_lazy_registry(tmp_path: Path) -> None:
    metadata = MockConnector().metadata
    created: list[MockConnector] = []

    def factory(
        _context: ConnectorHostContext,
    ) -> SourceConnector[str, str, Mapping[str, object]]:
        instance = MockConnector()
        created.append(instance)
        return instance

    registry = ConnectorRegistry(
        context=ConnectorHostContext(
            secret_vault=WindowsDpapiSecretVault(tmp_path),
        ),
        registrations=(ConnectorRegistration(metadata=metadata, factory=factory),),
    )

    assert registry.connector_ids == ("yomira.mock",)
    assert registry.get_metadata("yomira.mock") is metadata
    assert registry.supports("yomira.mock", ConnectorCapability.SEARCH)
    assert not registry.supports("yomira.mock", ConnectorCapability.UPDATE_CHECK)
    assert created == []

    with pytest.raises(ConnectorRegistryError) as undefined_capability:
        registry.resolve(
            "yomira.mock",
            required_capability=ConnectorCapability.UPDATE_CHECK,
        )
    assert undefined_capability.value.reason is (
        ConnectorRegistryErrorReason.UPDATE_CHECK_UNDEFINED
    )
    assert created == []

    first = registry.resolve(
        "yomira.mock",
        required_capability=ConnectorCapability.SEARCH,
    )
    second = registry.resolve(
        "yomira.mock",
        required_capability=ConnectorCapability.SEARCH,
    )
    assert first is second
    assert created == [first]
