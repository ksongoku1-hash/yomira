"""Capability-limited host context for trusted, in-process connectors."""

from dataclasses import dataclass

from app.connectors.http_client import ConnectorHttpClient
from app.secrets.vault import SecretVault


@dataclass(frozen=True, slots=True)
class ConnectorHostContext:
    """Explicit host capabilities passed to a trusted connector factory."""

    secret_vault: SecretVault
    http_client: ConnectorHttpClient | None = None
