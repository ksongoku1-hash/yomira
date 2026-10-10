import json
from concurrent.futures import ThreadPoolExecutor
from http.client import IncompleteRead
from io import BytesIO
from threading import Barrier
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pytest
from mock_manga_server import MockMangaServer, MockScenario
from PIL import Image


def test_mock_server_uses_loopback_ephemeral_port_and_stops_cleanly() -> None:
    server = MockMangaServer()

    with server as running_server:
        host, port = running_server.bound_address
        assert running_server is server
        assert running_server.is_running
        assert host == "127.0.0.1"
        assert port > 0
        assert running_server.base_url == f"http://127.0.0.1:{port}"

        with urlopen(f"{running_server.base_url}/health", timeout=2) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "application/json"
            assert json.load(response) == {"status": "ok"}

    assert not server.is_running
    with pytest.raises(URLError):
        urlopen(f"http://127.0.0.1:{port}/health", timeout=2)


def test_mock_server_rejects_double_start_and_allows_repeated_stop() -> None:
    server = MockMangaServer()

    server.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            server.start()
    finally:
        server.stop()
        server.stop()

    assert not server.is_running


def test_mock_servers_get_distinct_ports_while_running() -> None:
    with MockMangaServer() as first, MockMangaServer() as second:
        assert first.bound_address[0] == second.bound_address[0] == "127.0.0.1"
        assert first.bound_address[1] != second.bound_address[1]


def test_normal_mode_exposes_search_metadata_chapter_and_page_contracts() -> None:
    with MockMangaServer(scenario=MockScenario.NORMAL) as server:
        with urlopen(
            f"{server.base_url}/api/v1/search?q=fixture",
            timeout=2,
        ) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "application/json"
            assert json.load(response) == {
                "results": [
                    {
                        "remote_id": "mock-series-1",
                        "remote_url": None,
                        "remote_title": "Mock Fixture Series",
                    }
                ]
            }

        with urlopen(
            f"{server.base_url}/api/v1/series/mock-series-1",
            timeout=2,
        ) as response:
            assert json.load(response) == {
                "title": "Mock Fixture Series",
                "author": "Yomira Test Fixture",
                "description": "Synthetic data for offline connector tests.",
                "genres": ["test", "fixture"],
                "status": "ongoing",
                "language": "es",
                "cover_url": None,
            }

        with urlopen(
            f"{server.base_url}/api/v1/series/mock-series-1/chapters",
            timeout=2,
        ) as response:
            assert json.load(response) == {
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
                        "page_count": 2,
                    }
                ]
            }

        with urlopen(
            f"{server.base_url}/api/v1/chapters/mock-chapter-1/pages",
            timeout=2,
        ) as response:
            assert json.load(response) == {
                "pages": [
                    {"remote_reference": "/api/v1/pages/mock-chapter-1/page-1"},
                    {"remote_reference": "/api/v1/pages/mock-chapter-1/page-2"},
                ]
            }


def test_normal_mode_pages_are_valid_small_images_with_exact_http_lengths() -> None:
    with MockMangaServer() as server:
        for page_number in (1, 2):
            with urlopen(
                f"{server.base_url}/api/v1/pages/mock-chapter-1/page-{page_number}",
                timeout=2,
            ) as response:
                body = response.read()
                assert response.status == 200
                assert response.headers.get_content_type() == "image/png"
                assert response.headers.get("Content-Length") == str(len(body))
                assert response.headers.get("Cache-Control") == "no-store"
                with Image.open(BytesIO(body)) as image:
                    assert image.format == "PNG"
                    assert image.size == (2, 2)
                    image.verify()


def test_normal_mode_uses_exact_search_text_and_rejects_unknown_routes() -> None:
    with MockMangaServer() as server:
        with urlopen(f"{server.base_url}/api/v1/search?q=Fixture", timeout=2) as response:
            assert json.load(response) == {"results": []}

        with pytest.raises(URLError) as raised:
            urlopen(f"{server.base_url}/api/v1/unsupported", timeout=2)
        assert getattr(raised.value, "code", None) == 404


def test_slow_mode_delays_source_response_by_a_fixed_finite_interval() -> None:
    with MockMangaServer(scenario=MockScenario.SLOW) as server:
        started_at = monotonic()
        with urlopen(f"{server.base_url}/api/v1/search?q=fixture", timeout=2) as response:
            assert response.status == 200
            assert json.load(response)["results"]
        elapsed = monotonic() - started_at

    assert 0.15 <= elapsed < 2


def test_timeout_mode_times_out_client_then_releases_server_worker() -> None:
    with MockMangaServer(scenario=MockScenario.TIMEOUT) as server:
        started_at = monotonic()
        with pytest.raises((TimeoutError, URLError)):
            urlopen(f"{server.base_url}/api/v1/search?q=fixture", timeout=0.15)
        elapsed = monotonic() - started_at

        assert 0.1 <= elapsed < 0.7
        assert server.peak_concurrency == 1

        deadline = monotonic() + 1.5
        while server.active_request_count and monotonic() < deadline:
            sleep(0.01)
        assert server.active_request_count == 0

        with urlopen(f"{server.base_url}/health", timeout=1) as response:
            assert response.status == 200


def test_slow_mode_concurrency_stays_inside_fixed_worker_limit() -> None:
    with MockMangaServer(scenario=MockScenario.SLOW) as server:
        request_barrier = Barrier(server.worker_limit)

        def request_search() -> int:
            request_barrier.wait(timeout=2)
            with urlopen(f"{server.base_url}/api/v1/search?q=fixture", timeout=2) as response:
                return response.status

        with ThreadPoolExecutor(max_workers=server.worker_limit) as executor:
            futures = tuple(
                executor.submit(request_search) for _ in range(server.worker_limit)
            )
            statuses = tuple(future.result(timeout=2) for future in futures)

        assert statuses == (200, 200, 200, 200)
        assert 2 <= server.peak_concurrency <= server.worker_limit


def test_first_page_concurrency_probe_synchronizes_bounded_server_requests() -> None:
    probe_path = "/api/v1/pages/mock-chapter-1/page-1"
    with MockMangaServer(concurrency_probe_path=probe_path) as server:
        host, port = server.bound_address
        request_barrier = Barrier(2)

        def request_page(hostname: str) -> int:
            request_barrier.wait(timeout=2)
            with urlopen(f"http://{hostname}:{port}{probe_path}", timeout=3) as response:
                return response.status

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(request_page, host),
                executor.submit(request_page, host),
            )
            statuses = tuple(future.result(timeout=4) for future in futures)

        assert statuses == (200, 200)
        assert server.peak_concurrency == 2


@pytest.mark.parametrize(
    ("scenario", "expected_status", "expected_body"),
    [
        (MockScenario.NOT_FOUND, 404, {"error": "not found"}),
        (MockScenario.RATE_LIMITED, 429, {"error": "rate limited"}),
        (MockScenario.SERVER_ERROR, 500, {"error": "internal error"}),
    ],
)
def test_status_scenarios_are_exact_sanitized_and_keep_server_alive(
    scenario: MockScenario,
    expected_status: int,
    expected_body: dict[str, str],
) -> None:
    with MockMangaServer(scenario=scenario) as server:
        with pytest.raises(HTTPError) as raised:
            urlopen(f"{server.base_url}/api/v1/search?q=private-query", timeout=2)

        error = raised.value
        body = error.read()
        assert error.code == expected_status
        assert json.loads(body) == expected_body
        assert b"private-query" not in body
        if scenario is MockScenario.RATE_LIMITED:
            assert error.headers.get("Retry-After") == "1"

        with urlopen(f"{server.base_url}/health", timeout=1) as response:
            assert response.status == 200


def test_disconnect_scenario_truncates_page_body_and_releases_worker() -> None:
    with MockMangaServer(scenario=MockScenario.DISCONNECT) as server:
        with urlopen(
            f"{server.base_url}/api/v1/series/mock-series-1",
            timeout=2,
        ) as response:
            assert response.status == 200
            assert json.load(response)["title"] == "Mock Fixture Series"

        with urlopen(
            f"{server.base_url}/api/v1/pages/mock-chapter-1/page-1",
            timeout=2,
        ) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "image/png"
            declared_length = int(response.headers["Content-Length"])
            with pytest.raises(IncompleteRead) as raised:
                response.read()

        partial = raised.value.partial
        assert len(partial) == declared_length // 2
        assert raised.value.expected == declared_length - len(partial)

        deadline = monotonic() + 1
        while server.active_request_count and monotonic() < deadline:
            sleep(0.01)
        assert server.active_request_count == 0
        with urlopen(f"{server.base_url}/health", timeout=1) as response:
            assert response.status == 200


@pytest.mark.parametrize(
    "scenario",
    [MockScenario.HTML_AS_IMAGE, MockScenario.CORRUPT_JPEG],
)
def test_invalid_image_scenarios_return_unreadable_jpeg_payloads(
    scenario: MockScenario,
) -> None:
    with MockMangaServer(scenario=scenario) as server:
        with urlopen(
            f"{server.base_url}/api/v1/pages/mock-chapter-1/page-1",
            timeout=2,
        ) as response:
            body = response.read()
            assert response.status == 200
            assert response.headers.get_content_type() == "image/jpeg"
            assert response.headers.get("Content-Length") == str(len(body))

    if scenario is MockScenario.HTML_AS_IMAGE:
        assert body.lower().startswith(b"<!doctype html>")
    else:
        assert body.startswith(b"\xff\xd8")

    with pytest.raises(OSError):
        with Image.open(BytesIO(body)) as image:
            image.load()


def test_huge_image_scenario_serves_valid_high_dimension_png_with_bounded_body() -> None:
    with MockMangaServer(scenario=MockScenario.HUGE_IMAGE) as server:
        with urlopen(
            f"{server.base_url}/api/v1/pages/mock-chapter-1/page-1",
            timeout=5,
        ) as response:
            body = response.read()
            assert response.status == 200
            assert response.headers.get_content_type() == "image/png"
            assert response.headers.get("Content-Length") == str(len(body))

    assert len(body) < 128 * 1024
    with Image.open(BytesIO(body)) as image:
        assert image.format == "PNG"
        assert image.size == (2048, 2048)
        image.load()


def test_image_bomb_scenario_is_rejected_at_header_before_pixel_decode() -> None:
    with MockMangaServer(scenario=MockScenario.IMAGE_BOMB) as server:
        with urlopen(
            f"{server.base_url}/api/v1/pages/mock-chapter-1/page-1",
            timeout=2,
        ) as response:
            body = response.read()
            assert response.status == 200
            assert response.headers.get_content_type() == "image/png"
            assert response.headers.get("Content-Length") == str(len(body))

    assert len(body) < 1024
    with pytest.raises(Image.DecompressionBombError):
        Image.open(BytesIO(body))


def test_malformed_metadata_scenario_returns_invalid_field_types_with_http_200() -> None:
    with MockMangaServer(scenario=MockScenario.MALFORMED_METADATA) as server:
        with urlopen(
            f"{server.base_url}/api/v1/series/mock-series-1",
            timeout=2,
        ) as response:
            metadata = json.load(response)
            assert response.status == 200
            assert response.headers.get_content_type() == "application/json"

        assert metadata == {
            "title": 42,
            "cover_url": None,
            "author": ["wrong", "shape"],
            "genres": "not-an-array",
            "description": None,
            "status": False,
            "language": [],
        }
        assert type(metadata["title"]) is int
        assert type(metadata["author"]) is list
        assert type(metadata["genres"]) is str
        assert type(metadata["status"]) is bool
        assert type(metadata["language"]) is list


def test_wrong_page_count_scenario_disagrees_with_page_reference_count() -> None:
    with MockMangaServer(scenario=MockScenario.WRONG_PAGE_COUNT) as server:
        with urlopen(
            f"{server.base_url}/api/v1/series/mock-series-1/chapters",
            timeout=2,
        ) as response:
            chapter_list = json.load(response)

        with urlopen(
            f"{server.base_url}/api/v1/chapters/mock-chapter-1/pages",
            timeout=2,
        ) as response:
            page_list = json.load(response)

        declared_page_count = chapter_list["chapters"][0]["page_count"]
        actual_page_count = len(page_list["pages"])
        assert declared_page_count == 3
        assert actual_page_count == 2
        assert declared_page_count != actual_page_count


def test_duplicate_page_scenario_preserves_order_and_occurrences() -> None:
    with MockMangaServer(scenario=MockScenario.DUPLICATE_PAGES) as server:
        with urlopen(
            f"{server.base_url}/api/v1/series/mock-series-1/chapters",
            timeout=2,
        ) as response:
            chapter_list = json.load(response)

        with urlopen(
            f"{server.base_url}/api/v1/chapters/mock-chapter-1/pages",
            timeout=2,
        ) as response:
            page_list = json.load(response)

        references = tuple(
            page["remote_reference"] for page in page_list["pages"]
        )
        assert references == (
            "/api/v1/pages/mock-chapter-1/page-1",
            "/api/v1/pages/mock-chapter-1/page-2",
            "/api/v1/pages/mock-chapter-1/page-1",
        )
        assert len(set(references)) == 2
        assert chapter_list["chapters"][0]["page_count"] == len(references) == 3
