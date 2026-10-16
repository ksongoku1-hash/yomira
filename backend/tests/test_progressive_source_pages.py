from __future__ import annotations

import asyncio
import base64
import io
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from PIL import Image

from app.connectors.errors import SourceConnectorError
from app.connectors.fmd2 import FMD2_API_ORIGIN, Fmd2Connector
from app.connectors.mock import MockConnector
from app.connectors.pages import ConnectorPageAvailability, ConnectorPageReference
from app.connectors.registry import ConnectorRegistration, ConnectorRegistry
from app.connectors.sandbox import ConnectorHostContext
from app.errors import ErrorCode
from app.persistence.windows_paths import DataRootIdentity
from app.secrets.reference import SecretReference
from app.secrets.vault import WindowsDpapiSecretVault
from app.services.download_transport import (
    DownloadTransport,
    DownloadTransportPolicy,
)
from app.services.image_validation import ImageValidationPolicy, validate_image_file
from app.services.source_pages import SourcePageService


class _ImageStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body[:32]
        yield self.body[32:]


def _chapter() -> str:
    payload = json.dumps(["/en/test/list", "/en/test/chapter", 1]).encode()
    return "yomira-fmd2:v1:" + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _connector(tmp_path: Path, handler: httpx.MockTransport) -> Fmd2Connector:
    client = httpx.AsyncClient(
        base_url=FMD2_API_ORIGIN,
        headers={"Authorization": "Bearer " + "a" * 64},
        transport=handler,
        trust_env=False,
        follow_redirects=False,
    )
    return Fmd2Connector(tmp_path / "fmd.exe", tmp_path / "stage", api_client=client)


def _service(tmp_path: Path, connector: Fmd2Connector | MockConnector) -> SourcePageService:
    return SourcePageService(
        ConnectorRegistry(
            context=ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(tmp_path)),
            registrations=(
                ConnectorRegistration(
                    metadata=connector.metadata,
                    factory=lambda _context: connector,
                    network_origins=frozenset({FMD2_API_ORIGIN}),
                ),
            ),
        )
    )


def _status(job: UUID, generation: UUID, **changes: object) -> dict[str, object]:
    return {
        "job_id": str(job),
        "status": "running",
        "completed_pages": 1,
        "total_pages": 3,
        "files": ["001.png", "002.png"],  # Old inventory may contain a partial file.
        "page_generation": str(generation),
        "planned_pages": 3,
        "ready_pages": [{"ordinal": 1, "file_name": "001.png"}],
        **changes,
    }


def test_first_page_is_downloaded_and_validated_before_acquisition_finishes(tmp_path: Path) -> None:
    async def exercise() -> None:
        job, generation = uuid4(), uuid4()
        finished = asyncio.Event()
        started = asyncio.Event()
        posts: list[str] = []
        images: list[str] = []
        buffer = io.BytesIO()
        Image.new("RGB", (4, 6), "white").save(buffer, format="PNG")
        body = buffer.getvalue()

        async def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                posts.append(request.url.path)
                started.set()
                return httpx.Response(202, json={"job_id": str(job)})
            if "/ready/" in request.url.path:
                assert request.headers["Authorization"] == "Bearer " + "a" * 64
                images.append(request.url.path)
                assert not finished.is_set()
                return httpx.Response(
                    200, stream=_ImageStream(body), headers={"Content-Type": "image/png"}
                )
            snapshot = _status(job, generation)
            if finished.is_set():
                snapshot.update(
                    status="completed", completed_pages=3, files=["003.png", "001.png", "002.png"]
                )
            return httpx.Response(200, json=snapshot)

        handler = httpx.MockTransport(handle)
        connector = _connector(tmp_path, handler)
        service = _service(tmp_path, connector)
        client = httpx.AsyncClient(transport=handler, trust_env=False, follow_redirects=False)
        data = tmp_path / "data"
        data.mkdir()
        transport = DownloadTransport(
            client,
            DownloadTransportPolicy(
                allowed_origins=service.download_origins(connector.metadata.connector_id),
                max_bytes=1024,
                chunk_size=64,
                connect_timeout_seconds=1,
                read_timeout_seconds=1,
                write_timeout_seconds=1,
                pool_timeout_seconds=1,
                max_redirects=0,
                max_retry_after_seconds=1,
                global_concurrency=1,
                per_host_concurrency=1,
            ),
            data_root=data,
            data_root_identity=DataRootIdentity.capture(data),
            test_transport=handler,
            secret_header_provider=connector.page_secret_headers,
        )
        acquisition = asyncio.create_task(service.acquire_pages(connector, job, _chapter(), "Test"))
        try:
            await started.wait()
            snapshot = await service.get_ready_pages(connector, job)
            assert not snapshot.is_complete and snapshot.total_pages == 3
            assert [ordinal for ordinal, _ in snapshot.pages] == [1]
            assert not acquisition.done()
            ordinal, page = snapshot.pages[0]
            request = connector.resolve_page(page)
            assert request.secret_reference is not None
            assert "Bearer" not in repr(request) and "001.png" not in repr(request)
            downloaded = await transport.download(request, job_id=job, ordinal=ordinal, attempt=1)
            validated = validate_image_file(
                downloaded.path,
                downloaded.content_type or "",
                policy=ImageValidationPolicy(
                    max_bytes=1024, max_width=100, max_height=100, max_pixels=10_000
                ),
            )
            assert validated.sha256 == downloaded.sha256
            assert (validated.width, validated.height) == (4, 6)
            assert not acquisition.done()
            assert posts == ["/v1/downloads"] and len(images) == 1
            finished.set()
            completed_pages = await acquisition
            assert len(completed_pages) == 3
            completed = await service.get_ready_pages(connector, job, snapshot)
            assert completed.is_complete and len(completed.pages) == 3
            assert completed.pages[0] == snapshot.pages[0]
        finally:
            finished.set()
            if not acquisition.done():
                acquisition.cancel()
            await asyncio.gather(acquisition, return_exceptions=True)
            await client.aclose()
            await connector.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "changes",
    [
        {"job_id": str(uuid4())},
        {"status": "unexpected"},
        {"status": []},
        {"page_generation": str(UUID(int=0))},
        {"planned_pages": True},
        {"planned_pages": 10_001},
        {"ready_pages": [{"ordinal": 1, "file_name": "../001.png"}]},
        {"ready_pages": [{"ordinal": True, "file_name": "001.png"}]},
        {"ready_pages": [{"ordinal": 1, "file_name": "002.png"}]},
        {"ready_pages": [{"ordinal": 4, "file_name": "004.png"}]},
        {"ready_pages": [{"ordinal": 1, "file_name": "001.png"}] * 2},
        {"status": "completed", "completed_pages": 3, "files": ["001.png", "003.png"]},
        {"status": "completed", "completed_pages": 3, "files": ["001.jpg", "002.png", "003.png"]},
    ],
)
def test_malformed_readiness_is_rejected(tmp_path: Path, changes: dict[str, object]) -> None:
    async def exercise() -> None:
        job, generation = uuid4(), uuid4()
        connector = _connector(
            tmp_path,
            httpx.MockTransport(
                lambda _request: httpx.Response(200, json=_status(job, generation, **changes))
            ),
        )
        try:
            with pytest.raises(SourceConnectorError) as failure:
                await connector.get_ready_pages(job)
            assert failure.value.code is ErrorCode.SRC_SCHEMA_CHANGED
        finally:
            await connector.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["generation", "count", "disappeared", "changed-file"])
def test_ready_plan_cannot_change_after_publication(tmp_path: Path, change: str) -> None:
    async def exercise() -> None:
        job, generation = uuid4(), uuid4()
        state = _status(job, generation)
        connector = _connector(
            tmp_path, httpx.MockTransport(lambda _request: httpx.Response(200, json=state))
        )
        try:
            previous = await connector.get_ready_pages(job)
            if change == "generation":
                state["page_generation"] = str(uuid4())
            if change == "count":
                state["planned_pages"] = 4
            if change == "disappeared":
                state["ready_pages"] = []
            if change == "changed-file":
                state["ready_pages"] = [{"ordinal": 1, "file_name": "001.jpg"}]
            with pytest.raises(SourceConnectorError) as failure:
                await connector.get_ready_pages(job, previous)
            assert failure.value.code is ErrorCode.SRC_SCHEMA_CHANGED
        finally:
            await connector.close()

    asyncio.run(exercise())


def test_pending_plan_can_start_and_recovered_completion_uses_verified_inventory(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        job, generation = uuid4(), uuid4()
        state = _status(job, generation, status="queued", planned_pages=0, ready_pages=[])
        connector = _connector(
            tmp_path, httpx.MockTransport(lambda _request: httpx.Response(200, json=state))
        )
        try:
            previous = await connector.get_ready_pages(job)
            state.update(
                page_generation=str(uuid4()),
                status="completed",
                completed_pages=3,
                files=["001.png", "002.png", "003.png"],
            )
            recovered = await connector.get_ready_pages(job, previous)
            assert recovered.is_complete and recovered.total_pages == 3
            assert len(recovered.pages) == 3
            with pytest.raises(ValueError):
                connector.page_secret_headers(SecretReference.generate())
        finally:
            await connector.close()

    asyncio.run(exercise())


def test_sources_without_progressive_proof_fail_explicitly(tmp_path: Path) -> None:
    connector = MockConnector()
    with pytest.raises(SourceConnectorError) as failure:
        asyncio.run(_service(tmp_path, connector).get_ready_pages(connector, uuid4()))
    assert failure.value.code is ErrorCode.SRC_CAPABILITY_UNSUPPORTED


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_terminal_failure_does_not_expose_partial_files(tmp_path: Path, state: str) -> None:
    async def exercise() -> None:
        job, generation = uuid4(), uuid4()
        connector = _connector(
            tmp_path,
            httpx.MockTransport(
                lambda _request: httpx.Response(200, json=_status(job, generation, status=state))
            ),
        )
        try:
            with pytest.raises(SourceConnectorError) as failure:
                await connector.get_ready_pages(job)
            assert failure.value.code is ErrorCode.SRC_CONNECTOR_FAILED
            assert failure.value.retryable is (state == "failed")
        finally:
            await connector.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("invalid_id", ["anonymous", UUID(int=0)])
def test_invalid_acquisition_does_not_reach_the_provider(
    tmp_path: Path, invalid_id: object
) -> None:
    connector = MockConnector()
    with pytest.raises(ValueError, match="UUIDv4"):
        asyncio.run(
            _service(tmp_path, connector).get_ready_pages(connector, cast(UUID, invalid_id))
        )


@pytest.mark.parametrize(
    "pages", [((0, ConnectorPageReference("p")),), ((1, ConnectorPageReference("p")),) * 2]
)
def test_snapshot_contract_rejects_invalid_ordinals(
    pages: tuple[tuple[int, ConnectorPageReference], ...],
) -> None:
    with pytest.raises(ValueError):
        ConnectorPageAvailability(uuid4(), 2, pages, False)
