import logging
import re
from collections.abc import Mapping, Sequence
from time import perf_counter
from typing import cast
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.connectors.builtins import create_connector_registry
from app.connectors.errors import SourceConnectorError
from app.errors import ApiErrorBody, ApiErrorDetail, ApiErrorEnvelope, ErrorCode
from app.frontend_distribution import (
    FrontendDistributionError,
    register_frontend_routes,
    resolve_frontend_directory,
)
from app.lifecycle import application_lifespan
from app.logging_config import configure_logging
from app.routes.cbr_exports import router as cbr_exports_router
from app.routes.connectors import router as connectors_router
from app.routes.jobs import cancel_router as download_cancellation_router
from app.routes.jobs import router as jobs_router
from app.routes.library import router as library_router
from app.routes.reader import router as reader_router
from app.routes.recommendations import router as recommendations_router
from app.routes.settings import router as settings_router
from app.routes.system import router as system_router
from app.routes.updates import router as updates_router
from app.routes.usb_exports import router as usb_exports_router
from app.runtime import RuntimeState
from app.secrets.vault import WindowsDpapiSecretVault
from app.services.app_updates import AppUpdater
from app.services.cbr_builder import CbrExportFailure
from app.services.cover_assets import CoverAssetError
from app.services.jobs import AmbiguousLibrarySeries
from app.settings import Settings, SettingsValidationError, load_settings
from app.tracing import (
    TRACE_ID_HEADER,
    activate_trace_id,
    reset_trace_id,
    resolve_request_trace_id,
)
from app.version import APP_VERSION

logger = logging.getLogger("yomira.api")


def _trace_id_for_error(request: Request) -> str:
    return resolve_request_trace_id(request)


def _sanitize_validation_details(
    validation_errors: Sequence[Mapping[str, object]],
) -> list[ApiErrorDetail]:
    details: list[ApiErrorDetail] = []
    for validation_error in validation_errors[:20]:
        raw_location = validation_error.get("loc", ())
        location: Sequence[object] = (
            cast(Sequence[object], raw_location) if isinstance(raw_location, (tuple, list)) else ()
        )

        path_parts: list[str] = []
        for part in location[:8]:
            if isinstance(part, int):
                path_parts.append("index")
            elif isinstance(part, str):
                path_parts.append(re.sub(r"[^A-Za-z0-9_.-]", "_", part)[:24])
        field = ".".join(path_parts)[:128] or "request"

        raw_issue = validation_error.get("type", "invalid")
        issue = raw_issue if isinstance(raw_issue, str) else "invalid"
        issue = re.sub(r"[^A-Za-z0-9_.-]", "_", issue)[:64] or "invalid"
        details.append(ApiErrorDetail(field=field, issue=issue))
    return details


def _error_response(
    request: Request,
    *,
    code: ErrorCode,
    message: str,
    status_code: int,
    retryable: bool = False,
    details: list[ApiErrorDetail] | None = None,
) -> JSONResponse:
    trace_id = _trace_id_for_error(request)
    envelope = ApiErrorEnvelope(
        error=ApiErrorBody(
            code=code,
            message=message,
            trace_id=trace_id,
            retryable=retryable,
            details=details,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json", exclude_none=True),
        headers={"X-Trace-ID": trace_id},
    )


async def _validation_error_handler(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    if not isinstance(exception, RequestValidationError):
        raise exception

    logger.warning(
        "api.request_validation_failed",
        extra={
            "error_code": ErrorCode.API_VALIDATION_FAILED.value,
            "retryable": False,
            "status_code": 422,
        },
    )
    return _error_response(
        request,
        code=ErrorCode.API_VALIDATION_FAILED,
        message="La solicitud no cumple el contrato de la API.",
        status_code=422,
        details=_sanitize_validation_details(exception.errors()),
    )


async def _http_error_handler(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    if not isinstance(exception, StarletteHTTPException):
        raise exception

    if 400 <= exception.status_code < 500:
        code = ErrorCode.API_REQUEST_REJECTED
        message = "La API rechazó la solicitud."
    else:
        code = ErrorCode.SYS_INTERNAL_ERROR
        message = "Ocurrió un error interno del sistema."

    logger.warning(
        "api.http_error",
        extra={
            "error_code": code.value,
            "retryable": False,
            "status_code": exception.status_code,
        },
    )
    return _error_response(
        request,
        code=code,
        message=message,
        status_code=exception.status_code,
    )


async def _library_conflict_error_handler(request: Request, exception: Exception) -> JSONResponse:
    if not isinstance(exception, AmbiguousLibrarySeries):
        raise exception
    return _error_response(
        request,
        code=ErrorCode.API_REQUEST_REJECTED,
        message="Hay varias fichas compatibles en la biblioteca; "
        "especifica la edición antes de añadir capítulos.",
        status_code=409,
    )


async def _unhandled_error_handler(request: Request, exception: Exception) -> JSONResponse:
    trace_id = _trace_id_for_error(request)
    logger.exception(
        "api.unhandled_exception",
        extra={
            "error_code": ErrorCode.SYS_INTERNAL_ERROR.value,
            "retryable": False,
            "trace_id": trace_id,
            "status_code": 500,
        },
    )
    return _error_response(
        request,
        code=ErrorCode.SYS_INTERNAL_ERROR,
        message="Ocurrió un error interno del sistema.",
        status_code=500,
    )


async def _source_connector_error_handler(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    if not isinstance(exception, SourceConnectorError):
        raise exception

    if exception.code is ErrorCode.SRC_CAPABILITY_UNSUPPORTED:
        status_code = 501
    elif exception.code is ErrorCode.SRC_CIRCUIT_OPEN or exception.retryable:
        status_code = 503
    else:
        status_code = 502

    runtime = cast(RuntimeState, request.app.state.runtime)
    registry = runtime.connector_registry
    connector_id = request.path_params.get("connector_id")
    registered_connector_id = (
        connector_id
        if isinstance(connector_id, str)
        and registry is not None
        and connector_id in registry.connector_ids
        else None
    )
    logger.warning(
        "api.connector_operation_failed",
        extra={
            "error_code": exception.code.value,
            "operation": exception.operation.value,
            "connector_id": registered_connector_id,
            "retryable": exception.retryable,
            "status_code": status_code,
            "trace_id": _trace_id_for_error(request),
        },
    )
    return _error_response(
        request,
        code=exception.code,
        message=str(exception),
        status_code=status_code,
        retryable=exception.retryable,
    )


async def _cbr_export_error_handler(
    request: Request,
    exception: Exception,
) -> JSONResponse:
    if not isinstance(exception, CbrExportFailure):
        raise exception
    logger.error(
        "api.cbr_export_failed",
        extra={
            "error_code": exception.code.value,
            "retryable": exception.retryable,
            "stage": exception.stage,
            "cause_type": exception.cause_type,
            "status_code": exception.status_code,
            "trace_id": exception.trace_id,
        },
    )
    return _error_response(
        request,
        code=exception.code,
        message=str(exception),
        status_code=exception.status_code,
        retryable=exception.retryable,
    )


async def _cover_error_handler(request: Request, exception: Exception) -> JSONResponse:
    if not isinstance(exception, CoverAssetError):
        raise exception
    status_code = 503 if exception.retryable else 502
    logger.warning(
        "series.cover_operation_failed",
        extra={
            "error_code": exception.code.value,
            "stage": "cover_processing",
            "trace_id": _trace_id_for_error(request),
            "retryable": exception.retryable,
            "status_code": status_code,
        },
    )
    return _error_response(
        request,
        code=exception.code,
        message=str(exception),
        status_code=status_code,
        retryable=exception.retryable,
    )


def _trusted_local_request(request: Request, settings: Settings) -> bool:
    hosts = {"localhost", "127.0.0.1", "::1", settings.api_host}
    if settings.app_env == "test":
        hosts.add("testserver")
    try:
        target = urlsplit("http://" + request.headers.get("host", ""))
        port = target.port or (443 if request.url.scheme == "https" else 80)
        if (
            target.hostname not in hosts
            or target.username is not None
            or target.password is not None
            or target.path
            or target.query
            or target.fragment
        ):
            return False
        origin = request.headers.get("origin")
        if origin is not None:
            parsed = urlsplit(origin)
            ports = {port, settings.api_port}
            if settings.app_env == "dev":
                ports.add(5173)
            if (
                parsed.scheme != request.url.scheme
                or parsed.hostname not in hosts
                or (parsed.port or (443 if parsed.scheme == "https" else 80)) not in ports
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                return False
        return not (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("sec-fetch-site") == "cross-site"
        )
    except ValueError:
        return False


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging("INFO")
    try:
        resolved_settings = settings if settings is not None else load_settings()
    except SettingsValidationError as error:
        logger.error(
            "system.configuration_invalid",
            extra={
                "error_code": ErrorCode.SYS_INVALID_CONFIG.value,
                "retryable": False,
                "invalid_fields": list(error.fields),
            },
        )
        raise

    configure_logging(resolved_settings.log_level)
    is_development = resolved_settings.app_env == "dev"
    app = FastAPI(
        title="Yomira API",
        version=APP_VERSION,
        docs_url="/docs" if is_development else None,
        openapi_url="/openapi.json" if is_development else None,
        redoc_url=None,
        lifespan=application_lifespan,
    )
    app.state.settings = resolved_settings
    app.state.updater = AppUpdater(resolved_settings.data_root)
    runtime = RuntimeState()
    runtime.secret_vault = WindowsDpapiSecretVault(resolved_settings.data_root)
    if resolved_settings.app_env == "test" and resolved_settings.fmd2_executable is None:
        runtime.connector_registry = create_connector_registry(
            resolved_settings,
            runtime.secret_vault,
        )
    app.state.runtime = runtime
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(AmbiguousLibrarySeries, _library_conflict_error_handler)
    app.add_exception_handler(SourceConnectorError, _source_connector_error_handler)
    app.add_exception_handler(CbrExportFailure, _cbr_export_error_handler)
    app.add_exception_handler(CoverAssetError, _cover_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)

    @app.middleware("http")
    async def trace_request(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        trace_id = resolve_request_trace_id(request)
        token = activate_trace_id(trace_id)
        started_at = perf_counter()
        try:
            if _trusted_local_request(request, resolved_settings):
                response = await call_next(request)
            else:
                response = _error_response(
                    request,
                    code=ErrorCode.API_REQUEST_REJECTED,
                    message="La solicitud no procede de la página local de Yomira.",
                    status_code=403,
                )
        finally:
            reset_trace_id(token)

        response.headers[TRACE_ID_HEADER] = trace_id
        logger.info(
            "api.request.completed",
            extra={
                "trace_id": trace_id,
                "method": request.method,
                "status_code": response.status_code,
                "duration": round(perf_counter() - started_at, 6),
            },
        )
        return response

    app.include_router(system_router)
    app.include_router(updates_router)
    app.include_router(library_router)
    app.include_router(reader_router)
    app.include_router(recommendations_router)
    app.include_router(cbr_exports_router)
    app.include_router(settings_router)
    app.include_router(connectors_router)
    app.include_router(jobs_router)
    app.include_router(download_cancellation_router)
    app.include_router(usb_exports_router)

    if resolved_settings.app_env == "prod":
        frontend_directory = resolve_frontend_directory()
        if frontend_directory is None:
            logger.error(
                "system.frontend_distribution_missing",
                extra={
                    "error_code": ErrorCode.SYS_STARTUP_FAILED.value,
                    "retryable": False,
                },
            )
            raise FrontendDistributionError()
        register_frontend_routes(app, frontend_directory)

    return app
