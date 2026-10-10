from uuid import UUID

from app.domain.connector_health import CircuitState, ConnectorHealth
from app.domain.source import Source, SourceHealthStatus
from app.persistence.mappers.timestamps import datetime_to_epoch_us, epoch_us_to_datetime
from app.persistence.models.source import ConnectorHealthRow, SourceRow


def source_to_row(source: Source) -> SourceRow:
    return SourceRow(
        source_id=str(source.source_id),
        connector_id=source.connector_id,
        connector_version=source.connector_version,
        display_name=source.display_name,
        enabled=source.enabled,
        health_status=source.health_status.value,
        created_at_us=datetime_to_epoch_us(source.created_at),
        updated_at_us=datetime_to_epoch_us(source.updated_at),
    )


def source_from_row(row: SourceRow) -> Source:
    return Source(
        source_id=UUID(row.source_id),
        connector_id=row.connector_id,
        connector_version=row.connector_version,
        display_name=row.display_name,
        enabled=row.enabled,
        health_status=SourceHealthStatus(row.health_status),
        created_at=epoch_us_to_datetime(row.created_at_us),
        updated_at=epoch_us_to_datetime(row.updated_at_us),
    )


def connector_health_to_row(health: ConnectorHealth) -> ConnectorHealthRow:
    return ConnectorHealthRow(
        source_id=str(health.source_id),
        last_success_at_us=(
            datetime_to_epoch_us(health.last_success_at)
            if health.last_success_at is not None
            else None
        ),
        last_failure_at_us=(
            datetime_to_epoch_us(health.last_failure_at)
            if health.last_failure_at is not None
            else None
        ),
        circuit_state=health.circuit_state.value,
        updated_at_us=datetime_to_epoch_us(health.updated_at),
    )


def connector_health_from_row(row: ConnectorHealthRow) -> ConnectorHealth:
    return ConnectorHealth(
        source_id=UUID(row.source_id),
        last_success_at=(
            epoch_us_to_datetime(row.last_success_at_us)
            if row.last_success_at_us is not None
            else None
        ),
        last_failure_at=(
            epoch_us_to_datetime(row.last_failure_at_us)
            if row.last_failure_at_us is not None
            else None
        ),
        circuit_state=CircuitState(row.circuit_state),
        updated_at=epoch_us_to_datetime(row.updated_at_us),
    )
