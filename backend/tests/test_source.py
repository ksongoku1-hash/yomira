from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from support.sqlite import open_test_database

from app.domain import CircuitState, ConnectorHealth, Source, SourceHealthStatus
from app.persistence.base import PersistenceBase
from app.persistence.mappers.source import (
    connector_health_from_row,
    connector_health_to_row,
    source_from_row,
    source_to_row,
)
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models import ConnectorHealthRow, SourceRow

NOW = datetime(2026, 9, 25, 12, 30, 45, 678_901, tzinfo=UTC)
NOW_US = datetime_to_epoch_us(NOW)


def test_source_preserves_connector_identity_and_normalizes_time() -> None:
    source_time = datetime(
        2026,
        9,
        25,
        8,
        30,
        45,
        678_901,
        tzinfo=timezone(timedelta(hours=-4)),
    )
    source = Source.new_local(
        "Connector / Region A",
        "build-v2+future",
        "  Custom display name  ",
        True,
        SourceHealthStatus.HEALTHY,
        now=source_time,
    )

    assert source.source_id.version == 4
    assert source.connector_id == "Connector / Region A"
    assert source.connector_version == "build-v2+future"
    assert source.display_name == "  Custom display name  "
    assert source.enabled is True
    assert source.health_status is SourceHealthStatus.HEALTHY
    assert source.created_at == source_time.astimezone(UTC)
    assert source.updated_at == source.created_at


def test_source_rejects_invalid_identity_text_and_naive_timestamp() -> None:
    legacy_id = UUID("123e4567-e89b-12d3-a456-426614174000")
    with pytest.raises(ValueError, match="source_id must be a UUIDv4"):
        Source(
            source_id=legacy_id,
            connector_id="connector",
            connector_version="1",
            display_name="Connector",
            enabled=True,
            health_status=SourceHealthStatus.HEALTHY,
            created_at=NOW,
            updated_at=NOW,
        )

    with pytest.raises(ValueError, match="connector_id must not be empty"):
        Source.new_local("", "1", "Connector", True, SourceHealthStatus.HEALTHY, now=NOW)

    with pytest.raises(ValueError, match="connector_version must not be empty"):
        Source.new_local("connector", "", "Connector", True, SourceHealthStatus.HEALTHY, now=NOW)

    with pytest.raises(ValueError, match="timezone information"):
        Source.new_local(
            "connector",
            "future-version",
            "Connector",
            True,
            SourceHealthStatus.HEALTHY,
            now=datetime(2026, 9, 25),
        )


def test_connector_health_preserves_event_timestamps_and_circuit_state() -> None:
    health = ConnectorHealth(
        source_id=uuid4(),
        last_success_at=NOW - timedelta(days=1, microseconds=7),
        last_failure_at=None,
        circuit_state=CircuitState.HALF_OPEN,
        updated_at=NOW,
    )

    restored = connector_health_from_row(connector_health_to_row(health))

    assert restored == health
    assert restored.last_success_at == NOW - timedelta(days=1, microseconds=7)
    assert restored.last_failure_at is None
    assert restored.circuit_state is CircuitState.HALF_OPEN


def test_source_and_connector_health_reject_unsupported_statuses() -> None:
    with pytest.raises(ValueError, match="supported SourceHealthStatus"):
        Source.new_local(
            "connector",
            "1",
            "Connector",
            True,
            cast(SourceHealthStatus, "UNKNOWN"),
            now=NOW,
        )

    with pytest.raises(ValueError, match="supported CircuitState"):
        ConnectorHealth(
            source_id=uuid4(),
            last_success_at=None,
            last_failure_at=None,
            circuit_state=cast(CircuitState, "UNKNOWN"),
            updated_at=NOW,
        )


def test_source_and_health_metadata_have_resolved_restricted_foreign_key() -> None:
    assert SourceRow.__tablename__ == "sources"
    assert ConnectorHealthRow.__tablename__ == "connector_health"
    sorted_table_names = {table.name for table in PersistenceBase.metadata.sorted_tables}
    assert {"series", "sources", "connector_health"} <= sorted_table_names
    foreign_key = next(iter(ConnectorHealthRow.__table__.foreign_keys))
    assert foreign_key.target_fullname == "sources.source_id"
    assert foreign_key.ondelete == "RESTRICT"


def _insert_source(
    session: Session,
    source_id: str,
    connector_id: str,
    connector_version: str,
    display_name: str,
    enabled: bool | int,
    health_status: str,
    created_at_us: int | float,
    updated_at_us: int | float,
) -> None:
    session.execute(
        text(
            "INSERT INTO sources (source_id, connector_id, connector_version, display_name, "
            "enabled, health_status, created_at_us, updated_at_us) VALUES "
            "(:source_id, :connector_id, :connector_version, :display_name, :enabled, "
            ":health_status, :created_at_us, :updated_at_us)"
        ),
        {
            "source_id": source_id,
            "connector_id": connector_id,
            "connector_version": connector_version,
            "display_name": display_name,
            "enabled": enabled,
            "health_status": health_status,
            "created_at_us": created_at_us,
            "updated_at_us": updated_at_us,
        },
    )


def test_source_schema_enforces_values_indexes_and_health_restrict(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    try:
        report = runner.upgrade_to_head()
        assert report.current_revision == "0024_reader_sessions"

        source = Source.new_local(
            "shared-connector-id",
            "version-not-parsed",
            "Source One",
            True,
            SourceHealthStatus.HEALTHY,
            now=NOW,
        )
        health = ConnectorHealth(
            source_id=source.source_id,
            last_success_at=NOW - timedelta(hours=3),
            last_failure_at=NOW - timedelta(days=2),
            circuit_state=CircuitState.CLOSED,
            updated_at=NOW,
        )
        source_row = source_to_row(source)
        health_row = connector_health_to_row(health)
        with database.write_session() as session:
            session.add(source_row)
            session.flush()
            session.add(health_row)

        with database.read_session() as session:
            persisted_source = session.get(SourceRow, str(source.source_id))
            persisted_health = session.get(ConnectorHealthRow, str(source.source_id))
            assert persisted_source is not None
            assert persisted_health is not None
            assert source_from_row(persisted_source) == source
            assert connector_health_from_row(persisted_health) == health

        second_source = Source.new_local(
            "shared-connector-id",
            "version-not-parsed",
            "Source Two",
            False,
            SourceHealthStatus.DISABLED,
            now=NOW,
        )
        with database.write_session() as session:
            session.add(source_to_row(second_source))
        with database.read_session() as session:
            duplicate_connector_rows = session.execute(
                text("SELECT count(*) FROM sources WHERE connector_id = 'shared-connector-id'")
            ).scalar_one()
            source_indexes = {
                row[1]
                for row in session.connection()
                .exec_driver_sql("PRAGMA index_list('sources')")
                .all()
            }
            source_health = session.execute(
                text("SELECT health_status FROM sources WHERE source_id = :source_id"),
                {"source_id": str(source.source_id)},
            ).scalar_one()
        assert duplicate_connector_rows == 2
        assert {"ix_sources_connector_id", "ix_sources_enabled", "ix_sources_health_status"} <= (
            source_indexes
        )
        assert source_health == "HEALTHY"

        invalid_rows: tuple[
            tuple[str, str, str, str, bool | int, str, int | float, int | float], ...
        ] = (
            ("not-a-uuid", "connector", "1", "Name", True, "HEALTHY", NOW_US, NOW_US),
            (
                "123e4567-e89b-12d3-a456-426614174000",
                "connector",
                "1",
                "Name",
                True,
                "HEALTHY",
                NOW_US,
                NOW_US,
            ),
            (str(uuid4()).upper(), "connector", "1", "Name", True, "HEALTHY", NOW_US, NOW_US),
            (str(uuid4()), "", "1", "Name", True, "HEALTHY", NOW_US, NOW_US),
            (str(uuid4()), "connector", "", "Name", True, "HEALTHY", NOW_US, NOW_US),
            (str(uuid4()), "connector", "1", "", True, "HEALTHY", NOW_US, NOW_US),
            (str(uuid4()), "connector", "1", "Name", 2, "HEALTHY", NOW_US, NOW_US),
            (str(uuid4()), "connector", "1", "Name", True, "UNKNOWN", NOW_US, NOW_US),
            (str(uuid4()), "connector", "1", "Name", True, "HEALTHY", NOW_US, NOW_US - 1),
            (str(uuid4()), "connector", "1", "Name", True, "HEALTHY", 1.5, 1.5),
        )
        for row in invalid_rows:
            with pytest.raises(IntegrityError):
                with database.write_session() as session:
                    _insert_source(session, *row)

        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text(
                        "INSERT INTO connector_health (source_id, circuit_state, updated_at_us) "
                        "VALUES (:source_id, 'CLOSED', :updated_at_us)"
                    ),
                    {"source_id": str(uuid4()), "updated_at_us": NOW_US},
                )
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text(
                        "INSERT INTO connector_health (source_id, circuit_state, updated_at_us) "
                        "VALUES (:source_id, 'UNKNOWN', :updated_at_us)"
                    ),
                    {"source_id": str(source.source_id), "updated_at_us": NOW_US},
                )
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text("DELETE FROM sources WHERE source_id = :source_id"),
                    {"source_id": str(source.source_id)},
                )
    finally:
        database.close()
