from __future__ import annotations

from pathlib import Path

import pytest

from app.connectors import (
    ConnectorCapability,
    ConnectorMetadata,
    ConnectorPageReference,
    ConnectorRegistration,
    ConnectorRegistry,
    MockConnector,
    SourceConnectorError,
    SourceConnectorOperation,
)
from app.connectors.registry import ConnectorRegistryError
from app.connectors.sandbox import ConnectorHostContext
from app.secrets.vault import WindowsDpapiSecretVault


def test_mock_connector_requires_explicit_fixture_origin_before_page_resolution() -> None:
    connector = MockConnector()

    with pytest.raises(SourceConnectorError) as raised:
        connector.resolve_page(ConnectorPageReference("mock-chapter-1/page-1"))

    assert raised.value.code.value == "SRC_CAPABILITY_UNSUPPORTED"
    assert raised.value.operation is SourceConnectorOperation.RESOLVE_PAGE
    assert "mock-chapter-1/page-1" not in str(raised.value)


def test_mock_connector_resolves_only_its_known_opaque_page_references() -> None:
    connector = MockConnector(page_server_base_url="http://127.0.0.1:43127")

    request = connector.resolve_page(ConnectorPageReference("mock-chapter-1/page-2"))

    assert request.url == "http://127.0.0.1:43127/api/v1/pages/mock-chapter-1/page-2"
    assert request.referer is None
    assert request.secret_reference is None
    assert "127.0.0.1" not in repr(request)


@pytest.mark.parametrize(
    "reference",
    (
        "mock-chapter-1/page-3",
        "mock-chapter-1/../secret",
        "https://attacker.invalid/page.jpg",
    ),
)
def test_mock_connector_rejects_unknown_or_path_injecting_page_references(
    reference: str,
) -> None:
    connector = MockConnector(page_server_base_url="http://127.0.0.1:43127")

    with pytest.raises(SourceConnectorError) as raised:
        connector.resolve_page(ConnectorPageReference(reference))

    assert raised.value.code.value == "SRC_CONNECTOR_FAILED"
    assert raised.value.operation is SourceConnectorOperation.RESOLVE_PAGE
    assert reference not in str(raised.value)


@pytest.mark.parametrize(
    "origin",
    (
        "http://example.com",
        "https://example.com/path",
        "https://user:secret@example.com",
        "file://example.com",
        "https://example.com?token=private",
    ),
)
def test_connector_registration_rejects_non_origin_network_entries(
    origin: str,
) -> None:
    metadata = ConnectorMetadata(
        connector_id="origin-fixture",
        connector_version="1.0",
        capabilities=frozenset({ConnectorCapability.PAGE_LIST}),
    )

    with pytest.raises(ValueError, match="origin"):
        ConnectorRegistration(
            metadata=metadata,
            factory=lambda _context: MockConnector(),
            network_origins=frozenset({origin}),
        )


def test_registry_exposes_only_the_registration_origin_allowlist(tmp_path: Path) -> None:
    metadata = ConnectorMetadata(
        connector_id="origin-fixture",
        connector_version="1.0",
        capabilities=frozenset({ConnectorCapability.PAGE_LIST}),
    )
    registration = ConnectorRegistration(
        metadata=metadata,
        factory=lambda _context: MockConnector(),
        network_origins=frozenset({"https://cdn.example.test", "https://img.example.test:8443"}),
    )
    registry = ConnectorRegistry(
        context=ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(tmp_path)),
        registrations=(registration,),
    )

    assert registry.get_network_origins("origin-fixture") == frozenset(
        {"https://cdn.example.test", "https://img.example.test:8443"}
    )
    with pytest.raises(ConnectorRegistryError):
        registry.get_network_origins("not-registered")


@pytest.mark.parametrize(
    ("origin", "expected"),
    (
        ("https://[2001:DB8::1]:443", "https://[2001:db8::1]"),
        ("http://[::1]:43127", "http://[::1]:43127"),
    ),
)
def test_connector_registration_normalizes_ipv6_origins(
    origin: str,
    expected: str,
) -> None:
    metadata = ConnectorMetadata(
        connector_id="ipv6-fixture",
        connector_version="1.0",
        capabilities=frozenset({ConnectorCapability.PAGE_LIST}),
    )

    registration = ConnectorRegistration(
        metadata=metadata,
        factory=lambda _context: MockConnector(),
        network_origins=frozenset({origin}),
    )

    assert registration.network_origins == frozenset({expected})
