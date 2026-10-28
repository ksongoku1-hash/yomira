from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.http_client import (
    MAX_CONNECTOR_HTML_BYTES,
    MAX_CONNECTOR_REQUEST_BYTES,
    ConnectorHttpResponse,
    ConnectorHttpSession,
)
from app.errors import ErrorCode
from app.logging_config import JsonLogFormatter


def _session(
    handler: Callable[[httpx.Request], httpx.Response],
) -> ConnectorHttpSession:
    return ConnectorHttpSession(test_transport=httpx.MockTransport(handler))


def test_post_json_sends_one_bounded_request_with_no_cookie_or_auth() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"data":{}}',
        )

    async def exercise() -> None:
        session = _session(respond)
        try:
            client = session.scoped_client(frozenset({"http://127.0.0.1:4567"}))
            response = await client.post_json(
                "http://127.0.0.1:4567/api/graphql",
                {"query": "query Test { aboutServer { version } }"},
                operation=SourceConnectorOperation.SEARCH,
            )
            assert response.status_code == 200
            assert response.content_type == "application/json"
            assert response.body == b'{"data":{}}'
        finally:
            await session.aclose()

    asyncio.run(exercise())
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url == "http://127.0.0.1:4567/api/graphql"
    assert request.headers["content-type"] == "application/json; charset=utf-8"
    assert len(request.content) <= MAX_CONNECTOR_REQUEST_BYTES
    assert "cookie" not in request.headers
    assert "authorization" not in request.headers


@pytest.mark.parametrize("scenario", ["origin", "oversized", "redirect"])
def test_post_json_rejects_unapproved_origins_oversize_and_redirects(scenario: str) -> None:
    private_marker = "SIGNED_VALUE_MUST_NOT_LEAK"
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"location": f"/again?token={private_marker}"})

    async def exercise() -> SourceConnectorError:
        session = _session(respond)
        try:
            client = session.scoped_client(frozenset({"http://127.0.0.1:4567"}))
            url = "http://127.0.0.1:4567/api/graphql"
            payload: object = {"query": "query Test { aboutServer { version } }"}
            if scenario == "origin":
                url = "http://127.0.0.1:4568/api/graphql"
            elif scenario == "oversized":
                payload = {"query": "x" * MAX_CONNECTOR_REQUEST_BYTES}
            with pytest.raises(SourceConnectorError) as raised:
                await client.post_json(url, payload, operation=SourceConnectorOperation.SEARCH)
            assert private_marker not in str(raised.value)
            return raised.value
        finally:
            await session.aclose()

    failure = asyncio.run(exercise())
    assert failure.code is ErrorCode.SRC_CONNECTOR_FAILED
    assert len(requests) == (1 if scenario == "redirect" else 0)
    if scenario == "redirect":
        assert requests[0].method == "POST"
        assert private_marker not in str(failure)


@pytest.mark.parametrize(
    ("scenario", "stage", "reason", "http_status", "retryable", "request_count"),
    [
        ("origin", "request_validation", "REQUEST_POLICY_REJECTED", None, False, 0),
        ("signed_cover", "request_validation", "REQUEST_POLICY_REJECTED", None, False, 0),
        ("redirect_origin", "redirect_validation", "REDIRECT_POLICY_REJECTED", 302, False, 1),
        ("redirect_missing", "redirect_validation", "REDIRECT_LOCATION_MISSING", 302, False, 1),
        ("redirect_limit", "redirect_validation", "REDIRECT_LIMIT_EXCEEDED", 302, False, 6),
        ("encoding", "response_encoding", "UNSUPPORTED_CONTENT_ENCODING", 200, False, 1),
        ("length_invalid", "response_body_validation", "INVALID_CONTENT_LENGTH", 200, False, 1),
        ("length_negative", "response_body_validation", "BODY_BYTE_LIMIT_REJECTED", 200, False, 1),
        ("length_large", "response_body_validation", "BODY_BYTE_LIMIT_REJECTED", 200, False, 1),
        ("body_large", "response_body_validation", "BODY_BYTE_LIMIT_REJECTED", 200, False, 1),
        ("timeout", "network_request", "TRANSPORT_TIMEOUT_OR_NETWORK_ERROR", None, True, 1),
        ("protocol", "network_request", "TRANSPORT_HTTP_ERROR", None, False, 1),
        ("body_timeout", "response_body_read", "BODY_TIMEOUT_OR_NETWORK_ERROR", 200, True, 1),
        ("body_protocol", "response_body_read", "BODY_HTTP_ERROR", 200, False, 1),
    ],
)
def test_rejected_request_records_safe_reason_and_same_trace(
    scenario: str,
    stage: str,
    reason: str,
    http_status: int | None,
    retryable: bool,
    request_count: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_marker = "PRIVATE_DIAGNOSTIC_DATA"
    requests: list[httpx.Request] = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            if scenario == "body_timeout":
                raise httpx.ReadTimeout(private_marker)
            if scenario == "body_protocol":
                raise httpx.RemoteProtocolError(private_marker)
            yield b"x" * MAX_CONNECTOR_HTML_BYTES
            yield b"x"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if scenario == "timeout":
            raise httpx.ConnectTimeout(private_marker, request=request)
        if scenario == "protocol":
            raise httpx.RemoteProtocolError(private_marker, request=request)
        if scenario.startswith("redirect_"):
            headers: dict[str, str] = {"set-cookie": private_marker}
            if scenario == "redirect_origin":
                headers["location"] = f"https://other.example/{private_marker}"
            elif scenario == "redirect_limit":
                headers["location"] = f"/again?token={private_marker}"
            return httpx.Response(302, headers=headers)
        headers = {"content-type": "text/html", "set-cookie": private_marker}
        if scenario == "encoding":
            headers["content-encoding"] = "gzip"
        elif scenario.startswith("length_"):
            headers["content-length"] = {
                "length_invalid": private_marker,
                "length_negative": "-1",
                "length_large": str(MAX_CONNECTOR_HTML_BYTES + 1),
            }[scenario]
        if scenario in {"encoding", "body_large", "body_timeout", "body_protocol"}:
            return httpx.Response(200, headers=headers, stream=Body())
        return httpx.Response(200, headers=headers, content=private_marker.encode())

    async def exercise() -> SourceConnectorError:
        session = _session(respond)
        client = session.scoped_client(frozenset({"https://source.example"}))
        try:
            with pytest.raises(SourceConnectorError) as raised:
                url = f"https://source.example/path?token={private_marker}"
                if scenario == "origin":
                    url = f"https://other.example/path?token={private_marker}"
                if scenario == "signed_cover":
                    await client.get_image(url)
                else:
                    await client.get_html(url, operation=SourceConnectorOperation.SEARCH)
            return raised.value
        finally:
            await session.aclose()

    logger = logging.getLogger("yomira.connector.http_client")
    caplog.set_level(logging.WARNING, logger=logger.name)
    previous_propagation = logger.propagate
    logger.propagate = False
    logger.addHandler(caplog.handler)
    try:
        failure = asyncio.run(exercise())
    finally:
        logger.removeHandler(caplog.handler)
        logger.propagate = previous_propagation
    records = [record for record in caplog.records if record.name == logger.name]
    assert len(records) == 1
    serialized = JsonLogFormatter().format(records[0])
    event = json.loads(serialized)
    assert event["message"] == "source.connector.http_request_rejected"
    assert event["trace_id"] == failure.trace_id
    assert event["operation"] == failure.operation.value
    assert event["stage"] == stage
    assert event["reason"] == reason
    assert event["error_code"] == failure.code.value
    assert event["retryable"] is retryable
    if http_status is None:
        assert "http_status" not in event
    else:
        assert event["http_status"] == http_status
    if scenario in {"timeout", "protocol", "body_timeout", "body_protocol"}:
        assert event["cause_type"] in {"ConnectTimeout", "ReadTimeout", "RemoteProtocolError"}
    assert private_marker not in serialized
    assert "source.example" not in serialized
    assert "other.example" not in serialized
    assert all(field not in event for field in ("url", "body", "headers", "location"))
    assert len(requests) == request_count


def test_catalog_user_agent_is_explicit_and_does_not_change_other_requests() -> None:
    requests: list[httpx.Request] = []

    async def exercise() -> None:
        session = _session(lambda req: requests.append(req) or httpx.Response(200, content=b"{}"))
        try:
            client = session.scoped_client(frozenset({"https://catalog.example"}))
            await client.get_json(
                "https://catalog.example/manga",
                operation=SourceConnectorOperation.SEARCH,
                user_agent="Yomira/0.1.6 (local manga metadata)",
            )
            await client.get_image(
                "https://catalog.example/cover.jpg",
                user_agent="Yomira/0.1.6 (local manga metadata)",
            )
            await client.get_json(
                "https://catalog.example/manga", operation=SourceConnectorOperation.SEARCH
            )
        finally:
            await session.close()

    asyncio.run(exercise())
    assert requests[0].headers["user-agent"] == requests[1].headers["user-agent"]
    assert "user-agent" not in requests[2].headers
    assert all("cookie" not in r.headers and "authorization" not in r.headers for r in requests)


@pytest.mark.parametrize(
    "agent", ["", "x" * 129, "Yomira\r\nCookie: secret", "Yomira\x7f", "Yomira/é"]
)
def test_invalid_catalog_user_agent_fails_before_network(agent: str) -> None:
    requests: list[httpx.Request] = []

    async def exercise() -> None:
        session = _session(lambda req: requests.append(req) or httpx.Response(200))
        try:
            with pytest.raises(SourceConnectorError):
                await session.scoped_client(frozenset({"https://catalog.example"})).get_json(
                    "https://catalog.example/manga",
                    operation=SourceConnectorOperation.SEARCH,
                    user_agent=agent,
                )
        finally:
            await session.close()

    asyncio.run(exercise())
    assert requests == []


@pytest.mark.parametrize("redirect", [False, True])
def test_cover_signed_urls_are_rejected_before_following_or_reading(redirect: bool) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302, headers={"location": "https://source.example/cover.png?signature=SECRET"}
        )

    async def exercise() -> None:
        session = _session(respond)
        try:
            client = session.scoped_client(frozenset({"https://source.example"}))
            with pytest.raises(SourceConnectorError) as error:
                await client.get_image(
                    "https://source.example/cover.png" + ("" if redirect else "?signature=SECRET")
                )
            assert "SECRET" not in str(error.value)
            assert len(requests) == (1 if redirect else 0)
        finally:
            await session.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "query,allowed", [("v=2", True), ("v=2&token=secret", False), ("v=secret", False)]
)
@pytest.mark.parametrize("redirect", [True, False])
def test_tmo_cover_version_marker_is_bounded_on_requests_and_redirects(
    query: str, allowed: bool, redirect: bool
) -> None:
    requests: list[httpx.Request] = []
    url = "https://zonatmo.org/storage/proxy/cover.webp?" + query

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if redirect and len(requests) == 1:
            return httpx.Response(302, headers={"location": url})
        return httpx.Response(200, headers={"content-type": "image/webp"}, content=b"cover")

    async def exercise() -> None:
        session = _session(respond)
        try:
            client = session.scoped_client(frozenset({"https://zonatmo.org"}))
            initial = "https://zonatmo.org/storage/proxy/cover.webp" if redirect else url
            if allowed:
                assert (await client.get_image(initial)).body == b"cover"
            else:
                with pytest.raises(SourceConnectorError):
                    await client.get_image(initial)
                assert len(requests) == int(redirect)
        finally:
            await session.close()

    asyncio.run(exercise())


def test_cover_accepts_only_an_allowlisted_origin_root_referer() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "image/webp"}, content=b"image")

    async def exercise() -> None:
        session = _session(respond)
        client = session.scoped_client(
            frozenset({"https://images.example", "https://source.example"})
        )
        try:
            await client.get_image(
                "https://images.example/cover.webp",
                referer="https://source.example/",
            )
            assert requests[-1].headers["referer"] == "https://source.example/"
            assert "cookie" not in requests[-1].headers
            assert "authorization" not in requests[-1].headers

            for referer in (
                "https://source.example/chapter/1",
                "https://source.example/?signature=SECRET",
            ):
                await client.get_image("https://images.example/cover.webp", referer=referer)
                assert "referer" not in requests[-1].headers
                assert "SECRET" not in str(requests[-1].headers)

            with pytest.raises(SourceConnectorError):
                await client.get_image(
                    "https://images.example/cover.webp",
                    referer="https://unapproved.example/",
                )
            assert len(requests) == 3
        finally:
            await session.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("status_code", [403, 429])
def test_http_statuses_are_returned_without_retry_or_body_logging(status_code: int) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"source response",
        )

    async def exercise() -> None:
        session = _session(respond)
        client = session.scoped_client(frozenset({"https://source.example"}))
        try:
            response = await client.get_html(
                "https://source.example/search?q=private-query",
                operation=SourceConnectorOperation.SEARCH,
            )
        finally:
            await session.aclose()

        assert response == ConnectorHttpResponse(
            status_code=status_code,
            body=b"source response",
            final_url="https://source.example/search?q=private-query",
            content_type="text/html",
        )
        assert len(requests) == 1
        assert str(requests[0].url) == "https://source.example/search?q=private-query"
        assert "cookie" not in requests[0].headers
        assert "authorization" not in requests[0].headers
        assert "private-query" not in repr(response)
        assert b"source response" not in repr(response).encode()

    asyncio.run(exercise())


def test_disallowed_origin_is_rejected_before_network_and_redacted() -> None:
    requests: list[httpx.Request] = []

    async def exercise() -> None:
        session = _session(lambda request: requests.append(request) or httpx.Response(200))
        client = session.scoped_client(frozenset({"https://source.example"}))
        try:
            with pytest.raises(SourceConnectorError) as raised:
                await client.get_html(
                    "https://evil.example/path?token=private-token",
                    operation=SourceConnectorOperation.SEARCH,
                )
        finally:
            await session.aclose()

        assert requests == []
        assert "evil.example" not in str(raised.value)
        assert "private-token" not in str(raised.value)

    asyncio.run(exercise())


def test_external_redirect_is_rejected_before_following_and_redacted() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://evil.example/steal?token=private-token"},
        )

    async def exercise() -> None:
        session = _session(respond)
        client = session.scoped_client(frozenset({"https://source.example"}))
        try:
            with pytest.raises(SourceConnectorError) as raised:
                await client.get_html(
                    "https://source.example/start",
                    operation=SourceConnectorOperation.GET_CHAPTERS,
                )
        finally:
            await session.aclose()

        assert len(requests) == 1
        assert "evil.example" not in str(raised.value)
        assert "private-token" not in str(raised.value)

    asyncio.run(exercise())


def test_html_body_limit_is_enforced_before_returning_body() -> None:
    class OversizedBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(MAX_CONNECTOR_HTML_BYTES // (64 * 1024)):
                yield b"x" * (64 * 1024)
            yield b"x"

        async def aclose(self) -> None:
            return

    async def exercise() -> None:
        session = _session(
            lambda _: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                stream=OversizedBody(),
            )
        )
        client = session.scoped_client(frozenset({"https://source.example"}))
        try:
            with pytest.raises(SourceConnectorError) as raised:
                await client.get_html(
                    "https://source.example/large",
                    operation=SourceConnectorOperation.SEARCH,
                )
        finally:
            await session.aclose()

        assert "source.example" not in str(raised.value)
        assert str(MAX_CONNECTOR_HTML_BYTES) not in str(raised.value)

    asyncio.run(exercise())


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "gzip, identity"])
def test_encoded_html_is_rejected_before_reading_or_decompressing(encoding: str) -> None:
    requests: list[httpx.Request] = []
    chunks_read = 0
    closed = False

    class EncodedBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal chunks_read
            chunks_read += 1
            yield b"encoded response must not be read"

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-encoding": encoding},
            stream=EncodedBody(),
        )

    async def exercise() -> None:
        session = _session(respond)
        try:
            with pytest.raises(SourceConnectorError) as raised:
                await session.scoped_client(frozenset({"https://source.example"})).get_html(
                    "https://source.example/search?token=private-token",
                    operation=SourceConnectorOperation.SEARCH,
                )
            assert raised.value.retryable is False
            assert "private-token" not in str(raised.value)
        finally:
            await session.aclose()

    asyncio.run(exercise())
    assert requests[0].headers["accept-encoding"] == "identity"
    assert chunks_read == 0
    assert closed


def test_json_client_uses_json_accept_and_preserves_response_type() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            json={"data": []},
        )

    async def exercise() -> None:
        session = _session(respond)
        client = session.scoped_client(frozenset({"https://api.example"}))
        try:
            response = await client.get_json(
                "https://api.example/library?page=0",
                operation=SourceConnectorOperation.SEARCH,
            )
        finally:
            await session.aclose()

        assert response.status_code == 200
        assert response.content_type == "application/json"
        assert response.body == b'{"data":[]}'
        assert len(requests) == 1
        assert requests[0].headers["accept"] == "application/json"
        assert "cookie" not in requests[0].headers
        assert "authorization" not in requests[0].headers

    asyncio.run(exercise())


def test_set_cookie_is_not_carried_to_later_request() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        headers = {"set-cookie": "session=private-cookie; Path=/"} if len(requests) == 1 else {}
        return httpx.Response(200, headers={"content-type": "text/html", **headers}, content=b"ok")

    async def exercise() -> None:
        session = _session(respond)
        client = session.scoped_client(frozenset({"https://source.example"}))
        try:
            for path in ("first", "second"):
                await client.get_html(
                    f"https://source.example/{path}",
                    operation=SourceConnectorOperation.SEARCH,
                )
        finally:
            await session.aclose()

        assert len(requests) == 2
        assert all("cookie" not in request.headers for request in requests)

    asyncio.run(exercise())


def test_same_origin_redirect_preserves_query_and_returns_final_url() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "/result?page=3"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"chapter list",
        )

    async def exercise() -> None:
        session = _session(respond)
        client = session.scoped_client(frozenset({"https://source.example"}))
        try:
            response = await client.get_html(
                "https://source.example/start?q=one+two",
                operation=SourceConnectorOperation.GET_CHAPTERS,
            )
        finally:
            await session.aclose()

        assert [str(request.url) for request in requests] == [
            "https://source.example/start?q=one+two",
            "https://source.example/result?page=3",
        ]
        assert response.final_url == "https://source.example/result?page=3"
        assert response.body == b"chapter list"

    asyncio.run(exercise())


def test_referer_query_is_not_sent_to_a_different_allowed_origin() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"ok",
        )

    async def exercise() -> None:
        session = _session(respond)
        client = session.scoped_client(
            frozenset({"https://source.example", "https://images.example"})
        )
        try:
            await client.get_html(
                "https://images.example/page",
                operation=SourceConnectorOperation.GET_PAGES,
                referer="https://source.example/page?token=private-token",
            )
        finally:
            await session.aclose()

    asyncio.run(exercise())
    assert len(requests) == 1
    assert "referer" not in requests[0].headers
    assert "private-token" not in str(requests[0].headers)


def test_shared_per_host_limit_serializes_wrappers() -> None:
    active = 0
    peak = 0

    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            class SlowBody(httpx.AsyncByteStream):
                async def __aiter__(self) -> AsyncIterator[bytes]:
                    nonlocal active, peak
                    active += 1
                    peak = max(peak, active)
                    try:
                        await asyncio.sleep(0.01)
                        yield b"ok"
                    finally:
                        active -= 1

                async def aclose(self) -> None:
                    return

            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                stream=SlowBody(),
            )

        async def aclose(self) -> None:
            return

    async def exercise() -> None:
        session = ConnectorHttpSession(test_transport=SlowTransport())
        first = session.scoped_client(frozenset({"https://source.example"}))
        second = session.scoped_client(frozenset({"https://source.example"}))
        try:
            await asyncio.gather(
                first.get_html(
                    "https://source.example/one",
                    operation=SourceConnectorOperation.SEARCH,
                ),
                second.get_html(
                    "https://source.example/two",
                    operation=SourceConnectorOperation.SEARCH,
                ),
            )
        finally:
            await session.aclose()

    asyncio.run(exercise())
    assert peak == 1


@pytest.mark.parametrize("redirect", [False, True])
@pytest.mark.parametrize(
    "url,allowed",
    [
        ("https://oni.ntr-files.online/public/archivos/mangas/manga/cover.jpg?1707674542", True),
        ("https://oni.ntr-files.online/public/archivos/mangas/manga/cover.jpg?170767454", False),
        ("https://oni.ntr-files.online/public/archivos/mangas/manga/cover.jpg?17076745422", False),
        (
            "https://oni.ntr-files.online/public/archivos/mangas/manga/cover.jpg"
            "?1707674542&token=secret",
            False,
        ),
        (
            "https://oni.ntr-files.online/public/archivos/mangas/manga/cover.jpg?signature=secret",
            False,
        ),
        ("https://oni.ntr-files.online/private/cover.jpg?1707674542", False),
        ("https://oni.ntr-files.online/public/archivos/mangas/manga/page.jpg?1707674542", False),
        ("https://other.example/public/archivos/mangas/manga/cover.jpg?1707674542", False),
    ],
)
def test_mangaoni_cover_timestamp_is_bounded_and_public(
    url: str, allowed: bool, redirect: bool
) -> None:
    requests: list[httpx.Request] = []
    initial = "https://oni.ntr-files.online/public/archivos/mangas/manga/cover.jpg"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if redirect and len(requests) == 1:
            return httpx.Response(302, headers={"location": url})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"cover")

    async def exercise() -> None:
        session = _session(respond)
        try:
            client = session.scoped_client(frozenset({"https://oni.ntr-files.online", "https://other.example"}))
            if allowed:
                assert (await client.get_image(initial if redirect else url)).body == b"cover"
            else:
                with pytest.raises(SourceConnectorError):
                    await client.get_image(initial if redirect else url)
                assert len(requests) == int(redirect)
        finally:
            await session.close()

    asyncio.run(exercise())
