"""Shared, bounded, cookie-free HTTP access for first-party source connectors."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit
from weakref import WeakKeyDictionary

import httpx

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.errors import ErrorCode

MAX_CONNECTOR_HTML_BYTES = 4 * 1024 * 1024
MAX_CONNECTOR_REQUEST_BYTES = 64 * 1024
MAX_CONNECTOR_REDIRECTS = 5
CONNECTOR_HTTP_MAX_CONNECTIONS = 2
CONNECTOR_HTTP_PER_HOST_CONCURRENCY = 1
CONNECTOR_HTTP_TIMEOUT_SECONDS = {
    "connect": 10.0,
    "read": 30.0,
    "write": 10.0,
    "pool": 10.0,
}
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_HTML_ACCEPT = "text/html, application/xhtml+xml;q=0.9"
_JSON_ACCEPT = "application/json"
logger = logging.getLogger("yomira.connector.http_client")


def _require_static_cover_url(url: str) -> None:
    parsed = urlsplit(url)
    if not parsed.query:
        return
    # TMO's public version marker is a cache key; authorization queries stay forbidden.
    if (
        parsed.scheme == "https"
        and parsed.netloc.casefold() == "zonatmo.org"
        and parsed.path.startswith(
            ("/storage/mangas/covers/", "/storage/proxy/", "/storage/covers/")
        )
        and parsed.query.startswith("v=")
        and 1 <= len(parsed.query[2:]) <= 10
        and parsed.query[2:].isascii()
        and parsed.query[2:].isdigit()
    ):
        return
    # MangaOni adds a public Unix timestamp to its static cover URLs.
    if (
        parsed.scheme == "https"
        and parsed.netloc.casefold() == "oni.ntr-files.online"
        and parsed.path.startswith("/public/archivos/mangas/")
        and parsed.path.endswith("/cover.jpg")
        and len(parsed.query) == 10
        and parsed.query.isascii()
        and parsed.query.isdigit()
    ):
        return
    raise ValueError("Cover URLs must not contain authorization queries")


@dataclass(frozen=True, slots=True)
class ConnectorHttpResponse:
    """Bounded response data; repr deliberately excludes URL and body contents."""

    status_code: int
    body: bytes = field(repr=False)
    final_url: str = field(repr=False)
    content_type: str | None = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise ValueError("status_code must be a valid HTTP status")
        if type(self.body) is not bytes:
            raise TypeError("body must be bytes")
        if len(self.body) > MAX_CONNECTOR_HTML_BYTES:
            raise ValueError("body exceeds the connector HTML limit")
        if type(self.final_url) is not str or not self.final_url:
            raise ValueError("final_url must be a non-empty string")
        if self.content_type is not None and type(self.content_type) is not str:
            raise TypeError("content_type must be a string or None")

    def __repr__(self) -> str:
        return (
            "ConnectorHttpResponse("
            f"status_code={self.status_code}, body_bytes={len(self.body)}, "
            "final_url=<redacted>, content_type=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class _Origin:
    scheme: str
    host: str
    port: int

    @property
    def key(self) -> str:
        rendered_host = f"[{self.host}]" if ":" in self.host else self.host
        default_port = 443 if self.scheme == "https" else 80
        if self.port == default_port:
            return f"{self.scheme}://{rendered_host}"
        return f"{self.scheme}://{rendered_host}:{self.port}"


def _parse_origin(value: str, *, require_origin_only: bool) -> _Origin:
    if type(value) is not str or not value:
        raise ValueError("URL must be an absolute HTTP(S) URL")
    if (
        any(
            character.isspace() or ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
            for character in value
        )
        or "\\" in value
        or "#" in value
    ):
        raise ValueError("URL is invalid")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("URL is invalid") from None

    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65535)
        or "%" in hostname
        or not hostname.isascii()
    ):
        raise ValueError("URL is invalid")
    if require_origin_only and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Network origins must not contain paths or queries")

    normalized_host = hostname.lower().rstrip(".")
    if not normalized_host:
        raise ValueError("URL is invalid")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        address = None
    if isinstance(address, ipaddress.IPv6Address):
        normalized_host = address.compressed
    elif isinstance(address, ipaddress.IPv4Address):
        normalized_host = str(address)

    if scheme == "http":
        is_loopback = normalized_host == "localhost" or (
            address is not None and address.is_loopback
        )
        if not is_loopback:
            raise ValueError("HTTP is permitted only for loopback origins")

    return _Origin(
        scheme=scheme,
        host=normalized_host,
        port=port if port is not None else (443 if scheme == "https" else 80),
    )


def _connector_error(
    operation: SourceConnectorOperation,
    *,
    retryable: bool,
    stage: str,
    reason: str,
    http_status: int | None = None,
    cause_type: str | None = None,
) -> SourceConnectorError:
    error = SourceConnectorError(
        code=ErrorCode.SRC_CONNECTOR_FAILED,
        operation=operation,
        retryable=retryable,
    )
    logger.warning(
        "source.connector.http_request_rejected",
        extra={
            "trace_id": error.trace_id,
            "error_code": error.code.value,
            "retryable": error.retryable,
            "operation": error.operation.value,
            "stage": stage,
            "reason": reason,
            "http_status": http_status,
            "cause_type": cause_type,
        },
    )
    return error


class ConnectorHttpClientFactory(Protocol):
    """Creates a client wrapper scoped to one registration's exact origins."""

    def __call__(self, allowed_origins: frozenset[str]) -> ConnectorHttpClient: ...


class ConnectorHttpSession:
    """One shared HTTP pool with no cookie jar and a hard two-connection limit."""

    def __init__(
        self,
        *,
        test_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if test_transport is not None and (
            not callable(getattr(test_transport, "handle_async_request", None))
            or not callable(getattr(test_transport, "aclose", None))
        ):
            raise TypeError("test_transport must be an HTTPX async transport")
        self._transport = test_transport or httpx.AsyncHTTPTransport(
            verify=True,
            trust_env=False,
            retries=0,
            limits=httpx.Limits(
                max_connections=CONNECTOR_HTTP_MAX_CONNECTIONS,
                max_keepalive_connections=CONNECTOR_HTTP_MAX_CONNECTIONS,
            ),
        )
        self._closed = False
        self._semaphore_lock = threading.RLock()
        self._host_semaphores: WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]
        ] = WeakKeyDictionary()

    @property
    def is_closed(self) -> bool:
        return self._closed

    def scoped_client(self, allowed_origins: frozenset[str]) -> ConnectorHttpClient:
        if self._closed:
            raise RuntimeError("Connector HTTP session is closed.")
        if type(allowed_origins) is not frozenset:
            raise TypeError("allowed_origins must be a frozenset")
        normalized: set[str] = set()
        for value in allowed_origins:
            origin = _parse_origin(value, require_origin_only=True)
            if value != origin.key:
                raise ValueError("Network origin must be normalized")
            normalized.add(origin.key)
        return ConnectorHttpClient(self, frozenset(normalized))

    @asynccontextmanager
    async def response_for_host(
        self,
        host: str,
        request: httpx.Request,
    ) -> AsyncGenerator[httpx.Response]:
        """Send one request under the shared host slot and own its response stream."""
        if type(host) is not str or not host:
            raise ValueError("host must be a non-empty string")
        if type(request) is not httpx.Request:
            raise TypeError("request must be an HTTPX Request")
        if self._closed:
            raise RuntimeError("Connector HTTP session is closed.")
        loop = asyncio.get_running_loop()
        with self._semaphore_lock:
            loop_semaphores = self._host_semaphores.setdefault(loop, {})
            semaphore = loop_semaphores.setdefault(
                host,
                asyncio.Semaphore(CONNECTOR_HTTP_PER_HOST_CONCURRENCY),
            )
        async with semaphore:
            response = await self._transport.handle_async_request(request)
            try:
                yield response
            finally:
                await response.aclose()

    async def aclose(self) -> None:
        if not self._closed:
            await self._transport.aclose()
            self._closed = True

    async def close(self) -> None:
        """Close the shared connection pool; repeated calls are safe."""
        await self.aclose()


class ConnectorHttpClient:
    """Bounded client restricted to one registration's declared origins."""

    def __init__(
        self,
        session: ConnectorHttpSession,
        allowed_origins: frozenset[str],
    ) -> None:
        self._session = session
        self._allowed_origins = allowed_origins

    @property
    def allowed_origins(self) -> frozenset[str]:
        return self._allowed_origins

    async def get_html(
        self,
        url: str,
        *,
        operation: SourceConnectorOperation,
        referer: str | None = None,
    ) -> ConnectorHttpResponse:
        """Fetch bounded HTML without retries, cookie state, or URL logging."""
        return await self._get_response(
            url,
            operation=operation,
            referer=referer,
            accept=_HTML_ACCEPT,
        )

    async def get_json(
        self,
        url: str,
        *,
        operation: SourceConnectorOperation,
        referer: str | None = None,
        user_agent: str | None = None,
    ) -> ConnectorHttpResponse:
        """Fetch a bounded JSON response using the same scoped transport."""
        return await self._get_response(
            url,
            operation=operation,
            referer=referer,
            accept=_JSON_ACCEPT,
            user_agent=user_agent,
        )

    async def get_image(
        self,
        url: str,
        *,
        referer: str | None = None,
        user_agent: str | None = None,
    ) -> ConnectorHttpResponse:
        """Fetch a public cover; signed URLs are forbidden, including redirects."""
        return await self._get_response(
            url,
            operation=SourceConnectorOperation.GET_SERIES,
            referer=referer,
            accept="image/*",
            static_url=True,
            user_agent=user_agent,
        )

    async def post_json(
        self,
        url: str,
        payload: object,
        *,
        operation: SourceConnectorOperation,
    ) -> ConnectorHttpResponse:
        """POST one bounded JSON request without cookies, auth, or redirects."""
        if type(operation) is not SourceConnectorOperation:
            raise TypeError("operation must be a SourceConnectorOperation")
        try:
            origin = _parse_origin(url, require_origin_only=False)
            self._require_allowed(origin)
            body = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(body) > MAX_CONNECTOR_REQUEST_BYTES:
                raise ValueError("request body exceeds the connector limit")
        except (TypeError, ValueError, RecursionError, OverflowError):
            raise _connector_error(
                operation,
                retryable=False,
                stage="request_validation",
                reason="REQUEST_POLICY_REJECTED",
            ) from None

        request = httpx.Request(
            "POST",
            url,
            headers={
                "accept": _JSON_ACCEPT,
                "accept-encoding": "identity",
                "content-type": "application/json; charset=utf-8",
            },
            content=body,
            extensions={"timeout": dict(CONNECTOR_HTTP_TIMEOUT_SECONDS)},
        )
        try:
            async with self._session.response_for_host(origin.host, request) as response:
                if response.status_code in _REDIRECT_STATUS_CODES:
                    raise _connector_error(
                        operation,
                        retryable=False,
                        stage="redirect_validation",
                        reason="REDIRECT_NOT_FOLLOWED",
                        http_status=response.status_code,
                    )
                response_body = await self._read_bounded_body(response, operation)
                return ConnectorHttpResponse(
                    status_code=response.status_code,
                    body=response_body,
                    final_url=url,
                    content_type=self._media_type(response.headers.get("content-type")),
                )
        except (httpx.TimeoutException, httpx.NetworkError) as cause:
            raise _connector_error(
                operation,
                retryable=True,
                stage="network_request",
                reason="TRANSPORT_TIMEOUT_OR_NETWORK_ERROR",
                cause_type=type(cause).__name__,
            ) from cause
        except httpx.HTTPError as cause:
            raise _connector_error(
                operation,
                retryable=False,
                stage="network_request",
                reason="TRANSPORT_HTTP_ERROR",
                cause_type=type(cause).__name__,
            ) from cause

    async def _get_response(
        self,
        url: str,
        *,
        operation: SourceConnectorOperation,
        referer: str | None,
        accept: str,
        static_url: bool = False,
        user_agent: str | None = None,
    ) -> ConnectorHttpResponse:
        """Fetch a bounded response without retries, cookie state, or URL logging."""
        if type(operation) is not SourceConnectorOperation:
            raise TypeError("operation must be a SourceConnectorOperation")
        try:
            if user_agent is not None and (
                type(user_agent) is not str
                or not 1 <= len(user_agent) <= 128
                or any(not 0x20 <= ord(character) < 0x7F for character in user_agent)
            ):
                raise ValueError("User-Agent must be bounded printable ASCII")
            initial_origin = _parse_origin(url, require_origin_only=False)
            self._require_allowed(initial_origin)
            if static_url:
                _require_static_cover_url(url)
            safe_referer: str | None = None
            if referer is not None:
                parsed_referer = urlsplit(referer)
                referer_origin = _parse_origin(referer, require_origin_only=False)
                self._require_allowed(referer_origin)
                if referer_origin == initial_origin:
                    safe_referer = urlunsplit(
                        (
                            parsed_referer.scheme,
                            parsed_referer.netloc,
                            parsed_referer.path,
                            "",
                            "",
                        )
                    )
                elif static_url and parsed_referer.path in {"", "/"} and not parsed_referer.query:
                    safe_referer = f"{referer_origin.key}/"
        except ValueError:
            raise _connector_error(
                operation,
                retryable=False,
                stage="request_validation",
                reason="REQUEST_POLICY_REJECTED",
            ) from None

        current_url = url
        current_origin = initial_origin
        current_referer = safe_referer
        for redirect_count in range(MAX_CONNECTOR_REDIRECTS + 1):
            request_headers = {"accept": accept, "accept-encoding": "identity"}
            if user_agent is not None:
                request_headers["user-agent"] = user_agent
            if current_referer is not None:
                request_headers["referer"] = current_referer
            request = httpx.Request(
                "GET",
                current_url,
                headers=request_headers,
                extensions={"timeout": dict(CONNECTOR_HTTP_TIMEOUT_SECONDS)},
            )
            try:
                async with self._session.response_for_host(
                    current_origin.host,
                    request,
                ) as response:
                    if response.status_code in _REDIRECT_STATUS_CODES:
                        location = response.headers.get("location")
                        if location is None or not location:
                            raise _connector_error(
                                operation,
                                retryable=False,
                                stage="redirect_validation",
                                reason="REDIRECT_LOCATION_MISSING",
                                http_status=response.status_code,
                            )
                        if redirect_count >= MAX_CONNECTOR_REDIRECTS:
                            raise _connector_error(
                                operation,
                                retryable=False,
                                stage="redirect_validation",
                                reason="REDIRECT_LIMIT_EXCEEDED",
                                http_status=response.status_code,
                            )
                        next_url = urljoin(current_url, location)
                        try:
                            next_origin = _parse_origin(
                                next_url,
                                require_origin_only=False,
                            )
                            self._require_allowed(next_origin)
                            if static_url:
                                _require_static_cover_url(next_url)
                        except ValueError:
                            raise _connector_error(
                                operation,
                                retryable=False,
                                stage="redirect_validation",
                                reason="REDIRECT_POLICY_REJECTED",
                                http_status=response.status_code,
                            ) from None
                        if next_origin != current_origin:
                            current_referer = None
                        current_url = next_url
                        current_origin = next_origin
                        continue

                    body = await self._read_bounded_body(response, operation)
                    content_type = self._media_type(response.headers.get("content-type"))
                    return ConnectorHttpResponse(
                        status_code=response.status_code,
                        body=body,
                        final_url=current_url,
                        content_type=content_type,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as cause:
                raise _connector_error(
                    operation,
                    retryable=True,
                    stage="network_request",
                    reason="TRANSPORT_TIMEOUT_OR_NETWORK_ERROR",
                    cause_type=type(cause).__name__,
                ) from cause
            except httpx.HTTPError as cause:
                raise _connector_error(
                    operation,
                    retryable=False,
                    stage="network_request",
                    reason="TRANSPORT_HTTP_ERROR",
                    cause_type=type(cause).__name__,
                ) from cause

        raise _connector_error(
            operation,
            retryable=False,
            stage="redirect_validation",
            reason="REDIRECT_LIMIT_EXCEEDED",
        )

    def _require_allowed(self, origin: _Origin) -> None:
        if origin.key not in self._allowed_origins:
            raise ValueError("Request origin is not registered")

    @staticmethod
    async def _read_bounded_body(
        response: httpx.Response,
        operation: SourceConnectorOperation,
    ) -> bytes:
        content_encoding = response.headers.get("content-encoding")
        if content_encoding is not None and content_encoding.strip().lower() != "identity":
            raise _connector_error(
                operation,
                retryable=False,
                stage="response_encoding",
                reason="UNSUPPORTED_CONTENT_ENCODING",
                http_status=response.status_code,
            )
        raw_length = response.headers.get("content-length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except ValueError:
                raise _connector_error(
                    operation,
                    retryable=False,
                    stage="response_body_validation",
                    reason="INVALID_CONTENT_LENGTH",
                    http_status=response.status_code,
                ) from None
            if declared_length < 0 or declared_length > MAX_CONNECTOR_HTML_BYTES:
                raise _connector_error(
                    operation,
                    retryable=False,
                    stage="response_body_validation",
                    reason="BODY_BYTE_LIMIT_REJECTED",
                    http_status=response.status_code,
                )

        if response.is_stream_consumed:
            if len(response.content) > MAX_CONNECTOR_HTML_BYTES:
                raise _connector_error(
                    operation,
                    retryable=False,
                    stage="response_body_validation",
                    reason="BODY_BYTE_LIMIT_REJECTED",
                    http_status=response.status_code,
                )
            return response.content

        body = bytearray()
        try:
            async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                if len(body) + len(chunk) > MAX_CONNECTOR_HTML_BYTES:
                    raise _connector_error(
                        operation,
                        retryable=False,
                        stage="response_body_validation",
                        reason="BODY_BYTE_LIMIT_REJECTED",
                        http_status=response.status_code,
                    )
                body.extend(chunk)
        except SourceConnectorError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as cause:
            raise _connector_error(
                operation,
                retryable=True,
                stage="response_body_read",
                reason="BODY_TIMEOUT_OR_NETWORK_ERROR",
                http_status=response.status_code,
                cause_type=type(cause).__name__,
            ) from cause
        except httpx.HTTPError as cause:
            raise _connector_error(
                operation,
                retryable=False,
                stage="response_body_read",
                reason="BODY_HTTP_ERROR",
                http_status=response.status_code,
                cause_type=type(cause).__name__,
            ) from cause
        return bytes(body)

    @staticmethod
    def _media_type(content_type: str | None) -> str | None:
        if content_type is None:
            return None
        return content_type.partition(";")[0].strip().lower() or None
