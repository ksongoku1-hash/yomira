from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from support.sqlite import open_test_database

from app.domain import Series, SeriesSource, Source, SourceHealthStatus
from app.persistence.base import PersistenceBase
from app.persistence.mappers.series_source import (
    series_source_from_row,
    series_source_to_row,
)
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models import SeriesSourceRow
from app.persistence.models.series import SeriesRow
from app.persistence.models.source import SourceRow

NOW = datetime(2026, 9, 25, 15, 45, 12, 345_678, tzinfo=UTC)
NOW_US = datetime_to_epoch_us(NOW)


def test_series_source_preserves_remote_values_and_normalizes_time() -> None:
    local_time = datetime(
        2026,
        9,
        25,
        11,
        45,
        12,
        345_678,
        tzinfo=timezone(timedelta(hours=-4)),
    )
    series_source = SeriesSource.new_local(
        uuid4(),
        uuid4(),
        remote_id="  /work/001  ",
        remote_url="https://example.invalid/work/001?edition=A",
        remote_title="  Remote title\nkept as provided  ",
        last_checked_at=local_time,
    )

    assert series_source.series_source_id.version == 4
    assert series_source.remote_id == "  /work/001  "
    assert series_source.remote_url == "https://example.invalid/work/001?edition=A"
    assert series_source.remote_title == "  Remote title\nkept as provided  "
    assert series_source.last_checked_at == local_time.astimezone(UTC)


def test_series_source_validates_references_ids_and_timestamp() -> None:
    legacy_id = UUID("123e4567-e89b-12d3-a456-426614174000")
    with pytest.raises(ValueError, match="series_source_id must be a UUIDv4"):
        SeriesSource(
            series_source_id=legacy_id,
            series_id=uuid4(),
            source_id=uuid4(),
            remote_id="work-1",
            remote_url=None,
            remote_title=None,
            last_checked_at=None,
        )

    with pytest.raises(ValueError, match="remote_id or remote_url must be provided"):
        SeriesSource.new_local(uuid4(), uuid4(), remote_title="title only")

    with pytest.raises(ValueError, match="remote_id must not be empty"):
        SeriesSource.new_local(uuid4(), uuid4(), remote_id="", remote_url="/work/1")

    with pytest.raises(ValueError, match="remote_url must not be empty"):
        SeriesSource.new_local(uuid4(), uuid4(), remote_url="")

    with pytest.raises(ValueError, match="timezone information"):
        SeriesSource.new_local(
            uuid4(), uuid4(), remote_url="/work/1", last_checked_at=datetime(2026, 9, 25)
        )


def test_series_source_mapper_round_trips_optional_fields_and_microseconds() -> None:
    association_without_check = SeriesSource.new_local(
        uuid4(), uuid4(), remote_url="/work/without-id", remote_title=""
    )
    association_with_check = SeriesSource.new_local(
        uuid4(),
        uuid4(),
        remote_id="opaque-id-v2",
        remote_title=None,
        last_checked_at=datetime(1969, 12, 31, 23, 59, 59, 123_456, tzinfo=UTC),
    )

    assert series_source_from_row(series_source_to_row(association_without_check)) == (
        association_without_check
    )
    assert series_source_from_row(series_source_to_row(association_with_check)) == (
        association_with_check
    )


def test_series_source_model_has_resolved_restrict_foreign_keys() -> None:
    assert SeriesSourceRow.__tablename__ == "series_sources"
    sorted_table_names = {table.name for table in PersistenceBase.metadata.sorted_tables}
    assert {"series", "sources", "series_sources"} <= sorted_table_names

    foreign_keys = {
        foreign_key.target_fullname: foreign_key
        for foreign_key in SeriesSourceRow.__table__.foreign_keys
    }
    assert foreign_keys["series.series_id"].ondelete == "RESTRICT"
    assert foreign_keys["sources.source_id"].ondelete == "RESTRICT"


def _insert_series_source(
    session: Session,
    series_source_id: str,
    series_id: str,
    source_id: str,
    remote_id: str | None,
    remote_url: str | None,
    remote_title: str | None,
    last_checked_at_us: int | float | None,
) -> None:
    session.execute(
        text(
            "INSERT INTO series_sources (series_source_id, series_id, source_id, remote_id, "
            "remote_url, remote_title, last_checked_at_us) VALUES (:series_source_id, "
            ":series_id, :source_id, :remote_id, :remote_url, :remote_title, :last_checked_at_us)"
        ),
        {
            "series_source_id": series_source_id,
            "series_id": series_id,
            "source_id": source_id,
            "remote_id": remote_id,
            "remote_url": remote_url,
            "remote_title": remote_title,
            "last_checked_at_us": last_checked_at_us,
        },
    )


def test_series_source_migration_preserves_data_and_rejects_ambiguous_references(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    try:
        report = runner.upgrade_to_head()
        assert report.current_revision == "0023_whole_manga_export_size"

        series = Series.new_local("Canonical", "Display", now=NOW)
        second_series = Series.new_local("Another", "Display 2", now=NOW)
        source = Source.new_local(
            "connector-a",
            "1.0",
            "Connector A",
            True,
            SourceHealthStatus.HEALTHY,
            now=NOW,
        )
        other_source = Source.new_local(
            "connector-a",
            "1.0",
            "Connector A profile 2",
            True,
            SourceHealthStatus.HEALTHY,
            now=NOW,
        )
        association = SeriesSource.new_local(
            series.series_id,
            source.source_id,
            remote_id="  /work/001  ",
            remote_url="https://example.invalid/work/001?edition=A",
            remote_title="  Remote title\nkept as provided  ",
            last_checked_at=NOW,
        )
        with database.write_session() as session:
            session.add_all(
                [
                    SeriesRow(
                        series_id=str(series.series_id),
                        canonical_title=series.canonical_title,
                        display_title=series.display_title,
                        description=series.description,
                        status=series.status,
                        language=series.language,
                        cover_asset_id=None,
                        created_at_us=datetime_to_epoch_us(series.created_at),
                        updated_at_us=datetime_to_epoch_us(series.updated_at),
                    ),
                    SeriesRow(
                        series_id=str(second_series.series_id),
                        canonical_title=second_series.canonical_title,
                        display_title=second_series.display_title,
                        description=second_series.description,
                        status=second_series.status,
                        language=second_series.language,
                        cover_asset_id=None,
                        created_at_us=datetime_to_epoch_us(second_series.created_at),
                        updated_at_us=datetime_to_epoch_us(second_series.updated_at),
                    ),
                    SourceRow(
                        source_id=str(source.source_id),
                        connector_id=source.connector_id,
                        connector_version=source.connector_version,
                        display_name=source.display_name,
                        enabled=source.enabled,
                        health_status=source.health_status.value,
                        created_at_us=datetime_to_epoch_us(source.created_at),
                        updated_at_us=datetime_to_epoch_us(source.updated_at),
                    ),
                    SourceRow(
                        source_id=str(other_source.source_id),
                        connector_id=other_source.connector_id,
                        connector_version=other_source.connector_version,
                        display_name=other_source.display_name,
                        enabled=other_source.enabled,
                        health_status=other_source.health_status.value,
                        created_at_us=datetime_to_epoch_us(other_source.created_at),
                        updated_at_us=datetime_to_epoch_us(other_source.updated_at),
                    ),
                ]
            )
            session.flush()
            session.add(series_source_to_row(association))

        with database.read_session() as session:
            persisted = session.get(SeriesSourceRow, str(association.series_source_id))
            assert persisted is not None
            assert series_source_from_row(persisted) == association
            index_names = {
                row[1]
                for row in session.connection()
                .exec_driver_sql("PRAGMA index_list('series_sources')")
                .all()
            }
        assert {
            "ix_series_sources_series_id",
            "ix_series_sources_source_id",
            "uq_series_sources_source_remote_id",
            "uq_series_sources_source_remote_url",
        } <= index_names

        # The same remote identity is allowed in a different configured Source.
        with database.write_session() as session:
            session.add(
                series_source_to_row(
                    SeriesSource.new_local(
                        second_series.series_id,
                        other_source.source_id,
                        remote_id="  /work/001  ",
                        remote_url="https://example.invalid/work/001?edition=A",
                    )
                )
            )

        # Multiple distinct remote works can be associated with the same local Series.
        with database.write_session() as session:
            session.add(
                series_source_to_row(
                    SeriesSource.new_local(
                        series.series_id,
                        source.source_id,
                        remote_id="work-2",
                        remote_title=association.remote_title,
                    )
                )
            )

        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                _insert_series_source(
                    session,
                    str(uuid4()),
                    str(second_series.series_id),
                    str(source.source_id),
                    "  /work/001  ",
                    "https://example.invalid/other-path",
                    "Different title",
                    None,
                )
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                _insert_series_source(
                    session,
                    str(uuid4()),
                    str(second_series.series_id),
                    str(source.source_id),
                    "different-id",
                    "https://example.invalid/work/001?edition=A",
                    "Same URL",
                    None,
                )

        invalid_references: tuple[tuple[str | None, str | None], ...] = (
            (None, None),
            ("", None),
            (None, ""),
        )
        for remote_id, remote_url in invalid_references:
            with pytest.raises(IntegrityError):
                with database.write_session() as session:
                    _insert_series_source(
                        session,
                        str(uuid4()),
                        str(series.series_id),
                        str(source.source_id),
                        remote_id,
                        remote_url,
                        "title must not act as identity",
                        NOW_US,
                    )

        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                _insert_series_source(
                    session,
                    str(uuid4()),
                    str(uuid4()),
                    str(source.source_id),
                    "remote-with-missing-series",
                    None,
                    None,
                    None,
                )
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                _insert_series_source(
                    session,
                    str(uuid4()),
                    str(series.series_id),
                    str(uuid4()),
                    "remote-with-missing-source",
                    None,
                    None,
                    None,
                )

        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text("DELETE FROM series WHERE series_id = :series_id"),
                    {"series_id": str(series.series_id)},
                )
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text("DELETE FROM sources WHERE source_id = :source_id"),
                    {"source_id": str(source.source_id)},
                )

        invalid_ids = (
            "not-a-uuid",
            "123e4567-e89b-12d3-a456-426614174000",
            str(uuid4()).upper(),
        )
        for invalid_id in invalid_ids:
            with pytest.raises(IntegrityError):
                with database.write_session() as session:
                    _insert_series_source(
                        session,
                        invalid_id,
                        str(second_series.series_id),
                        str(other_source.source_id),
                        f"remote-{uuid4()}",
                        None,
                        None,
                        1.5,
                    )

        with pytest.raises(IntegrityError):
            with database.write_session() as session:
                _insert_series_source(
                    session,
                    str(uuid4()),
                    str(second_series.series_id),
                    str(other_source.source_id),
                    f"remote-{uuid4()}",
                    None,
                    None,
                    1.5,
                )
    finally:
        database.close()
