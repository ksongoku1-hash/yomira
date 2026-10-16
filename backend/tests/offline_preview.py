"""Explicit, process-local launcher for the offline UI/API pipeline preview.

This module is test support. It is never imported by the application runtime.
It starts only the bounded MockMangaServer fixture and composes the app with a
single mock connector whose page origin is that fixture's exact loopback origin.
"""

# This standalone script adjusts sys.path before importing the application.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_TESTS_ROOT = Path(__file__).resolve().parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

import httpx
import uvicorn
from mock_manga_server import MockMangaServer

from app.connectors.http_client import ConnectorHttpClientFactory
from app.connectors.mock import MockConnector
from app.connectors.registry import ConnectorRegistration, ConnectorRegistry
from app.connectors.sandbox import ConnectorHostContext
from app.domain.job_backpressure import GIBIBYTE, ResourceSample
from app.persistence.disk_budget import DiskBudgetManager
from app.persistence.storage_gate import SharedStorageGate
from app.persistence.windows_paths import DataRootIdentity
from app.secrets.vault import SecretVault
from app.services.download_transport import (
    DownloadTransport,
    DownloadTransportPolicy,
    NetworkOrigin,
    SecretHeaderProvider,
)
from app.settings import Settings, load_settings
from app.single_instance import WindowsMutexGuard


class _HealthyPreviewSampler:
    """Fixed healthy readings keep this deterministic fixture independent of PDH."""

    def sample(self) -> ResourceSample:
        return ResourceSample(
            available_memory_bytes=8 * GIBIBYTE,
            total_memory_bytes=16 * GIBIBYTE,
            disk_busy_percent=5.0,
            disk_latency_ms=5.0,
            sampled_at_monotonic=time.monotonic(),
        )

    def close(self) -> None:
        return None


class PreviewInstanceGuard(WindowsMutexGuard):
    """Process-local no-op guard restricted to this isolated test profile."""

    def __init__(self, *, app_env: str) -> None:
        super().__init__()
        self._enabled = app_env == "test"

    def acquire(self) -> None:
        if not self._enabled:
            raise RuntimeError("The preview instance guard requires app_env='test'.")


def _validate_preview_data_root(settings: Settings) -> None:
    data_root = settings.data_root.resolve(strict=False)
    normal_data_root = load_settings().data_root.resolve(strict=False)
    if not data_root.is_dir() or not data_root.name.startswith("YomiraOfflinePreview-"):
        raise RuntimeError("The preview requires a newly created isolated temp data_root.")
    if _paths_overlap(data_root, normal_data_root):
        raise RuntimeError("The preview data_root overlaps Yomira's normal data root.")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _paths_overlap(first: Path, second: Path) -> bool:
    return _is_within(first, second) or _is_within(second, first)


def create_preview_data_root(*, base_dir: Path | None = None) -> Path:
    """Create a unique, persistent temp root disjoint from the normal library.

    The returned directory is intentionally not managed by a temporary-directory
    context and is never deleted by this harness. Its exact path is printed at
    launch so fixture jobs and their diagnostics remain inspectable after exit.
    """

    parent = Path(base_dir) if base_dir is not None else Path(tempfile.gettempdir())
    parent = parent.resolve(strict=True)
    normal_data_root = load_settings().data_root.resolve(strict=False)
    if _is_within(parent, normal_data_root):
        raise RuntimeError("The OS temp directory is inside Yomira's normal data root.")

    for _attempt in range(8):
        candidate = parent / f"YomiraOfflinePreview-{uuid4()}"
        if _paths_overlap(candidate, normal_data_root):
            continue
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        resolved = candidate.resolve(strict=True)
        if _paths_overlap(resolved, normal_data_root):
            raise RuntimeError("The preview data root overlaps Yomira's normal data root.")
        return resolved

    raise RuntimeError("Could not reserve an isolated offline preview data root.")


@contextmanager
def isolated_preview_composition(
    server: MockMangaServer,
    *,
    settings: Settings,
) -> Generator[None]:
    """Patch only this process to register the fixture and inject its test transport."""

    if settings.app_env != "test":
        raise RuntimeError("The isolated preview composition only accepts app_env='test'.")
    _validate_preview_data_root(settings)
    if not server.is_running:
        raise RuntimeError("The preview fixture server must already be running.")
    host, port = server.bound_address
    if host != "127.0.0.1" or not 1 <= port <= 65_535:
        raise RuntimeError("The preview fixture must be bound to IPv4 loopback.")

    fixture_origin_url = server.base_url
    fixture_origin = NetworkOrigin.from_url(fixture_origin_url)
    expected_origins = frozenset({fixture_origin})
    if not fixture_origin.is_loopback or fixture_origin.scheme != "http":
        raise RuntimeError("The preview fixture origin must be loopback HTTP.")

    def create_fixture_registry(
        settings: Settings,
        secret_vault: SecretVault,
        *,
        http_client_factory: ConnectorHttpClientFactory | None = None,
        fmd2_connector: object | None = None,
    ) -> ConnectorRegistry:
        _ = http_client_factory, fmd2_connector
        if settings.app_env != "test":
            raise RuntimeError("The offline preview only accepts app_env='test'.")
        metadata = MockConnector(page_server_base_url=fixture_origin_url).metadata
        return ConnectorRegistry(
            context=ConnectorHostContext(secret_vault=secret_vault),
            registrations=(
                ConnectorRegistration(
                    metadata=metadata,
                    factory=lambda _context: MockConnector(
                        page_server_base_url=fixture_origin_url
                    ),
                    network_origins=frozenset({fixture_origin_url}),
                ),
            ),
        )

    def create_fixture_download_transport(
        client: httpx.AsyncClient,
        policy: DownloadTransportPolicy,
        *,
        data_root: Path,
        data_root_identity: DataRootIdentity,
        storage_gate: SharedStorageGate,
        disk_budget_manager: DiskBudgetManager,
        secret_header_provider: SecretHeaderProvider | None = None,
    ) -> DownloadTransport:
        if policy.allowed_origins != expected_origins:
            raise RuntimeError("The preview download allowlist is not the exact fixture origin.")
        injected_transport = getattr(client, "_transport", None)
        if not isinstance(injected_transport, httpx.AsyncBaseTransport):
            raise RuntimeError("The preview HTTP client has no injectable test transport.")
        return DownloadTransport(
            client,
            policy,
            data_root=data_root,
            data_root_identity=data_root_identity,
            storage_gate=storage_gate,
            disk_budget_manager=disk_budget_manager,
            secret_header_provider=secret_header_provider,
            test_transport=injected_transport,
        )

    def create_preview_instance_guard() -> PreviewInstanceGuard:
        if settings.app_env != "test":
            raise RuntimeError("The preview instance guard requires app_env='test'.")
        return PreviewInstanceGuard(app_env=settings.app_env)

    with ExitStack() as patches:
        patches.enter_context(
            patch("app.factory.create_connector_registry", create_fixture_registry)
        )
        patches.enter_context(
            patch("app.lifecycle.create_connector_registry", create_fixture_registry)
        )
        patches.enter_context(
            patch("app.lifecycle.DownloadTransport", create_fixture_download_transport)
        )
        patches.enter_context(
            patch("app.lifecycle.WindowsResourceSampler", _HealthyPreviewSampler)
        )
        patches.enter_context(
            patch("app.lifecycle.create_instance_guard", create_preview_instance_guard)
        )
        yield


@dataclass(frozen=True, slots=True)
class _PreviewArguments:
    api_port: int


def _parse_args() -> _PreviewArguments:
    parser = argparse.ArgumentParser(
        description="Run Yomira's isolated, offline UI-to-reader preview API."
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=8000,
        help="Loopback API port (default: 8000; Vite's default proxy target).",
    )
    parsed = parser.parse_args()
    if not 1 <= parsed.api_port <= 65_535:
        parser.error("--api-port must be between 1 and 65535")
    return _PreviewArguments(api_port=int(parsed.api_port))


def main() -> None:
    args = _parse_args()
    data_root = create_preview_data_root()
    settings = Settings(
        app_env="test",
        api_host="127.0.0.1",
        api_port=args.api_port,
        data_root=data_root,
        log_level="INFO",
    )

    with MockMangaServer() as fixture_server:
        with isolated_preview_composition(fixture_server, settings=settings):
            from app.factory import create_app

            app = create_app(settings)
            print("UI (inicia el frontend aparte): http://127.0.0.1:5173")
            print(f"API: http://127.0.0.1:{settings.api_port}")
            print(f"Fixture origin allowlist: {fixture_server.base_url}")
            print(f"Persistent isolated data_root: {data_root}")
            print("Connector enabled: yomira.mock; search query: fixture")
            print("Stop with Ctrl+C. The isolated data_root is preserved.", flush=True)
            uvicorn.run(
                app,
                host=settings.api_host,
                port=settings.api_port,
                log_level="info",
                access_log=False,
            )


if __name__ == "__main__":
    main()
