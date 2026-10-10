import asyncio
import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any
from uuid import UUID

import mock_manga_server
import pytest
from PIL import Image
from test_download_worker import _exercise_download  # pyright: ignore[reportPrivateUsage]

from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.domain.metadata import MetadataField, MetadataValue
from app.domain.series_source import SeriesSource
from app.domain.source import Source, SourceHealthStatus
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.series_source import series_source_to_row
from app.persistence.mappers.source import source_to_row
from app.services.cbz_builder import (
    CBZBuilder,
    CBZBuildResult,
    ComicInfoMetadata,
    ValidatedPage,
)
from app.services.jobs import DownloadJobService, DownloadJobSummary
from app.services.metadata import MetadataService


def _png(color: str) -> bytes:
    buffer = io.BytesIO()
    with Image.new("RGB", (2, 2), color) as image:
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def _seed_metadata(
    database: SQLiteDatabase,
    series_id: UUID,
    variant: str,
) -> dict[str, str]:
    service = MetadataService(database)
    association = service.list_sources(series_id)[0]
    assert service.refresh_source(
        association.series_source_id,
        ConnectorSeriesMetadata(
            title="Remote title",
            cover_url="https://cover.invalid/cover.jpg",
            author="Remote writer",
            description="Remote <summary>",
            genres=("Adventure", "Mystery"),
            status="ongoing",
            language="ja",
        ),
    )
    expected = {
        "Title": "Mock Fixture Series",
        "Series": "Mock Fixture Series",
        "Summary": "Remote <summary>",
        "Writer": "Remote writer",
        "Genre": "Adventure, Mystery",
        "LanguageISO": "ja",
    }
    if variant == "source":
        return expected
    if variant == "conflict":
        other = Source.new_local(
            "other.mock", "1.0.0", "Other source", True, SourceHealthStatus.HEALTHY
        )
        with database.write_session() as session:
            session.add(source_to_row(other))
            session.flush()
            session.add(
                series_source_to_row(
                    SeriesSource.new_local(
                        series_id,
                        other.source_id,
                        remote_id="alternate-1",
                        remote_title="Conflicting title",
                        remote_author="Conflicting writer",
                        remote_description="Conflicting summary",
                        remote_genres=("Conflicting genre",),
                        remote_language="ko",
                    )
                )
            )
        return {"Title": "Mock Fixture Series", "Series": "Mock Fixture Series"}

    local_values: dict[MetadataField, MetadataValue] = {
        MetadataField.TITLE: "Local title",
        MetadataField.DESCRIPTION: "Local summary",
        MetadataField.AUTHOR: "Local writer",
        MetadataField.GENRES: ("Local", "Drama"),
        MetadataField.LANGUAGE: "es",
    }
    for field, value in local_values.items():
        assert service.set_local_value(series_id, field, value)
    if variant == "local":
        return {
            "Title": "Local title",
            "Series": "Local title",
            "Summary": "Local summary",
            "Writer": "Local writer",
            "Genre": "Local, Drama",
            "LanguageISO": "es",
        }
    if variant in {"null", "empty"}:
        for field in local_values:
            value = None if variant == "null" else (() if field is MetadataField.GENRES else "")
            assert service.set_user_override(series_id, field, value)
        return {}

    overrides: dict[MetadataField, MetadataValue] = {
        MetadataField.TITLE: "Override title",
        MetadataField.DESCRIPTION: "Override summary",
        MetadataField.AUTHOR: "Override writer",
        MetadataField.GENRES: ("Override", "Fantasy"),
        MetadataField.LANGUAGE: "ko",
    }
    for field, value in overrides.items():
        assert service.set_user_override(series_id, field, value)
    return {
        "Title": "Override title",
        "Series": "Override title",
        "Summary": "Override summary",
        "Writer": "Override writer",
        "Genre": "Override, Fantasy",
        "LanguageISO": "ko",
    }


@pytest.mark.parametrize("variant", ("source", "local", "override", "null", "empty", "conflict"))
def test_pipeline_comicinfo_uses_effective_metadata_and_preserves_original_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    page_payloads = {"page-1": _png("blue"), "page-2": _png("red")}
    request_paths: list[str] = []
    normal_response = mock_manga_server._normal_response  # pyright: ignore[reportPrivateUsage]

    def distinct_page_response(
        path: str,
        query_string: str,
        *,
        page_count: int = 2,
        duplicate_pages: bool = False,
    ) -> tuple[int, str, bytes]:
        request_paths.append(path)
        if path.startswith("/api/v1/pages/mock-chapter-1/"):
            return 200, "image/png", page_payloads[path.rsplit("/", 1)[1]]
        return normal_response(
            path, query_string, page_count=page_count, duplicate_pages=duplicate_pages
        )

    monkeypatch.setattr(mock_manga_server, "_normal_response", distinct_page_response)
    create_selected_jobs = DownloadJobService.create_selected_jobs
    fixture_database: SQLiteDatabase | None = None
    expected_fields: dict[str, str] = {}

    def create_jobs_with_metadata(
        service: DownloadJobService,
        **kwargs: Any,
    ) -> tuple[DownloadJobSummary, ...]:
        nonlocal fixture_database, expected_fields
        jobs = create_selected_jobs(service, **kwargs)
        fixture_database = service._database  # pyright: ignore[reportPrivateUsage]
        expected_fields = _seed_metadata(fixture_database, jobs[0].series_id, variant)
        return jobs

    monkeypatch.setattr(DownloadJobService, "create_selected_jobs", create_jobs_with_metadata)
    build = CBZBuilder.build
    builds = 0

    def verify_metadata_snapshot_before_build(
        builder: CBZBuilder,
        *,
        destination: Path,
        metadata: ComicInfoMetadata,
        pages: tuple[ValidatedPage, ...],
        job_id: UUID | str | None = None,
    ) -> CBZBuildResult:
        nonlocal builds
        assert fixture_database is not None
        assert fixture_database._active_sessions == 0  # pyright: ignore[reportPrivateUsage]
        assert tuple(page.ordinal for page in pages) == (1, 2, 3)
        assert tuple(page.path.read_bytes() for page in pages) == (
            page_payloads["page-2"],
            page_payloads["page-1"],
            page_payloads["page-1"],
        )
        builds += 1
        return build(
            builder, destination=destination, metadata=metadata, pages=pages, job_id=job_id
        )

    monkeypatch.setattr(CBZBuilder, "build", verify_metadata_snapshot_before_build)
    asyncio.run(
        _exercise_download(
            tmp_path,
            library_root=tmp_path / "data" / "library",
        )
    )

    assert builds == 1
    assert request_paths == [
        "/api/v1/pages/mock-chapter-1/page-2",
        "/api/v1/pages/mock-chapter-1/page-1",
        "/api/v1/pages/mock-chapter-1/page-1",
    ]
    cbzs = tuple((tmp_path / "data" / "library" / "chapters").rglob("*.cbz"))
    assert len(cbzs) == 1
    with zipfile.ZipFile(cbzs[0]) as archive:
        assert archive.namelist() == ["ComicInfo.xml", "001.png", "002.png", "003.png"]
        assert archive.testzip() is None
        comic_info = ET.fromstring(archive.read("ComicInfo.xml"))
        assert {element.tag: element.text for element in comic_info} == {
            **expected_fields,
            "PageCount": "3",
        }
        assert tuple(archive.read(f"{ordinal:03d}.png") for ordinal in range(1, 4)) == (
            page_payloads["page-2"],
            page_payloads["page-1"],
            page_payloads["page-1"],
        )
    assert not (tmp_path / "data" / "library" / "covers").exists()
