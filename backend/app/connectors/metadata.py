"""Immutable identity and capability declarations for source connectors."""

from dataclasses import dataclass
from enum import StrEnum
from typing import cast


def _is_string(value: object) -> bool:
    return isinstance(value, str)


def _is_frozenset(value: object) -> bool:
    return isinstance(value, frozenset)


def _has_only_connector_capabilities(value: object) -> bool:
    if not isinstance(value, frozenset):
        return False
    capabilities = cast(frozenset[object], value)
    return all(isinstance(capability, ConnectorCapability) for capability in capabilities)


class ConnectorCapability(StrEnum):
    """Operations a source connector declares that it can provide."""

    SEARCH = "SEARCH"
    METADATA = "METADATA"
    CHAPTER_LIST = "CHAPTER_LIST"
    PAGE_LIST = "PAGE_LIST"
    UPDATE_CHECK = "UPDATE_CHECK"


@dataclass(frozen=True, slots=True)
class ConnectorMetadata:
    """Stable connector identity and its declared operation capabilities."""

    connector_id: str
    connector_version: str
    capabilities: frozenset[ConnectorCapability]

    def __post_init__(self) -> None:
        if not _is_string(self.connector_id):
            raise TypeError("connector_id must be a string")
        if not self.connector_id:
            raise ValueError("connector_id must not be empty")
        if not _is_string(self.connector_version):
            raise TypeError("connector_version must be a string")
        if not self.connector_version:
            raise ValueError("connector_version must not be empty")
        if not _is_frozenset(self.capabilities):
            raise TypeError("capabilities must be an immutable frozenset")
        if not _has_only_connector_capabilities(self.capabilities):
            raise ValueError("capabilities must contain ConnectorCapability values")
