"""Framework-independent Yomira domain types and rules."""

from app.domain.asset import Asset, AssetType
from app.domain.chapter import Chapter
from app.domain.chapter_cbz_asset import ChapterCbzAsset
from app.domain.chapter_source import ChapterSource
from app.domain.connector_health import CircuitState, ConnectorHealth
from app.domain.download_job import DownloadJob, DownloadJobStatus, DownloadPriority
from app.domain.download_page import DownloadPage
from app.domain.metadata import (
    MetadataAuthority,
    MetadataField,
    MetadataOverride,
    MetadataResolution,
    MetadataSourceValue,
)
from app.domain.reader_progress import ReaderProgress
from app.domain.series import Series
from app.domain.series_source import SeriesSource
from app.domain.setting import Setting
from app.domain.source import Source, SourceHealthStatus

__all__ = [
    "Asset",
    "AssetType",
    "Chapter",
    "ChapterCbzAsset",
    "ChapterSource",
    "CircuitState",
    "ConnectorHealth",
    "DownloadJob",
    "DownloadJobStatus",
    "DownloadPriority",
    "DownloadPage",
    "MetadataAuthority",
    "MetadataField",
    "MetadataOverride",
    "MetadataResolution",
    "MetadataSourceValue",
    "ReaderProgress",
    "Setting",
    "Series",
    "SeriesSource",
    "Source",
    "SourceHealthStatus",
]
