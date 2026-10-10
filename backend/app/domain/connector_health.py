from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.timestamps import normalise_utc


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    source_id: UUID
    last_success_at: datetime | None
    last_failure_at: datetime | None
    circuit_state: CircuitState
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.source_id.version != 4:
            raise ValueError("source_id must be a UUIDv4")
        try:
            circuit_state = CircuitState(self.circuit_state)
        except (TypeError, ValueError) as error:
            raise ValueError("circuit_state must use a supported CircuitState") from error
        object.__setattr__(self, "circuit_state", circuit_state)

        updated_at = normalise_utc(self.updated_at, "updated_at")
        object.__setattr__(self, "updated_at", updated_at)
        for field_name in ("last_success_at", "last_failure_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, normalise_utc(value, field_name))
