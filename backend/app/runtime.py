from dataclasses import dataclass

import httpx

from app.connectors.fmd2 import Fmd2Connector
from app.connectors.http_client import ConnectorHttpSession
from app.connectors.registry import ConnectorRegistry
from app.domain.job_backpressure import BackpressureManager
from app.domain.job_pause import JobPauseController
from app.persistence.database import SQLiteDatabase
from app.persistence.disk_budget import DiskBudgetManager
from app.persistence.windows_paths import DataRootIdentity
from app.secrets.vault import SecretVault
from app.services.cbr_builder import CbrExportService
from app.services.cover_assets import CoverAssetService
from app.services.download_dispatcher import DownloadDispatcher
from app.services.download_transport import DownloadTransport
from app.services.download_worker import DownloadWorker
from app.services.export_jobs import UsbExportJobService
from app.services.fmd2_catalog_refresh import Fmd2CatalogRefreshService
from app.services.job_recovery import RecoveryCoordinator
from app.services.jobs import DownloadJobService
from app.services.library import LibraryService
from app.services.local_import import LocalImportService
from app.services.metadata import MetadataService
from app.services.online_reader import OnlineReaderSessionService
from app.services.reader_asset import ReaderAssetService
from app.services.reader_progress import ReaderProgressService
from app.services.reader_session import ReaderSessionService
from app.services.reader_settings import ReaderSettingsService
from app.services.recommendations import RecommendationsService
from app.services.resource_monitor import ResourceMonitorService


@dataclass(slots=True)
class RuntimeState:
    ready: bool = False
    data_root_identity: DataRootIdentity | None = None
    library_root_identity: DataRootIdentity | None = None
    database: SQLiteDatabase | None = None
    disk_budget_manager: DiskBudgetManager | None = None
    library_service: LibraryService | None = None
    local_import_service: LocalImportService | None = None
    metadata_service: MetadataService | None = None
    cover_asset_service: CoverAssetService | None = None
    recovery_coordinator: RecoveryCoordinator | None = None
    download_job_service: DownloadJobService | None = None
    secret_vault: SecretVault | None = None
    connector_http_session: ConnectorHttpSession | None = None
    connector_registry: ConnectorRegistry | None = None
    fmd2_connector: Fmd2Connector | None = None
    fmd2_catalog_refresh_service: Fmd2CatalogRefreshService | None = None
    download_http_client: httpx.AsyncClient | None = None
    download_transport: DownloadTransport | None = None
    download_worker: DownloadWorker | None = None
    download_dispatcher: DownloadDispatcher | None = None
    job_pause_controller: JobPauseController | None = None
    backpressure_manager: BackpressureManager | None = None
    resource_monitor: ResourceMonitorService | None = None
    reader_asset_service: ReaderAssetService | None = None
    cbr_export_service: CbrExportService | None = None
    reader_progress_service: ReaderProgressService | None = None
    reader_settings_service: ReaderSettingsService | None = None
    recommendations_service: RecommendationsService | None = None
    reader_session_service: ReaderSessionService | OnlineReaderSessionService | None = None
    usb_export_job_service: UsbExportJobService | None = None
