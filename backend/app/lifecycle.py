import logging
import stat
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

import httpx
from fastapi import FastAPI

from app.connectors.builtins import create_connector_registry
from app.connectors.fmd2 import Fmd2Connector
from app.connectors.http_client import ConnectorHttpSession
from app.domain.job_backpressure import BackpressureManager
from app.domain.job_pause import JobPauseController, PauseReason, UnknownActiveJob
from app.errors import ErrorCode
from app.logging_config import configure_logging
from app.persistence.atomic_file_publisher import (
    ADR_0087_RETRY_POLICY,
    AtomicFilePublisher,
)
from app.persistence.database import DatabaseInitializationError, SQLiteDatabase
from app.persistence.disk_budget import DiskBudgetManager, DiskBudgetUnavailableError
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.windows_paths import (
    DataRootIdentity,
    DataRootIdentityError,
    validate_supported_volume_for_path,
)
from app.platform.windows_resources import WindowsResourceSampler
from app.runtime import RuntimeState
from app.secrets.vault import WindowsDpapiSecretVault
from app.services.cbr_builder import CbrExportService
from app.services.cbz_builder import MAX_CBZ_BYTES_V1, CBZBuilder
from app.services.chapter_fallback import ChapterFallbackService
from app.services.cover_assets import CoverAssetService
from app.services.download_dispatcher import DownloadDispatcher
from app.services.download_transport import (
    DownloadTransport,
    DownloadTransportPolicy,
    NetworkOrigin,
)
from app.services.download_worker import DownloadWorker
from app.services.export_jobs import UsbExportJobService
from app.services.fmd2_catalog_refresh import (
    FMD2_CATALOG_ORIGINS,
    Fmd2CatalogRefreshService,
)
from app.services.image_validation import ImageValidationPolicy
from app.services.job_recovery import RecoveryCoordinator
from app.services.jobs import DownloadJobService
from app.services.library import LibraryService
from app.services.local_import import LocalImportService
from app.services.metadata import MetadataService
from app.services.online_image_cache import OnlineImageCache
from app.services.online_reader import OnlineReaderSessionService
from app.services.reader_asset import ReaderAssetError, ReaderAssetLimits, ReaderAssetService
from app.services.reader_progress import ReaderProgressService
from app.services.reader_session import HybridReaderChapterLookup, ReaderSessionService
from app.services.reader_settings import ReaderSettingsService
from app.services.recommendations import RecommendationsService
from app.services.resource_monitor import ResourceMonitorService
from app.services.source_pages import SourcePageService
from app.services.startup_self_test import run_startup_self_tests
from app.services.usb_export import UsbExportService, WindowsRemovableDriveProvider
from app.services.wpd_worker import IsolatedWpdClient
from app.settings import Settings
from app.single_instance import (
    InstanceAlreadyRunningError,
    WindowsMutexGuard,
    create_instance_guard,
)
from app.tracing import (
    activate_trace_id,
    create_trace_id,
    current_trace_id,
    reset_trace_id,
)

logger = logging.getLogger("yomira.lifecycle")

_PAGE_BYTES_V1 = 67_108_864
_PAGE_DIMENSION_V1 = 40_000
_PAGE_PIXELS_V1 = 100_000_000
_DOWNLOAD_HTTP_TIMEOUT_SECONDS = {
    "connect": 10.0,
    "read": 30.0,
    "write": 10.0,
    "pool": 10.0,
}
_DOWNLOAD_HTTP_CONNECTIONS = 2
_DOWNLOAD_HTTP_PER_HOST = 1


def _log_database_bootstrap_failure(
    event: str,
    stage: str,
    error: BaseException | None = None,
) -> None:
    extra: dict[str, object] = {
        "error_code": ErrorCode.DB_CONNECTION_FAILED.value,
        "retryable": True,
        "stage": stage,
        "trace_id": current_trace_id() or create_trace_id(),
    }
    if error is not None:
        extra["cause_type"] = type(error).__name__
    logger.error(event, extra=extra)


async def _release_instance_guard(app: FastAPI) -> None:
    guard = getattr(app.state, "instance_guard", None)
    if isinstance(guard, WindowsMutexGuard):
        guard.release()
        app.state.instance_guard = None


async def initialize_runtime(app: FastAPI) -> None:
    settings = getattr(app.state, "settings", None)
    runtime = getattr(app.state, "runtime", None)
    if not isinstance(settings, Settings) or not isinstance(runtime, RuntimeState):
        raise RuntimeError("Required runtime state was not initialized.")

    if runtime.data_root_identity is None:
        try:
            validate_supported_volume_for_path(settings.data_root)
        except DataRootIdentityError as error:
            _log_database_bootstrap_failure(
                "database.open_failed",
                "root_identity",
                error,
            )
            raise DatabaseInitializationError() from None
        try:
            settings.data_root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            _log_database_bootstrap_failure(
                "database.data_directory_create_failed",
                "data_root_create",
                error,
            )
            raise DatabaseInitializationError() from None
        try:
            runtime.data_root_identity = DataRootIdentity.capture(settings.data_root)
        except DataRootIdentityError as error:
            _log_database_bootstrap_failure(
                "database.open_failed",
                "root_identity",
                error,
            )
            raise DatabaseInitializationError() from None

    data_root_identity = runtime.data_root_identity
    data_root = data_root_identity.canonical_path

    data_root_identity.assert_current()
    configure_logging(
        settings.log_level,
        log_file_path=data_root / "logs" / "yomira.log",
        max_bytes=settings.log_max_bytes,
        file_count=settings.log_file_count,
    )
    if settings.app_env == "prod" and data_root.name == "AlphaData":
        logger.warning(
            "runtime.legacy_data_root_reused",
            extra={
                "component": "settings",
                "stage": "legacy_data_root_adoption",
                "action": "REUSE_IN_PLACE",
                "trace_id": current_trace_id() or create_trace_id(),
            },
        )
    data_root_identity.assert_current()

    if runtime.library_root_identity is None:
        try:
            validate_supported_volume_for_path(settings.library_root)
            settings.library_root.mkdir(parents=True, exist_ok=True)
            runtime.library_root_identity = DataRootIdentity.capture(settings.library_root)
        except Exception as error:
            logger.critical(
                "system.library_root_initialization_failed",
                extra={
                    "error_code": ErrorCode.LIB_PATH_UNAVAILABLE.value,
                    "retryable": False,
                    "stage": "library_root_initialization",
                    "cause_type": type(error).__name__,
                    "trace_id": current_trace_id() or create_trace_id(),
                },
            )
            raise RuntimeError("The configured manga library is unavailable.") from None

    library_root_identity = runtime.library_root_identity
    try:
        library_root_identity.assert_current()
        library_root = library_root_identity.canonical_path
        library_info = settings.library_root.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        overlaps_data_root = (
            library_root == data_root
            or library_root.is_relative_to(data_root)
            or data_root.is_relative_to(library_root)
        )
        if (
            stat.S_ISLNK(library_info.st_mode)
            or bool(getattr(library_info, "st_file_attributes", 0) & reparse_flag)
            or not stat.S_ISDIR(library_info.st_mode)
            or library_root != settings.library_root.resolve()
            or library_root_identity.volume_serial != data_root_identity.volume_serial
            or (overlaps_data_root and settings.app_env != "test")
        ):
            raise DataRootIdentityError("The configured manga library root is unsupported.")
    except Exception as error:
        logger.critical(
            "system.library_root_initialization_failed",
            extra={
                "error_code": ErrorCode.LIB_PATH_UNAVAILABLE.value,
                "retryable": False,
                "stage": "library_root_identity_validation",
                "cause_type": type(error).__name__,
                "trace_id": current_trace_id() or create_trace_id(),
            },
        )
        raise RuntimeError("The configured manga library is unavailable.") from None

    if runtime.database is None:
        try:
            data_root_identity.assert_current()
            database_directory = data_root / "database"
            database_directory.mkdir(parents=True, exist_ok=True)
            data_root_identity.assert_current()
        except DataRootIdentityError as error:
            _log_database_bootstrap_failure(
                "database.open_failed",
                "root_identity",
                error,
            )
            raise DatabaseInitializationError() from None
        except OSError as error:
            _log_database_bootstrap_failure(
                "database.data_directory_create_failed",
                "data_directory_create",
                error,
            )
            raise DatabaseInitializationError() from None
        runtime.database = SQLiteDatabase(
            database_directory / "yomira.sqlite3",
            data_root_identity=data_root_identity,
        )
    elif runtime.database.data_root_identity is not data_root_identity:
        _log_database_bootstrap_failure(
            "database.open_failed",
            "root_identity",
        )
        raise DatabaseInitializationError()

    database = runtime.database
    if runtime.disk_budget_manager is None:
        runtime.disk_budget_manager = DiskBudgetManager(database)
    elif runtime.disk_budget_manager.database is not database:
        raise DatabaseInitializationError()
    startup_budget = runtime.disk_budget_manager.snapshot()
    migration_runner = MigrationRunner(database, data_root / "backups")
    # Reading needs metadata growth, not the download engine's 512 MiB margin.
    metadata_startup_safe = (
        startup_budget.free_bytes is not None
        and startup_budget.recovery_demand_bytes is not None
        and startup_budget.free_bytes >= startup_budget.recovery_demand_bytes + 16 * 1024 * 1024
        and startup_budget.reserved_bytes == 0
        and startup_budget.reason is None
        and (startup_budget.safe or migration_runner.is_current())
    )
    if not startup_budget.safe and not metadata_startup_safe:
        logger.critical(
            "system.disk_budget_startup_preflight_failed",
            extra={
                "error_code": ErrorCode.SYS_DISK_BUDGET_EXCEEDED.value,
                "retryable": True,
                "stage": "startup_disk_budget_preflight",
                "volume_serial": startup_budget.volume_serial,
                "free_bytes": startup_budget.free_bytes,
                "required_bytes": startup_budget.required_bytes,
                "reason": startup_budget.reason,
                "trace_id": create_trace_id(),
            },
        )
        raise DiskBudgetUnavailableError(
            startup_budget.reason or "STARTUP_BUDGET_UNAVAILABLE",
            free_bytes=startup_budget.free_bytes,
            required_bytes=startup_budget.required_bytes,
        )
    disk_budget_manager = runtime.disk_budget_manager
    assert disk_budget_manager is not None
    migration_report = migration_runner.upgrade_to_head()
    run_startup_self_tests(
        settings,
        database=database,
        data_root_identity=data_root_identity,
        library_root_identity=library_root_identity,
        expected_schema_revision=migration_report.current_revision,
    )
    portable_device_backend = IsolatedWpdClient()
    runtime.usb_export_job_service = UsbExportJobService(
        database=database,
        exporter=UsbExportService(
            target_provider=WindowsRemovableDriveProvider(
                portable_device_provider=portable_device_backend,
            ),
            library_root=library_root_identity.canonical_path,
            data_root=data_root_identity.canonical_path,
            portable_exporter=portable_device_backend,
        ),
    )
    runtime.usb_export_job_service.recover_after_restart()
    try:
        runtime.recovery_coordinator = RecoveryCoordinator(
            database,
            data_root_identity,
        )
        interrupted_count = runtime.recovery_coordinator.reconcile_startup()
    except Exception:
        logger.exception(
            "job.recovery.startup_reconciliation_failed",
            extra={
                "error_code": ErrorCode.SYS_STARTUP_FAILED.value,
                "retryable": False,
                "stage": "job_interruption_recovery",
                "action": "RECOVERY_ABORTED",
                "reason": "ACTIVE_JOB_RECONCILIATION_FAILED",
            },
        )
        raise
    if interrupted_count:
        logger.warning(
            "job.recovery.stale_active_jobs_interrupted",
            extra={
                "stage": "startup_job_recovery",
                "observed_value": interrupted_count,
                "action": "INTERRUPT",
                "reason": "PROCESS_RESTART",
            },
        )
    runtime.library_service = LibraryService(database)
    runtime.download_job_service = DownloadJobService(
        database, library_root_identity=library_root_identity
    )
    runtime.metadata_service = MetadataService(database)
    runtime.reader_settings_service = ReaderSettingsService(database)
    runtime.secret_vault = WindowsDpapiSecretVault(
        data_root,
        storage_gate=database.storage_gate,
    )
    if runtime.connector_http_session is None:
        runtime.connector_http_session = ConnectorHttpSession()
    if settings.fmd2_executable is not None:
        if runtime.fmd2_connector is None:
            runtime.fmd2_connector = Fmd2Connector(
                settings.fmd2_executable,
                data_root_identity.canonical_path / "staging" / "fmd2",
            )
        try:
            await runtime.fmd2_connector.start()
        except (OSError, RuntimeError) as error:
            logger.warning(
                "fmd2.startup_unavailable",
                extra={
                    "component": "fmd2",
                    "stage": "startup",
                    "cause_type": type(error).__name__,
                    "retryable": True,
                },
            )
        else:
            try:
                runtime.fmd2_catalog_refresh_service = Fmd2CatalogRefreshService(
                    runtime.connector_http_session.scoped_client(FMD2_CATALOG_ORIGINS),
                    settings.fmd2_executable.parent / "data",
                    data_root / "cache" / "fmd2-catalog-refresh-v1.json",
                )
                await runtime.fmd2_catalog_refresh_service.start()
            except (OSError, RuntimeError) as error:
                logger.warning(
                    "fmd2.catalog_refresh_unavailable",
                    extra={
                        "component": "fmd2",
                        "stage": "catalog_refresh_start",
                        "cause_type": type(error).__name__,
                        "retryable": True,
                    },
                )
    elif settings.app_env != "test":
        logger.warning(
            "fmd2.source_unavailable",
            extra={
                "component": "fmd2",
                "stage": "startup",
                "action": "OMIT_SOURCE",
                "trace_id": current_trace_id() or create_trace_id(),
            },
        )
    runtime.connector_registry = create_connector_registry(
        settings,
        runtime.secret_vault,
        http_client_factory=runtime.connector_http_session.scoped_client,
        fmd2_connector=runtime.fmd2_connector,
    )
    runtime.job_pause_controller = JobPauseController()
    runtime.recommendations_service = RecommendationsService(
        data_root_identity.canonical_path / "cache" / "recommendations-v1.json"
    )
    runtime.backpressure_manager = BackpressureManager(runtime.job_pause_controller)
    runtime.reader_asset_service = ReaderAssetService(
        database,
        data_root_identity=data_root_identity,
        library_root_identity=library_root_identity,
        limits=ReaderAssetLimits(
            max_cbz_bytes=2_147_483_648,
            max_page_bytes=_PAGE_BYTES_V1,
            chunk_bytes=65_536,
        ),
    )
    assert runtime.connector_registry is not None
    allowed_origins = frozenset(
        NetworkOrigin.from_url(origin)
        for connector_id in runtime.connector_registry.connector_ids
        for origin in runtime.connector_registry.get_download_origins(connector_id)
    )
    if not allowed_origins:
        raise RuntimeError("No registered download origin is available.")

    download_http_transport = httpx.AsyncHTTPTransport(
        verify=True,
        trust_env=False,
        retries=0,
        limits=httpx.Limits(
            max_connections=_DOWNLOAD_HTTP_CONNECTIONS,
            max_keepalive_connections=_DOWNLOAD_HTTP_CONNECTIONS,
        ),
    )
    runtime.download_http_client = httpx.AsyncClient(
        transport=download_http_transport,
        timeout=httpx.Timeout(**_DOWNLOAD_HTTP_TIMEOUT_SECONDS),
        follow_redirects=False,
        trust_env=False,
    )
    runtime.download_transport = DownloadTransport(
        runtime.download_http_client,
        DownloadTransportPolicy(
            allowed_origins=allowed_origins,
            max_bytes=_PAGE_BYTES_V1,
            chunk_size=65_536,
            connect_timeout_seconds=10,
            read_timeout_seconds=30,
            write_timeout_seconds=10,
            pool_timeout_seconds=10,
            max_redirects=5,
            max_retry_after_seconds=60,
            global_concurrency=2,
            per_host_concurrency=2,
        ),
        data_root=data_root,
        data_root_identity=data_root_identity,
        storage_gate=database.storage_gate,
        disk_budget_manager=disk_budget_manager,
        secret_header_provider=(
            runtime.fmd2_connector.page_secret_headers
            if runtime.fmd2_connector is not None
            else None
        ),
    )
    file_publisher = AtomicFilePublisher(
        data_root_identity=data_root_identity,
        retry_policy=ADR_0087_RETRY_POLICY,
        sleep=time.sleep,
        clock=time.monotonic,
        storage_gate=database.storage_gate,
    )
    library_file_publisher = AtomicFilePublisher(
        data_root_identity=library_root_identity,
        retry_policy=ADR_0087_RETRY_POLICY,
        sleep=time.sleep,
        clock=time.monotonic,
        storage_gate=database.storage_gate,
    )
    reader_image_policy = ImageValidationPolicy(
        max_bytes=_PAGE_BYTES_V1,
        max_width=_PAGE_DIMENSION_V1,
        max_height=_PAGE_DIMENSION_V1,
        max_pixels=_PAGE_PIXELS_V1,
    )
    legacy_reader_cache = ReaderSessionService(
        database,
        source_pages=SourcePageService(runtime.connector_registry),
        transport=runtime.download_transport,
        disk_budget=disk_budget_manager,
        publisher=file_publisher,
        image_policy=reader_image_policy,
    )
    try:
        legacy_reader_cache.recover()
        await legacy_reader_cache.retire_cache()
    except (ReaderAssetError, OSError) as error:
        logger.warning("reader.cache_recovery_blocked", extra={"cause_type": type(error).__name__})
    runtime.reader_session_service = OnlineReaderSessionService(
        database,
        source_pages=SourcePageService(runtime.connector_registry),
        transport=runtime.download_transport,
        image_policy=reader_image_policy,
        disk_cache=OnlineImageCache(database, disk_budget_manager),
    )
    hybrid_lookup = HybridReaderChapterLookup(
        runtime.reader_asset_service, runtime.reader_session_service
    )
    runtime.reader_progress_service = ReaderProgressService(
        database, hybrid_lookup, page_available=hybrid_lookup.page_available
    )
    assert runtime.reader_asset_service is not None
    assert runtime.library_service is not None
    runtime.cbr_export_service = CbrExportService(
        database=database,
        reader=runtime.reader_asset_service,
        library=runtime.library_service,
        disk_budget=disk_budget_manager,
        library_root_identity=library_root_identity,
        publisher=library_file_publisher,
    )
    runtime.cbr_export_service.recover_stale_staging()
    cbz_builder = CBZBuilder(
        publisher=library_file_publisher,
        max_archive_bytes=MAX_CBZ_BYTES_V1,
        storage_gate=database.storage_gate,
        disk_budget_manager=disk_budget_manager,
    )
    assert runtime.recovery_coordinator is not None
    assert runtime.reader_asset_service is not None
    assert runtime.download_transport is not None
    runtime.download_worker = DownloadWorker(
        database=database,
        data_root_identity=data_root_identity,
        library_root_identity=library_root_identity,
        connector_registry=runtime.connector_registry,
        recovery_coordinator=runtime.recovery_coordinator,
        transport=runtime.download_transport,
        image_validation_policy=reader_image_policy,
        cbz_builder=cbz_builder,
        file_publisher=file_publisher,
        library_file_publisher=library_file_publisher,
        reader_asset_service=runtime.reader_asset_service,
        pause_controller=runtime.job_pause_controller,
    )
    runtime.download_dispatcher = DownloadDispatcher(
        chapter_fallback=ChapterFallbackService(database, runtime.connector_registry),
        database=database,
        worker=runtime.download_worker,
        recovery_coordinator=runtime.recovery_coordinator,
        pause_controller=runtime.job_pause_controller,
        disk_budget_manager=disk_budget_manager,
        admission_allowed=lambda: (
            runtime.resource_monitor is not None and runtime.resource_monitor.admission_allowed
        ),
    )
    runtime.cover_asset_service = CoverAssetService(
        database=database,
        registry=runtime.connector_registry,
        http_session=runtime.connector_http_session,
        reader=runtime.reader_asset_service,
        disk_budget=disk_budget_manager,
        publisher=file_publisher,
        admission_allowed=lambda: (
            runtime.resource_monitor is not None and runtime.resource_monitor.admission_allowed
        ),
    )
    runtime.resource_monitor = ResourceMonitorService(
        WindowsResourceSampler(),
        runtime.backpressure_manager,
        active_jobs=lambda: (
            runtime.download_dispatcher.active_job_snapshots
            if runtime.download_dispatcher is not None
            else ()
        ),
    )
    await runtime.resource_monitor.start()
    runtime.local_import_service = LocalImportService(
        database=database,
        library_root=library_root_identity,
        builder=cbz_builder,
        covers=runtime.cover_asset_service,
        disk_budget=disk_budget_manager,
        additional_roots=(
            (settings.library_root.parent / "Mangas",)
            + ((settings.fmd2_executable.parent / "downloads",) if settings.fmd2_executable else ())
        )
        if settings.app_env != "test"
        else (),
        admission_allowed=lambda: (
            runtime.resource_monitor is not None and runtime.resource_monitor.admission_allowed
        ),
    )
    if settings.app_env != "test":
        await runtime.local_import_service.start()


async def shutdown_runtime(app: FastAPI) -> None:
    runtime = cast(RuntimeState, app.state.runtime)
    if runtime.ready:
        raise RuntimeError("Runtime must leave readiness before shutdown.")
    dispatcher = runtime.download_dispatcher
    if dispatcher is not None:
        # Close admission before stopping the monitor so no new job slips into
        # the queue while runtime dependencies are being dismantled.
        dispatcher.request_stop()
    if runtime.reader_session_service is not None:
        await runtime.reader_session_service.close()
        runtime.reader_session_service = None
    if runtime.local_import_service is not None:
        await runtime.local_import_service.stop()
        runtime.local_import_service = None
    if runtime.resource_monitor is not None:
        await runtime.resource_monitor.stop()
        runtime.resource_monitor = None
    if dispatcher is not None:
        pause_controller = runtime.job_pause_controller
        if pause_controller is not None:
            for job_id in dispatcher.active_job_ids:
                try:
                    reasons = pause_controller.pause_reasons(job_id)
                except UnknownActiveJob:
                    # The task may have been scheduled but not registered yet.
                    continue
                for reason in (
                    PauseReason.BACKPRESSURE,
                    PauseReason.MONITOR_UNAVAILABLE,
                ):
                    if reason in reasons:
                        pause_controller.resume(job_id, reason=reason)
        await dispatcher.stop()
        runtime.download_dispatcher = None
    fmd2_close_failed = False
    if runtime.fmd2_catalog_refresh_service is not None:
        try:
            await runtime.fmd2_catalog_refresh_service.close()
        except Exception as error:
            fmd2_close_failed = True
            logger.error(
                "fmd2.catalog_refresh_close_failed",
                extra={
                    "error_code": ErrorCode.SYS_INTERNAL_ERROR.value,
                    "retryable": False,
                    "stage": "catalog_refresh_close",
                    "cause_type": type(error).__name__,
                },
            )
        else:
            runtime.fmd2_catalog_refresh_service = None
    if runtime.fmd2_connector is not None:
        try:
            await runtime.fmd2_connector.close()
        except Exception as error:
            fmd2_close_failed = True
            logger.error(
                "fmd2_bridge.close_failed",
                extra={
                    "error_code": ErrorCode.SYS_INTERNAL_ERROR.value,
                    "retryable": False,
                    "stage": "fmd2_bridge_close",
                    "cause_type": type(error).__name__,
                },
            )
        else:
            runtime.fmd2_connector = None
    if runtime.cover_asset_service is not None:
        await runtime.cover_asset_service.close()
        runtime.cover_asset_service = None
    runtime.reader_asset_service = None
    runtime.cbr_export_service = None
    runtime.reader_progress_service = None
    runtime.reader_settings_service = None
    runtime.usb_export_job_service = None
    runtime.download_worker = None
    runtime.download_transport = None
    runtime.backpressure_manager = None
    runtime.job_pause_controller = None
    download_http_client_close_failed = False
    download_http_client = runtime.download_http_client
    if download_http_client is not None:
        try:
            await download_http_client.aclose()
        except Exception as error:
            download_http_client_close_failed = True
            logger.error(
                "download_http_client.close_failed",
                extra={
                    "error_code": ErrorCode.SYS_INTERNAL_ERROR.value,
                    "retryable": False,
                    "stage": "download_http_client_close",
                    "cause_type": type(error).__name__,
                },
            )
        else:
            runtime.download_http_client = None
    http_session_close_failed = False
    connector_http_session = runtime.connector_http_session
    if connector_http_session is not None:
        try:
            await connector_http_session.close()
        except Exception as error:
            http_session_close_failed = True
            logger.error(
                "connector_http_session.close_failed",
                extra={
                    "error_code": ErrorCode.SYS_INTERNAL_ERROR.value,
                    "retryable": False,
                    "stage": "connector_http_session_close",
                    "cause_type": type(error).__name__,
                },
            )
        else:
            runtime.connector_http_session = None
    database = runtime.database
    if database is not None:
        database.shutdown()
        runtime.database = None
        runtime.disk_budget_manager = None
        runtime.library_service = None
        runtime.download_job_service = None
        runtime.metadata_service = None
    runtime.recovery_coordinator = None
    runtime.secret_vault = None
    runtime.connector_registry = None
    await _release_instance_guard(app)
    if http_session_close_failed or download_http_client_close_failed or fmd2_close_failed:
        raise RuntimeError("An HTTP session failed to close.") from None


@asynccontextmanager
async def application_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    runtime = cast(RuntimeState, app.state.runtime)
    runtime.ready = False

    startup_trace_token = None
    if current_trace_id() is None:
        startup_trace_token = activate_trace_id(create_trace_id())
    try:
        try:
            guard = create_instance_guard()
            guard.acquire()
            app.state.instance_guard = guard
            await initialize_runtime(app)
            runtime.ready = True
            if runtime.download_dispatcher is not None:
                await runtime.download_dispatcher.start()
        except InstanceAlreadyRunningError:
            logger.error(
                "system.instance_already_running",
                extra={
                    "error_code": ErrorCode.SYS_STARTUP_FAILED.value,
                    "retryable": False,
                },
            )
            raise
        except Exception:
            runtime.ready = False
            logger.exception(
                "system.startup_failed",
                extra={
                    "error_code": ErrorCode.SYS_STARTUP_FAILED.value,
                    "retryable": False,
                },
            )
            try:
                await shutdown_runtime(app)
            except Exception:
                logger.exception(
                    "system.startup_cleanup_failed",
                    extra={
                        "error_code": ErrorCode.SYS_INTERNAL_ERROR.value,
                        "retryable": False,
                    },
                )
            raise

        logger.info("system.startup_complete")
        app.state.updater.start()
    finally:
        if startup_trace_token is not None:
            reset_trace_id(startup_trace_token)

    try:
        yield
    finally:
        runtime.ready = False
        await app.state.updater.close()
        shutdown_trace_token = None
        if current_trace_id() is None:
            shutdown_trace_token = activate_trace_id(create_trace_id())
        try:
            logger.info("system.shutdown_started")
            try:
                await shutdown_runtime(app)
            except Exception:
                logger.exception(
                    "system.shutdown_failed",
                    extra={
                        "error_code": ErrorCode.SYS_INTERNAL_ERROR.value,
                        "retryable": False,
                    },
                )
                raise
            finally:
                database = cast(RuntimeState, app.state.runtime).database
                if database is None or database.is_closed:
                    try:
                        await _release_instance_guard(app)
                    except Exception:
                        logger.exception(
                            "system.instance_guard_release_failed",
                            extra={
                                "error_code": ErrorCode.SYS_INTERNAL_ERROR.value,
                                "retryable": False,
                            },
                        )
                else:
                    logger.error(
                        "system.instance_guard_retained",
                        extra={
                            "error_code": ErrorCode.SYS_INTERNAL_ERROR.value,
                            "retryable": False,
                        },
                    )
            logger.info("system.shutdown_complete")
        finally:
            if shutdown_trace_token is not None:
                reset_trace_id(shutdown_trace_token)
