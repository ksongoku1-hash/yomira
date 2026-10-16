import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy import func, select
from test_library_api import _configured_app, _get  # pyright: ignore[reportPrivateUsage]

from app.connectors.metadata import ConnectorCapability, ConnectorMetadata
from app.connectors.mock import MockConnector
from app.connectors.registry import ConnectorRegistry
from app.domain.series import Series
from app.domain.series_source import SeriesSource
from app.domain.source import Source, SourceHealthStatus
from app.fmd2_bridge import FMD2_METADATA, FMD2_PUBLIC_SOURCES
from app.persistence.mappers.series_source import series_source_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.models.download_job import DownloadJobRow


def test_empty_library_series_exposes_only_enabled_compatible_reading_sources(
    tmp_path: Path,
) -> None:
    app, database, library = _configured_app(tmp_path)
    series = Series.new_local("The Boxer", "The Boxer")
    library.add_series(series)
    full = MockConnector().metadata
    descriptors = [
        full,
        replace(full, connector_id="yomira.disabled"),
        replace(
            full,
            connector_id="yomira.metadata",
            capabilities=frozenset({ConnectorCapability.METADATA}),
        ),
        replace(full, connector_id="yomira.old"),
        FMD2_METADATA,
    ]
    registry = Mock(spec=ConnectorRegistry)
    def descriptor_for(name: str) -> ConnectorMetadata:
        return next(d for d in descriptors if d.connector_id == name)

    registry.get_metadata.side_effect = descriptor_for
    app.state.runtime.connector_registry = registry
    with database.write_session() as session:
        for index, descriptor in enumerate(descriptors):
            source = Source.new_local(
                descriptor.connector_id,
                "0.0.1" if index == 3 else descriptor.connector_version,
                descriptor.connector_id,
                index != 1,
                SourceHealthStatus.HEALTHY,
            )
            session.add(source_to_row(source))
            session.flush()
            remote = (
                "work"
                if index != 4
                else "fmd2:v1:"
                + (next(iter(FMD2_PUBLIC_SOURCES)) + "\n/en/fixture/list").encode().hex()
            )
            link = SeriesSource.new_local(
                series.series_id,
                source.source_id,
                remote_id=remote,
                remote_author="Author",
                remote_description="Source synopsis",
                remote_genres=("Drama",),
            )
            session.add(series_source_to_row(link))
    try:
        response = asyncio.run(_get(f"/api/v1/series/{series.series_id}", app))
        assert response.status_code == 200
        reading = response.json()["data"]["reading_sources"]
        assert {item["connector_id"] for item in reading} == {
            full.connector_id,
            FMD2_METADATA.connector_id,
        }
        assert (
            next(item for item in reading if item["connector_id"] == FMD2_METADATA.connector_id)[
                "display_name"
            ]
            == "WebToons"
        )
        assert all(item["description"] == "Source synopsis" for item in reading)
        assert asyncio.run(_get(f"/api/v1/series/{uuid4()}", app)).status_code == 404
        with database.read_session() as session:
            assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 0
    finally:
        database.close()
