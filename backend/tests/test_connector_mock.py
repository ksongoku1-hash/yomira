import asyncio
from collections.abc import Mapping

import pytest

from app.connectors import (
    ConnectorCapability,
    ConnectorChapter,
    ConnectorMetadata,
    ConnectorPageReference,
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


def test_mock_connector_is_deterministic_in_memory_fixture() -> None:
    connector: SourceConnector[str, str, Mapping[str, object]] = MockConnector()

    async def exercise() -> None:
        assert await connector.health_check() is SourceHealthStatus.HEALTHY
        assert await connector.search(ConnectorSearchQuery("fixture")) == (
            ConnectorSearchResult(
                remote_id="mock-series-1",
                remote_url=None,
                remote_title="Mock Fixture Series",
            ),
        )
        assert await connector.search(ConnectorSearchQuery(" Fixture ")) == ()

        raw_metadata = await connector.get_series("mock-series-1")
        assert raw_metadata == {
            "title": "Mock Fixture Series",
            "author": "Yomira Test Fixture",
            "genres": ("test", "fixture"),
        }
        with pytest.raises(TypeError):
            raw_metadata["title"] = "changed"  # type: ignore[index]

        chapter = _fixture_chapter()
        assert await connector.get_chapters("mock-series-1") == (chapter,)
        assert await connector.get_chapter("mock-chapter-1") == chapter
        assert await connector.get_pages("mock-chapter-1") == (
            ConnectorPageReference("mock-chapter-1/page-2"),
            ConnectorPageReference("mock-chapter-1/page-1"),
            ConnectorPageReference("mock-chapter-1/page-1"),
        )

    asyncio.run(exercise())
    assert connector.normalize_metadata(
        {
            "title": "Mock Fixture Series",
            "author": "Yomira Test Fixture",
            "genres": ("test", "fixture"),
            "untrusted": "ignored",
        }
    ) == ConnectorSeriesMetadata(
        title="Mock Fixture Series",
        author="Yomira Test Fixture",
        genres=("test", "fixture"),
    )
    assert connector.normalize_metadata({"title": object(), "genres": ["invalid"]}) == (
        ConnectorSeriesMetadata()
    )


def test_mock_connector_metadata_is_fixed_and_excludes_undefined_capability() -> None:
    connector = MockConnector()

    assert connector.metadata == ConnectorMetadata(
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
    assert ConnectorCapability.UPDATE_CHECK not in connector.metadata.capabilities


@pytest.mark.parametrize(
    ("operation", "method_name", "reference"),
    [
        (
            SourceConnectorOperation.GET_SERIES, "get_series", "unknown-series"
        ),
        (
            SourceConnectorOperation.GET_CHAPTERS,
            "get_chapters",
            "unknown-series",
        ),
        (
            SourceConnectorOperation.GET_CHAPTER, "get_chapter", "unknown-chapter"
        ),
        (
            SourceConnectorOperation.GET_PAGES, "get_pages", "unknown-chapter"
        ),
    ],
)
def test_mock_connector_unknown_references_fail_with_sanitized_source_error(
    operation: SourceConnectorOperation,
    method_name: str,
    reference: str,
) -> None:
    connector = MockConnector()

    async def exercise() -> SourceConnectorError:
        with pytest.raises(SourceConnectorError) as raised:
            method = getattr(connector, method_name)
            await method(reference)
        return raised.value

    error = asyncio.run(exercise())
    assert error.code is ErrorCode.SRC_CONNECTOR_FAILED
    assert error.operation is operation
    assert error.retryable is False
    assert str(error) == "La operación del conector falló."


def _fixture_chapter() -> ConnectorChapter:
    return ConnectorChapter(
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
