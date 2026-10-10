"""Explicit translations between domain entities and persistence rows."""

from app.persistence.mappers.asset import asset_from_row, asset_to_row
from app.persistence.mappers.chapter import chapter_from_row, chapter_to_row
from app.persistence.mappers.chapter_cbz_asset import (
    chapter_cbz_asset_from_row,
    chapter_cbz_asset_to_row,
)
from app.persistence.mappers.chapter_source import chapter_source_from_row, chapter_source_to_row
from app.persistence.mappers.download_job import download_job_from_row, download_job_to_row
from app.persistence.mappers.download_page import download_page_from_row, download_page_to_row
from app.persistence.mappers.export_job import export_job_from_row, export_job_to_row
from app.persistence.mappers.reader_progress import (
    reader_progress_from_row,
    reader_progress_to_row,
)
from app.persistence.mappers.series import series_from_row, series_to_row
from app.persistence.mappers.series_source import series_source_from_row, series_source_to_row
from app.persistence.mappers.setting import setting_from_row, setting_to_row
from app.persistence.mappers.source import (
    connector_health_from_row,
    connector_health_to_row,
    source_from_row,
    source_to_row,
)

__all__ = [
    "asset_from_row",
    "asset_to_row",
    "chapter_from_row",
    "chapter_cbz_asset_from_row",
    "chapter_cbz_asset_to_row",
    "chapter_source_from_row",
    "chapter_source_to_row",
    "download_job_from_row",
    "download_job_to_row",
    "download_page_from_row",
    "download_page_to_row",
    "export_job_from_row",
    "export_job_to_row",
    "reader_progress_from_row",
    "reader_progress_to_row",
    "setting_from_row",
    "setting_to_row",
    "chapter_to_row",
    "connector_health_from_row",
    "connector_health_to_row",
    "series_from_row",
    "series_source_from_row",
    "series_source_to_row",
    "series_to_row",
    "source_from_row",
    "source_to_row",
]
