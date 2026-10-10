import asyncio
from pathlib import Path
from typing import cast
from uuid import UUID

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from support.sqlite import open_test_database

from app.factory import create_app
from app.runtime import RuntimeState
from app.settings import Settings
from app.version import API_VERSION, APP_VERSION

app = create_app(Settings(app_env="dev"))


async def _get(
    path: str,
    target_app: FastAPI = app,
    *,
    raise_app_exceptions: bool = True,
    headers: dict[str, str] | None = None,
) -> Response:
    transport = ASGITransport(app=target_app, raise_app_exceptions=raise_app_exceptions)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.get(path, headers=headers)


def get(
    path: str,
    target_app: FastAPI = app,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    return asyncio.run(_get(path, target_app, headers=headers))


def test_liveness_returns_expected_schema() -> None:
    response = get("/api/v1/system/liveness")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert UUID(response.headers["x-trace-id"]).version == 4


def test_version_returns_application_and_api_versions() -> None:
    response = get("/api/v1/system/version")

    assert response.status_code == 200
    assert response.json() == {"app_version": APP_VERSION, "api_version": API_VERSION}


def test_openapi_contains_declared_response_models() -> None:
    schema = get("/openapi.json").json()

    assert "/api/v1/system/liveness" in schema["paths"]
    assert "/api/v1/system/version" in schema["paths"]
    assert "LivenessResponse" in schema["components"]["schemas"]
    assert "VersionResponse" in schema["components"]["schemas"]


def test_unknown_route_returns_not_found() -> None:
    response = get("/api/v1/not-a-route")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "API_REQUEST_REJECTED"
    assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]


def test_valid_incoming_trace_id_is_reused_for_response() -> None:
    trace_id = "f4d4c9d4-7f3a-4b01-9db4-60e1f61ba274"

    response = get("/api/v1/system/liveness", headers={"X-Trace-ID": trace_id})

    assert response.headers["x-trace-id"] == trace_id


def test_invalid_incoming_trace_id_is_replaced_with_a_new_uuid() -> None:
    response = get(
        "/api/v1/system/liveness",
        headers={"X-Trace-ID": "invalid-trace-value"},
    )

    returned_trace_id = response.headers["x-trace-id"]
    assert returned_trace_id != "invalid-trace-value"
    assert UUID(returned_trace_id).version == 4


def test_validation_errors_are_sanitized_and_traceable() -> None:
    target_app = create_app(Settings(app_env="dev"))

    def test_validation_endpoint(value: int) -> int:
        return value

    target_app.get("/__tests/validation")(test_validation_endpoint)
    trace_id = "bbf649e8-3d3e-45b2-bd59-7f8157a7a946"
    response = get(
        "/__tests/validation?value=private-secret",
        target_app,
        headers={"X-Trace-ID": trace_id},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "API_VALIDATION_FAILED"
    assert response.json()["error"]["details"] == [{"field": "query.value", "issue": "int_parsing"}]
    assert "private-secret" not in response.text
    assert response.json()["error"]["trace_id"] == trace_id
    assert response.headers["x-trace-id"] == trace_id


def test_unhandled_errors_return_generic_message_without_exception_details() -> None:
    target_app = create_app(Settings(app_env="dev"))

    async def test_internal_error_endpoint() -> None:
        raise RuntimeError("private-secret")

    target_app.get("/__tests/internal-error")(test_internal_error_endpoint)
    response = asyncio.run(
        _get(
            "/__tests/internal-error",
            target_app,
            raise_app_exceptions=False,
        )
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "SYS_INTERNAL_ERROR"
    assert response.json()["error"]["message"] == "Ocurrió un error interno del sistema."
    assert "private-secret" not in response.text
    assert response.json()["error"]["trace_id"] == response.headers["x-trace-id"]


def test_readiness_is_not_ready_before_successful_startup() -> None:
    target_app = create_app(Settings(app_env="dev"))

    response = get("/api/v1/system/readiness", target_app)

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readiness_becomes_ready_after_runtime_initialization() -> None:
    target_app = create_app(Settings(app_env="dev"))
    runtime = cast(RuntimeState, target_app.state.runtime)
    runtime.ready = True

    response = get("/api/v1/system/readiness", target_app)

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_closes_when_shared_storage_gate_is_closed(tmp_path: Path) -> None:
    target_app = create_app(Settings(app_env="dev"))
    runtime = cast(RuntimeState, target_app.state.runtime)
    runtime.ready = True
    database = open_test_database(tmp_path)
    runtime.database = database
    database.storage_gate.close("SQLITE_FULL")

    try:
        response = get("/api/v1/system/readiness", target_app)
    finally:
        database.close()

    assert runtime.ready is True
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_openapi_is_exposed_only_in_development() -> None:
    development_app = create_app(Settings(app_env="dev"))
    production_app = create_app(Settings(app_env="prod"))

    assert get("/openapi.json", development_app).status_code == 200
    assert get("/openapi.json", production_app).status_code == 404
