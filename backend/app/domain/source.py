from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from app.domain.timestamps import normalise_utc


class SourceHealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    BROKEN = "BROKEN"
    DISABLED = "DISABLED"
    INCOMPATIBLE = "INCOMPATIBLE"


@dataclass(frozen=True, slots=True)
class Source:
    source_id: UUID
    connector_id: str
    connector_version: str
    display_name: str
    enabled: bool
    health_status: SourceHealthStatus
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.source_id.version != 4:
            raise ValueError("source_id must be a UUIDv4")
        if not self.connector_id:
            raise ValueError("connector_id must not be empty")
        if not self.connector_version:
            raise ValueError("connector_version must not be empty")
        if not self.display_name:
            raise ValueError("display_name must not be empty")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        try:
            health_status = SourceHealthStatus(self.health_status)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "health_status must use a supported SourceHealthStatus"
            ) from error
        object.__setattr__(self, "health_status", health_status)

        created_at = normalise_utc(self.created_at, "created_at")
        updated_at = normalise_utc(self.updated_at, "updated_at")
        if updated_at < created_at:
            raise ValueError("updated_at must not precede created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

    @classmethod
    def new_local(
        cls,
        connector_id: str,
        connector_version: str,
        display_name: str,
        enabled: bool,
        health_status: SourceHealthStatus,
        *,
        source_id: UUID | None = None,
        now: datetime | None = None,
    ) -> Source:
        created_at = now if now is not None else datetime.now(UTC)
        return cls(
            source_id=source_id if source_id is not None else uuid4(),
            connector_id=connector_id,
            connector_version=connector_version,
            display_name=display_name,
            enabled=enabled,
            health_status=health_status,
            created_at=created_at,
            updated_at=created_at,
        )
