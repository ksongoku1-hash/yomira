from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from app.domain.timestamps import normalise_utc

HUNDRED = Decimal("100")
BASIS_POINTS_PER_PERCENT = Decimal("100")


@dataclass(frozen=True, slots=True)
class ReaderProgress:
    series_id: UUID
    chapter_id: UUID
    page_index: int
    percentage: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("series_id", "chapter_id"):
            if getattr(self, field_name).version != 4:
                raise ValueError(f"{field_name} must be a UUIDv4")
        if type(self.page_index) is not int or self.page_index < 0:
            raise ValueError("page_index must be a non-negative integer")
        if type(self.percentage) is not Decimal or not self.percentage.is_finite():
            raise ValueError("percentage must be a finite Decimal")
        if self.percentage < 0 or self.percentage > HUNDRED:
            raise ValueError("percentage must be between 0 and 100 inclusive")
        basis_points = self.percentage * BASIS_POINTS_PER_PERCENT
        if basis_points != basis_points.to_integral_value():
            raise ValueError("percentage must be exactly representable to 0.01")

        object.__setattr__(self, "updated_at", normalise_utc(self.updated_at, "updated_at"))

    @property
    def percentage_basis_points(self) -> int:
        return int(self.percentage * BASIS_POINTS_PER_PERCENT)

    @classmethod
    def new_local(
        cls,
        series_id: UUID,
        chapter_id: UUID,
        page_index: int,
        percentage: Decimal,
        *,
        updated_at: datetime | None = None,
    ) -> ReaderProgress:
        return cls(
            series_id=series_id,
            chapter_id=chapter_id,
            page_index=page_index,
            percentage=percentage,
            updated_at=updated_at if updated_at is not None else datetime.now(UTC),
        )
