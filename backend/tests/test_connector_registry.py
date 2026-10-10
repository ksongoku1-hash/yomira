from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.connectors import (
    ConnectorCapability,
    ConnectorChapter,
    ConnectorHostContext,
    ConnectorMetadata,
    ConnectorPageReference,
    ConnectorSearchQuery,
    ConnectorSearchResult,
    ConnectorSeriesMetadata,
    PageFetchRequest,
    SourceConnector,
    SourceConnectorError,
    SourceConnectorOperation,
)
from app.connectors.builtins import create_connector_registry
from app.connectors.fmd2 import Fmd2Connector
from app.connectors.http_client import ConnectorHttpSession
from app.connectors.registry import (
    ConnectorRegistration,
    ConnectorRegistry,
    ConnectorRegistryError,
    ConnectorRegistryErrorReason,
)
from app.domain.source import SourceHealthStatus
from app.secrets.vault import WindowsDpapiSecretVault
from app.settings import Settings


class RegistryFixtureConnector:
    def __init__(self, metadata: ConnectorMetadata) -> None:
        self._metadata = metadata

    @property
    def metadata(self) -> ConnectorMetadata:
        return self._metadata

    async def health_check(self) -> SourceHealthStatus:
        return SourceHealthStatus.HEALTHY

    async def search(
        self,
        query: ConnectorSearchQuery,
    ) -> Sequence[ConnectorSearchResult]:
        return (ConnectorSearchResult(query.query, None, query.query),)

    async def get_series(self, remote_series_reference: str) -> Mapping[str, object]:
        return {"title": remote_series_reference}

    async def get_chapters(
        self,
        remote_series_reference: str,
    ) -> Sequence[ConnectorChapter]:
        return (_chapter(remote_series_reference),)

    async def get_chapter(self, remote_chapter_reference: str) -> ConnectorChapter:
        return _chapter(remote_chapter_reference)

    async def get_pages(
        self,
        remote_chapter_reference: str,
    ) -> Sequence[ConnectorPageReference]:
        return (ConnectorPageReference(remote_chapter_reference),)

    async def acquire_pages(
        self, job_id: UUID, remote_chapter_reference: str, chapter_title: str
    ) -> Sequence[ConnectorPageReference]:
        return await self.get_pages(remote_chapter_reference)

    def resolve_page(self, page_reference: ConnectorPageReference) -> PageFetchRequest:
        return PageFetchRequest(
            f"https://pages.example.test/{page_reference.remote_reference}"
        )

    def normalize_metadata(self, raw_metadata: Mapping[str, object]) -> ConnectorSeriesMetadata:
        title = raw_metadata.get("title")
        return ConnectorSeriesMetadata(title=title if isinstance(title, str) else None)


def _chapter(remote_id: str) -> ConnectorChapter:
    return ConnectorChapter(
        remote_id=remote_id,
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


def _metadata(
    connector_id: str = "fixture-source",
    capabilities: frozenset[ConnectorCapability] | None = None,
) -> ConnectorMetadata:
    resolved_capabilities = (
        capabilities
        if capabilities is not None
        else frozenset(
            {
                ConnectorCapability.SEARCH,
                ConnectorCapability.METADATA,
                ConnectorCapability.CHAPTER_LIST,
                ConnectorCapability.PAGE_LIST,
            }
        )
    )
    return ConnectorMetadata(
        connector_id=connector_id,
        connector_version="fixture-v1",
        capabilities=resolved_capabilities,
    )


def _registration(
    metadata: ConnectorMetadata,
    created: list[RegistryFixtureConnector],
    *,
    actual_metadata: ConnectorMetadata | None = None,
    received_contexts: list[ConnectorHostContext] | None = None,
) -> ConnectorRegistration:
    def factory(context: ConnectorHostContext) -> SourceConnector[
        str,
        str,
        Mapping[str, object],
    ]:
        if received_contexts is not None:
            received_contexts.append(context)
        connector = RegistryFixtureConnector(actual_metadata or metadata)
        created.append(connector)
        return connector

    return ConnectorRegistration(metadata=metadata, factory=factory)


def _registry(
    tmp_path: Path,
    *registrations: ConnectorRegistration,
) -> ConnectorRegistry:
    return ConnectorRegistry(
        context=ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(tmp_path)),
        registrations=registrations,
    )


def test_registry_is_explicit_metadata_only_until_lazy_resolution(tmp_path: Path) -> None:
    metadata = _metadata()
    created: list[RegistryFixtureConnector] = []
    registry = _registry(tmp_path, _registration(metadata, created))

    assert registry.connector_ids == ("fixture-source",)
    assert registry.registered_metadata == (metadata,)
    assert registry.supports("fixture-source", ConnectorCapability.SEARCH)
    assert not registry.supports("fixture-source", ConnectorCapability.UPDATE_CHECK)
    assert created == []


def test_production_registry_exposes_fmd2_and_internal_metadata(tmp_path: Path) -> None:
    bridge = Fmd2Connector(tmp_path / "fmd.exe", tmp_path / "staging")
    registry = create_connector_registry(
        Settings(
            app_env="prod",
            data_root=tmp_path,
            library_root=tmp_path / "library",
        ),
        WindowsDpapiSecretVault(tmp_path),
        fmd2_connector=bridge,
    )

    assert registry.connector_ids == ("yomira.fmd2.webtoons", "yomira.metadata.mangadex")
    asyncio.run(bridge.close())


def test_test_registry_keeps_metadata_and_local_mock(tmp_path: Path) -> None:
    registry = create_connector_registry(
        Settings(
            app_env="test",
            data_root=tmp_path,
            library_root=tmp_path / "library",
        ),
        WindowsDpapiSecretVault(tmp_path),
    )

    assert registry.connector_ids == ("yomira.metadata.mangadex", "yomira.mock")


def test_production_registry_without_fmd2_keeps_library_metadata(tmp_path: Path) -> None:
    registry = create_connector_registry(
        Settings(
            app_env="prod",
            data_root=tmp_path,
            library_root=tmp_path / "library",
        ),
        WindowsDpapiSecretVault(tmp_path),
    )

    assert registry.connector_ids == ("yomira.metadata.mangadex",)


def test_registry_lazily_caches_one_connector_and_injects_host_context(tmp_path: Path) -> None:
    metadata = _metadata()
    created: list[RegistryFixtureConnector] = []
    context = ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(tmp_path))
    received_contexts: list[ConnectorHostContext] = []
    registration = _registration(metadata, created, received_contexts=received_contexts)
    registry = ConnectorRegistry(context=context, registrations=(registration,))

    first = registry.resolve("fixture-source", required_capability=ConnectorCapability.SEARCH)
    second = registry.resolve("fixture-source", required_capability=ConnectorCapability.SEARCH)

    assert first is second
    assert created == [first]
    assert received_contexts == [context]
    assert first.metadata == metadata
    assert callable(first.search)


def test_registry_scopes_http_client_to_each_registration(tmp_path: Path) -> None:
    first_metadata = _metadata("source-one")
    second_metadata = _metadata("source-two")
    created: list[RegistryFixtureConnector] = []
    received_contexts: list[ConnectorHostContext] = []

    def registration(
        metadata: ConnectorMetadata,
        origin: str,
    ) -> ConnectorRegistration:
        base = _registration(metadata, created, received_contexts=received_contexts)
        return ConnectorRegistration(
            metadata=base.metadata,
            factory=base.factory,
            network_origins=frozenset({origin}),
        )

    session = ConnectorHttpSession(
        test_transport=httpx.MockTransport(lambda _: httpx.Response(200))
    )
    root_context = ConnectorHostContext(
        secret_vault=WindowsDpapiSecretVault(tmp_path),
    )
    registry = ConnectorRegistry(
        context=root_context,
        registrations=(
            registration(first_metadata, "https://one.example"),
            registration(second_metadata, "https://two.example"),
        ),
        http_client_factory=session.scoped_client,
    )
    try:
        registry.resolve("source-one")
        registry.resolve("source-two")
    finally:
        asyncio.run(session.aclose())

    assert len(received_contexts) == 2
    first_http_client = received_contexts[0].http_client
    second_http_client = received_contexts[1].http_client
    assert first_http_client is not None
    assert second_http_client is not None
    assert first_http_client is not second_http_client
    assert first_http_client.allowed_origins == frozenset({"https://one.example"})
    assert second_http_client.allowed_origins == frozenset({"https://two.example"})
    assert all(context.secret_vault is root_context.secret_vault for context in received_contexts)


def test_duplicate_connector_ids_fail_without_replacing_registration(tmp_path: Path) -> None:
    first_created: list[RegistryFixtureConnector] = []
    second_created: list[RegistryFixtureConnector] = []
    first = _registration(_metadata(), first_created)
    duplicate = _registration(_metadata(), second_created)

    with pytest.raises(ConnectorRegistryError) as error:
        _registry(tmp_path, first, duplicate)

    assert error.value.reason is ConnectorRegistryErrorReason.DUPLICATE_CONNECTOR_ID
    assert first_created == []
    assert second_created == []


def test_unknown_connector_id_fails_without_factory_execution(tmp_path: Path) -> None:
    created: list[RegistryFixtureConnector] = []
    registry = _registry(tmp_path, _registration(_metadata(), created))

    with pytest.raises(ConnectorRegistryError) as error:
        registry.resolve("not-registered")

    assert error.value.reason is ConnectorRegistryErrorReason.UNKNOWN_CONNECTOR_ID
    assert "not-registered" not in str(error.value)
    assert created == []


def test_missing_capability_is_rejected_before_factory_execution(tmp_path: Path) -> None:
    metadata = _metadata(capabilities=frozenset({ConnectorCapability.PAGE_LIST}))
    created: list[RegistryFixtureConnector] = []
    registry = _registry(tmp_path, _registration(metadata, created))

    with pytest.raises(SourceConnectorError) as error:
        registry.resolve("fixture-source", required_capability=ConnectorCapability.SEARCH)

    assert error.value.code.value == "SRC_CAPABILITY_UNSUPPORTED"
    assert error.value.operation is SourceConnectorOperation.SEARCH
    assert created == []


def test_update_check_is_not_dispatched_without_an_approved_protocol_contract(
    tmp_path: Path,
) -> None:
    metadata = _metadata(capabilities=frozenset({ConnectorCapability.UPDATE_CHECK}))
    created: list[RegistryFixtureConnector] = []
    registry = _registry(tmp_path, _registration(metadata, created))

    assert registry.supports("fixture-source", ConnectorCapability.UPDATE_CHECK)
    with pytest.raises(ConnectorRegistryError) as error:
        registry.resolve(
            "fixture-source",
            required_capability=ConnectorCapability.UPDATE_CHECK,
        )

    assert error.value.reason is ConnectorRegistryErrorReason.UPDATE_CHECK_UNDEFINED
    assert created == []


def test_factory_metadata_mismatch_is_rejected_and_instance_is_not_cached(tmp_path: Path) -> None:
    expected = _metadata()
    actual = _metadata(connector_id="different-id")
    created: list[RegistryFixtureConnector] = []
    registry = _registry(
        tmp_path,
        _registration(expected, created, actual_metadata=actual),
    )

    with pytest.raises(ConnectorRegistryError) as error:
        registry.resolve("fixture-source")

    assert error.value.reason is ConnectorRegistryErrorReason.CONNECTOR_METADATA_MISMATCH
    assert len(created) == 1


def test_factory_failure_is_traced_sanitized_and_not_cached(tmp_path: Path) -> None:
    metadata = _metadata()
    calls = 0

    def failing_factory(_context: ConnectorHostContext) -> SourceConnector[
        str,
        str,
        Mapping[str, object],
    ]:
        nonlocal calls
        calls += 1
        raise RuntimeError("private-cookie-value")

    registry = _registry(
        tmp_path,
        ConnectorRegistration(metadata=metadata, factory=failing_factory),
    )
    for _ in range(2):
        with pytest.raises(ConnectorRegistryError) as error:
            registry.resolve("fixture-source")
        assert error.value.reason is ConnectorRegistryErrorReason.FACTORY_FAILED
        assert error.value.cause_type == "RuntimeError"
        assert "private-cookie-value" not in str(error.value)
        assert UUID(error.value.trace_id).version == 4

    assert calls == 2


def test_concurrent_resolution_creates_at_most_one_instance(tmp_path: Path) -> None:
    metadata = _metadata()
    created: list[RegistryFixtureConnector] = []

    def slow_factory(_context: ConnectorHostContext) -> SourceConnector[
        str,
        str,
        Mapping[str, object],
    ]:
        time.sleep(0.01)
        connector = RegistryFixtureConnector(metadata)
        created.append(connector)
        return connector

    registration = ConnectorRegistration(metadata=metadata, factory=slow_factory)
    registry = _registry(tmp_path, registration)

    def resolve_connector(_index: int) -> SourceConnector[str, str, Mapping[str, object]]:
        return registry.resolve("fixture-source")

    with ThreadPoolExecutor(max_workers=8) as executor:
        connectors = tuple(
            executor.map(resolve_connector, range(8))
        )

    assert len(created) == 1
    assert all(connector is connectors[0] for connector in connectors)


def test_registry_error_has_trace_id_and_fixed_message(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    with pytest.raises(ConnectorRegistryError) as error:
        registry.resolve("private-connector-id")

    assert UUID(error.value.trace_id).version == 4
    assert "private-connector-id" not in str(error.value)
