from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Mapping
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

import app.persistence.filesystem_retry as filesystem_retry_module
import app.services.download_transport as download_transport_module
from app.connectors.pages import PageFetchRequest
from app.errors import ErrorCode
from app.persistence.manifest_store import TemporaryPageStore
from app.persistence.windows_paths import DataRootIdentity
from app.secrets.reference import SecretReference
from app.services.download_transport import (
    DownloadCancellationRequested,
    DownloadTransport,
    DownloadTransportError,
    DownloadTransportPolicy,
    NetworkOrigin,
)

_APPROVED_PAGE_MAX_BYTES = 64 * 1024 * 1024


def _transport_private_attribute(service: DownloadTransport, name: str) -> object:
    return getattr(service, name)


def _async_test[**P, R](function: Callable[P, Awaitable[R]]) -> Callable[P, None]:
    @wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(function(*args, **kwargs))

    return wrapper


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(
        self,
        chunks: tuple[bytes, ...],
        *,
        fail_after: int | None = None,
        wait_after: asyncio.Event | None = None,
        waiting: asyncio.Event | None = None,
    ) -> None:
        self._chunks = chunks
        self._fail_after = fail_after
        self._wait_after = wait_after
        self._waiting = waiting
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self._chunks, start=1):
            yield chunk
            if self._fail_after == index:
                raise httpx.ReadError("secret-query-must-not-be-logged")
            if self._wait_after is not None and index == len(self._chunks):
                if self._waiting is not None:
                    self._waiting.set()
                await self._wait_after.wait()

    async def aclose(self) -> None:
        self.closed = True


class _RepeatedChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunk: bytes, repeats: int, *, trailing: bytes = b"") -> None:
        self._chunk = chunk
        self._repeats = repeats
        self._trailing = trailing

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._repeats):
            yield self._chunk
        if self._trailing:
            yield self._trailing

    async def aclose(self) -> None:
        return


class _CountingSink:
    def __init__(self) -> None:
        self.bytes_written = 0

    def write(self, chunk: bytes) -> int:
        self.bytes_written += len(chunk)
        return len(chunk)

    def sync(self) -> None:
        return


def _patch_page_store_with_counting_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> _CountingSink:
    sink = _CountingSink()

    @contextmanager
    def create(
        _store: TemporaryPageStore,
        _temporary_path: str,
        *,
        job_id: UUID | None = None,
        trace_id: str | None = None,
    ) -> Generator[_CountingSink]:
        del job_id, trace_id
        yield sink

    monkeypatch.setattr(TemporaryPageStore, "create", create)
    return sink


class _Win32LockError(OSError):
    def __init__(self, code: int) -> None:
        super().__init__(f"injected Win32 error {code}")
        self.winerror = code


class _Harness:
    def __init__(
        self,
        tmp_path: Path,
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        max_bytes: int = 1024,
        chunk_size: int = 3,
        max_redirects: int = 2,
        max_retry_after_seconds: float = 4.0,
        global_concurrency: int = 2,
        per_host_concurrency: int = 1,
        additional_origins: frozenset[NetworkOrigin] = frozenset(),
        secret_header_provider: Callable[[SecretReference], Mapping[str, str]] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.data_root = tmp_path / "data"
        self.data_root.mkdir(exist_ok=True)
        self.identity = DataRootIdentity.capture(self.data_root)
        self.transport = httpx.MockTransport(handler)
        self.client = httpx.AsyncClient(transport=self.transport)
        self.delays: list[float] = []
        self.reserved_attempts: list[tuple[UUID, int, int]] = []
        self.policy = DownloadTransportPolicy(
            allowed_origins=frozenset(
                {
                    NetworkOrigin.from_url("https://cdn.example.invalid/"),
                    NetworkOrigin.from_url("https://reader.example.invalid/"),
                    NetworkOrigin.from_url("http://127.0.0.1:43127/"),
                }
                | additional_origins
            ),
            max_bytes=max_bytes,
            chunk_size=chunk_size,
            connect_timeout_seconds=2.0,
            read_timeout_seconds=3.0,
            write_timeout_seconds=4.0,
            pool_timeout_seconds=5.0,
            max_redirects=max_redirects,
            max_retry_after_seconds=max_retry_after_seconds,
            global_concurrency=global_concurrency,
            per_host_concurrency=per_host_concurrency,
        )

        async def record_sleep(delay: float) -> None:
            self.delays.append(delay)
            if sleep is not None:
                await sleep(delay)

        self.service = DownloadTransport(
            self.client,
            self.policy,
            data_root=self.data_root,
            data_root_identity=self.identity,
            test_transport=self.transport,
            secret_header_provider=secret_header_provider,
            sleep=record_sleep,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def reserve_next_attempt(self, job_id: UUID, ordinal: int, previous: int) -> int:
        self.reserved_attempts.append((job_id, ordinal, previous))
        return previous + 1


def _response(
    chunks: tuple[bytes, ...],
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
    fail_after: int | None = None,
    wait_after: asyncio.Event | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers=headers,
        stream=_ChunkStream(chunks, fail_after=fail_after, wait_after=wait_after),
    )


@_async_test
async def test_memory_transport_reuses_throttling_retries_and_never_creates_parts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return _response(
            (b"png",),
            status_code=429 if count == 1 else 200,
            headers={"Content-Type": "image/png", "Retry-After": "2"},
        )

    harness = _Harness(tmp_path, handler)

    def forbid_files(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Memory transport must not open a page file")

    monkeypatch.setattr(TemporaryPageStore, "create", forbid_files)
    try:
        result = await harness.service.fetch_memory(
            PageFetchRequest("https://cdn.example.invalid/1"),
            session_id=uuid4(),
            ordinal=1,
            allowed_origins=frozenset({NetworkOrigin.from_url("https://cdn.example.invalid")}),
        )
        assert result.body == b"png" and result.content_type == "image/png"
        assert count == 2 and harness.delays == [2.0] and harness.reserved_attempts == []
        assert not list(harness.data_root.rglob("*.part"))
    finally:
        await harness.close()


@_async_test
@pytest.mark.parametrize(
    "headers,chunks,stage",
    [
        ({"Content-Length": "33"}, (b"x",), "content_length_limit"),
        ({}, (b"x" * 33,), "byte_limit"),
        ({"Content-Encoding": "gzip"}, (b"x",), "content_encoding"),
    ],
)
async def test_memory_transport_rejects_oversized_or_encoded_images(
    tmp_path: Path,
    headers: dict[str, str],
    chunks: tuple[bytes, ...],
    stage: str,
) -> None:
    harness = _Harness(tmp_path, lambda _request: _response(chunks, headers=headers), max_bytes=32)
    try:
        with pytest.raises(DownloadTransportError) as failed:
            await harness.service.fetch_memory(
                PageFetchRequest("https://cdn.example.invalid/1"),
                session_id=uuid4(),
                ordinal=1,
                allowed_origins=frozenset({NetworkOrigin.from_url("https://cdn.example.invalid")}),
            )
        assert failed.value.stage == stage
        assert not list(harness.data_root.rglob("*.part"))
    finally:
        await harness.close()


@_async_test
async def test_temporary_inventory_yields_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_started = threading.Event()
    release_inventory = threading.Event()
    heartbeat_ran_before_release = threading.Event()
    watchdog_observation: dict[str, bool] = {}

    def blocking_inventory(_job_id: UUID) -> int:
        inventory_started.set()
        release_inventory.wait(timeout=1.0)
        return 0

    def release_after_heartbeat_or_timeout() -> None:
        if inventory_started.wait(timeout=1.0):
            watchdog_observation["heartbeat_ran"] = heartbeat_ran_before_release.wait(
                timeout=0.1
            )
        release_inventory.set()

    watchdog = threading.Thread(target=release_after_heartbeat_or_timeout, daemon=True)
    watchdog.start()
    harness = _Harness(tmp_path, lambda _: _response((b"ok",)))
    monkeypatch.setattr(
        harness.service,
        "_inventory_job_temporary_bytes",
        blocking_inventory,
    )

    async def heartbeat() -> None:
        while not inventory_started.is_set():
            await asyncio.sleep(0)
        heartbeat_ran_before_release.set()

    try:
        await asyncio.gather(
            harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/image.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
            ),
            heartbeat(),
        )
    finally:
        release_inventory.set()
        await asyncio.to_thread(watchdog.join, 1.0)
        await harness.close()

    assert watchdog_observation.get("heartbeat_ran") is True


@_async_test
async def test_download_streams_chunks_and_returns_incremental_sha256(tmp_path: Path) -> None:
    body = b"abcdefgh"
    timeouts: list[Mapping[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        response = _response(
            (b"abc", b"def", b"gh"),
            headers={"Content-Length": "8"},
        )
        with pytest.raises(httpx.ResponseNotRead):
            _ = response.content
        return response

    harness = _Harness(tmp_path, handler)
    job_id = uuid4()
    try:
        result = await harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/image.jpg"),
            job_id=job_id,
            ordinal=2,
            attempt=7,
        )
    finally:
        await harness.close()

    assert result.temporary_path == f"jobs/{job_id}/pages/2/attempt-7.part"
    assert result.path == harness.data_root / "temp" / result.temporary_path
    assert result.size_bytes == len(body)
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert result.path.read_bytes() == body
    assert timeouts == [{"connect": 2.0, "read": 3.0, "write": 4.0, "pool": 5.0}]


@_async_test
async def test_download_result_exposes_normalized_safe_content_type(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path,
        lambda _: _response(
            (b"image-bytes",),
            headers={"Content-Type": "Image/JPEG; charset=binary"},
        ),
    )
    try:
        result = await harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/image.jpg"),
            job_id=uuid4(),
            ordinal=1,
            attempt=1,
        )
    finally:
        await harness.close()

    assert result.content_type == "image/jpeg"


@_async_test
async def test_download_result_uses_safe_mime_fallback_for_ambiguous_header(
    tmp_path: Path,
) -> None:
    response = httpx.Response(
        200,
        headers=[("Content-Type", "image/png"), ("Content-Type", "text/html")],
        stream=_ChunkStream((b"opaque",)),
    )
    harness = _Harness(tmp_path, lambda _: response)
    try:
        result = await harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/image.jpg"),
            job_id=uuid4(),
            ordinal=1,
            attempt=1,
        )
    finally:
        await harness.close()

    assert result.content_type == "application/octet-stream"


@_async_test
async def test_download_uses_connector_origin_subset_for_request(tmp_path: Path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response((b"reader-page",))

    harness = _Harness(tmp_path, handler)
    connector_origins = frozenset(
        {NetworkOrigin.from_url("https://reader.example.invalid/")}
    )
    try:
        result = await harness.service.download(
            PageFetchRequest("https://reader.example.invalid/page.jpg"),
            job_id=uuid4(),
            ordinal=1,
            attempt=1,
            allowed_origins=connector_origins,
        )
    finally:
        await harness.close()

    assert calls == 1
    assert result.path.read_bytes() == b"reader-page"


@_async_test
async def test_download_rejects_global_origin_outside_connector_subset(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response((b"should-not-fetch",))

    harness = _Harness(tmp_path, handler)
    connector_origins = frozenset(
        {NetworkOrigin.from_url("https://reader.example.invalid/")}
    )
    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/page.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
                allowed_origins=connector_origins,
            )
    finally:
        await harness.close()

    assert raised.value.stage == "request_origin_not_allowed"
    assert calls == 0


@_async_test
async def test_referer_must_stay_inside_connector_origin_subset(tmp_path: Path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response((b"should-not-fetch",))

    harness = _Harness(tmp_path, handler)
    connector_origins = frozenset(
        {NetworkOrigin.from_url("https://reader.example.invalid/")}
    )
    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest(
                    "https://reader.example.invalid/page.jpg",
                    referer="https://cdn.example.invalid/chapter",
                ),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
                allowed_origins=connector_origins,
            )
    finally:
        await harness.close()

    assert raised.value.stage == "referer_origin_not_allowed"
    assert calls == 0


@_async_test
async def test_redirect_must_stay_inside_connector_origin_subset(tmp_path: Path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            302,
            headers={"Location": "https://cdn.example.invalid/page.jpg"},
        )

    harness = _Harness(tmp_path, handler)
    connector_origins = frozenset(
        {NetworkOrigin.from_url("https://reader.example.invalid/")}
    )
    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://reader.example.invalid/page.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
                allowed_origins=connector_origins,
            )
    finally:
        await harness.close()

    assert raised.value.stage == "redirect_origin_not_allowed"
    assert calls == 1


@_async_test
async def test_download_origin_allowlist_cannot_expand_transport_policy(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, lambda _: _response((b"should-not-fetch",)))
    expanded_origins = frozenset(
        {
            NetworkOrigin.from_url("https://cdn.example.invalid/"),
            NetworkOrigin.from_url("https://outside.example.invalid/"),
        }
    )
    try:
        with pytest.raises(ValueError):
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/page.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
                allowed_origins=expanded_origins,
            )
    finally:
        await harness.close()


@_async_test
async def test_job_temporary_budget_accepts_exact_aggregate_including_final_and_old_parts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download_transport_module, "MAX_JOB_TEMPORARY_BYTES", 8, raising=False)
    harness = _Harness(tmp_path, lambda _: _response((b"1",)))
    job_id = uuid4()
    pages_root = harness.data_root / "temp" / "jobs" / str(job_id) / "pages"
    previous_attempt = pages_root / "1" / "attempt-1.part"
    validated_page = pages_root / "2" / "page.webp"
    previous_attempt.parent.mkdir(parents=True)
    validated_page.parent.mkdir(parents=True)
    previous_attempt.write_bytes(b"old")
    validated_page.write_bytes(b"data")

    try:
        result = await harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/image.jpg"),
            job_id=job_id,
            ordinal=3,
            attempt=1,
        )
    finally:
        await harness.close()

    assert result.path.read_bytes() == b"1"
    assert previous_attempt.read_bytes() == b"old"
    assert validated_page.read_bytes() == b"data"
    assert sum(path.stat().st_size for path in pages_root.rglob("*") if path.is_file()) == 8


@_async_test
async def test_job_temporary_budget_rejects_first_excess_byte_before_writing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download_transport_module, "MAX_JOB_TEMPORARY_BYTES", 8, raising=False)
    harness = _Harness(
        tmp_path,
        lambda _: _response((b"5678x",)),
        chunk_size=1,
    )
    job_id = uuid4()
    previous_attempt = (
        harness.data_root
        / "temp"
        / "jobs"
        / str(job_id)
        / "pages"
        / "1"
        / "attempt-1.part"
    )
    previous_attempt.parent.mkdir(parents=True)
    previous_attempt.write_bytes(b"1234")

    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/image.jpg"),
                job_id=job_id,
                ordinal=2,
                attempt=1,
            )
    finally:
        await harness.close()

    current_attempt = (
        harness.data_root
        / "temp"
        / "jobs"
        / str(job_id)
        / "pages"
        / "2"
        / "attempt-1.part"
    )
    assert raised.value.code is ErrorCode.DL_TEMPORARY_BUDGET_EXCEEDED
    assert raised.value.stage == "temporary_budget_chunk"
    assert previous_attempt.read_bytes() == b"1234"
    assert current_attempt.read_bytes() == b"5678"


@_async_test
async def test_job_temporary_budget_preflight_counts_old_part_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download_transport_module, "MAX_JOB_TEMPORARY_BYTES", 8, raising=False)
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response((b"unused",))

    harness = _Harness(tmp_path, handler)
    job_id = uuid4()
    previous_attempt = (
        harness.data_root
        / "temp"
        / "jobs"
        / str(job_id)
        / "pages"
        / "1"
        / "attempt-1.part"
    )
    previous_attempt.parent.mkdir(parents=True)
    previous_attempt.write_bytes(b"ambiguous")

    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/image.jpg"),
                job_id=job_id,
                ordinal=2,
                attempt=1,
            )
    finally:
        await harness.close()

    assert raised.value.code is ErrorCode.DL_TEMPORARY_BUDGET_EXCEEDED
    assert raised.value.stage == "temporary_budget_preflight"
    assert calls == 0
    assert previous_attempt.read_bytes() == b"ambiguous"


@_async_test
async def test_job_temporary_budget_reuses_inventory_for_sequential_page_transfers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download_transport_module, "MAX_JOB_TEMPORARY_BYTES", 10)
    request_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_calls
        request_calls += 1
        payload = {
            "/first": b"12345",
            "/second": b"ab",
            "/third": b"z",
        }[request.url.path]
        return _response((payload,))

    harness = _Harness(tmp_path, handler, max_bytes=5, chunk_size=5)
    job_id = uuid4()
    previous_attempt = (
        harness.data_root
        / "temp"
        / "jobs"
        / str(job_id)
        / "pages"
        / "99"
        / "attempt-2.part"
    )
    previous_attempt.parent.mkdir(parents=True)
    previous_attempt.write_bytes(b"old")
    inventory_calls = 0
    original_inventory = cast(
        Callable[[UUID], int],
        _transport_private_attribute(
            harness.service,
            "_inventory_job_temporary_bytes",
        ),
    )

    def counted_inventory(target_job_id: UUID) -> int:
        nonlocal inventory_calls
        inventory_calls += 1
        return original_inventory(target_job_id)

    monkeypatch.setattr(
        harness.service,
        "_inventory_job_temporary_bytes",
        counted_inventory,
    )

    try:
        first = await harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/first"),
            job_id=job_id,
            ordinal=1,
            attempt=1,
        )
        second = await harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/second"),
            job_id=job_id,
            ordinal=2,
            attempt=1,
        )
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/third"),
                job_id=job_id,
                ordinal=3,
                attempt=1,
            )
        harness.service.finish_job_temporary_budget(job_id)
        assert job_id not in cast(
            dict[UUID, object],
            _transport_private_attribute(harness.service, "_job_budget_states"),
        )

        preserved_after_finish = (
            harness.data_root
            / "temp"
            / "jobs"
            / str(job_id)
            / "pages"
            / "98"
            / "attempt-1.part"
        )
        preserved_after_finish.parent.mkdir(parents=True)
        preserved_after_finish.write_bytes(b"x")
        with pytest.raises(DownloadTransportError) as resumed:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/first"),
                job_id=job_id,
                ordinal=4,
                attempt=1,
            )
    finally:
        harness.service.finish_job_temporary_budget(job_id)
        await harness.close()

    rejected_attempt = (
        harness.data_root
        / "temp"
        / "jobs"
        / str(job_id)
        / "pages"
        / "3"
        / "attempt-1.part"
    )
    assert first.path.read_bytes() == b"12345"
    assert second.path.read_bytes() == b"ab"
    assert previous_attempt.read_bytes() == b"old"
    assert rejected_attempt.is_file()
    assert rejected_attempt.stat().st_size == 0
    assert raised.value.code is ErrorCode.DL_TEMPORARY_BUDGET_EXCEEDED
    assert raised.value.stage == "temporary_budget_chunk"
    assert resumed.value.code is ErrorCode.DL_TEMPORARY_BUDGET_EXCEEDED
    assert resumed.value.stage == "temporary_budget_preflight"
    assert inventory_calls == 2
    assert request_calls == 3


@_async_test
async def test_ambiguous_partial_write_invalidates_job_temporary_budget_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download_transport_module, "MAX_JOB_TEMPORARY_BYTES", 5)
    harness = _Harness(
        tmp_path,
        lambda request: _response(
            (b"abcd" if request.url.path == "/first" else b"1234",)
        ),
        max_bytes=4,
        chunk_size=4,
    )
    job_id = uuid4()
    original_write = filesystem_retry_module.RetryingFileStream.write
    fail_first_write = True

    def partial_write_then_fail(
        stream: filesystem_retry_module.RetryingFileStream,
        value: object,
    ) -> int:
        nonlocal fail_first_write
        assert isinstance(value, (bytes, bytearray, memoryview))
        payload = bytes(cast(bytes | bytearray | memoryview[int], value))
        if fail_first_write:
            fail_first_write = False
            assert original_write(stream, payload[:2]) == 2
            raise OSError("injected ambiguous partial write")
        return original_write(stream, payload)

    monkeypatch.setattr(
        filesystem_retry_module.RetryingFileStream,
        "write",
        partial_write_then_fail,
    )
    inventory_calls = 0
    original_inventory = cast(
        Callable[[UUID], int],
        _transport_private_attribute(
            harness.service,
            "_inventory_job_temporary_bytes",
        ),
    )

    def counted_inventory(target_job_id: UUID) -> int:
        nonlocal inventory_calls
        inventory_calls += 1
        return original_inventory(target_job_id)

    monkeypatch.setattr(
        harness.service,
        "_inventory_job_temporary_bytes",
        counted_inventory,
    )

    try:
        with pytest.raises(DownloadTransportError):
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/first"),
                job_id=job_id,
                ordinal=1,
                attempt=1,
            )

        partial_path = (
            harness.data_root
            / "temp"
            / "jobs"
            / str(job_id)
            / "pages"
            / "1"
            / "attempt-1.part"
        )
        assert partial_path.read_bytes() == b"ab"

        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/second"),
                job_id=job_id,
                ordinal=2,
                attempt=1,
            )
    finally:
        harness.service.finish_job_temporary_budget(job_id)
        await harness.close()

    second_attempt_path = (
        harness.data_root
        / "temp"
        / "jobs"
        / str(job_id)
        / "pages"
        / "2"
        / "attempt-1.part"
    )
    assert raised.value.code is ErrorCode.DL_TEMPORARY_BUDGET_EXCEEDED
    assert raised.value.stage == "temporary_budget_chunk"
    assert second_attempt_path.stat().st_size == 0
    assert inventory_calls == 2


@_async_test
async def test_job_temporary_budget_initial_inventory_is_shared_by_concurrent_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(download_transport_module, "MAX_JOB_TEMPORARY_BYTES", 4)
    request_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_calls
        request_calls += 1
        return _response((b"xyz",))

    harness = _Harness(
        tmp_path,
        handler,
        max_bytes=3,
        chunk_size=3,
        global_concurrency=2,
        per_host_concurrency=1,
    )
    job_id = uuid4()
    inventory_started = threading.Event()
    release_inventory = threading.Event()
    inventory_calls = 0
    inventory_calls_lock = threading.Lock()
    original_inventory = cast(
        Callable[[UUID], int],
        _transport_private_attribute(
            harness.service,
            "_inventory_job_temporary_bytes",
        ),
    )
    original_preflight = cast(
        Callable[..., Awaitable[None]],
        _transport_private_attribute(
            harness.service,
            "_preflight_job_temporary_budget",
        ),
    )
    preflight_calls = 0
    both_preflights_started = asyncio.Event()

    def blocked_inventory(target_job_id: UUID) -> int:
        nonlocal inventory_calls
        with inventory_calls_lock:
            inventory_calls += 1
        inventory_started.set()
        if not release_inventory.wait(timeout=3):
            raise TimeoutError("test did not release the first inventory")
        return original_inventory(target_job_id)

    async def signal_preflight(**kwargs: object) -> None:
        nonlocal preflight_calls
        preflight_calls += 1
        if preflight_calls == 2:
            both_preflights_started.set()
        await original_preflight(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        harness.service,
        "_inventory_job_temporary_bytes",
        blocked_inventory,
    )
    monkeypatch.setattr(
        harness.service,
        "_preflight_job_temporary_budget",
        signal_preflight,
    )

    try:
        first = asyncio.create_task(
            harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/first"),
                job_id=job_id,
                ordinal=1,
                attempt=1,
            )
        )
        assert await asyncio.wait_for(
            asyncio.to_thread(inventory_started.wait, 1),
            timeout=2,
        )
        second = asyncio.create_task(
            harness.service.download(
                PageFetchRequest("https://reader.example.invalid/second"),
                job_id=job_id,
                ordinal=2,
                attempt=1,
            )
        )
        await asyncio.wait_for(both_preflights_started.wait(), timeout=2)
        release_inventory.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
    finally:
        release_inventory.set()
        harness.service.finish_job_temporary_budget(job_id)
        await harness.close()

    results = [value for value in outcomes if not isinstance(value, Exception)]
    failures = [value for value in outcomes if isinstance(value, DownloadTransportError)]
    assert len(results) == 1
    assert len(failures) == 1
    assert failures[0].stage == "temporary_budget_chunk"
    assert inventory_calls == 1
    assert request_calls == 2
    temporary_files = list(
        (
            harness.data_root
            / "temp"
            / "jobs"
            / str(job_id)
            / "pages"
        ).rglob("attempt-1.part")
    )
    assert sorted(path.stat().st_size for path in temporary_files) == [0, 3]
    assert sum(path.stat().st_size for path in temporary_files) == 3


@_async_test
async def test_job_temporary_budget_rejects_reparse_point_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response((b"unused",))

    harness = _Harness(tmp_path, handler)
    job_id = uuid4()
    pages_root = harness.data_root / "temp" / "jobs" / str(job_id) / "pages"
    pages_root.mkdir(parents=True)
    reparse = pages_root / "1"
    reparse.mkdir()
    original_lstat = Path.lstat

    def lstat_with_reparse(path: Path) -> object:
        if path == reparse:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_size=0,
                st_file_attributes=0x0400,
            )
        return original_lstat(path)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "lstat", lstat_with_reparse)
            with pytest.raises(DownloadTransportError) as raised:
                await harness.service.download(
                    PageFetchRequest("https://cdn.example.invalid/image.jpg"),
                    job_id=job_id,
                    ordinal=2,
                    attempt=1,
                )
    finally:
        await harness.close()

    assert raised.value.code is ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE
    assert raised.value.stage == "temporary_inventory_unavailable"
    assert calls == 0
    assert reparse.is_dir()


@_async_test
async def test_job_temporary_inventory_entry_count_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        download_transport_module,
        "_MAX_TEMPORARY_INVENTORY_ENTRIES",
        2,
        raising=False,
    )
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response((b"unused",))

    harness = _Harness(tmp_path, handler)
    job_id = uuid4()
    pages_root = harness.data_root / "temp" / "jobs" / str(job_id) / "pages"
    first = pages_root / "1" / "attempt-1.part"
    second = pages_root / "2" / "attempt-1.part"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/image.jpg"),
                job_id=job_id,
                ordinal=3,
                attempt=1,
            )
    finally:
        await harness.close()

    assert raised.value.code is ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE
    assert raised.value.stage == "temporary_inventory_unavailable"
    assert calls == 0


@_async_test
async def test_temporary_inventory_restarts_closed_scandir_after_transient_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, lambda _: httpx.Response(200))
    job_id = uuid4()
    pages_root = harness.data_root / "temp" / "jobs" / str(job_id) / "pages"
    chapter_directory = pages_root / "1"
    chapter_directory.mkdir(parents=True)
    first = chapter_directory / "attempt-1.part"
    second = chapter_directory / "attempt-2.part"
    first.write_bytes(b"first partial page")
    second.write_bytes(b"second page")
    expected_bytes = len(first.read_bytes()) + len(second.read_bytes())
    real_scandir = os.scandir
    scan_calls = 0
    scanner_closed: list[bool] = []
    sleeps: list[float] = []

    class LockOnceScandir:
        def __init__(self, iterator: object, *, fail_after_first: bool) -> None:
            self._iterator = iterator
            self._fail_after_first = fail_after_first
            self._raised = False
            self.closed = False

        def __enter__(self) -> LockOnceScandir:
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

        def __iter__(self) -> object:
            for entry in self._iterator:  # type: ignore[union-attr]
                yield entry
                if self._fail_after_first and not self._raised:
                    self._raised = True
                    raise _Win32LockError(32)

        def close(self) -> None:
            if not self.closed:
                self._iterator.close()  # type: ignore[attr-defined]
                self.closed = True
                scanner_closed.append(True)

    def injected_scandir(path: str | bytes | os.PathLike[str] | int) -> Any:
        nonlocal scan_calls
        iterator = real_scandir(path)
        if isinstance(path, int) or Path(os.fsdecode(path)) != pages_root:
            return iterator
        scan_calls += 1
        return LockOnceScandir(iterator, fail_after_first=scan_calls == 1)

    monkeypatch.setattr(download_transport_module.os, "scandir", injected_scandir)
    monkeypatch.setattr(filesystem_retry_module.time, "sleep", sleeps.append)
    try:
        observed_bytes = harness.service._inventory_job_temporary_bytes(job_id)  # pyright: ignore[reportPrivateUsage]
    finally:
        await harness.close()

    assert observed_bytes == expected_bytes
    assert scan_calls == 3
    assert scanner_closed == [True, True, True]
    assert sleeps == [0.1]


@_async_test
async def test_download_rejects_untrusted_origin_before_sending_request(tmp_path: Path) -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _response((b"data",))

    harness = _Harness(tmp_path, handler)
    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://attacker.example.invalid/image.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
            )
    finally:
        await harness.close()

    assert raised.value.code is ErrorCode.SRC_CONNECTOR_FAILED
    assert not called


@_async_test
async def test_http_loopback_requires_the_matching_injected_test_transport(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda _: _response((b"ok",)))
    client = httpx.AsyncClient(transport=transport)
    identity = DataRootIdentity.capture(tmp_path)
    policy = DownloadTransportPolicy(
        allowed_origins=frozenset({NetworkOrigin.from_url("http://127.0.0.1:43127/")}),
        max_bytes=8,
        chunk_size=2,
        connect_timeout_seconds=1.0,
        read_timeout_seconds=1.0,
        write_timeout_seconds=1.0,
        pool_timeout_seconds=1.0,
        max_redirects=0,
        max_retry_after_seconds=0.0,
        global_concurrency=1,
        per_host_concurrency=1,
    )
    try:
        with pytest.raises(ValueError):
            DownloadTransport(
                client,
                policy,
                data_root=tmp_path,
                data_root_identity=identity,
            )
    finally:
        await client.aclose()


@_async_test
async def test_fmd2_loopback_origin_is_allowed_without_test_transport(tmp_path: Path) -> None:
    mock_transport = httpx.MockTransport(lambda _: _response((b"ok",)))
    client = httpx.AsyncClient(transport=mock_transport)
    identity = DataRootIdentity.capture(tmp_path)
    policy = DownloadTransportPolicy(
        allowed_origins=frozenset({NetworkOrigin.from_url("http://127.0.0.1:18873/")}),
        max_bytes=8,
        chunk_size=2,
        connect_timeout_seconds=1.0,
        read_timeout_seconds=1.0,
        write_timeout_seconds=1.0,
        pool_timeout_seconds=1.0,
        max_redirects=0,
        max_retry_after_seconds=0.0,
        global_concurrency=1,
        per_host_concurrency=1,
    )
    service = DownloadTransport(
        client,
        policy,
        data_root=tmp_path,
        data_root_identity=identity,
    )
    try:
        result = await service.download(
            PageFetchRequest("http://127.0.0.1:18873/api/v1/pages/test"),
            job_id=uuid4(),
            ordinal=1,
            attempt=1,
        )
    finally:
        await client.aclose()

    assert result.path.read_bytes() == b"ok"


@_async_test
async def test_loopback_http_works_through_injected_test_transport(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, lambda _: _response((b"fixture",)))
    try:
        result = await harness.service.download(
            PageFetchRequest("http://127.0.0.1:43127/page.jpg"),
            job_id=uuid4(),
            ordinal=1,
            attempt=1,
        )
    finally:
        await harness.close()

    assert result.path.read_bytes() == b"fixture"


@_async_test
async def test_redirect_is_checked_against_allowlist_before_following(tmp_path: Path) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://attacker.example.invalid/x"})

    harness = _Harness(tmp_path, handler)
    try:
        with pytest.raises(DownloadTransportError):
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/image.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
            )
    finally:
        await harness.close()

    assert requested == ["https://cdn.example.invalid/image.jpg"]


@_async_test
async def test_redirect_to_trusted_origin_does_not_forward_referer_or_secret_headers(
    tmp_path: Path,
) -> None:
    observed: list[tuple[str, str | None, str | None]] = []
    secret_reference = SecretReference.generate()

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(
            (
                request.url.host,
                request.headers.get("referer"),
                request.headers.get("authorization"),
            )
        )
        if request.url.host == "cdn.example.invalid":
            return httpx.Response(
                302,
                headers={"Location": "https://reader.example.invalid/image.jpg"},
            )
        return _response((b"body",))

    harness = _Harness(
        tmp_path,
        handler,
        secret_header_provider=lambda _: {"Authorization": "Bearer secret"},
    )
    job_id = uuid4()
    try:
        await harness.service.download(
            PageFetchRequest(
                "https://cdn.example.invalid/image.jpg?token=hidden",
                referer="https://reader.example.invalid/chapter/1?secret=hidden",
                secret_reference=secret_reference,
            ),
            job_id=job_id,
            ordinal=1,
            attempt=1,
        )
    finally:
        await harness.close()

    assert observed[0] == (
        "cdn.example.invalid",
        "https://reader.example.invalid/chapter/1?secret=hidden",
        "Bearer secret",
    )
    assert observed[1] == ("reader.example.invalid", None, None)


@_async_test
async def test_missing_secret_provider_rejects_authenticated_request_without_http(
    tmp_path: Path,
) -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _response((b"ok",))

    harness = _Harness(tmp_path, handler)
    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest(
                    "https://cdn.example.invalid/image.jpg",
                    secret_reference=SecretReference.generate(),
                ),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
            )
    finally:
        await harness.close()

    assert raised.value.code is ErrorCode.AUTH_SECRET_UNAVAILABLE
    assert not called


@_async_test
async def test_retries_use_fixed_1_3_8_schedule_and_new_durable_attempt_paths(
    tmp_path: Path,
) -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            return httpx.Response(503)
        return _response((b"done",))

    harness = _Harness(tmp_path, handler)
    job_id = uuid4()
    try:
        result = await harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/image.jpg"),
            job_id=job_id,
            ordinal=4,
            attempt=10,
            reserve_next_attempt=harness.reserve_next_attempt,
        )
    finally:
        await harness.close()

    assert harness.delays == [1.0, 3.0, 8.0]
    assert harness.reserved_attempts == [
        (job_id, 4, 10),
        (job_id, 4, 11),
        (job_id, 4, 12),
    ]
    assert result.temporary_path.endswith("attempt-13.part")
    assert result.path.read_bytes() == b"done"


@_async_test
async def test_retry_after_is_bounded_by_explicit_policy(tmp_path: Path) -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "900"})
        return _response((b"ok",))

    harness = _Harness(tmp_path, handler, max_retry_after_seconds=2.5)
    try:
        await harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/image.jpg"),
            job_id=uuid4(),
            ordinal=1,
            attempt=1,
            reserve_next_attempt=harness.reserve_next_attempt,
        )
    finally:
        await harness.close()

    assert harness.delays == [2.5]


@pytest.mark.parametrize("status_code", [403, 404])
@_async_test
async def test_forbidden_and_not_found_are_not_retried(
    tmp_path: Path, status_code: int
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code)

    harness = _Harness(tmp_path, handler)
    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/image.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
                reserve_next_attempt=harness.reserve_next_attempt,
            )
    finally:
        await harness.close()

    assert calls == 1
    assert harness.delays == []
    assert not raised.value.retryable
    assert raised.value.status_code == status_code
    assert raised.value.attempt == 1


@_async_test
async def test_retry_requires_durable_attempt_reservation_callback(tmp_path: Path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    harness = _Harness(tmp_path, handler)
    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/image.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
            )
    finally:
        await harness.close()

    assert calls == 1
    assert raised.value.code is ErrorCode.DL_JOB_RECOVERY_BLOCKED
    assert raised.value.stage == "attempt_reservation_required"


@_async_test
async def test_retry_does_not_reuse_or_truncate_ambiguous_part(tmp_path: Path) -> None:
    streams: list[_ChunkStream] = []
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            stream = _ChunkStream((b"par", b"tial"), fail_after=1)
        else:
            stream = _ChunkStream((b"complete",))
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    harness = _Harness(tmp_path, handler)
    job_id = uuid4()
    try:
        result = await harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/image.jpg?secret=query"),
            job_id=job_id,
            ordinal=3,
            attempt=1,
            reserve_next_attempt=harness.reserve_next_attempt,
        )
    finally:
        await harness.close()

    old_part = harness.data_root / "temp" / f"jobs/{job_id}/pages/3/attempt-1.part"
    assert old_part.read_bytes() == b"par"
    assert result.path.read_bytes() == b"complete"
    assert result.path != old_part
    assert streams[0].closed
    assert result.temporary_path.endswith("attempt-2.part")


@_async_test
async def test_declared_length_over_limit_is_rejected_before_creating_part(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path,
        lambda _: _response((b"not-read",), headers={"Content-Length": "1025"}),
        max_bytes=1024,
    )
    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/image.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
            )
    finally:
        await harness.close()

    assert raised.value.stage == "content_length_limit"
    assert not list((harness.data_root / "temp").rglob("*.part"))


@_async_test
async def test_actual_stream_over_limit_preserves_only_bytes_already_written(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, lambda _: _response((b"123", b"456")), max_bytes=5)
    job_id = uuid4()
    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/image.jpg"),
                job_id=job_id,
                ordinal=1,
                attempt=1,
            )
    finally:
        await harness.close()

    path = harness.data_root / "temp" / f"jobs/{job_id}/pages/1/attempt-1.part"
    assert raised.value.stage == "byte_limit"
    assert path.read_bytes() == b"123"


@_async_test
async def test_approved_page_byte_limit_accepts_exact_chunked_stream_without_large_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = b"x" * (64 * 1024)
    sink = _patch_page_store_with_counting_sink(monkeypatch)
    harness = _Harness(
        tmp_path,
        lambda _: httpx.Response(
            200,
            headers={"Content-Length": str(_APPROVED_PAGE_MAX_BYTES)},
            stream=_RepeatedChunkStream(chunk, _APPROVED_PAGE_MAX_BYTES // len(chunk)),
        ),
        max_bytes=_APPROVED_PAGE_MAX_BYTES,
        chunk_size=len(chunk),
    )

    try:
        result = await harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/exact-limit.jpg"),
            job_id=uuid4(),
            ordinal=1,
            attempt=1,
        )
    finally:
        await harness.close()

    assert result.size_bytes == _APPROVED_PAGE_MAX_BYTES
    assert sink.bytes_written == _APPROVED_PAGE_MAX_BYTES


@_async_test
async def test_approved_page_byte_limit_rejects_content_length_plus_one_before_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = _patch_page_store_with_counting_sink(monkeypatch)
    harness = _Harness(
        tmp_path,
        lambda _: _response(
            (),
            headers={"Content-Length": str(_APPROVED_PAGE_MAX_BYTES + 1)},
        ),
        max_bytes=_APPROVED_PAGE_MAX_BYTES,
    )
    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/over-limit.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
            )
    finally:
        await harness.close()

    assert raised.value.stage == "content_length_limit"
    assert sink.bytes_written == 0


@_async_test
async def test_approved_page_byte_limit_rejects_chunked_stream_plus_one_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = b"x" * (64 * 1024)
    sink = _patch_page_store_with_counting_sink(monkeypatch)
    harness = _Harness(
        tmp_path,
        lambda _: httpx.Response(
            200,
            stream=_RepeatedChunkStream(
                chunk,
                _APPROVED_PAGE_MAX_BYTES // len(chunk),
                trailing=b"x",
            ),
        ),
        max_bytes=_APPROVED_PAGE_MAX_BYTES,
        chunk_size=len(chunk),
    )
    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/chunked-over-limit.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
            )
    finally:
        await harness.close()

    assert raised.value.stage == "byte_limit"
    assert sink.bytes_written == _APPROVED_PAGE_MAX_BYTES


@_async_test
async def test_disk_flush_failure_preserves_part_and_is_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path, lambda _: _response((b"written-before-disk-full",)))
    job_id = uuid4()

    def fail_fsync(_: int) -> None:
        raise OSError(28, "disk full")

    monkeypatch.setattr("app.services.download_transport.os.fsync", fail_fsync)
    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/image.jpg"),
                job_id=job_id,
                ordinal=1,
                attempt=1,
                reserve_next_attempt=harness.reserve_next_attempt,
            )
    finally:
        await harness.close()

    path = harness.data_root / "temp" / f"jobs/{job_id}/pages/1/attempt-1.part"
    assert raised.value.code is ErrorCode.SYS_DISK_BUDGET_EXCEEDED
    assert not raised.value.retryable
    assert path.read_bytes() == b"written-before-disk-full"
    assert harness.reserved_attempts == []


@_async_test
async def test_download_transport_retries_proven_lock_without_changing_page_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"download-page"
    harness = _Harness(
        tmp_path,
        lambda _: _response(
            (body[:3], body[3:6], body[6:]),
            headers={"Content-Length": str(len(body))},
        ),
    )
    job_id = uuid4()
    relative = f"jobs/{job_id}/pages/1/attempt-1.part"
    target = harness.data_root / "temp" / relative
    real_open = Path.open
    write_failures = 0
    sleeps: list[float] = []

    class LockOnceStream:
        def __init__(self, stream: object) -> None:
            self._stream = stream
            self._failed = False

        def write(self, value: bytes) -> int:
            nonlocal write_failures
            if not self._failed:
                self._failed = True
                write_failures += 1
                raise _Win32LockError(32)
            return self._stream.write(value)  # type: ignore[attr-defined]

        def __getattr__(self, name: str) -> object:
            return getattr(self._stream, name)

    def patched_open(
        self: Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        stream = real_open(self, mode, *args, **kwargs)
        if self == target and mode == "x+b":
            return LockOnceStream(stream)
        return stream

    monkeypatch.setattr(Path, "open", patched_open)
    monkeypatch.setattr(filesystem_retry_module.time, "sleep", sleeps.append)
    try:
        result = await harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/image.jpg"),
            job_id=job_id,
            ordinal=1,
            attempt=1,
        )
    finally:
        await harness.close()

    assert write_failures == 1
    assert result.path.read_bytes() == body
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert sleeps == [0.1]
    assert harness.reserved_attempts == []


@_async_test
async def test_content_length_mismatch_preserves_partial_and_retries_to_new_part(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _response((b"short",), headers={"Content-Length": "10"})
        return _response((b"complete",), headers={"Content-Length": "8"})

    harness = _Harness(tmp_path, handler)
    job_id = uuid4()
    try:
        result = await harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/image.jpg"),
            job_id=job_id,
            ordinal=1,
            attempt=1,
            reserve_next_attempt=harness.reserve_next_attempt,
        )
    finally:
        await harness.close()

    previous = harness.data_root / "temp" / f"jobs/{job_id}/pages/1/attempt-1.part"
    assert previous.read_bytes() == b"short"
    assert result.path.read_bytes() == b"complete"
    assert result.temporary_path.endswith("attempt-2.part")


@_async_test
async def test_existing_part_is_preserved_and_never_truncated(tmp_path: Path) -> None:
    job_id = uuid4()
    relative = f"jobs/{job_id}/pages/1/attempt-1.part"
    (tmp_path / "data").mkdir()
    store = TemporaryPageStore(
        tmp_path / "data",
        data_root_identity=DataRootIdentity.capture(tmp_path / "data"),
    )
    with store.create(relative) as stream:
        stream.write(b"preexisting-evidence")

    harness = _Harness(tmp_path, lambda _: _response((b"replacement",)))
    harness.data_root.mkdir(exist_ok=True)
    try:
        # The harness and precreated part must share the same captured root.
        assert harness.data_root == tmp_path / "data"
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/image.jpg"),
                job_id=job_id,
                ordinal=1,
                attempt=1,
            )
    finally:
        await harness.close()

    assert raised.value.code is ErrorCode.DL_TEMPORARY_PATH_CONFLICT
    assert (harness.data_root / "temp" / relative).read_bytes() == b"preexisting-evidence"


@_async_test
async def test_cancellation_closes_stream_and_preserves_partial_file(tmp_path: Path) -> None:
    wait = asyncio.Event()
    waiting = asyncio.Event()
    stream: _ChunkStream | None = None

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal stream
        stream = _ChunkStream((b"cancelled!!!",), wait_after=wait, waiting=waiting)
        return httpx.Response(200, stream=stream)

    harness = _Harness(tmp_path, handler)
    job_id = uuid4()
    task = asyncio.create_task(
        harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/image.jpg"),
            job_id=job_id,
            ordinal=2,
            attempt=1,
            reserve_next_attempt=harness.reserve_next_attempt,
        )
    )
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await harness.close()

    path = harness.data_root / "temp" / f"jobs/{job_id}/pages/2/attempt-1.part"
    assert path.read_bytes() == b"cancelled!!!"
    assert stream is not None and stream.closed
    assert harness.reserved_attempts == []


@_async_test
async def test_retry_wait_accepts_generic_awaitable_with_cancellation(
    tmp_path: Path,
) -> None:
    sleep_started = asyncio.Event()
    pending_sleep: asyncio.Future[None] | None = None
    response_count = 0

    def wait_for_retry(_delay: float) -> Awaitable[None]:
        nonlocal pending_sleep
        pending_sleep = asyncio.get_running_loop().create_future()
        sleep_started.set()
        return pending_sleep

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal response_count
        response_count += 1
        if response_count == 1:
            return httpx.Response(503, content=b"retry")
        return _response((b"ok",))

    harness = _Harness(tmp_path, handler)
    harness.service = DownloadTransport(
        harness.client,
        harness.policy,
        data_root=harness.data_root,
        data_root_identity=harness.identity,
        test_transport=harness.transport,
        sleep=wait_for_retry,
    )
    cancellation_event = asyncio.Event()
    task = asyncio.create_task(
        harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/image.jpg"),
            job_id=uuid4(),
            ordinal=1,
            attempt=1,
            cancellation_event=cancellation_event,
            reserve_next_attempt=harness.reserve_next_attempt,
        )
    )
    try:
        await asyncio.wait_for(sleep_started.wait(), timeout=2)
        assert pending_sleep is not None
        pending_sleep.set_result(None)
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        if pending_sleep is not None and not pending_sleep.done():
            pending_sleep.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await harness.close()

    assert response_count == 2
    assert result.size_bytes == 2


@_async_test
async def test_download_rejects_invalid_cancellation_event_at_runtime(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, lambda _: _response((b"ok",)))
    try:
        with pytest.raises(TypeError, match="cancellation_event must be"):
            await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/image.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
                cancellation_event=cast(asyncio.Event, object()),
            )
    finally:
        await harness.close()


@_async_test
async def test_durable_cancellation_during_body_closes_stream_and_preserves_part(
    tmp_path: Path,
) -> None:
    body_waiting = asyncio.Event()
    release_body = asyncio.Event()
    stream = _ChunkStream(
        (b"verified-prefix",),
        wait_after=release_body,
        waiting=body_waiting,
    )
    harness = _Harness(tmp_path, lambda _: httpx.Response(200, stream=stream))
    job_id = uuid4()
    cancellation_event = asyncio.Event()
    task = asyncio.create_task(
        harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/body.jpg"),
            job_id=job_id,
            ordinal=1,
            attempt=1,
            cancellation_event=cancellation_event,
            reserve_next_attempt=harness.reserve_next_attempt,
        )
    )
    try:
        await asyncio.wait_for(body_waiting.wait(), timeout=2)
        cancellation_event.set()
        with pytest.raises(DownloadCancellationRequested):
            await asyncio.wait_for(task, timeout=1)
    finally:
        release_body.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await harness.close()

    part = harness.data_root / "temp" / f"jobs/{job_id}/pages/1/attempt-1.part"
    assert part.read_bytes() == b"verified-prefix"
    assert stream.closed
    assert harness.reserved_attempts == []
    global_semaphore = harness.service._global_semaphore  # pyright: ignore[reportPrivateUsage]
    await asyncio.wait_for(global_semaphore.acquire(), timeout=1)
    global_semaphore.release()
    host_semaphore = harness.service._host_semaphores["cdn.example.invalid"]  # pyright: ignore[reportPrivateUsage]
    await asyncio.wait_for(host_semaphore.acquire(), timeout=1)
    host_semaphore.release()


@_async_test
async def test_durable_cancellation_while_waiting_for_semaphore_sends_no_new_get(
    tmp_path: Path,
) -> None:
    first_request_started = asyncio.Event()
    release_first_headers = asyncio.Event()
    requested_urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.path == "/first.jpg":
            first_request_started.set()
            await release_first_headers.wait()
        return _response((b"ok",))

    harness = _Harness(
        tmp_path,
        cast(Callable[[httpx.Request], httpx.Response], handler),
        global_concurrency=2,
        per_host_concurrency=1,
    )
    cancellation_event = asyncio.Event()
    first_task = asyncio.create_task(
        harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/first.jpg"),
            job_id=uuid4(),
            ordinal=1,
            attempt=1,
        )
    )
    second_task: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(first_request_started.wait(), timeout=2)
        second_task = asyncio.create_task(
            harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/second.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
                cancellation_event=cancellation_event,
            )
        )
        semaphore = harness.service._host_semaphores["cdn.example.invalid"]  # pyright: ignore[reportPrivateUsage]
        async def wait_for_host_waiter() -> None:
            while not semaphore._waiters:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_for_host_waiter(), timeout=2.0)
        assert semaphore._waiters

        cancellation_event.set()
        release_first_headers.set()
        first_result, second_result = await asyncio.gather(
            first_task, second_task, return_exceptions=True
        )
    finally:
        release_first_headers.set()
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first_task, second_task) if task is not None),
            return_exceptions=True,
        )
        await harness.close()

    assert not isinstance(first_result, BaseException)
    assert isinstance(second_result, DownloadCancellationRequested)
    assert requested_urls == ["https://cdn.example.invalid/first.jpg"]


@_async_test
async def test_durable_cancellation_interrupts_request_waiting_for_headers(
    tmp_path: Path,
) -> None:
    request_started = asyncio.Event()
    request_interrupted = asyncio.Event()
    never_release_headers = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        request_started.set()
        try:
            await never_release_headers.wait()
        except asyncio.CancelledError:
            request_interrupted.set()
            raise
        return _response((b"should-not-be-read",))

    harness = _Harness(
        tmp_path,
        cast(Callable[[httpx.Request], httpx.Response], handler),
    )
    job_id = uuid4()
    cancellation_event = asyncio.Event()
    task = asyncio.create_task(
        harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/waiting.jpg"),
            job_id=job_id,
            ordinal=1,
            attempt=1,
            cancellation_event=cancellation_event,
        )
    )
    try:
        await asyncio.wait_for(request_started.wait(), timeout=2)
        cancellation_event.set()
        with pytest.raises(DownloadCancellationRequested):
            await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await harness.close()

    assert request_interrupted.is_set()
    assert not (harness.data_root / "temp" / f"jobs/{job_id}/pages/1/attempt-1.part").exists()


@_async_test
async def test_per_host_concurrency_is_shared_across_ports(tmp_path: Path) -> None:
    active = 0
    maximum_active = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return _response((b"ok",))

    harness = _Harness(
        tmp_path,
        cast(Callable[[httpx.Request], httpx.Response], handler),
        additional_origins=frozenset({NetworkOrigin("https", "cdn.example.invalid", 444)}),
    )
    try:
        results = await asyncio.gather(
            harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/page.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
            ),
            harness.service.download(
                PageFetchRequest("https://cdn.example.invalid:444/page.jpg"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
            ),
        )
    finally:
        await harness.close()

    assert len(results) == 2
    assert maximum_active == 1


@_async_test
async def test_approved_two_page_concurrency_respects_global_and_host_limits(
    tmp_path: Path,
) -> None:
    active = 0
    maximum_active = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return _response((b"ok",))

    harness = _Harness(
        tmp_path,
        cast(Callable[[httpx.Request], httpx.Response], handler),
        global_concurrency=2,
        per_host_concurrency=2,
    )
    try:
        results = await asyncio.gather(
            *(
                harness.service.download(
                    PageFetchRequest(f"https://cdn.example.invalid/page-{ordinal}.jpg"),
                    job_id=uuid4(),
                    ordinal=1,
                    attempt=1,
                )
                for ordinal in range(3)
            )
        )
    finally:
        await harness.close()

    assert len(results) == 3
    assert maximum_active == 2


@_async_test
async def test_http_error_does_not_leak_url_or_secret_in_error_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    harness = _Harness(tmp_path, lambda _: httpx.Response(403))
    private_url = "https://cdn.example.invalid/image.jpg?token=do-not-log"
    try:
        with pytest.raises(DownloadTransportError) as raised:
            await harness.service.download(
                PageFetchRequest(private_url),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
            )
    finally:
        await harness.close()

    rendered = str(raised.value) + caplog.text
    assert "do-not-log" not in rendered
    assert "cdn.example.invalid" not in rendered


def test_transport_policy_requires_explicit_positive_resource_limits() -> None:
    with pytest.raises(ValueError):
        DownloadTransportPolicy(
            allowed_origins=frozenset(),
            max_bytes=0,
            chunk_size=0,
            connect_timeout_seconds=0.0,
            read_timeout_seconds=0.0,
            write_timeout_seconds=0.0,
            pool_timeout_seconds=0.0,
            max_redirects=-1,
            max_retry_after_seconds=-1.0,
            global_concurrency=0,
            per_host_concurrency=0,
        )


def test_retry_attempt_allocator_must_advance_durable_counter(tmp_path: Path) -> None:
    async def scenario(tmp_path: Path) -> None:
        harness = _Harness(tmp_path, lambda _: httpx.Response(503))
        try:
            with pytest.raises(DownloadTransportError) as raised:
                await harness.service.download(
                    PageFetchRequest("https://cdn.example.invalid/image.jpg"),
                    job_id=uuid4(),
                    ordinal=1,
                    attempt=1,
                    reserve_next_attempt=lambda _job, _ordinal, previous: asyncio.sleep(
                        0, result=previous
                    ),
                )
        finally:
            await harness.close()
        assert raised.value.code is ErrorCode.DL_JOB_RECOVERY_BLOCKED
        assert raised.value.stage == "attempt_reservation_sequence"

    asyncio.run(scenario(tmp_path))


@pytest.mark.parametrize(
    ("redirect_count", "expected_stage", "expected_requests"),
    [(5, None, 6), (6, "redirect_limit", 6)],
)
@_async_test
async def test_approved_redirect_limit_accepts_five_and_rejects_six(
    tmp_path: Path,
    redirect_count: int,
    expected_stage: str | None,
    expected_requests: int,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        hop = int(request.url.params["hop"])
        if hop < redirect_count:
            return httpx.Response(302, headers={"Location": f"/page?hop={hop + 1}"})
        return _response((b"ok",))

    harness = _Harness(tmp_path, handler, max_redirects=5)
    try:
        if expected_stage is None:
            result = await harness.service.download(
                PageFetchRequest("https://cdn.example.invalid/page?hop=0"),
                job_id=uuid4(),
                ordinal=1,
                attempt=1,
            )
            assert result.size_bytes == 2
        else:
            with pytest.raises(DownloadTransportError) as raised:
                await harness.service.download(
                    PageFetchRequest("https://cdn.example.invalid/page?hop=0"),
                    job_id=uuid4(),
                    ordinal=1,
                    attempt=1,
                )
            assert raised.value.stage == expected_stage
    finally:
        await harness.close()

    assert calls == expected_requests


@pytest.mark.parametrize(
    ("retry_after", "expected_delay"),
    [
        ("60", 60.0),
        ("600", 60.0),
        ("not-an-http-date", 1.0),
        ("Sun, 06 Nov 1994 08:49:37 GMT", 0.0),
    ],
)
@_async_test
async def test_approved_retry_after_cap_handles_exact_excess_malformed_and_past(
    tmp_path: Path,
    retry_after: str,
    expected_delay: float,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return _response((b"ok",))

    harness = _Harness(tmp_path, handler, max_retry_after_seconds=60.0)
    try:
        await harness.service.download(
            PageFetchRequest("https://cdn.example.invalid/retry-after.jpg"),
            job_id=uuid4(),
            ordinal=1,
            attempt=1,
            reserve_next_attempt=harness.reserve_next_attempt,
        )
    finally:
        await harness.close()

    assert harness.delays == [expected_delay]
