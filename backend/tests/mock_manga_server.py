"""Bounded, loopback-only HTTP fixture server for Manga source tests."""

from __future__ import annotations

import json
import socket
import struct
import zlib
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Barrier, BoundedSemaphore, BrokenBarrierError, Lock, Thread
from time import sleep
from types import TracebackType
from typing import cast
from urllib.parse import parse_qs, urlsplit

_MAX_WORKERS = 4
_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_SLOW_RESPONSE_SECONDS = 0.2
_TIMEOUT_RESPONSE_SECONDS = 0.8
_CONCURRENCY_PROBE_TIMEOUT_SECONDS = 2.0
_HEALTH_BODY = b'{"status":"ok"}'
_NOT_FOUND_BODY = b'{"error":"not found"}'
_HTML_AS_IMAGE_BODY = b"<!doctype html><html><body>Not an image</body></html>"
_CORRUPT_JPEG_BODY = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x02"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
type _ServerRequest = socket.socket | tuple[bytes, socket.socket]
type _FixtureResponse = tuple[int, str, bytes]
type _ExtraHeaders = tuple[tuple[str, str], ...]


class MockScenario(StrEnum):
    NORMAL = "NORMAL"
    SLOW = "SLOW"
    TIMEOUT = "TIMEOUT"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    DISCONNECT = "DISCONNECT"
    HTML_AS_IMAGE = "HTML_AS_IMAGE"
    CORRUPT_JPEG = "CORRUPT_JPEG"
    HUGE_IMAGE = "HUGE_IMAGE"
    IMAGE_BOMB = "IMAGE_BOMB"
    MALFORMED_METADATA = "MALFORMED_METADATA"
    WRONG_PAGE_COUNT = "WRONG_PAGE_COUNT"
    DUPLICATE_PAGES = "DUPLICATE_PAGES"


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", checksum)


def _small_png() -> bytes:
    header = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    pixels = bytes(
        [
            0,
            255,
            0,
            0,
            0,
            255,
            0,
            0,
            0,
            0,
            255,
            255,
            255,
            255,
        ]
    )
    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(pixels, level=9))
        + _png_chunk(b"IEND", b"")
    )


def _large_png() -> bytes:
    width = 2048
    height = 2048
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes((32, 64, 96)) * width
    compressor = zlib.compressobj(level=9)
    compressed = bytearray()
    for _ in range(height):
        compressed.extend(compressor.compress(row))
    compressed.extend(compressor.flush())
    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", bytes(compressed))
        + _png_chunk(b"IEND", b"")
    )


def _image_bomb_png() -> bytes:
    header = struct.pack(">IIBBBBB", 20_000, 20_000, 8, 2, 0, 0, 0)
    incomplete_raster = zlib.compress(b"\x00", level=9)
    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", incomplete_raster)
        + _png_chunk(b"IEND", b"")
    )


def _json_response(payload: object) -> _FixtureResponse:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return 200, "application/json; charset=utf-8", body


def _normal_response(
    path: str,
    query_string: str,
    *,
    page_count: int = 2,
    duplicate_pages: bool = False,
) -> _FixtureResponse:
    if path == "/api/v1/search":
        queries = parse_qs(query_string, keep_blank_values=True).get("q", [])
        if len(queries) != 1:
            return _json_response({"results": []})
        if queries[0] != "fixture":
            return _json_response({"results": []})
        return _json_response(
            {
                "results": [
                    {
                        "remote_id": "mock-series-1",
                        "remote_url": None,
                        "remote_title": "Mock Fixture Series",
                    }
                ]
            }
        )

    if path == "/api/v1/series/mock-series-1":
        return _json_response(
            {
                "title": "Mock Fixture Series",
                "cover_url": None,
                "author": "Yomira Test Fixture",
                "description": "Synthetic data for offline connector tests.",
                "genres": ["test", "fixture"],
                "status": "ongoing",
                "language": "es",
            }
        )

    if path == "/api/v1/series/mock-series-1/chapters":
        return _json_response(
            {
                "chapters": [
                    {
                        "remote_id": "mock-chapter-1",
                        "remote_url": None,
                        "remote_label": "1",
                        "chapter_raw_label": "1",
                        "chapter_major": 1,
                        "chapter_minor": None,
                        "chapter_variant": None,
                        "chapter_type": None,
                        "chapter_sort_key": [1, 0],
                        "volume_number": None,
                        "language": "es",
                        "display_label": "Capítulo 1",
                        "page_count": page_count,
                    }
                ]
            }
        )

    if path == "/api/v1/chapters/mock-chapter-1/pages":
        pages = [
            {"remote_reference": "/api/v1/pages/mock-chapter-1/page-1"},
            {"remote_reference": "/api/v1/pages/mock-chapter-1/page-2"},
        ]
        if duplicate_pages:
            pages.append({"remote_reference": "/api/v1/pages/mock-chapter-1/page-1"})
        return _json_response(
            {
                "pages": pages,
            }
        )

    if path in {
        "/api/v1/pages/mock-chapter-1/page-1",
        "/api/v1/pages/mock-chapter-1/page-2",
    }:
        return 200, "image/png", _small_png()

    return 404, "application/json; charset=utf-8", _NOT_FOUND_BODY


def _status_error_response(
    scenario: MockScenario,
) -> tuple[int, bytes, _ExtraHeaders] | None:
    if scenario is MockScenario.NOT_FOUND:
        return 404, _NOT_FOUND_BODY, ()
    if scenario is MockScenario.RATE_LIMITED:
        return 429, b'{"error":"rate limited"}', (("Retry-After", "1"),)
    if scenario is MockScenario.SERVER_ERROR:
        return 500, b'{"error":"internal error"}', ()
    return None


class _BoundedHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server with a fixed worker ceiling and small backlog."""

    address_family = socket.AF_INET
    daemon_threads = False
    block_on_close = True
    request_queue_size = _MAX_WORKERS

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: type[BaseHTTPRequestHandler],
        *,
        scenario: MockScenario,
        concurrency_probe_path: str | None,
    ) -> None:
        self._worker_slots = BoundedSemaphore(_MAX_WORKERS)
        self._activity_lock = Lock()
        self._active_requests = 0
        self._peak_concurrency = 0
        self.scenario = scenario
        self._concurrency_probe_path = concurrency_probe_path
        self._concurrency_probe = (
            Barrier(2) if concurrency_probe_path is not None else None
        )
        super().__init__(server_address, request_handler, bind_and_activate=True)

    @property
    def active_request_count(self) -> int:
        with self._activity_lock:
            return self._active_requests

    @property
    def peak_concurrency(self) -> int:
        with self._activity_lock:
            return self._peak_concurrency

    def request_started(self) -> None:
        with self._activity_lock:
            self._active_requests += 1
            self._peak_concurrency = max(self._peak_concurrency, self._active_requests)

    def request_finished(self) -> None:
        with self._activity_lock:
            if self._active_requests < 1:
                raise RuntimeError("Mock Manga Server request counter underflow")
            self._active_requests -= 1

    def wait_for_concurrency_probe(self, request_path: str) -> None:
        probe = self._concurrency_probe
        if probe is None or request_path != self._concurrency_probe_path:
            return
        try:
            probe.wait(timeout=_CONCURRENCY_PROBE_TIMEOUT_SECONDS)
        except BrokenBarrierError:
            # A serialized worker must fail the peak-concurrency gate without
            # leaving this test server blocked forever.
            return

    def process_request(
        self,
        request: _ServerRequest,
        client_address: tuple[str, int],
    ) -> None:
        if not self._worker_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(
        self,
        request: _ServerRequest,
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


class _MockRequestHandler(BaseHTTPRequestHandler):
    server_version = "YomiraMock/1.0"
    sys_version = ""

    def do_GET(self) -> None:
        server = cast(_BoundedHTTPServer, self.server)
        server.request_started()
        try:
            request = urlsplit(self.path)
            server.wait_for_concurrency_probe(request.path)
            if request.path == "/health":
                self._write_response(200, "application/json", _HEALTH_BODY)
                return
            if server.scenario is MockScenario.SLOW:
                sleep(_SLOW_RESPONSE_SECONDS)
            elif server.scenario is MockScenario.TIMEOUT:
                sleep(_TIMEOUT_RESPONSE_SECONDS)
            if (
                server.scenario is MockScenario.DISCONNECT
                and request.path == "/api/v1/pages/mock-chapter-1/page-1"
            ):
                body = _small_png()
                self._write_partial_response(body, body[: len(body) // 2])
                return
            if (
                server.scenario is MockScenario.MALFORMED_METADATA
                and request.path == "/api/v1/series/mock-series-1"
            ):
                status, content_type, body = _json_response(
                    {
                        "title": 42,
                        "cover_url": None,
                        "author": ["wrong", "shape"],
                        "description": None,
                        "genres": "not-an-array",
                        "status": False,
                        "language": [],
                    }
                )
                self._write_response(status, content_type, body)
                return
            if request.path == "/api/v1/pages/mock-chapter-1/page-1":
                if server.scenario is MockScenario.HTML_AS_IMAGE:
                    self._write_response(200, "image/jpeg", _HTML_AS_IMAGE_BODY)
                    return
                if server.scenario is MockScenario.CORRUPT_JPEG:
                    self._write_response(200, "image/jpeg", _CORRUPT_JPEG_BODY)
                    return
                if server.scenario is MockScenario.HUGE_IMAGE:
                    self._write_response(200, "image/png", _large_png())
                    return
                if server.scenario is MockScenario.IMAGE_BOMB:
                    self._write_response(200, "image/png", _image_bomb_png())
                    return
            status_error = _status_error_response(server.scenario)
            if status_error is not None:
                status, body, extra_headers = status_error
                self._write_response(
                    status,
                    "application/json; charset=utf-8",
                    body,
                    extra_headers=extra_headers,
                )
                return
            declared_page_count = (
                3
                if server.scenario
                in {MockScenario.WRONG_PAGE_COUNT, MockScenario.DUPLICATE_PAGES}
                else 2
            )
            status, content_type, body = _normal_response(
                request.path,
                request.query,
                page_count=declared_page_count,
                duplicate_pages=server.scenario is MockScenario.DUPLICATE_PAGES,
            )
            self._write_response(status, content_type, body)
        finally:
            server.request_finished()

    def log_message(self, format: str, *_args: object) -> None:
        _ = format
        return

    def _write_response(
        self,
        status: int,
        content_type: str,
        body: bytes,
        *,
        extra_headers: _ExtraHeaders = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _write_partial_response(self, complete_body: bytes, transmitted_body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(complete_body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(transmitted_body)
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            return
        finally:
            self.close_connection = True


class MockMangaServer:
    """Explicitly started local HTTP fixture with bounded worker resources."""

    def __init__(
        self,
        *,
        scenario: MockScenario = MockScenario.NORMAL,
        concurrency_probe_path: str | None = None,
    ) -> None:
        if type(scenario) is not MockScenario:
            raise TypeError("scenario must be a MockScenario")
        if concurrency_probe_path is not None and (
            type(concurrency_probe_path) is not str
            or not concurrency_probe_path.startswith("/")
            or concurrency_probe_path.startswith("//")
            or "?" in concurrency_probe_path
            or "#" in concurrency_probe_path
        ):
            raise ValueError("concurrency_probe_path must be a local absolute path")
        self._scenario = scenario
        self._concurrency_probe_path = concurrency_probe_path
        self._http_server: _BoundedHTTPServer | None = None
        self._serve_thread: Thread | None = None
        self._lifecycle_lock = Lock()

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return self._http_server is not None

    @property
    def worker_limit(self) -> int:
        return _MAX_WORKERS

    @property
    def active_request_count(self) -> int:
        with self._lifecycle_lock:
            server = self._http_server
            return server.active_request_count if server is not None else 0

    @property
    def peak_concurrency(self) -> int:
        with self._lifecycle_lock:
            server = self._http_server
            return server.peak_concurrency if server is not None else 0

    @property
    def bound_address(self) -> tuple[str, int]:
        with self._lifecycle_lock:
            server = self._http_server
            if server is None:
                raise RuntimeError("Mock Manga Server is not running")
            raw_host, raw_port = server.server_address[:2]
            if not isinstance(raw_host, str):
                raise RuntimeError("Mock Manga Server bound to an invalid address")
            return raw_host, raw_port

    @property
    def base_url(self) -> str:
        host, port = self.bound_address
        return f"http://{host}:{port}"

    def start(self) -> MockMangaServer:
        with self._lifecycle_lock:
            if self._http_server is not None:
                raise RuntimeError("Mock Manga Server is already running")

            server = _BoundedHTTPServer(
                ("127.0.0.1", 0),
                _MockRequestHandler,
                scenario=self._scenario,
                concurrency_probe_path=self._concurrency_probe_path,
            )
            serve_thread = Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="yomira-mock-manga-server",
                daemon=False,
            )
            try:
                serve_thread.start()
            except BaseException:
                server.server_close()
                raise

            self._http_server = server
            self._serve_thread = serve_thread
            return self

    def stop(self) -> None:
        with self._lifecycle_lock:
            server = self._http_server
            serve_thread = self._serve_thread
            if server is None and serve_thread is None:
                return
            if server is None or serve_thread is None:
                raise RuntimeError("Mock Manga Server lifecycle state is inconsistent")

            try:
                server.shutdown()
            finally:
                server.server_close()
                serve_thread.join(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
                if serve_thread.is_alive():
                    raise RuntimeError("Mock Manga Server thread did not stop")
                self._http_server = None
                self._serve_thread = None

    def __enter__(self) -> MockMangaServer:
        return self.start()

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.stop()
