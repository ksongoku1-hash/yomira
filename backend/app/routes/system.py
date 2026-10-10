import ipaddress
import logging
import os
import re
import shutil
import stat
from io import BytesIO
from typing import Literal, cast

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from app.errors import ErrorCode
from app.persistence.database import DatabaseBusyError
from app.runtime import RuntimeState
from app.settings import Settings
from app.tracing import current_trace_id
from app.version import API_VERSION, APP_VERSION

router = APIRouter(prefix="/api/v1/system", tags=["system"])
logger = logging.getLogger("yomira.system")
_SCHEMA_REVISION_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class LivenessResponse(BaseModel):
    status: str


class VersionResponse(BaseModel):
    app_version: str
    api_version: int


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]


class DiagnosticCheckResponse(BaseModel):
    id: Literal[
        "backend",
        "database",
        "library",
        "disk",
        "image_processor",
        "cbz_builder",
        "connectors",
        "lan",
    ]
    status: Literal["ok", "warning", "error", "unknown"]
    code: ErrorCode | None
    message: str
    trace_id: str | None


class DiagnosticsResponse(BaseModel):
    app_version: str
    api_version: int
    schema_version: str | None
    checks: list[DiagnosticCheckResponse]


@router.get("/liveness", response_model=LivenessResponse)
async def get_liveness() -> LivenessResponse:
    return LivenessResponse(status="alive")


@router.get(
    "/readiness",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
async def get_readiness(request: Request, response: Response) -> ReadinessResponse:
    runtime = cast(RuntimeState, request.app.state.runtime)
    storage_is_usable = runtime.database is None or not runtime.database.storage_gate.closed
    is_ready = runtime.ready and storage_is_usable
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ready" if is_ready else "not_ready")


@router.get("/version", response_model=VersionResponse)
async def get_version() -> VersionResponse:
    return VersionResponse(app_version=APP_VERSION, api_version=API_VERSION)


def _diagnostic_check(
    check_id: Literal[
        "backend",
        "database",
        "library",
        "disk",
        "image_processor",
        "cbz_builder",
        "connectors",
        "lan",
    ],
    check_status: Literal["ok", "warning", "error", "unknown"],
    message: str,
    *,
    code: ErrorCode | None = None,
) -> DiagnosticCheckResponse:
    trace_id = current_trace_id() if check_status in {"warning", "error", "unknown"} else None
    if check_status != "ok":
        log_method = logger.error if check_status == "error" else logger.warning
        log_method(
            "system.diagnostics.check_not_ok",
            extra={
                "check_id": check_id,
                "status": check_status,
                "error_code": code.value if code is not None else None,
                "trace_id": trace_id,
            },
        )
    return DiagnosticCheckResponse(
        id=check_id,
        status=check_status,
        code=code,
        message=message,
        trace_id=trace_id,
    )


def _log_probe_failure(check_id: str, code: ErrorCode | None, error: Exception) -> None:
    cause_type = re.sub(r"[^A-Za-z0-9_]", "_", type(error).__name__)[:64] or "Exception"
    logger.error(
        "system.diagnostics.probe_failed",
        extra={
            "check_id": check_id,
            "error_code": code.value if code is not None else None,
            "cause_type": cause_type,
            "trace_id": current_trace_id(),
        },
    )


def _check_database(runtime: RuntimeState) -> tuple[DiagnosticCheckResponse, str | None]:
    database = runtime.database
    if database is None:
        return (
            _diagnostic_check(
                "database", "unknown", "La base de datos todavía no está disponible."
            ),
            None,
        )
    if database.is_closed:
        return (
            _diagnostic_check(
                "database",
                "error",
                "La conexión local con SQLite está cerrada.",
                code=ErrorCode.DB_CONNECTION_FAILED,
            ),
            None,
        )

    schema_version: str | None = None
    try:
        with database.read_session() as session:
            connection = session.connection()
            integrity_rows = connection.exec_driver_sql("PRAGMA quick_check(1)").scalars().all()
            if integrity_rows != ["ok"]:
                return (
                    _diagnostic_check(
                        "database",
                        "error",
                        "SQLite informó una inconsistencia de integridad.",
                        code=ErrorCode.DB_INTEGRITY_FAILED,
                    ),
                    None,
                )

            table_names = set(
                connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")
                .scalars()
                .all()
            )
            if not {"alembic_version", "schema_history"}.issubset(table_names):
                return (
                    _diagnostic_check(
                        "database",
                        "error",
                        "La base de datos no tiene un historial de esquema verificable.",
                        code=ErrorCode.DB_MIGRATION_FAILED,
                    ),
                    None,
                )

            revisions = (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version")
                .scalars()
                .all()
            )
            if (
                len(revisions) != 1
                or not isinstance(revisions[0], str)
                or _SCHEMA_REVISION_PATTERN.fullmatch(revisions[0]) is None
            ):
                return (
                    _diagnostic_check(
                        "database",
                        "error",
                        "La revisión de esquema no es válida.",
                        code=ErrorCode.DB_MIGRATION_FAILED,
                    ),
                    None,
                )
            schema_version = revisions[0]
            history_revision = connection.exec_driver_sql(
                "SELECT resulting_revision FROM schema_history ORDER BY event_id DESC LIMIT 1"
            ).scalar_one_or_none()
            if history_revision != schema_version:
                return (
                    _diagnostic_check(
                        "database",
                        "error",
                        "El historial de migraciones no coincide con el esquema actual.",
                        code=ErrorCode.DB_MIGRATION_FAILED,
                    ),
                    schema_version,
                )
    except DatabaseBusyError as error:
        _log_probe_failure("database", ErrorCode.DB_BUSY_TIMEOUT, error)
        return (
            _diagnostic_check(
                "database",
                "warning",
                "SQLite está ocupado y no pudo completar la comprobación dentro del límite.",
                code=ErrorCode.DB_BUSY_TIMEOUT,
            ),
            None,
        )
    except Exception as error:
        _log_probe_failure("database", ErrorCode.DB_CONNECTION_FAILED, error)
        return (
            _diagnostic_check(
                "database",
                "error",
                "No se pudo verificar la base de datos local.",
                code=ErrorCode.DB_CONNECTION_FAILED,
            ),
            schema_version,
        )

    return (
        _diagnostic_check("database", "ok", "SQLite pasó la comprobación de integridad y esquema."),
        schema_version,
    )


def _check_library(runtime: RuntimeState) -> DiagnosticCheckResponse:
    identity = runtime.library_root_identity
    if identity is None:
        return _diagnostic_check(
            "library",
            "unknown",
            "La raíz local de la biblioteca no está disponible.",
        )
    try:
        identity.assert_current()
        library_path = identity.canonical_path
        info = library_path.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISDIR(info.st_mode)
            or bool(info.st_file_attributes & reparse_flag)
            or library_path.is_symlink()
        ):
            raise OSError("Library root is not a plain directory")
        with os.scandir(library_path):
            pass
        identity.assert_current()
    except Exception as error:
        _log_probe_failure("library", ErrorCode.LIB_PATH_UNAVAILABLE, error)
        return _diagnostic_check(
            "library",
            "error",
            "La carpeta local de la biblioteca no está disponible para lectura.",
            code=ErrorCode.LIB_PATH_UNAVAILABLE,
        )
    return _diagnostic_check("library", "ok", "La carpeta local de la biblioteca se puede leer.")


def _check_disk(runtime: RuntimeState) -> DiagnosticCheckResponse:
    identity = runtime.data_root_identity
    if identity is None:
        return _diagnostic_check("disk", "unknown", "No se pudo identificar el volumen de datos.")
    try:
        identity.assert_current()
        usage = shutil.disk_usage(identity.canonical_path)
        if (
            type(usage.total) is not int
            or type(usage.used) is not int
            or type(usage.free) is not int
            or usage.total < 0
            or usage.used < 0
            or usage.free < 0
            or usage.free > usage.total
        ):
            raise OSError("Disk usage returned invalid measurements")
        identity.assert_current()
    except Exception as error:
        _log_probe_failure("disk", None, error)
        return _diagnostic_check(
            "disk",
            "unknown",
            "No se pudo medir el espacio libre del volumen local.",
        )
    if usage.free == 0:
        return _diagnostic_check(
            "disk",
            "warning",
            "El volumen local reporta cero bytes libres.",
        )
    return _diagnostic_check(
        "disk", "ok", "El espacio libre del volumen local se midió correctamente."
    )


def _check_image_processor() -> DiagnosticCheckResponse:
    try:
        from PIL import Image

        from app.services.thumbnails import ThumbnailService, ThumbnailVariant
    except ImportError as error:
        _log_probe_failure("image_processor", None, error)
        return _diagnostic_check(
            "image_processor",
            "unknown",
            "No se pudo cargar el procesador para realizar una comprobación.",
        )
    try:
        service = ThumbnailService(cache_max_bytes=1_024, cache_max_entries=1)
        with Image.new("RGB", (1, 1), color=(12, 34, 56)) as image:
            thumbnail = service.generate(image, "0" * 64, ThumbnailVariant.SMALL)
        with Image.open(BytesIO(thumbnail.content)) as decoded:
            decoded.load()
            if decoded.format != "WEBP" or decoded.size != (1, 1):
                raise ValueError("Image round-trip did not meet the expected output")
    except Exception as error:
        _log_probe_failure("image_processor", ErrorCode.IMG_THUMBNAIL_FAILED, error)
        return _diagnostic_check(
            "image_processor",
            "error",
            "El procesador no completó una conversión de imagen en memoria.",
            code=ErrorCode.IMG_THUMBNAIL_FAILED,
        )
    return _diagnostic_check(
        "image_processor",
        "ok",
        "El procesador convirtió y volvió a leer una imagen sintética en memoria.",
    )


def _check_connectors(runtime: RuntimeState) -> DiagnosticCheckResponse:
    registry = runtime.connector_registry
    if registry is None or not registry.connector_ids:
        return _diagnostic_check(
            "connectors",
            "unknown",
            "No hay un allowlist de connectors disponible para resolver.",
        )
    try:
        for connector_id in registry.connector_ids:
            registry.resolve(connector_id)
    except Exception as error:
        _log_probe_failure("connectors", ErrorCode.SRC_CONNECTOR_FAILED, error)
        return _diagnostic_check(
            "connectors",
            "error",
            "No se pudo resolver localmente un connector permitido; no se consultó la red.",
            code=ErrorCode.SRC_CONNECTOR_FAILED,
        )
    return _diagnostic_check(
        "connectors",
        "ok",
        "Connectors permitidos resueltos localmente; no se sondeó la red ni su estado remoto.",
    )


def _check_lan(settings: object) -> DiagnosticCheckResponse:
    if not isinstance(settings, Settings):
        return _diagnostic_check(
            "lan", "unknown", "La configuración de red local no está disponible."
        )
    try:
        is_loopback = ipaddress.ip_address(settings.api_host).is_loopback
    except ValueError as error:
        _log_probe_failure("lan", ErrorCode.SYS_INVALID_CONFIG, error)
        return _diagnostic_check(
            "lan",
            "error",
            "La dirección configurada para la API no es válida.",
            code=ErrorCode.SYS_INVALID_CONFIG,
        )
    if not is_loopback:
        return _diagnostic_check(
            "lan",
            "error",
            "La API no está configurada para limitarse a loopback.",
            code=ErrorCode.SYS_INVALID_CONFIG,
        )
    return _diagnostic_check(
        "lan",
        "ok",
        "La API está configurada para loopback; no se realizaron pruebas de socket.",
    )


@router.get("/diagnostics", response_model=DiagnosticsResponse)
def get_diagnostics(request: Request) -> DiagnosticsResponse:
    runtime = cast(RuntimeState, request.app.state.runtime)
    database_check, schema_version = _check_database(runtime)
    checks = [
        _diagnostic_check("backend", "ok", "La API local está respondiendo."),
        database_check,
        _check_library(runtime),
        _check_disk(runtime),
        _check_image_processor(),
        _diagnostic_check(
            "cbz_builder",
            "unknown",
            "Se omitió la verificación integral de CBZ porque su publicación escribe archivos.",
        ),
        _check_connectors(runtime),
        _check_lan(getattr(request.app.state, "settings", None)),
    ]
    return DiagnosticsResponse(
        app_version=APP_VERSION,
        api_version=API_VERSION,
        schema_version=schema_version,
        checks=checks,
    )
