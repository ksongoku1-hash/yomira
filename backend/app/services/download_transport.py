"""Bounded HTTP streaming for page bytes into exclusive ``.part`` files."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import math
import os
import re
import stat
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import NoReturn
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import httpx

from app.connectors.pages import PageFetchRequest
from app.domain.download_page import validate_temporary_path
from app.domain.job_manifest import MAX_MANIFEST_PAGE_COUNT
from app.errors import ErrorCode
from app.persistence.disk_budget import PAGE_TEMP_RESERVATION_BYTES, DiskBudgetManager
from app.persistence.filesystem_retry import (
    RetryingFileStream,
    retry_filesystem_operation,
    retry_lstat,
)
from app.persistence.manifest_store import ManifestStoreError, TemporaryPageStore
from app.persistence.storage_gate import (
    SharedStorageGate,
    is_disk_full_os_error,
)
from app.persistence.windows_paths import DataRootIdentity, DataRootIdentityError
from app.secrets.reference import SecretReference
from app.tracing import create_trace_id, current_trace_id

logger = logging.getLogger("yomira.download_transport")

_RETRY_DELAYS_SECONDS = (1.0, 3.0, 8.0)
_RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
MAX_JOB_TEMPORARY_BYTES = PAGE_TEMP_RESERVATION_BYTES
_MAX_TEMPORARY_INVENTORY_ENTRIES = MAX_MANIFEST_PAGE_COUNT * (len(_RETRY_DELAYS_SECONDS) + 3)
_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_MAX_TEMPORARY_PATH_UTF16_UNITS = 259
# Approved local engines serve page bytes from these exact loopback origins.
_PRODUCTION_HTTP_LOOPBACK_ORIGINS = frozenset({("127.0.0.1", 4567), ("127.0.0.1", 18873)})
_SAFE_MEDIA_TYPE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+/[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_FORBIDDEN_SECRET_HEADERS = frozenset(
    {"connection", "content-length", "host", "proxy-authorization", "referer", "transfer-encoding"}
)


@dataclass(frozen=True, slots=True)
class NetworkOrigin:
    """An exact HTTP origin: normalized scheme, host, and effective port."""

    scheme: str
    host: str
    port: int

    def __post_init__(self) -> None:
        if type(self.scheme) is not str or self.scheme.lower() not in {"http", "https"}:
            raise ValueError("origin scheme must be HTTP or HTTPS")
        if type(self.host) is not str or not self.host or any(char.isspace() for char in self.host):
            raise ValueError("origin host must be non-empty")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("origin port must be between 1 and 65535")
        normalized_host = self.host.lower().rstrip(".")
        if not normalized_host:
            raise ValueError("origin host must be non-empty")
        object.__setattr__(self, "scheme", self.scheme.lower())
        object.__setattr__(self, "host", normalized_host)

    @classmethod
    def from_url(cls, value: str) -> NetworkOrigin:
        if type(value) is not str:
            raise TypeError("origin URL must be a string")
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            raise ValueError("origin URL must be a valid HTTP(S) URL") from None
        scheme = parsed.scheme.lower()
        if (
            scheme not in {"http", "https"}
            or not parsed.netloc
            or hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or "@" in parsed.netloc
        ):
            raise ValueError("origin URL must be a credential-free HTTP(S) URL")
        effective_port = port if port is not None else (443 if scheme == "https" else 80)
        return cls(scheme, hostname, effective_port)

    @property
    def is_loopback(self) -> bool:
        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False


@dataclass(frozen=True, slots=True)
class DownloadTransportPolicy:
    """All operational limits are supplied by the application configuration."""

    allowed_origins: frozenset[NetworkOrigin]
    max_bytes: int
    chunk_size: int
    connect_timeout_seconds: float
    read_timeout_seconds: float
    write_timeout_seconds: float
    pool_timeout_seconds: float
    max_redirects: int
    max_retry_after_seconds: float
    global_concurrency: int
    per_host_concurrency: int

    def __post_init__(self) -> None:
        if type(self.allowed_origins) is not frozenset or not self.allowed_origins:
            raise ValueError("allowed_origins must be a non-empty frozenset")
        if any(type(origin) is not NetworkOrigin for origin in self.allowed_origins):
            raise TypeError("allowed_origins must contain NetworkOrigin values")
        for name in ("max_bytes", "chunk_size", "global_concurrency", "per_host_concurrency"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.max_redirects) is not int or self.max_redirects < 0:
            raise ValueError("max_redirects must be a non-negative integer")
        for name in (
            "connect_timeout_seconds",
            "read_timeout_seconds",
            "write_timeout_seconds",
            "pool_timeout_seconds",
        ):
            value = getattr(self, name)
            if type(value) not in {float, int} or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if (
            type(self.max_retry_after_seconds) not in {float, int}
            or not math.isfinite(self.max_retry_after_seconds)
            or self.max_retry_after_seconds < 0
        ):
            raise ValueError("max_retry_after_seconds must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Verified transport metadata for a closed, still-unvalidated ``.part`` file."""

    job_id: UUID
    ordinal: int
    attempt: int
    temporary_path: str
    path: Path
    size_bytes: int
    sha256: str
    content_type: str


@dataclass(frozen=True, slots=True)
class MemoryPageResult:
    body: bytes
    content_type: str


@dataclass(slots=True)
class _JobTemporaryBudgetState:
    total_bytes: int | None = None
    active_downloads: int = 0
    retire_when_idle: bool = False
    inventory_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DownloadTransportError(RuntimeError):
    """Sanitized, traceable transport failure; contains no URL or response body."""

    def __init__(
        self,
        *,
        code: ErrorCode,
        stage: str,
        trace_id: str,
        retryable: bool,
        attempt: int,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.stage = stage
        self.trace_id = trace_id
        self.retryable = retryable
        self.attempt = attempt
        self.status_code = status_code
        self.cause_type: str | None = None
        super().__init__(f"Page download failed during {stage}.")


class DownloadCancellationRequested(Exception):
    """A durable job cancellation closed the current response stream safely."""


class _RetryableAttemptFailure(Exception):
    def __init__(
        self,
        *,
        stage: str,
        status_code: int | None = None,
        retry_after: str | None = None,
        cause_type: str | None = None,
    ) -> None:
        self.stage = stage
        self.status_code = status_code
        self.retry_after = retry_after
        self.cause_type = cause_type


AttemptReservation = Callable[[UUID, int, int], Awaitable[int]]
SecretHeaderProvider = Callable[[SecretReference], Mapping[str, str]]
Sleep = Callable[[float], Awaitable[None]]


class DownloadTransport:
    """Download one page with explicit policy, shared HTTPX client, and safe staging."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        policy: DownloadTransportPolicy,
        *,
        data_root: Path,
        data_root_identity: DataRootIdentity,
        test_transport: httpx.AsyncBaseTransport | None = None,
        secret_header_provider: SecretHeaderProvider | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        storage_gate: SharedStorageGate | None = None,
        disk_budget_manager: DiskBudgetManager | None = None,
    ) -> None:
        if type(client) is not httpx.AsyncClient:
            raise TypeError("client must be a shared httpx.AsyncClient")
        if type(policy) is not DownloadTransportPolicy:
            raise TypeError("policy must be a DownloadTransportPolicy")
        if type(data_root_identity) is not DataRootIdentity:
            raise TypeError("data_root_identity must be a DataRootIdentity")
        if test_transport is not None and getattr(client, "_transport", None) is not test_transport:
            raise ValueError("test_transport must be the transport injected into AsyncClient")
        for origin in policy.allowed_origins:
            if origin.scheme == "http" and (
                not origin.is_loopback
                or (
                    test_transport is None
                    and (origin.host, origin.port) not in _PRODUCTION_HTTP_LOOPBACK_ORIGINS
                )
            ):
                raise ValueError(
                    "HTTP is permitted only for an approved local acquisition origin "
                    "or with an injected test transport"
                )

        self._client = client
        self._policy = policy
        self._data_root = data_root_identity.canonical_path
        if storage_gate is not None and type(storage_gate) is not SharedStorageGate:
            raise TypeError("storage_gate must be a SharedStorageGate or None")
        self._storage_gate = storage_gate if storage_gate is not None else SharedStorageGate()
        self._page_store = TemporaryPageStore(
            data_root,
            data_root_identity=data_root_identity,
            storage_gate=self._storage_gate,
        )
        if disk_budget_manager is not None and type(disk_budget_manager) is not DiskBudgetManager:
            raise TypeError("disk_budget_manager must be a DiskBudgetManager or None")
        self._disk_budget_manager = disk_budget_manager
        self._test_transport = test_transport
        self._secret_header_provider = secret_header_provider
        self._sleep = sleep
        self._clock = clock
        self._host_cooldowns: dict[str, float] = {}
        self._timeouts = httpx.Timeout(
            connect=policy.connect_timeout_seconds,
            read=policy.read_timeout_seconds,
            write=policy.write_timeout_seconds,
            pool=policy.pool_timeout_seconds,
        )
        self._global_semaphore = asyncio.Semaphore(policy.global_concurrency)
        self._host_semaphores = {
            host: asyncio.Semaphore(policy.per_host_concurrency)
            for host in {origin.host for origin in policy.allowed_origins}
        }
        self._job_budget_registry_lock = threading.Lock()
        self._job_budget_states: dict[UUID, _JobTemporaryBudgetState] = {}

    async def download(
        self,
        request: PageFetchRequest,
        *,
        job_id: UUID,
        ordinal: int,
        attempt: int,
        reserve_next_attempt: AttemptReservation | None = None,
        allowed_origins: frozenset[NetworkOrigin] | None = None,
        cancellation_event: object | None = None,
    ) -> DownloadResult:
        """Stream one page; every retry requires a durable new attempt reservation."""
        self._storage_gate.ensure_open()
        if type(request) is not PageFetchRequest:
            raise TypeError("request must be a PageFetchRequest")
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        if type(ordinal) is not int or ordinal <= 0:
            raise ValueError("ordinal must be a positive integer")
        if type(attempt) is not int or attempt <= 0:
            raise ValueError("attempt must be a positive integer")
        if cancellation_event is not None and not isinstance(cancellation_event, asyncio.Event):
            raise TypeError("cancellation_event must be an asyncio.Event or None")

        connector_origins = self._connector_origins(allowed_origins)
        budget_state = self._acquire_job_budget_state(job_id)
        try:
            result = await self._download_registered(
                request,
                job_id=job_id,
                ordinal=ordinal,
                attempt=attempt,
                reserve_next_attempt=reserve_next_attempt,
                allowed_origins=connector_origins,
                budget_state=budget_state,
                cancellation_event=cancellation_event,
            )
            assert isinstance(result, DownloadResult)
            return result
        finally:
            self._release_job_budget_state(job_id, budget_state)

    async def fetch_memory(
        self,
        request: PageFetchRequest,
        *,
        session_id: UUID,
        ordinal: int,
        allowed_origins: frozenset[NetworkOrigin],
    ) -> MemoryPageResult:
        """Use the same HTTP checks and retries without files or disk reservations."""
        if type(request) is not PageFetchRequest:
            raise TypeError("request must be a PageFetchRequest")
        if type(session_id) is not UUID or session_id.version != 4:
            raise ValueError("session_id must be a UUIDv4")
        if type(ordinal) is not int or not 1 <= ordinal <= MAX_MANIFEST_PAGE_COUNT:
            raise ValueError("ordinal is outside the page contract")

        async def next_attempt(_id: UUID, _ordinal: int, previous: int) -> int:
            return previous + 1

        result = await self._download_registered(
            request,
            job_id=session_id,
            ordinal=ordinal,
            attempt=1,
            reserve_next_attempt=next_attempt,
            allowed_origins=self._connector_origins(allowed_origins),
            budget_state=_JobTemporaryBudgetState(),
            cancellation_event=None,
            memory=True,
        )
        assert isinstance(result, MemoryPageResult)
        return result

    async def _download_registered(
        self,
        request: PageFetchRequest,
        *,
        job_id: UUID,
        ordinal: int,
        attempt: int,
        reserve_next_attempt: AttemptReservation | None,
        allowed_origins: frozenset[NetworkOrigin],
        budget_state: _JobTemporaryBudgetState,
        cancellation_event: asyncio.Event | None,
        memory: bool = False,
    ) -> DownloadResult | MemoryPageResult:

        trace_id = current_trace_id() or create_trace_id()
        initial_origin = self._require_allowed_origin(
            request.url,
            stage="request_origin",
            trace_id=trace_id,
            job_id=job_id,
            ordinal=ordinal,
            attempt=attempt,
            allowed_origins=allowed_origins,
        )
        if request.referer is not None:
            self._require_allowed_origin(
                request.referer,
                stage="referer_origin",
                trace_id=trace_id,
                job_id=job_id,
                ordinal=ordinal,
                attempt=attempt,
                allowed_origins=allowed_origins,
            )

        secret_headers: dict[str, str] = {}
        if request.secret_reference is not None:
            if self._secret_header_provider is None:
                self._raise(
                    code=ErrorCode.AUTH_SECRET_UNAVAILABLE,
                    stage="secret_resolution",
                    trace_id=trace_id,
                    job_id=job_id,
                    ordinal=ordinal,
                    attempt=attempt,
                    retryable=False,
                )
            try:
                raw_headers = self._secret_header_provider(request.secret_reference)
                secret_headers = self._validate_secret_headers(raw_headers)
            except DownloadTransportError:
                raise
            except Exception as error:
                self._raise(
                    code=ErrorCode.AUTH_SECRET_UNAVAILABLE,
                    stage="secret_resolution",
                    trace_id=trace_id,
                    job_id=job_id,
                    ordinal=ordinal,
                    attempt=attempt,
                    retryable=False,
                    cause_type=type(error).__name__,
                )

        active_attempt = attempt
        retry_index = 0
        while True:
            self._raise_if_cancelled(cancellation_event)
            try:
                return await self._download_attempt(
                    request,
                    job_id=job_id,
                    ordinal=ordinal,
                    attempt=active_attempt,
                    initial_origin=initial_origin,
                    secret_headers=secret_headers,
                    trace_id=trace_id,
                    allowed_origins=allowed_origins,
                    budget_state=budget_state,
                    cancellation_event=cancellation_event,
                    memory=memory,
                )
            except _RetryableAttemptFailure as failure:
                if retry_index >= len(_RETRY_DELAYS_SECONDS):
                    self._raise(
                        code=ErrorCode.SRC_CONNECTOR_FAILED,
                        stage=failure.stage,
                        trace_id=trace_id,
                        job_id=job_id,
                        ordinal=ordinal,
                        attempt=active_attempt,
                        retryable=False,
                        status_code=failure.status_code,
                        cause_type=failure.cause_type,
                    )
                if reserve_next_attempt is None:
                    self._raise(
                        code=ErrorCode.DL_JOB_RECOVERY_BLOCKED,
                        stage="attempt_reservation_required",
                        trace_id=trace_id,
                        job_id=job_id,
                        ordinal=ordinal,
                        attempt=active_attempt,
                        retryable=False,
                        status_code=failure.status_code,
                    )
                delay = self._retry_delay(failure, retry_index)
                if (
                    failure.status_code == 429
                    and failure.retry_after is not None
                    and self._parse_retry_after(failure.retry_after) is not None
                    and delay > self._policy.max_retry_after_seconds
                ):
                    self._raise(
                        code=ErrorCode.SRC_CONNECTOR_FAILED,
                        stage="source_retry_after",
                        trace_id=trace_id,
                        job_id=job_id,
                        ordinal=ordinal,
                        attempt=active_attempt,
                        retryable=False,
                        status_code=failure.status_code,
                    )
                logger.warning(
                    "download_transport.retry",
                    extra={
                        "component": "download_transport",
                        "error_code": ErrorCode.SRC_CONNECTOR_FAILED.value,
                        "trace_id": trace_id,
                        "job_id": str(job_id),
                        "ordinal": ordinal,
                        "attempt": active_attempt,
                        "retry_delay_seconds": delay,
                        "stage": failure.stage,
                        "status_code": failure.status_code,
                    },
                )
                await self._sleep_or_cancel(delay, cancellation_event)
                previous_attempt = active_attempt
                try:
                    next_attempt = await reserve_next_attempt(job_id, ordinal, previous_attempt)
                except Exception as error:
                    self._raise(
                        code=ErrorCode.DL_JOB_RECOVERY_BLOCKED,
                        stage="attempt_reservation",
                        trace_id=trace_id,
                        job_id=job_id,
                        ordinal=ordinal,
                        attempt=previous_attempt,
                        retryable=False,
                        status_code=failure.status_code,
                        cause_type=type(error).__name__,
                    )
                if type(next_attempt) is not int or next_attempt != previous_attempt + 1:
                    self._raise(
                        code=ErrorCode.DL_JOB_RECOVERY_BLOCKED,
                        stage="attempt_reservation_sequence",
                        trace_id=trace_id,
                        job_id=job_id,
                        ordinal=ordinal,
                        attempt=previous_attempt,
                        retryable=False,
                        status_code=failure.status_code,
                    )
                active_attempt = next_attempt
                retry_index += 1

    async def _download_attempt(
        self,
        request: PageFetchRequest,
        *,
        job_id: UUID,
        ordinal: int,
        attempt: int,
        initial_origin: NetworkOrigin,
        secret_headers: Mapping[str, str],
        trace_id: str,
        allowed_origins: frozenset[NetworkOrigin],
        budget_state: _JobTemporaryBudgetState,
        cancellation_event: asyncio.Event | None,
        memory: bool = False,
    ) -> DownloadResult | MemoryPageResult:
        if not memory:
            await self._preflight_job_temporary_budget(
                job_id=job_id,
                ordinal=ordinal,
                attempt=attempt,
                trace_id=trace_id,
                budget_state=budget_state,
            )
        current_url = request.url
        redirects = 0
        while True:
            self._raise_if_cancelled(cancellation_event)
            origin = self._require_allowed_origin(
                current_url,
                stage="redirect_origin" if redirects else "request_origin",
                trace_id=trace_id,
                job_id=job_id,
                ordinal=ordinal,
                attempt=attempt,
                allowed_origins=allowed_origins,
            )
            host_semaphore = self._host_semaphores.get(origin.host)
            if host_semaphore is None:
                self._raise(
                    code=ErrorCode.SRC_CONNECTOR_FAILED,
                    stage="origin_not_configured",
                    trace_id=trace_id,
                    job_id=job_id,
                    ordinal=ordinal,
                    attempt=attempt,
                    retryable=False,
                )

            next_url: str | None = None
            result_data: tuple[str, Path, int, str, int | None, str] | None = None
            retry_failure: _RetryableAttemptFailure | None = None
            async with self._semaphore_slot(self._global_semaphore, cancellation_event):
                async with self._semaphore_slot(host_semaphore, cancellation_event):
                    if self._host_cooldowns.get(origin.host, 0) > self._clock():
                        self._raise(
                            code=ErrorCode.SRC_CONNECTOR_FAILED,
                            stage="source_cooldown",
                            trace_id=trace_id,
                            job_id=job_id,
                            ordinal=ordinal,
                            attempt=attempt,
                            retryable=False,
                            status_code=429,
                        )
                    headers = {"Accept-Encoding": "identity"}
                    if request.referer is not None and origin == initial_origin:
                        headers["Referer"] = request.referer
                    if origin == initial_origin:
                        headers.update(secret_headers)
                    try:
                        async with self._stream_or_cancel(
                            current_url,
                            headers=headers,
                            cancellation_event=cancellation_event,
                        ) as response:
                            self._raise_if_cancelled(cancellation_event)
                            if response.status_code in _REDIRECT_STATUS:
                                location = response.headers.get("location")
                                if location is None:
                                    self._raise(
                                        code=ErrorCode.SRC_CONNECTOR_FAILED,
                                        stage="redirect_without_location",
                                        trace_id=trace_id,
                                        job_id=job_id,
                                        ordinal=ordinal,
                                        attempt=attempt,
                                        retryable=False,
                                        status_code=response.status_code,
                                    )
                                if redirects >= self._policy.max_redirects:
                                    self._raise(
                                        code=ErrorCode.SRC_CONNECTOR_FAILED,
                                        stage="redirect_limit",
                                        trace_id=trace_id,
                                        job_id=job_id,
                                        ordinal=ordinal,
                                        attempt=attempt,
                                        retryable=False,
                                        status_code=response.status_code,
                                    )
                                candidate = urljoin(current_url, location)
                                self._require_allowed_origin(
                                    candidate,
                                    stage="redirect_origin",
                                    trace_id=trace_id,
                                    job_id=job_id,
                                    ordinal=ordinal,
                                    attempt=attempt,
                                    allowed_origins=allowed_origins,
                                )
                                next_url = candidate
                            elif response.status_code in _RETRYABLE_HTTP_STATUS:
                                retry_failure = _RetryableAttemptFailure(
                                    stage="http_status",
                                    status_code=response.status_code,
                                    retry_after=response.headers.get("retry-after")
                                    if response.status_code == 429
                                    else None,
                                )
                                if response.status_code == 429:
                                    self._host_cooldowns[origin.host] = max(
                                        self._host_cooldowns.get(origin.host, 0),
                                        self._clock() + self._retry_delay(retry_failure, 0),
                                    )
                            elif response.status_code != 200:
                                self._raise(
                                    code=ErrorCode.SRC_CONNECTOR_FAILED,
                                    stage="http_status",
                                    trace_id=trace_id,
                                    job_id=job_id,
                                    ordinal=ordinal,
                                    attempt=attempt,
                                    retryable=False,
                                    status_code=response.status_code,
                                )
                            else:
                                content_encoding = response.headers.get("content-encoding")
                                if (
                                    content_encoding is not None
                                    and content_encoding.lower() != "identity"
                                ):
                                    self._raise(
                                        code=ErrorCode.SRC_CONNECTOR_FAILED,
                                        stage="content_encoding",
                                        trace_id=trace_id,
                                        job_id=job_id,
                                        ordinal=ordinal,
                                        attempt=attempt,
                                        retryable=False,
                                        status_code=response.status_code,
                                    )
                                expected_length = self._content_length(
                                    response,
                                    trace_id=trace_id,
                                    job_id=job_id,
                                    ordinal=ordinal,
                                    attempt=attempt,
                                )
                                if (
                                    expected_length is not None
                                    and expected_length > self._policy.max_bytes
                                ):
                                    self._raise(
                                        code=ErrorCode.SRC_CONNECTOR_FAILED,
                                        stage="content_length_limit",
                                        trace_id=trace_id,
                                        job_id=job_id,
                                        ordinal=ordinal,
                                        attempt=attempt,
                                        retryable=False,
                                        status_code=response.status_code,
                                    )
                                if memory:
                                    body = bytearray()
                                    async for chunk in self._iter_response_chunks(
                                        response,
                                        cancellation_event=cancellation_event,
                                    ):
                                        if len(body) + len(chunk) > self._policy.max_bytes:
                                            self._raise(
                                                code=ErrorCode.SRC_CONNECTOR_FAILED,
                                                stage="byte_limit",
                                                trace_id=trace_id,
                                                job_id=job_id,
                                                ordinal=ordinal,
                                                attempt=attempt,
                                                retryable=False,
                                            )
                                        body.extend(chunk)
                                    if expected_length is not None and len(body) != expected_length:
                                        raise _RetryableAttemptFailure(
                                            stage="content_length_mismatch"
                                        )
                                    return MemoryPageResult(
                                        bytes(body), self._safe_content_type(response)
                                    )
                                self._assert_content_length_fits_job_budget(
                                    expected_length,
                                    job_id=job_id,
                                    ordinal=ordinal,
                                    attempt=attempt,
                                    trace_id=trace_id,
                                    status_code=response.status_code,
                                    budget_state=budget_state,
                                )
                                relative_path = self._relative_path(job_id, ordinal, attempt)
                                target_path = self._data_root / "temp" / relative_path
                                safe_content_type = self._safe_content_type(response)
                                digest = hashlib.sha256()
                                written_bytes = 0
                                try:
                                    with self._page_store.create(
                                        relative_path,
                                        job_id=job_id,
                                        trace_id=trace_id,
                                    ) as target:
                                        async for chunk in self._iter_response_chunks(
                                            response,
                                            cancellation_event=cancellation_event,
                                        ):
                                            if not chunk:
                                                continue
                                            if written_bytes + len(chunk) > self._policy.max_bytes:
                                                self._raise(
                                                    code=ErrorCode.SRC_CONNECTOR_FAILED,
                                                    stage="byte_limit",
                                                    trace_id=trace_id,
                                                    job_id=job_id,
                                                    ordinal=ordinal,
                                                    attempt=attempt,
                                                    retryable=False,
                                                    status_code=response.status_code,
                                                )
                                            self._assert_chunk_fits_job_budget(
                                                len(chunk),
                                                job_id=job_id,
                                                ordinal=ordinal,
                                                attempt=attempt,
                                                trace_id=trace_id,
                                                status_code=response.status_code,
                                                budget_state=budget_state,
                                            )
                                            with self._storage_gate.mutation():
                                                try:
                                                    if self._disk_budget_manager is None:
                                                        count = target.write(chunk)
                                                    else:
                                                        manager = self._disk_budget_manager
                                                        count = manager.write_reserved(
                                                            job_id,
                                                            category="pages",
                                                            requested_bytes=len(chunk),
                                                            write=lambda chunk=chunk: target.write(
                                                                chunk
                                                            ),
                                                        )
                                                except OSError as error:
                                                    budget_state.total_bytes = None
                                                    if is_disk_full_os_error(error):
                                                        self._storage_gate.close(
                                                            "FILESYSTEM_DISK_FULL"
                                                        )
                                                    raise
                                                except BaseException:
                                                    budget_state.total_bytes = None
                                                    raise
                                            if count != len(chunk):
                                                budget_state.total_bytes = None
                                                raise OSError("short page write")
                                            budget_state.total_bytes = (
                                                self._budget_state_total(
                                                    budget_state,
                                                    job_id=job_id,
                                                    ordinal=ordinal,
                                                    attempt=attempt,
                                                    trace_id=trace_id,
                                                )
                                                + count
                                            )
                                            digest.update(chunk)
                                            written_bytes += count
                                        with self._storage_gate.mutation():
                                            try:
                                                target.sync()
                                            except OSError as error:
                                                if is_disk_full_os_error(error):
                                                    self._storage_gate.close("FILESYSTEM_DISK_FULL")
                                                raise
                                except ManifestStoreError as error:
                                    self._raise(
                                        code=error.code,
                                        stage=error.stage,
                                        trace_id=trace_id,
                                        job_id=job_id,
                                        ordinal=ordinal,
                                        attempt=attempt,
                                        retryable=False,
                                        status_code=response.status_code,
                                        cause_type=type(error).__name__,
                                    )
                                result_data = (
                                    relative_path,
                                    target_path,
                                    written_bytes,
                                    digest.hexdigest(),
                                    expected_length,
                                    safe_content_type,
                                )
                    except DownloadTransportError:
                        raise
                    except httpx.TimeoutException as error:
                        retry_failure = _RetryableAttemptFailure(
                            stage="http_timeout",
                            cause_type=type(error).__name__,
                        )
                    except (httpx.NetworkError, httpx.RemoteProtocolError) as error:
                        retry_failure = _RetryableAttemptFailure(
                            stage="http_network",
                            cause_type=type(error).__name__,
                        )
                    except httpx.HTTPError as error:
                        self._raise(
                            code=ErrorCode.SRC_CONNECTOR_FAILED,
                            stage="http_protocol",
                            trace_id=trace_id,
                            job_id=job_id,
                            ordinal=ordinal,
                            attempt=attempt,
                            retryable=False,
                            cause_type=type(error).__name__,
                        )

            if next_url is not None:
                current_url = next_url
                redirects += 1
                continue
            if retry_failure is not None:
                raise retry_failure
            if result_data is not None:
                (
                    relative_path,
                    target_path,
                    written_bytes,
                    sha256,
                    expected_length,
                    content_type,
                ) = result_data
                if expected_length is not None and written_bytes != expected_length:
                    raise _RetryableAttemptFailure(stage="content_length_mismatch")
                return DownloadResult(
                    job_id=job_id,
                    ordinal=ordinal,
                    attempt=attempt,
                    temporary_path=relative_path,
                    path=target_path,
                    size_bytes=written_bytes,
                    sha256=sha256,
                    content_type=content_type,
                )

    @asynccontextmanager
    async def _semaphore_slot(
        self,
        semaphore: asyncio.Semaphore,
        cancellation_event: asyncio.Event | None,
    ) -> AsyncGenerator[None]:
        if cancellation_event is None:
            async with semaphore:
                yield
            return

        self._raise_if_cancelled(cancellation_event)
        acquisition = asyncio.create_task(semaphore.acquire())
        cancelled = asyncio.create_task(cancellation_event.wait())
        acquired = False
        try:
            await asyncio.wait(
                {acquisition, cancelled},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_event.is_set():
                raise DownloadCancellationRequested()
            await acquisition
            acquired = True
            self._raise_if_cancelled(cancellation_event)
            yield
        except BaseException:
            if acquired:
                semaphore.release()
                acquired = False
            else:
                if not acquisition.done():
                    acquisition.cancel()
                outcome = await asyncio.gather(acquisition, return_exceptions=True)
                if outcome[0] is True:
                    semaphore.release()
            raise
        else:
            semaphore.release()
        finally:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)

    @asynccontextmanager
    async def _stream_or_cancel(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        cancellation_event: asyncio.Event | None,
    ) -> AsyncGenerator[httpx.Response]:
        stream_context = self._client.stream(
            "GET",
            url,
            headers=headers,
            timeout=self._timeouts,
            follow_redirects=False,
        )
        if cancellation_event is None:
            async with stream_context as response:
                yield response
            return

        self._raise_if_cancelled(cancellation_event)
        opening = asyncio.create_task(stream_context.__aenter__())
        cancelled = asyncio.create_task(cancellation_event.wait())
        response: httpx.Response | None = None
        try:
            await asyncio.wait(
                {opening, cancelled},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_event.is_set():
                if opening.done():
                    try:
                        response = opening.result()
                    except BaseException:
                        pass
                    else:
                        await stream_context.__aexit__(None, None, None)
                else:
                    opening.cancel()
                    await asyncio.gather(opening, return_exceptions=True)
                raise DownloadCancellationRequested()
            response = await opening
        except BaseException:
            if not opening.done():
                opening.cancel()
                await asyncio.gather(opening, return_exceptions=True)
            raise
        finally:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)

        try:
            yield response
        except BaseException as error:
            suppress = await stream_context.__aexit__(type(error), error, error.__traceback__)
            if not suppress:
                raise
        else:
            await stream_context.__aexit__(None, None, None)

    async def _iter_response_chunks(
        self,
        response: httpx.Response,
        *,
        cancellation_event: asyncio.Event | None,
    ) -> AsyncIterator[bytes]:
        iterator = response.aiter_raw(chunk_size=self._policy.chunk_size).__aiter__()
        while True:
            self._raise_if_cancelled(cancellation_event)
            if cancellation_event is None:
                try:
                    chunk = await anext(iterator)
                except StopAsyncIteration:
                    return
            else:
                next_chunk = asyncio.ensure_future(anext(iterator))
                cancelled = asyncio.create_task(cancellation_event.wait())
                done, _pending = await asyncio.wait(
                    {next_chunk, cancelled},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if next_chunk in done:
                    cancelled.cancel()
                    await asyncio.gather(cancelled, return_exceptions=True)
                    try:
                        chunk = next_chunk.result()
                    except StopAsyncIteration:
                        return
                else:
                    next_chunk.cancel()
                    await asyncio.gather(next_chunk, return_exceptions=True)
                    raise DownloadCancellationRequested()
            if chunk:
                yield chunk

    @staticmethod
    def _raise_if_cancelled(
        cancellation_event: asyncio.Event | None,
    ) -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            raise DownloadCancellationRequested()

    async def _sleep_or_cancel(
        self,
        delay: float,
        cancellation_event: asyncio.Event | None,
    ) -> None:
        if cancellation_event is None:
            await self._sleep(delay)
            return
        self._raise_if_cancelled(cancellation_event)
        sleeping = asyncio.ensure_future(self._sleep(delay))
        cancelled = asyncio.create_task(cancellation_event.wait())
        done, _pending = await asyncio.wait(
            {sleeping, cancelled},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if sleeping in done:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            await sleeping
            return
        sleeping.cancel()
        await asyncio.gather(sleeping, return_exceptions=True)
        raise DownloadCancellationRequested()

    def _require_allowed_origin(
        self,
        url: str,
        *,
        stage: str,
        trace_id: str,
        job_id: UUID,
        ordinal: int,
        attempt: int,
        allowed_origins: frozenset[NetworkOrigin],
    ) -> NetworkOrigin:
        try:
            PageFetchRequest(url=url)
            parsed = urlsplit(url)
            origin = NetworkOrigin.from_url(url)
            if (
                not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or "@" in parsed.netloc
                or parsed.fragment
                or any(char.isspace() or ord(char) < 0x20 for char in url)
                or "\\" in url
            ):
                raise ValueError
        except TypeError, ValueError:
            self._raise(
                code=ErrorCode.SRC_CONNECTOR_FAILED,
                stage=f"{stage}_invalid",
                trace_id=trace_id,
                job_id=job_id,
                ordinal=ordinal,
                attempt=attempt,
                retryable=False,
            )
        if origin not in allowed_origins:
            self._raise(
                code=ErrorCode.SRC_CONNECTOR_FAILED,
                stage=f"{stage}_not_allowed",
                trace_id=trace_id,
                job_id=job_id,
                ordinal=ordinal,
                attempt=attempt,
                retryable=False,
            )
        if origin.scheme == "http" and (
            not origin.is_loopback
            or (
                self._test_transport is None
                and (origin.host, origin.port) not in _PRODUCTION_HTTP_LOOPBACK_ORIGINS
            )
        ):
            self._raise(
                code=ErrorCode.SRC_CONNECTOR_FAILED,
                stage=f"{stage}_insecure",
                trace_id=trace_id,
                job_id=job_id,
                ordinal=ordinal,
                attempt=attempt,
                retryable=False,
            )
        if origin.scheme == "http" and self._test_transport is not None:
            transport_for_url = getattr(self._client, "_transport_for_url", None)
            if not callable(transport_for_url):
                self._raise(
                    code=ErrorCode.SRC_CONNECTOR_FAILED,
                    stage=f"{stage}_test_transport",
                    trace_id=trace_id,
                    job_id=job_id,
                    ordinal=ordinal,
                    attempt=attempt,
                    retryable=False,
                )
            try:
                selected_transport = transport_for_url(httpx.URL(url))
            except Exception as error:
                self._raise(
                    code=ErrorCode.SRC_CONNECTOR_FAILED,
                    stage=f"{stage}_test_transport",
                    trace_id=trace_id,
                    job_id=job_id,
                    ordinal=ordinal,
                    attempt=attempt,
                    retryable=False,
                    cause_type=type(error).__name__,
                )
            if selected_transport is not self._test_transport:
                self._raise(
                    code=ErrorCode.SRC_CONNECTOR_FAILED,
                    stage=f"{stage}_test_transport",
                    trace_id=trace_id,
                    job_id=job_id,
                    ordinal=ordinal,
                    attempt=attempt,
                    retryable=False,
                )
        return origin

    def _connector_origins(
        self,
        allowed_origins: frozenset[NetworkOrigin] | None,
    ) -> frozenset[NetworkOrigin]:
        if allowed_origins is None:
            return self._policy.allowed_origins
        if type(allowed_origins) is not frozenset or not allowed_origins:
            raise ValueError("connector allowed_origins must be a non-empty frozenset")
        if any(type(origin) is not NetworkOrigin for origin in allowed_origins):
            raise TypeError("connector allowed_origins must contain NetworkOrigin values")
        if not allowed_origins.issubset(self._policy.allowed_origins):
            raise ValueError("connector allowed_origins cannot expand transport policy")
        return allowed_origins

    def _acquire_job_budget_state(self, job_id: UUID) -> _JobTemporaryBudgetState:
        with self._job_budget_registry_lock:
            state = self._job_budget_states.get(job_id)
            if state is None:
                state = _JobTemporaryBudgetState()
                self._job_budget_states[job_id] = state
            elif state.retire_when_idle:
                raise RuntimeError("job temporary budget is already being retired")
            state.active_downloads += 1
            return state

    def _release_job_budget_state(
        self,
        job_id: UUID,
        state: _JobTemporaryBudgetState,
    ) -> None:
        with self._job_budget_registry_lock:
            state.active_downloads -= 1
            if state.active_downloads == 0 and state.retire_when_idle:
                self._job_budget_states.pop(job_id, None)

    def finish_job_temporary_budget(self, job_id: UUID) -> None:
        """Discard the in-memory budget only after the owning worker exits."""
        if type(job_id) is not UUID:
            raise TypeError("job_id must be a UUID")
        with self._job_budget_registry_lock:
            state = self._job_budget_states.get(job_id)
            if state is None:
                return
            state.retire_when_idle = True
            if state.active_downloads == 0:
                self._job_budget_states.pop(job_id, None)

    def existing_page_bytes_for_budget(self, job_id: UUID) -> int:
        """Return a verified bounded inventory before the dispatcher claims a job."""
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        return self._inventory_job_temporary_bytes(job_id)

    async def _preflight_job_temporary_budget(
        self,
        *,
        job_id: UUID,
        ordinal: int,
        attempt: int,
        trace_id: str,
        budget_state: _JobTemporaryBudgetState,
    ) -> None:
        async with budget_state.inventory_lock:
            observed_bytes = budget_state.total_bytes
            if observed_bytes is None:
                try:
                    observed_bytes = await asyncio.to_thread(
                        self._inventory_job_temporary_bytes,
                        job_id,
                    )
                except (DataRootIdentityError, OSError, ValueError) as error:
                    self._raise(
                        code=ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
                        stage="temporary_inventory_unavailable",
                        trace_id=trace_id,
                        job_id=job_id,
                        ordinal=ordinal,
                        attempt=attempt,
                        retryable=False,
                        cause_type=type(error).__name__,
                    )
            if observed_bytes > MAX_JOB_TEMPORARY_BYTES:
                self._raise(
                    code=ErrorCode.DL_TEMPORARY_BUDGET_EXCEEDED,
                    stage="temporary_budget_preflight",
                    trace_id=trace_id,
                    job_id=job_id,
                    ordinal=ordinal,
                    attempt=attempt,
                    retryable=False,
                )
            budget_state.total_bytes = max(
                observed_bytes,
                budget_state.total_bytes if budget_state.total_bytes is not None else 0,
            )

    def _assert_content_length_fits_job_budget(
        self,
        expected_length: int | None,
        *,
        job_id: UUID,
        ordinal: int,
        attempt: int,
        trace_id: str,
        status_code: int,
        budget_state: _JobTemporaryBudgetState,
    ) -> None:
        if expected_length is None:
            return
        used_bytes = self._budget_state_total(
            budget_state,
            job_id=job_id,
            ordinal=ordinal,
            attempt=attempt,
            trace_id=trace_id,
        )
        if used_bytes + expected_length > MAX_JOB_TEMPORARY_BYTES:
            self._raise(
                code=ErrorCode.DL_TEMPORARY_BUDGET_EXCEEDED,
                stage="temporary_budget_content_length",
                trace_id=trace_id,
                job_id=job_id,
                ordinal=ordinal,
                attempt=attempt,
                retryable=False,
                status_code=status_code,
            )

    def _assert_chunk_fits_job_budget(
        self,
        chunk_size: int,
        *,
        job_id: UUID,
        ordinal: int,
        attempt: int,
        trace_id: str,
        status_code: int,
        budget_state: _JobTemporaryBudgetState,
    ) -> None:
        used_bytes = self._budget_state_total(
            budget_state,
            job_id=job_id,
            ordinal=ordinal,
            attempt=attempt,
            trace_id=trace_id,
        )
        if used_bytes + chunk_size > MAX_JOB_TEMPORARY_BYTES:
            self._raise(
                code=ErrorCode.DL_TEMPORARY_BUDGET_EXCEEDED,
                stage="temporary_budget_chunk",
                trace_id=trace_id,
                job_id=job_id,
                ordinal=ordinal,
                attempt=attempt,
                retryable=False,
                status_code=status_code,
            )

    def _budget_state_total(
        self,
        budget_state: _JobTemporaryBudgetState,
        *,
        job_id: UUID,
        ordinal: int,
        attempt: int,
        trace_id: str,
    ) -> int:
        total_bytes = budget_state.total_bytes
        if total_bytes is None:
            self._raise(
                code=ErrorCode.DL_TEMPORARY_PATH_UNAVAILABLE,
                stage="temporary_budget_state_missing",
                trace_id=trace_id,
                job_id=job_id,
                ordinal=ordinal,
                attempt=attempt,
                retryable=False,
            )
        return total_bytes

    def _inventory_job_temporary_bytes(self, job_id: UUID) -> int:
        trace_id = current_trace_id() or create_trace_id()
        identity = self._page_store.data_root_identity
        identity.assert_current()
        temp_root = self._data_root / "temp"
        pages_root = temp_root / "jobs" / str(job_id) / "pages"
        directory_chain = (
            temp_root,
            temp_root / "jobs",
            temp_root / "jobs" / str(job_id),
            pages_root,
        )
        directory_states: dict[Path, tuple[object, ...]] = {}

        def stat_snapshot(path: Path) -> os.stat_result:
            return retry_lstat(
                path,
                component="download_transport",
                stage="temporary_inventory_stat",
                trace_id=trace_id,
                job_id=str(job_id),
                validate_path=identity.assert_current,
            )

        def snapshot(info: os.stat_result) -> tuple[object, ...]:
            file_identity = RetryingFileStream.identity_of(info)
            if file_identity is None:
                raise ValueError("temporary inventory identity is unavailable")
            return (
                file_identity,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )

        def valid_directory(info: os.stat_result) -> bool:
            return not self._is_reparse_point(info) and stat.S_ISDIR(info.st_mode)

        for directory in directory_chain:
            try:
                information = stat_snapshot(directory)
            except FileNotFoundError:
                return 0
            if not valid_directory(information):
                raise ValueError("temporary inventory contains an unsafe directory")
            directory_states[directory] = snapshot(information)

        observed_directories: dict[Path, tuple[object, ...]] = dict(directory_states)
        observed_files: dict[Path, tuple[object, ...]] = {}
        completed_listings: dict[Path, tuple[str, ...]] = {}

        def namespace_unchanged() -> bool:
            try:
                identity.assert_current()
                for path, expected_state in (
                    *observed_directories.items(),
                    *observed_files.items(),
                ):
                    current = stat_snapshot(path)
                    if snapshot(current) != expected_state:
                        return False
                    if path in observed_directories:
                        if not valid_directory(current):
                            return False
                    elif self._is_reparse_point(current) or not stat.S_ISREG(current.st_mode):
                        return False
                verification_entry_count = 0
                for directory, expected_names in completed_listings.items():
                    names: list[str] = []
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if type(entry.name) is not str:
                                return False
                            verification_entry_count += 1
                            if verification_entry_count > _MAX_TEMPORARY_INVENTORY_ENTRIES:
                                return False
                            names.append(entry.name)
                    if tuple(sorted(names)) != expected_names:
                        return False
                return True
            except OSError, ValueError, DataRootIdentityError:
                return False

        def scan_inventory() -> int:
            # Every attempt starts with fresh counters and observations. A lock
            # during scandir therefore never resumes a partially counted tree.
            observed_directories.clear()
            observed_directories.update(directory_states)
            observed_files.clear()
            completed_listings.clear()
            pending_directories = [pages_root]
            entry_count = 0
            total_bytes = 0
            while pending_directories:
                directory = pending_directories.pop()
                information = stat_snapshot(directory)
                if not valid_directory(information):
                    raise ValueError("temporary inventory contains an unsafe directory")
                observed_directories[directory] = snapshot(information)
                names: list[str] = []
                with os.scandir(directory) as entries:
                    for entry in entries:
                        entry_count += 1
                        if entry_count > _MAX_TEMPORARY_INVENTORY_ENTRIES:
                            raise ValueError("temporary inventory entry bound exceeded")
                        if type(entry.name) is not str:
                            raise ValueError("temporary inventory entry name is invalid")
                        names.append(entry.name)
                        child = directory / entry.name
                        relative_path = child.relative_to(temp_root).as_posix()
                        validate_temporary_path(relative_path)
                        if (
                            len(str(child).encode("utf-16-le")) // 2
                            > _MAX_TEMPORARY_PATH_UTF16_UNITS
                        ):
                            raise ValueError("temporary inventory path exceeds Windows limit")
                        child_information = stat_snapshot(child)
                        if self._is_reparse_point(child_information):
                            raise ValueError("temporary inventory contains a reparse point")
                        if stat.S_ISDIR(child_information.st_mode):
                            observed_directories[child] = snapshot(child_information)
                            pending_directories.append(child)
                        elif stat.S_ISREG(child_information.st_mode):
                            if (
                                type(child_information.st_size) is not int
                                or child_information.st_size < 0
                            ):
                                raise ValueError("temporary inventory file size is invalid")
                            observed_files[child] = snapshot(child_information)
                            total_bytes += child_information.st_size
                        else:
                            raise ValueError("temporary inventory contains a non-regular file")
                completed_listings[directory] = tuple(sorted(names))

            if not namespace_unchanged():
                raise OSError("temporary inventory namespace changed during scan")
            return total_bytes

        return retry_filesystem_operation(
            scan_inventory,
            unchanged=namespace_unchanged,
            component="download_transport",
            stage="temporary_inventory_scan",
            trace_id=trace_id,
            job_id=str(job_id),
        )

    @staticmethod
    def _is_reparse_point(information: os.stat_result) -> bool:
        return stat.S_ISLNK(information.st_mode) or bool(
            getattr(information, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
        )

    @staticmethod
    def _safe_content_type(response: httpx.Response) -> str:
        values = response.headers.get_list("content-type")
        if len(values) != 1:
            return "application/octet-stream"
        media_type = values[0].split(";", maxsplit=1)[0].strip().lower()
        if _SAFE_MEDIA_TYPE.fullmatch(media_type) is None:
            return "application/octet-stream"
        return media_type

    def _content_length(
        self,
        response: httpx.Response,
        *,
        trace_id: str,
        job_id: UUID,
        ordinal: int,
        attempt: int,
    ) -> int | None:
        values = response.headers.get_list("content-length")
        if not values:
            return None
        if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
            self._raise(
                code=ErrorCode.SRC_CONNECTOR_FAILED,
                stage="content_length_invalid",
                trace_id=trace_id,
                job_id=job_id,
                ordinal=ordinal,
                attempt=attempt,
                retryable=False,
                status_code=response.status_code,
            )
        raw_value = values[0]
        if len(raw_value) > 32:
            return self._policy.max_bytes + 1
        try:
            value = int(raw_value)
        except ValueError:
            self._raise(
                code=ErrorCode.SRC_CONNECTOR_FAILED,
                stage="content_length_invalid",
                trace_id=trace_id,
                job_id=job_id,
                ordinal=ordinal,
                attempt=attempt,
                retryable=False,
                status_code=response.status_code,
            )
        if value < 0:
            self._raise(
                code=ErrorCode.SRC_CONNECTOR_FAILED,
                stage="content_length_invalid",
                trace_id=trace_id,
                job_id=job_id,
                ordinal=ordinal,
                attempt=attempt,
                retryable=False,
                status_code=response.status_code,
            )
        return value

    @staticmethod
    def _relative_path(job_id: UUID, ordinal: int, attempt: int) -> str:
        return f"jobs/{job_id}/pages/{ordinal}/attempt-{attempt}.part"

    @staticmethod
    def _validate_secret_headers(headers: Mapping[str, str]) -> dict[str, str]:
        validated: dict[str, str] = {}
        for name, value in headers.items():
            if type(name) is not str or type(value) is not str:
                raise TypeError("secret headers must be strings")
            if name.lower() in _FORBIDDEN_SECRET_HEADERS:
                raise ValueError("secret provider returned a forbidden transport header")
            if not name or any(
                ord(character) <= 0x20 or ord(character) >= 0x7F for character in name
            ):
                raise ValueError("secret provider returned an invalid header name")
            if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
                raise ValueError("secret provider returned an invalid header value")
            validated[name] = value
        # Let HTTPX apply its own RFC token and encoding validation before network use.
        httpx.Headers(validated)
        return validated

    def _retry_delay(self, failure: _RetryableAttemptFailure, retry_index: int) -> float:
        if failure.status_code == 429 and failure.retry_after is not None:
            parsed = self._parse_retry_after(failure.retry_after)
            if parsed is not None:
                return parsed
        return _RETRY_DELAYS_SECONDS[retry_index]

    @staticmethod
    def _parse_retry_after(value: str) -> float | None:
        stripped = value.strip()
        if stripped.isascii() and stripped.isdigit():
            if len(stripped) > 18:
                return math.inf
            return float(int(stripped))
        try:
            retry_at = parsedate_to_datetime(stripped)
        except TypeError, ValueError, OverflowError:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at.astimezone(UTC) - datetime.now(UTC)).total_seconds())

    def _raise(
        self,
        *,
        code: ErrorCode,
        stage: str,
        trace_id: str,
        job_id: UUID,
        ordinal: int,
        attempt: int,
        retryable: bool,
        status_code: int | None = None,
        cause_type: str | None = None,
    ) -> NoReturn:
        logger.error(
            "download_transport.failed",
            extra={
                "component": "download_transport",
                "error_code": code.value,
                "trace_id": trace_id,
                "job_id": str(job_id),
                "ordinal": ordinal,
                "attempt": attempt,
                "retryable": retryable,
                "stage": stage,
                "status_code": status_code,
                "cause_type": cause_type,
            },
        )
        error = DownloadTransportError(
            code=code,
            stage=stage,
            trace_id=trace_id,
            retryable=retryable,
            attempt=attempt,
            status_code=status_code,
        )
        error.cause_type = cause_type
        raise error from None
