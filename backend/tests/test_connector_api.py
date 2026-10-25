import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from support.sqlite import open_test_database

from app.connectors.builtins import create_connector_registry
from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.http_client import ConnectorHttpSession
from app.connectors.mangadex import MangaDexCandidate, MangaDexConnector, MangaDexLookup
from app.connectors.metadata import ConnectorCapability, ConnectorMetadata
from app.connectors.mock import MockConnector
from app.connectors.registry import ConnectorRegistration, ConnectorRegistry
from app.connectors.sandbox import ConnectorHostContext
from app.connectors.search import ConnectorSearchQuery, ConnectorSearchResult
from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.errors import ErrorCode
from app.factory import create_app
from app.persistence.database import SQLiteDatabase
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.series import SeriesRow
from app.persistence.models.series_source import SeriesSourceRow
from app.persistence.models.source import SourceRow
from app.runtime import RuntimeState
from app.services.cover_assets import CoverAssetService
from app.services.jobs import DownloadJobService
from app.settings import Settings

RuntimeMode = Literal["dev", "test", "prod"]


def _app(environment: RuntimeMode = "test") -> FastAPI:
    return create_app(Settings(app_env=environment))


async def _get(target_app: FastAPI, path: str) -> Response:
    transport = ASGITransport(app=target_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.get(path)


def get(target_app: FastAPI, path: str) -> Response:
    return asyncio.run(_get(target_app, path))


async def _post(target_app: FastAPI, path: str, body: object = None) -> Response:
    transport = ASGITransport(app=target_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.post(path, json=body)


class _MetadataCatalog(MockConnector):
    @property
    def metadata(self) -> ConnectorMetadata:
        return replace(
            super().metadata,
            capabilities=frozenset({ConnectorCapability.SEARCH, ConnectorCapability.METADATA}),
        )


def _ready_catalog_app(
    tmp_path: Path,
    *,
    network_origins: frozenset[str] = frozenset(),
) -> tuple[FastAPI, SQLiteDatabase]:
    target_app = create_app(Settings(app_env="test", data_root=tmp_path))
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    runtime = cast(RuntimeState, target_app.state.runtime)
    assert runtime.secret_vault is not None
    catalog = _MetadataCatalog()
    runtime.database = database
    runtime.download_job_service = DownloadJobService(database)
    runtime.connector_registry = ConnectorRegistry(
        context=ConnectorHostContext(secret_vault=runtime.secret_vault),
        registrations=[
            ConnectorRegistration(
                metadata=catalog.metadata,
                factory=lambda _: catalog,
                network_origins=network_origins,
            )
        ],
    )
    return target_app, database


def test_library_import_supports_metadata_only_source_without_creating_jobs(tmp_path: Path) -> None:
    target_app, database = _ready_catalog_app(tmp_path)
    path = "/api/v1/connectors/yomira.mock/series/mock-series-1/library-series"
    try:
        first = asyncio.run(_post(target_app, path))
        second = asyncio.run(_post(target_app, path))
        assert first.status_code == second.status_code == 200
        first_data = first.json()["data"]
        assert first_data["series_id"] == second.json()["data"]["series_id"]
        assert first_data["created"] is True
        assert second.json()["data"]["created"] is False
        assert first_data["series_title"] == "Mock Fixture Series"
        assert UUID(first_data["series_id"]).version == 4
        assert first.json()["meta"] == {
            "connector_id": "yomira.mock",
            "remote_series_id": "mock-series-1",
        }
        assert first.headers["x-trace-id"]
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(SeriesRow)) == 1
            assert session.scalar(select(func.count()).select_from(SeriesSourceRow)) == 1
            assert session.scalar(select(func.count()).select_from(SourceRow)) == 1
            assert session.scalar(select(func.count()).select_from(ChapterRow)) == 0
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
    finally:
        database.shutdown()


def test_library_import_remains_successful_when_optional_cover_fails(tmp_path: Path) -> None:
    target_app, database = _ready_catalog_app(tmp_path)

    class FailingCoverService:
        async def ensure_cover(self, _series_id: UUID) -> None:
            raise RuntimeError("private-cover-url")

    runtime = cast(RuntimeState, target_app.state.runtime)
    runtime.cover_asset_service = cast(CoverAssetService, FailingCoverService())
    try:
        response = asyncio.run(
            _post(target_app, "/api/v1/connectors/yomira.mock/series/mock-series-1/library-series")
        )
        assert response.status_code == 200
        assert response.json()["data"]["created"] is True
        assert "private-cover-url" not in response.text
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(SeriesRow)) == 1
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
    finally:
        database.shutdown()


def test_library_import_failure_is_sanitized_and_does_not_persist(tmp_path: Path) -> None:
    target_app, database = _ready_catalog_app(tmp_path)
    try:
        failed = asyncio.run(
            _post(
                target_app,
                "/api/v1/connectors/yomira.mock/series/private-token-reference/library-series",
            )
        )
        assert failed.status_code == 502
        assert failed.json()["error"]["code"] == "SRC_CONNECTOR_FAILED"
        assert failed.json()["error"]["trace_id"] == failed.headers["x-trace-id"]
        assert "private-token-reference" not in failed.text
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(SourceRow)) == 0
            assert session.scalar(select(func.count()).select_from(SeriesRow)) == 0
    finally:
        database.shutdown()


@pytest.mark.parametrize(
    ("connector_id", "remote_id", "status_code", "error_code"),
    [
        ("private-connector", "mock-series-1", 404, "API_REQUEST_REJECTED"),
        ("yomira.mock", "x" * 513, 422, "API_VALIDATION_FAILED"),
    ],
)
def test_library_import_validates_remote_identity_before_persistence(
    tmp_path: Path, connector_id: str, remote_id: str, status_code: int, error_code: str
) -> None:
    target_app, database = _ready_catalog_app(tmp_path)
    try:
        response = asyncio.run(
            _post(
                target_app, f"/api/v1/connectors/{connector_id}/series/{remote_id}/library-series"
            )
        )
        assert response.status_code == status_code
        assert response.json()["error"]["code"] == error_code
        assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]
        assert remote_id not in response.text
        assert "private-connector" not in response.text
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(SourceRow)) == 0
            assert session.scalar(select(func.count()).select_from(SeriesRow)) == 0
    finally:
        database.shutdown()


def test_library_import_normalization_failure_is_sanitized_before_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_app, database = _ready_catalog_app(tmp_path)

    def broken_metadata(_self: MockConnector, _raw: object) -> object:
        raise ValueError("private-cookie-or-url")

    monkeypatch.setattr(MockConnector, "normalize_metadata", broken_metadata)
    try:
        response = asyncio.run(
            _post(target_app, "/api/v1/connectors/yomira.mock/series/mock-series-1/library-series")
        )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "SYS_INTERNAL_ERROR"
        assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]
        assert "private-cookie-or-url" not in response.text
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(SourceRow)) == 0
            assert session.scalar(select(func.count()).select_from(SeriesRow)) == 0
    finally:
        database.shutdown()


def test_download_jobs_reject_missing_pages_before_fetching_or_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_app, database = _ready_catalog_app(tmp_path)

    async def forbidden_metadata(_self: MockConnector, _reference: str) -> object:
        raise AssertionError("Metadata must not be fetched for a source without PAGE_LIST")

    monkeypatch.setattr(MockConnector, "get_series", forbidden_metadata)
    try:
        failed = asyncio.run(
            _post(
                target_app,
                "/api/v1/connectors/yomira.mock/series/mock-series-1/download-jobs",
                {"chapter_remote_ids": ["mock-chapter-1"]},
            )
        )
        assert failed.status_code == 501
        assert failed.json()["error"]["code"] == "SRC_CAPABILITY_UNSUPPORTED"
        assert failed.json()["error"]["retryable"] is False
        with database.read_session() as session:
            for model in (SourceRow, SeriesRow, DownloadJobRow):
                assert session.scalar(select(func.count()).select_from(model)) == 0
    finally:
        database.shutdown()


def test_mock_search_returns_the_stable_connector_envelope() -> None:
    target_app = _app()

    response = get(
        target_app,
        "/api/v1/connectors/yomira.mock/search?query=fixture",
    )

    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {
                "remote_id": "mock-series-1",
                "remote_url": None,
                "remote_title": "Mock Fixture Series",
                "cover_url": None,
                "source_label": None,
            }
        ],
        "meta": {"connector_id": "yomira.mock", "query": "fixture"},
    }
    assert response.headers["x-trace-id"]


def test_search_serializes_a_dynamic_source_label(monkeypatch: pytest.MonkeyPatch) -> None:
    async def labelled_search(
        _self: MockConnector,
        _query: ConnectorSearchQuery,
    ) -> tuple[ConnectorSearchResult, ...]:
        return (
            ConnectorSearchResult(
                "series-1",
                None,
                "Series 1",
                source_label="Extension Name (en)",
            ),
        )

    monkeypatch.setattr(MockConnector, "search", labelled_search)
    response = get(_app(), "/api/v1/connectors/yomira.mock/search?query=series")

    assert response.status_code == 200
    assert response.json()["data"][0]["source_label"] == "Extension Name (en)"


def test_connector_list_reports_registered_metadata_without_resolving_connectors() -> None:
    target_app = _app()
    runtime = cast(RuntimeState, target_app.state.runtime)
    registry = runtime.connector_registry
    assert registry is not None

    response = get(target_app, "/api/v1/connectors")

    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {
                "connector_id": "yomira.metadata.mangadex",
                "connector_version": "1.0.0",
                "capabilities": ["CHAPTER_LIST", "METADATA", "PAGE_LIST", "SEARCH"],
            },
            {
                "connector_id": "yomira.mock",
                "connector_version": "1.0.0",
                "capabilities": ["CHAPTER_LIST", "METADATA", "PAGE_LIST", "SEARCH"],
            },
        ]
    }
    assert registry.connector_ids == (
        "yomira.metadata.mangadex",
        "yomira.mock",
    )


def test_connector_list_requires_initialized_registry() -> None:
    target_app = _app()
    runtime = cast(RuntimeState, target_app.state.runtime)
    runtime.connector_registry = None

    response = get(target_app, "/api/v1/connectors")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SYS_INTERNAL_ERROR"


def test_mangadex_search_finds_titles_and_aliases_and_offers_chapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = MangaDexCandidate(
        "e9173a7b-d763-43ac-866f-aa5b8240a37c",
        "Someone Hertz",
        ("Samuwan Herutsu",),
        2025,
        ConnectorSeriesMetadata(title="Someone Hertz", author="Yamano Ei"),
    )

    async def lookup(_self: MangaDexConnector, _query: ConnectorSearchQuery) -> MangaDexLookup:
        return MangaDexLookup(1, (candidate,))

    async def catalog_chapters(_self: MangaDexConnector, reference: str) -> tuple[()]:
        assert reference == candidate.remote_id
        return ()

    monkeypatch.setattr(MangaDexConnector, "get_chapters", catalog_chapters)
    monkeypatch.setattr(MangaDexConnector, "lookup", lookup)
    target_app = _app()
    runtime = cast(RuntimeState, target_app.state.runtime)
    assert runtime.secret_vault is not None
    http = ConnectorHttpSession()
    runtime.connector_registry = create_connector_registry(
        Settings(app_env="test"), runtime.secret_vault, http_client_factory=http.scoped_client
    )
    for query in ("Someone%20Hertz", "Samuwan%20Herutsu"):
        response = get(
            target_app, f"/api/v1/connectors/yomira.metadata.mangadex/search?query={query}"
        )
        assert response.status_code == 200
        assert response.json()["data"] == [
            {
                "remote_id": candidate.remote_id,
                "remote_url": candidate.remote_url,
                "remote_title": "Someone Hertz",
                "cover_url": None,
                "source_label": "MangaDex",
            }
        ]
    chapters = get(
        target_app,
        f"/api/v1/connectors/yomira.metadata.mangadex/series/{candidate.remote_id}/chapters",
    )
    assert chapters.status_code == 200
    assert chapters.json()["data"] == []
    asyncio.run(http.close())


def test_search_preserves_query_whitespace_exactly() -> None:
    target_app = _app()

    response = get(
        target_app,
        "/api/v1/connectors/yomira.mock/search?query=%20fixture%20",
    )

    assert response.status_code == 200
    assert response.json() == {
        "data": [],
        "meta": {"connector_id": "yomira.mock", "query": " fixture "},
    }


def test_connector_cover_returns_only_an_allowlisted_public_https_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_app, database = _ready_catalog_app(
        tmp_path,
        network_origins=frozenset({"https://covers.example"}),
    )

    async def get_series(_self: MockConnector, _reference: str) -> object:
        return {"title": "Fixture", "cover_url": "https://covers.example/cover.jpg"}

    monkeypatch.setattr(MockConnector, "get_series", get_series)
    path = "/api/v1/connectors/yomira.mock/series/fixture%2Fseries/cover"
    try:
        response = get(target_app, path)
        assert response.status_code == 200
        assert response.json() == {
            "data": {"cover_url": "https://covers.example/cover.jpg"},
            "meta": {
                "connector_id": "yomira.mock",
                "remote_series_id": "fixture/series",
            },
        }
        assert response.headers["x-trace-id"]

        async def unsafe_series(_self: MockConnector, _reference: str) -> object:
            return {"title": "Fixture", "cover_url": "https://evil.example/cover.jpg"}

        monkeypatch.setattr(MockConnector, "get_series", unsafe_series)
        rejected = get(target_app, path)
        assert rejected.status_code == 200
        assert rejected.json()["data"]["cover_url"] is None
    finally:
        database.shutdown()


@pytest.mark.parametrize("page_budget", [0, 21])
def test_search_rejects_page_budget_outside_the_bounded_contract(page_budget: int) -> None:
    response = get(
        _app(),
        f"/api/v1/connectors/yomira.mock/search?query=fixture&page_budget={page_budget}",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "API_VALIDATION_FAILED"
    assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]


def test_search_passes_explicit_page_budget_to_the_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_budgets: list[int] = []
    original_search = MockConnector.search

    async def record_search(
        self: MockConnector, query: ConnectorSearchQuery
    ) -> tuple[ConnectorSearchResult, ...]:
        requested_budgets.append(query.page_budget)
        return await original_search(self, query)

    monkeypatch.setattr(MockConnector, "search", record_search)
    response = get(
        _app(),
        "/api/v1/connectors/yomira.mock/search?query=fixture&page_budget=20",
    )

    assert response.status_code == 200
    assert requested_budgets == [20]


@pytest.mark.parametrize("query", ["", "x" * 201], ids=["empty", "excessive"])
def test_search_rejects_empty_or_excessive_query_with_sanitized_trace(
    query: str,
) -> None:
    target_app = _app()
    response = get(
        target_app,
        f"/api/v1/connectors/yomira.mock/search?query={query}",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "API_VALIDATION_FAILED"
    assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]


def test_chapter_list_returns_the_normalized_connector_fields() -> None:
    target_app = _app()

    response = get(
        target_app,
        "/api/v1/connectors/yomira.mock/series/mock-series-1/chapters",
    )

    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {
                "remote_id": "mock-chapter-1",
                "display_label": "Capítulo 1",
                "remote_label": "1",
                "chapter_raw_label": "1",
                "chapter_sort_key": [1, 0],
                "language": "es",
            }
        ],
        "meta": {
            "connector_id": "yomira.mock",
            "remote_series_id": "mock-series-1",
        },
    }


def test_chapter_list_accepts_encoded_slashes_in_remote_series_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.connectors.mock import MockConnector

    async def get_chapters(
        _self: MockConnector,
        remote_series_reference: str,
    ) -> tuple[()]:
        assert remote_series_reference == "fixture/series"
        return ()

    monkeypatch.setattr(MockConnector, "get_chapters", get_chapters)
    target_app = _app()

    response = get(
        target_app,
        "/api/v1/connectors/yomira.mock/series/fixture%2Fseries/chapters",
    )

    assert response.status_code == 200
    assert response.json() == {
        "data": [],
        "meta": {
            "connector_id": "yomira.mock",
            "remote_series_id": "fixture/series",
        },
    }


def test_unknown_connector_is_rejected_without_echoing_the_identifier() -> None:
    target_app = _app()

    response = get(
        target_app,
        "/api/v1/connectors/private-connector/search?query=fixture",
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "API_REQUEST_REJECTED"
    assert "private-connector" not in response.text
    assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]


def test_unknown_remote_series_is_a_sanitized_traced_connector_error() -> None:
    target_app = _app()

    response = get(
        target_app,
        "/api/v1/connectors/yomira.mock/series/private-series/chapters",
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "SRC_CONNECTOR_FAILED"
    assert response.json()["error"]["message"] == "La operación del conector falló."
    assert "private-series" not in response.text
    assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]


def test_connector_failure_does_not_expose_exception_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.connectors.mock import MockConnector

    async def fail_search(_self: MockConnector, _query: object) -> tuple[()]:
        raise SourceConnectorError(
            code=ErrorCode.SRC_CONNECTOR_FAILED,
            operation=SourceConnectorOperation.SEARCH,
            retryable=False,
        ) from RuntimeError("private-cookie-value")

    monkeypatch.setattr(MockConnector, "search", fail_search)
    target_app = _app()

    response = get(
        target_app,
        "/api/v1/connectors/yomira.mock/search?query=fixture",
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "SRC_CONNECTOR_FAILED"
    assert response.json()["error"]["message"] == "La operación del conector falló."
    assert "private-cookie-value" not in response.text
    assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]


def test_connector_failure_json_log_preserves_registered_identity_and_operation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_app = _app()
    response = get(
        target_app,
        "/api/v1/connectors/yomira.mock/series/private-series-SECRET/chapters"
        "?cookie=SECRET&url=https%3A%2F%2Fprivate.invalid%2Fimage.jpg%3Ftoken%3DSECRET",
    )
    captured = capsys.readouterr().err
    events = [json.loads(line) for line in captured.splitlines() if line]
    failed = [event for event in events if event["message"] == "api.connector_operation_failed"]
    assert response.status_code == 502
    assert len(failed) == 1
    event = failed[0]
    assert event["operation"] == "GET_CHAPTERS"
    assert event["connector_id"] == "yomira.mock"
    assert event["error_code"] == "SRC_CONNECTOR_FAILED"
    assert event["status_code"] == 502
    assert event["trace_id"] == response.headers["x-trace-id"]
    assert "SECRET" not in captured + response.text
    assert "private.invalid" not in captured + response.text
    assert "private-series" not in captured + response.text
    assert "https://" not in captured
    assert "url=" not in captured and "cookie=" not in captured


def test_connector_failure_json_log_omits_unregistered_path_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_app = _app()

    @target_app.get("/diagnostic-fixture/{connector_id}/series/{remote_series_id:path}")
    async def fail_source() -> None:
        raise SourceConnectorError(
            code=ErrorCode.SRC_CONNECTOR_FAILED,
            operation=SourceConnectorOperation.GET_SERIES,
            retryable=False,
        ) from RuntimeError("https://private.invalid/chapter?token=SECRET")

    response = get(
        target_app,
        "/diagnostic-fixture/private-connector-SECRET/series/private-series-SECRET?cookie=SECRET",
    )
    captured = capsys.readouterr().err
    events = [json.loads(line) for line in captured.splitlines() if line]
    failed = [event for event in events if event["message"] == "api.connector_operation_failed"]
    assert response.status_code == 502
    assert len(failed) == 1
    assert failed[0]["operation"] == "GET_SERIES"
    assert "connector_id" not in failed[0]
    assert failed[0]["trace_id"] == response.headers["x-trace-id"]
    assert "SECRET" not in captured + response.text
    assert "private.invalid" not in captured + response.text
    assert "private-connector" not in captured + response.text
    assert "private-series" not in captured + response.text
    assert "https://" not in captured


@pytest.mark.parametrize("environment", ["test"])
def test_mock_connector_is_available_only_for_local_development_modes(
    environment: Literal["dev", "test"],
) -> None:
    runtime = cast(RuntimeState, _app(environment).state.runtime)

    assert runtime.connector_registry is not None
    assert runtime.connector_registry.connector_ids == (
        "yomira.metadata.mangadex",
        "yomira.mock",
    )


def test_production_runtime_defers_fmd2_until_startup() -> None:
    runtime = cast(RuntimeState, _app("prod").state.runtime)

    assert runtime.connector_registry is None
