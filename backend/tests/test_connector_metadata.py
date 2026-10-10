from typing import cast

import pytest

from app.connectors.metadata import ConnectorCapability, ConnectorMetadata


def test_connector_metadata_preserves_opaque_identity_and_capabilities() -> None:
    capabilities = frozenset(
        {ConnectorCapability.SEARCH, ConnectorCapability.PAGE_LIST}
    )
    metadata = ConnectorMetadata(
        connector_id="Connector / Region A ",
        connector_version="build-v2+future",
        capabilities=capabilities,
    )

    assert metadata.connector_id == "Connector / Region A "
    assert metadata.connector_version == "build-v2+future"
    assert metadata.capabilities == capabilities


def test_connector_metadata_allows_no_optional_operations() -> None:
    metadata = ConnectorMetadata(
        connector_id="health-only",
        connector_version="local-build",
        capabilities=frozenset(),
    )

    assert metadata.capabilities == frozenset()


@pytest.mark.parametrize(
    ("connector_id", "connector_version", "message"),
    [
        ("", "1", "connector_id must not be empty"),
        ("fixture", "", "connector_version must not be empty"),
    ],
)
def test_connector_metadata_rejects_empty_identity_fields(
    connector_id: str,
    connector_version: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ConnectorMetadata(
            connector_id=connector_id,
            connector_version=connector_version,
            capabilities=frozenset(),
        )


def test_connector_metadata_rejects_values_outside_capability_vocabulary() -> None:
    invalid_capabilities = cast(frozenset[ConnectorCapability], frozenset({"SEARCH"}))

    with pytest.raises(ValueError, match="capabilities must contain ConnectorCapability"):
        ConnectorMetadata(
            connector_id="fixture",
            connector_version="1",
            capabilities=invalid_capabilities,
        )


def test_connector_metadata_rejects_mutable_capability_collections() -> None:
    mutable_capabilities = cast(
        frozenset[ConnectorCapability],
        {ConnectorCapability.SEARCH},
    )

    with pytest.raises(TypeError, match="capabilities must be an immutable frozenset"):
        ConnectorMetadata(
            connector_id="fixture",
            connector_version="1",
            capabilities=mutable_capabilities,
        )
