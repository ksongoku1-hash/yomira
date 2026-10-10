"""Build Yomira's FMD2 source registry."""

from app.connectors.fmd2 import (
    FMD2_API_ORIGIN,
    FMD2_COVER_ONLY_ORIGINS,
    FMD2_METADATA,
    FMD2_NETWORK_ORIGINS,
    Fmd2Connector,
)
from app.connectors.http_client import ConnectorHttpClientFactory
from app.connectors.mangadex import (
    MANGADEX_COVER_ONLY_ORIGINS,
    MANGADEX_METADATA,
    MANGADEX_NETWORK_ORIGINS,
    MangaDexConnector,
)
from app.connectors.mock import MockConnector
from app.connectors.registry import ConnectorRegistration, ConnectorRegistry
from app.connectors.sandbox import ConnectorHostContext
from app.secrets.vault import SecretVault
from app.settings import Settings


def _create_mangadex_metadata_connector(context: ConnectorHostContext) -> MangaDexConnector:
    if context.http_client is None:
        raise RuntimeError("MangaDex metadata requires its scoped HTTP client.")
    return MangaDexConnector(context.http_client)


def create_connector_registry(
    settings: Settings,
    secret_vault: SecretVault,
    *,
    http_client_factory: ConnectorHttpClientFactory | None = None,
    fmd2_connector: Fmd2Connector | None = None,
) -> ConnectorRegistry:
    registrations: list[ConnectorRegistration] = []
    if fmd2_connector is not None:
        bridge = fmd2_connector
        registrations.append(
            ConnectorRegistration(
                metadata=FMD2_METADATA,
                factory=lambda _context: bridge,
                network_origins=(FMD2_NETWORK_ORIGINS - {FMD2_API_ORIGIN}) | {bridge.api_origin},
                cover_only_origins=FMD2_COVER_ONLY_ORIGINS,
            )
        )
    registrations.append(
        ConnectorRegistration(
            metadata=MANGADEX_METADATA,
            factory=_create_mangadex_metadata_connector,
            network_origins=MANGADEX_NETWORK_ORIGINS,
            cover_only_origins=MANGADEX_COVER_ONLY_ORIGINS,
        )
    )
    if settings.app_env in {"dev", "test"}:
        registrations.append(
            ConnectorRegistration(
                metadata=MockConnector().metadata,
                factory=lambda _context: MockConnector(),
            )
        )
    return ConnectorRegistry(
        context=ConnectorHostContext(secret_vault=secret_vault),
        registrations=registrations,
        http_client_factory=http_client_factory,
    )
