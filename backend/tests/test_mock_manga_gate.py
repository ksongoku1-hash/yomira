from http.client import IncompleteRead
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from mock_manga_server import MockMangaServer, MockScenario

type ScenarioSnapshot = tuple[
    str,
    int | None,
    str,
    str,
    str,
    bytes,
    int | None,
]

_REQUIRED_SCENARIOS = frozenset(
    {
        "NORMAL",
        "SLOW",
        "TIMEOUT",
        "DISCONNECT",
        "NOT_FOUND",
        "RATE_LIMITED",
        "SERVER_ERROR",
        "HTML_AS_IMAGE",
        "CORRUPT_JPEG",
        "DUPLICATE_PAGES",
        "HUGE_IMAGE",
        "IMAGE_BOMB",
        "WRONG_PAGE_COUNT",
        "MALFORMED_METADATA",
    }
)


def _probe_path(scenario: MockScenario) -> str:
    if scenario in {
        MockScenario.DISCONNECT,
        MockScenario.HTML_AS_IMAGE,
        MockScenario.CORRUPT_JPEG,
        MockScenario.HUGE_IMAGE,
        MockScenario.IMAGE_BOMB,
    }:
        return "/api/v1/pages/mock-chapter-1/page-1"
    if scenario is MockScenario.MALFORMED_METADATA:
        return "/api/v1/series/mock-series-1"
    if scenario is MockScenario.WRONG_PAGE_COUNT:
        return "/api/v1/series/mock-series-1/chapters"
    if scenario is MockScenario.DUPLICATE_PAGES:
        return "/api/v1/chapters/mock-chapter-1/pages"
    return "/api/v1/search?q=fixture"


def _capture_snapshot(scenario: MockScenario) -> ScenarioSnapshot:
    path = _probe_path(scenario)
    timeout = 0.15 if scenario is MockScenario.TIMEOUT else 3
    with MockMangaServer(scenario=scenario) as server:
        try:
            with urlopen(f"{server.base_url}{path}", timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                content_length = response.headers.get("Content-Length", "")
                retry_after = response.headers.get("Retry-After", "")
                try:
                    body = response.read()
                except IncompleteRead as error:
                    return (
                        "incomplete",
                        response.status,
                        content_type,
                        content_length,
                        retry_after,
                        error.partial,
                        error.expected,
                    )
                return (
                    "response",
                    response.status,
                    content_type,
                    content_length,
                    retry_after,
                    body,
                    None,
                )
        except HTTPError as error:
            return (
                "http-error",
                error.code,
                error.headers.get("Content-Type", ""),
                error.headers.get("Content-Length", ""),
                error.headers.get("Retry-After", ""),
                error.read(),
                None,
            )
        except TimeoutError:
            return ("timeout", None, "", "", "", b"", None)


def test_mock_gate_covers_every_specified_server_scenario() -> None:
    assert frozenset(scenario.value for scenario in MockScenario) == _REQUIRED_SCENARIOS


@pytest.mark.parametrize("scenario", tuple(MockScenario))
def test_mock_gate_scenarios_are_repeatable_across_fresh_instances(
    scenario: MockScenario,
) -> None:
    first = _capture_snapshot(scenario)
    second = _capture_snapshot(scenario)

    assert first == second
