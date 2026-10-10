from dataclasses import FrozenInstanceError

import pytest

from app.connectors.pages import ConnectorPageReference


def test_connector_page_reference_preserves_the_exact_remote_value() -> None:
    reference = ConnectorPageReference(
        remote_reference="  https://example.invalid/page/001?token=fixture  "
    )

    assert reference.remote_reference == (
        "  https://example.invalid/page/001?token=fixture  "
    )


def test_connector_page_reference_accepts_an_opaque_non_url_identifier() -> None:
    reference = ConnectorPageReference(remote_reference="page-id:edition/A/001")

    assert reference.remote_reference == "page-id:edition/A/001"


def test_connector_page_reference_rejects_an_empty_value() -> None:
    with pytest.raises(ValueError, match="remote_reference must not be empty"):
        ConnectorPageReference(remote_reference="")


def test_connector_page_reference_is_immutable() -> None:
    reference = ConnectorPageReference(remote_reference="page-1")

    with pytest.raises(FrozenInstanceError):
        reference.remote_reference = "changed"  # type: ignore[misc]
