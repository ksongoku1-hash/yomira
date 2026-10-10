"""Explicit, immutable registry for first-party source connector factories."""

import ipaddress
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.http_client import ConnectorHttpClientFactory
from app.connectors.metadata import ConnectorCapability, ConnectorMetadata
from app.connectors.protocol import SourceConnector
from app.connectors.sandbox import ConnectorHostContext
from app.errors import ErrorCode
from app.tracing import create_trace_id, current_trace_id

type RegisteredSourceConnector = SourceConnector[Any, Any, Any]
type ConnectorFactory = Callable[[ConnectorHostContext], RegisteredSourceConnector]


class ConnectorRegistryErrorReason(StrEnum):
    DUPLICATE_CONNECTOR_ID = "DUPLICATE_CONNECTOR_ID"
    FACTORY_FAILED = "FACTORY_FAILED"
    FACTORY_REENTRANT = "FACTORY_REENTRANT"
    INVALID_CONNECTOR_IMPLEMENTATION = "INVALID_CONNECTOR_IMPLEMENTATION"
    CONNECTOR_METADATA_MISMATCH = "CONNECTOR_METADATA_MISMATCH"
    UNKNOWN_CONNECTOR_ID = "UNKNOWN_CONNECTOR_ID"
    UPDATE_CHECK_UNDEFINED = "UPDATE_CHECK_UNDEFINED"


_REGISTRY_ERROR_MESSAGES = {
    ConnectorRegistryErrorReason.DUPLICATE_CONNECTOR_ID: (
        "El registry contiene identificadores de connector duplicados."
    ),
    ConnectorRegistryErrorReason.FACTORY_FAILED: (
        "No se pudo inicializar la implementación del connector."
    ),
    ConnectorRegistryErrorReason.FACTORY_REENTRANT: (
        "La factory del connector intentó resolver el mismo connector durante su creación."
    ),
    ConnectorRegistryErrorReason.INVALID_CONNECTOR_IMPLEMENTATION: (
        "La factory no produjo una implementación válida del contrato SourceConnector."
    ),
    ConnectorRegistryErrorReason.CONNECTOR_METADATA_MISMATCH: (
        "La metadata de la implementación no coincide con su registro."
    ),
    ConnectorRegistryErrorReason.UNKNOWN_CONNECTOR_ID: (
        "El connector solicitado no está registrado."
    ),
    ConnectorRegistryErrorReason.UPDATE_CHECK_UNDEFINED: (
        "UPDATE_CHECK no tiene todavía un contrato de operación aprobado."
    ),
}

_CAPABILITY_OPERATIONS = {
    ConnectorCapability.SEARCH: SourceConnectorOperation.SEARCH,
    ConnectorCapability.METADATA: SourceConnectorOperation.GET_SERIES,
    ConnectorCapability.CHAPTER_LIST: SourceConnectorOperation.GET_CHAPTERS,
    ConnectorCapability.PAGE_LIST: SourceConnectorOperation.GET_PAGES,
}
_REQUIRED_METHODS = (
    "health_check",
    "search",
    "get_series",
    "get_chapters",
    "get_chapter",
    "get_pages",
    "resolve_page",
    "normalize_metadata",
)


@dataclass(frozen=True, slots=True)
class ConnectorRegistration:
    metadata: ConnectorMetadata
    factory: ConnectorFactory
    network_origins: frozenset[str] = frozenset()
    cover_only_origins: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if type(self.metadata) is not ConnectorMetadata:
            raise TypeError("metadata must be ConnectorMetadata")
        if not callable(self.factory):
            raise TypeError("factory must be callable")
        if type(self.network_origins) is not frozenset:
            raise TypeError("network_origins must be a frozenset")
        normalized_origins: set[str] = set()
        for origin in self.network_origins:
            if type(origin) is not str or not origin:
                raise ValueError("network origin must be a credential-free HTTP(S) origin")
            try:
                parsed = urlsplit(origin)
                hostname = parsed.hostname
                port = parsed.port
            except ValueError:
                raise ValueError(
                    "network origin must be a credential-free HTTP(S) origin"
                ) from None
            scheme = parsed.scheme.lower()
            if (
                scheme not in {"http", "https"}
                or hostname is None
                or parsed.username is not None
                or parsed.password is not None
                or "@" in parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.netloc.endswith(":")
                or (port is not None and not 1 <= port <= 65535)
            ):
                raise ValueError("network origin must be a credential-free HTTP(S) origin")
            host = hostname.lower().rstrip(".")
            if "%" in host:
                raise ValueError("network origin must not contain a scoped IP address")
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if (
                scheme == "http"
                and host != "localhost"
                and (address is None or not address.is_loopback)
            ):
                raise ValueError("HTTP network origins must be loopback-only")
            if isinstance(address, ipaddress.IPv6Address):
                formatted_host = f"[{address.compressed}]"
            elif isinstance(address, ipaddress.IPv4Address):
                formatted_host = str(address)
            else:
                formatted_host = host
            if (
                port is None
                or (scheme == "https" and port == 443)
                or (scheme == "http" and port == 80)
            ):
                normalized = f"{scheme}://{formatted_host}"
            else:
                normalized = f"{scheme}://{formatted_host}:{port}"
            normalized_origins.add(normalized)
        object.__setattr__(self, "network_origins", frozenset(normalized_origins))
        if (
            type(self.cover_only_origins) is not frozenset
            or not self.cover_only_origins <= self.network_origins
        ):
            raise ValueError("Cover-only origins must be a subset of registered origins")


class ConnectorRegistryError(RuntimeError):
    """Sanitized registry failure with a stable reason and correlation trace."""

    def __init__(
        self,
        reason: ConnectorRegistryErrorReason,
        *,
        cause_type: str | None = None,
    ) -> None:
        if type(reason) is not ConnectorRegistryErrorReason:
            raise TypeError("reason must be a ConnectorRegistryErrorReason")
        if cause_type is not None and (
            type(cause_type) is not str
            or not cause_type.isascii()
            or not cause_type.replace("_", "").isalnum()
            or len(cause_type) > 64
        ):
            raise ValueError("cause_type must be a sanitized type name")
        self.reason = reason
        self.trace_id = current_trace_id() or create_trace_id()
        self.cause_type = cause_type
        super().__init__(_REGISTRY_ERROR_MESSAGES[reason])


class ConnectorRegistry:
    """Resolves an explicit allowlist and caches at most one instance per ID."""

    def __init__(
        self,
        *,
        context: ConnectorHostContext,
        registrations: Sequence[ConnectorRegistration],
        http_client_factory: ConnectorHttpClientFactory | None = None,
    ) -> None:
        if type(context) is not ConnectorHostContext:
            raise TypeError("context must be a ConnectorHostContext")

        registered: dict[str, ConnectorRegistration] = {}
        for registration in registrations:
            if type(registration) is not ConnectorRegistration:
                raise TypeError("registrations must contain ConnectorRegistration values")
            connector_id = registration.metadata.connector_id
            if connector_id in registered:
                raise ConnectorRegistryError(ConnectorRegistryErrorReason.DUPLICATE_CONNECTOR_ID)
            registered[connector_id] = registration

        self._context = context
        self._registrations = MappingProxyType(registered)
        if http_client_factory is not None and not callable(http_client_factory):
            raise TypeError("http_client_factory must be callable or None")
        self._http_client_factory = http_client_factory
        self._instances: dict[str, RegisteredSourceConnector] = {}
        self._constructing: set[str] = set()
        self._lock = RLock()

    @property
    def connector_ids(self) -> tuple[str, ...]:
        return tuple(self._registrations)

    @property
    def registered_metadata(self) -> tuple[ConnectorMetadata, ...]:
        return tuple(registration.metadata for registration in self._registrations.values())

    def get_network_origins(self, connector_id: str) -> frozenset[str]:
        """Return exactly the network origins declared for one registered connector."""
        return self._registration_for(connector_id).network_origins

    def get_metadata(self, connector_id: str) -> ConnectorMetadata:
        registration = self._registration_for(connector_id)
        return registration.metadata

    def get_download_origins(self, connector_id: str) -> frozenset[str]:
        registration = self._registration_for(connector_id)
        return registration.network_origins - registration.cover_only_origins

    def supports(self, connector_id: str, capability: ConnectorCapability) -> bool:
        if type(capability) is not ConnectorCapability:
            raise TypeError("capability must be a ConnectorCapability")
        registration = self._registration_for(connector_id)
        return capability in registration.metadata.capabilities

    def resolve(
        self,
        connector_id: str,
        *,
        required_capability: ConnectorCapability | None = None,
    ) -> RegisteredSourceConnector:
        registration = self._registration_for(connector_id)
        if required_capability is not None:
            if type(required_capability) is not ConnectorCapability:
                raise TypeError("required_capability must be a ConnectorCapability")
            if required_capability is ConnectorCapability.UPDATE_CHECK:
                raise ConnectorRegistryError(ConnectorRegistryErrorReason.UPDATE_CHECK_UNDEFINED)
            if required_capability not in registration.metadata.capabilities:
                raise SourceConnectorError(
                    code=ErrorCode.SRC_CAPABILITY_UNSUPPORTED,
                    operation=_CAPABILITY_OPERATIONS[required_capability],
                )

        with self._lock:
            cached = self._instances.get(connector_id)
            if cached is not None:
                self._verify_metadata(cached, registration.metadata)
                return cached
            if connector_id in self._constructing:
                raise ConnectorRegistryError(ConnectorRegistryErrorReason.FACTORY_REENTRANT)

            self._constructing.add(connector_id)
            try:
                try:
                    factory_context = self._context
                    if self._http_client_factory is not None:
                        factory_context = ConnectorHostContext(
                            secret_vault=self._context.secret_vault,
                            http_client=self._http_client_factory(registration.network_origins),
                        )
                    connector = registration.factory(factory_context)
                except Exception as error:
                    raise ConnectorRegistryError(
                        ConnectorRegistryErrorReason.FACTORY_FAILED,
                        cause_type=type(error).__name__,
                    ) from None
                self._verify_metadata(connector, registration.metadata)
            finally:
                self._constructing.remove(connector_id)
            self._instances[connector_id] = connector
            return connector

    def _registration_for(self, connector_id: str) -> ConnectorRegistration:
        if type(connector_id) is not str:
            raise TypeError("connector_id must be a string")
        registration = self._registrations.get(connector_id)
        if registration is None:
            raise ConnectorRegistryError(ConnectorRegistryErrorReason.UNKNOWN_CONNECTOR_ID)
        return registration

    @staticmethod
    def _verify_metadata(
        connector: RegisteredSourceConnector,
        registered_metadata: ConnectorMetadata,
    ) -> None:
        try:
            implementation_metadata = connector.metadata
            implements_contract = all(
                callable(getattr(connector, method_name, None)) for method_name in _REQUIRED_METHODS
            ) and (
                ConnectorCapability.PAGE_LIST not in registered_metadata.capabilities
                or callable(getattr(connector, "acquire_pages", None))
            )
        except Exception as error:
            raise ConnectorRegistryError(
                ConnectorRegistryErrorReason.INVALID_CONNECTOR_IMPLEMENTATION,
                cause_type=type(error).__name__,
            ) from None
        if not implements_contract:
            raise ConnectorRegistryError(
                ConnectorRegistryErrorReason.INVALID_CONNECTOR_IMPLEMENTATION
            )
        if type(implementation_metadata) is not ConnectorMetadata:
            raise ConnectorRegistryError(
                ConnectorRegistryErrorReason.INVALID_CONNECTOR_IMPLEMENTATION
            )
        if implementation_metadata != registered_metadata:
            raise ConnectorRegistryError(ConnectorRegistryErrorReason.CONNECTOR_METADATA_MISMATCH)
