from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase

SOURCE_ID_UUID4_CHECK = (
    "typeof(source_id) = 'text' "
    "AND length(source_id) = 36 "
    "AND length(replace(source_id, '-', '')) = 32 "
    "AND substr(source_id, 9, 1) = '-' "
    "AND substr(source_id, 14, 1) = '-' "
    "AND substr(source_id, 19, 1) = '-' "
    "AND substr(source_id, 24, 1) = '-' "
    "AND substr(source_id, 15, 1) = '4' "
    "AND substr(source_id, 20, 1) IN ('8', '9', 'a', 'b') "
    "AND source_id = lower(source_id) "
    "AND source_id NOT GLOB '*[^0-9a-f-]*'"
)
SOURCE_HEALTH_STATUSES = (
    "'HEALTHY', 'DEGRADED', 'BROKEN', 'DISABLED', 'INCOMPATIBLE'"
)
CIRCUIT_STATES = "'CLOSED', 'OPEN', 'HALF_OPEN'"


class SourceRow(PersistenceBase):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(SOURCE_ID_UUID4_CHECK, name="source_id_uuid4"),
        CheckConstraint("length(connector_id) > 0", name="connector_id_nonempty"),
        CheckConstraint("length(connector_version) > 0", name="connector_version_nonempty"),
        CheckConstraint("length(display_name) > 0", name="display_name_nonempty"),
        CheckConstraint("enabled IN (0, 1)", name="enabled_boolean"),
        CheckConstraint(
            f"health_status IN ({SOURCE_HEALTH_STATUSES})",
            name="health_status_supported",
        ),
        CheckConstraint(
            "typeof(created_at_us) = 'integer' AND typeof(updated_at_us) = 'integer'",
            name="timestamps_integer",
        ),
        CheckConstraint("updated_at_us >= created_at_us", name="timestamps_ordered"),
        Index("ix_sources_connector_id", "connector_id"),
        Index("ix_sources_enabled", "enabled"),
        Index("ix_sources_health_status", "health_status"),
    )

    source_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    connector_id: Mapped[str] = mapped_column(Text(), nullable=False)
    connector_version: Mapped[str] = mapped_column(Text(), nullable=False)
    display_name: Mapped[str] = mapped_column(Text(), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    health_status: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    updated_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)


class ConnectorHealthRow(PersistenceBase):
    __tablename__ = "connector_health"
    __table_args__ = (
        CheckConstraint(
            f"circuit_state IN ({CIRCUIT_STATES})",
            name="circuit_state_supported",
        ),
        CheckConstraint(
            "last_success_at_us IS NULL OR typeof(last_success_at_us) = 'integer'",
            name="last_success_integer",
        ),
        CheckConstraint(
            "last_failure_at_us IS NULL OR typeof(last_failure_at_us) = 'integer'",
            name="last_failure_integer",
        ),
        CheckConstraint("typeof(updated_at_us) = 'integer'", name="updated_at_integer"),
        Index("ix_connector_health_updated_at_us", "updated_at_us"),
    )

    source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "sources.source_id",
            ondelete="RESTRICT",
            name="fk_connector_health_source_id_sources",
        ),
        primary_key=True,
    )
    last_success_at_us: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    last_failure_at_us: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    circuit_state: Mapped[str] = mapped_column(Text(), nullable=False)
    updated_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
