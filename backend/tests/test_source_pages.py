from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.metadata import ConnectorCapability
from app.connectors.mock import MockConnector
from app.connectors.pages import ConnectorPageAvailability, ConnectorPageReference
from app.connectors.registry import ConnectorRegistration, ConnectorRegistry
from app.connectors.sandbox import ConnectorHostContext
from app.domain.job_manifest import MAX_MANIFEST_PAGE_COUNT
from app.errors import ErrorCode
from app.secrets.vault import WindowsDpapiSecretVault
from app.services.download_transport import NetworkOrigin
from app.services.source_pages import SourcePageService


@pytest.fixture
def source_pages(tmp_path: Path) -> tuple[SourcePageService, MockConnector]:
    connector = MockConnector()
    registry = ConnectorRegistry(
        context=ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(tmp_path)),
        registrations=(
            ConnectorRegistration(
                metadata=connector.metadata,
                factory=lambda _context: connector,
                network_origins=frozenset(
                    {"https://pages.example.test", "https://covers.example.test"}
                ),
                cover_only_origins=frozenset({"https://covers.example.test"}),
            ),
        ),
    )
    return SourcePageService(registry), connector


def test_resolve_reuses_the_registered_connector_and_checks_version(
    source_pages: tuple[SourcePageService, MockConnector],
) -> None:
    service, connector = source_pages
    assert (
        service.resolve(connector.metadata.connector_id, connector.metadata.connector_version)
        is connector
    )
    with pytest.raises(SourceConnectorError) as failure:
        service.resolve(connector.metadata.connector_id, "different-version")
    assert failure.value.code is ErrorCode.SRC_SCHEMA_CHANGED
    assert failure.value.operation is SourceConnectorOperation.GET_CHAPTER


def test_page_capability_is_checked_before_creating_connector(tmp_path: Path) -> None:
    connector = MockConnector()
    metadata = replace(connector.metadata, capabilities=frozenset({ConnectorCapability.SEARCH}))
    created: list[bool] = []

    def factory(_context: ConnectorHostContext) -> MockConnector:
        created.append(True)
        return connector

    service = SourcePageService(
        ConnectorRegistry(
            context=ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(tmp_path)),
            registrations=(ConnectorRegistration(metadata=metadata, factory=factory),),
        )
    )
    with pytest.raises(SourceConnectorError) as failure:
        service.resolve(metadata.connector_id, metadata.connector_version)
    assert failure.value.code is ErrorCode.SRC_CAPABILITY_UNSUPPORTED
    assert created == []


def test_page_origins_exclude_cover_only_hosts(
    source_pages: tuple[SourcePageService, MockConnector],
) -> None:
    service, connector = source_pages
    assert service.download_origins(connector.metadata.connector_id) == frozenset(
        {
            NetworkOrigin.from_url("https://pages.example.test"),
        }
    )


def test_acquisition_keeps_its_id_order_and_duplicate_references(
    source_pages: tuple[SourcePageService, MockConnector],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, connector = source_pages
    acquisition_id = uuid4()
    references = (ConnectorPageReference("second"), ConnectorPageReference("first"))
    expected = (*references, references[0])
    calls: list[tuple[UUID, str, str]] = []

    async def acquire(job_id: UUID, chapter: str, title: str) -> tuple[ConnectorPageReference, ...]:
        calls.append((job_id, chapter, title))
        return expected

    monkeypatch.setattr(connector, "acquire_pages", acquire)
    assert (
        asyncio.run(service.acquire_pages(connector, acquisition_id, "chapter", "Title"))
        == expected
    )
    assert calls == [(acquisition_id, "chapter", "Title")]


@pytest.mark.parametrize(
    "invalid",
    [(), ("untyped",), (ConnectorPageReference("page"),) * (MAX_MANIFEST_PAGE_COUNT + 1)],
    ids=["empty", "untyped", "too-many"],
)
def test_invalid_page_plans_are_rejected(
    source_pages: tuple[SourcePageService, MockConnector],
    monkeypatch: pytest.MonkeyPatch,
    invalid: tuple[object, ...],
) -> None:
    service, connector = source_pages

    async def acquire(
        _job_id: UUID, _chapter: str, _title: str
    ) -> tuple[ConnectorPageReference, ...]:
        return cast(tuple[ConnectorPageReference, ...], invalid)

    monkeypatch.setattr(connector, "acquire_pages", acquire)
    with pytest.raises(ValueError, match="V1 contract"):
        asyncio.run(service.acquire_pages(connector, uuid4(), "chapter", "Title"))


def test_acquisition_propagates_provider_error_and_trace(
    source_pages: tuple[SourcePageService, MockConnector],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, connector = source_pages
    expected = SourceConnectorError(
        code=ErrorCode.SRC_CONNECTOR_FAILED,
        operation=SourceConnectorOperation.GET_PAGES,
        retryable=True,
    )

    async def acquire(
        _job_id: UUID, _chapter: str, _title: str
    ) -> tuple[ConnectorPageReference, ...]:
        raise expected

    monkeypatch.setattr(connector, "acquire_pages", acquire)
    with pytest.raises(SourceConnectorError) as failure:
        asyncio.run(service.acquire_pages(connector, uuid4(), "chapter", "Title"))
    assert failure.value is expected


@pytest.mark.parametrize("invalid_id", ["anonymous", UUID(int=0)])
def test_acquisition_rejects_an_invalid_id_before_calling_provider(
    source_pages: tuple[SourcePageService, MockConnector],
    monkeypatch: pytest.MonkeyPatch,
    invalid_id: object,
) -> None:
    service, connector = source_pages

    async def acquire(
        _job_id: UUID, _chapter: str, _title: str
    ) -> tuple[ConnectorPageReference, ...]:
        raise AssertionError("Provider must not run with an invalid acquisition ID")

    monkeypatch.setattr(connector, "acquire_pages", acquire)
    with pytest.raises(ValueError, match="UUIDv4"):
        asyncio.run(service.acquire_pages(connector, cast(UUID, invalid_id), "chapter", "Title"))


def test_empty_download_origins_are_rejected(tmp_path: Path) -> None:
    connector = MockConnector()
    service = SourcePageService(
        ConnectorRegistry(
            context=ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(tmp_path)),
            registrations=(
                ConnectorRegistration(
                    metadata=connector.metadata,
                    factory=lambda _context: connector,
                ),
            ),
        )
    )
    with pytest.raises(SourceConnectorError) as failure:
        service.download_origins(connector.metadata.connector_id)
    assert failure.value.code is ErrorCode.SRC_CAPABILITY_UNSUPPORTED
    assert failure.value.operation is SourceConnectorOperation.RESOLVE_PAGE


def test_acquisition_cancellation_reaches_provider(
    source_pages: tuple[SourcePageService, MockConnector],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, connector = source_pages

    async def exercise() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def acquire(
            _job_id: UUID, _chapter: str, _title: str
        ) -> tuple[ConnectorPageReference, ...]:
            started.set()
            try:
                await asyncio.Future[None]()
            finally:
                cancelled.set()
            return ()

        monkeypatch.setattr(connector, "acquire_pages", acquire)
        task = asyncio.create_task(service.acquire_pages(connector, uuid4(), "chapter", "Title"))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["generation", "count", "missing", "reference", "valid"])
def test_progressive_plan_cannot_replace_or_remove_ready_pages(
    source_pages: tuple[SourcePageService, MockConnector],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    service, connector = source_pages
    previous = ConnectorPageAvailability(uuid4(), 2, ((1, ConnectorPageReference("one")),), False)
    snapshot = previous
    if change == "generation":
        snapshot = replace(previous, generation=uuid4())
    elif change == "count":
        snapshot = replace(previous, total_pages=3)
    elif change == "missing":
        snapshot = replace(previous, pages=())
    elif change == "reference":
        snapshot = replace(previous, pages=((1, ConnectorPageReference("replaced")),))
    else:
        snapshot = replace(
            previous, pages=previous.pages + ((2, ConnectorPageReference("two")),), is_complete=True
        )

    async def ready(
        _job: UUID, _previous: ConnectorPageAvailability | None = None
    ) -> ConnectorPageAvailability:
        return snapshot

    monkeypatch.setattr(connector, "get_ready_pages", ready, raising=False)
    if change == "valid":
        assert asyncio.run(service.get_ready_pages(connector, uuid4(), previous)) == snapshot
    else:
        with pytest.raises(ValueError, match="regressed"):
            asyncio.run(service.get_ready_pages(connector, uuid4(), previous))
