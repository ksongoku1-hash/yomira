import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import NoReturn

from app.domain.timestamps import normalise_utc


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"JSON constant {value} is not finite JSON.")


@dataclass(frozen=True, slots=True)
class Setting:
    setting_key: str
    value_json: str
    schema_version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if type(self.setting_key) is not str or not self.setting_key:
            raise ValueError("setting_key must not be empty")
        if type(self.value_json) is not str:
            raise ValueError("value_json must be valid finite JSON")
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise ValueError("schema_version must be a positive integer")
        try:
            json.loads(
                self.value_json,
                parse_float=Decimal,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as error:
            raise ValueError("value_json must be valid finite JSON") from error
        object.__setattr__(self, "updated_at", normalise_utc(self.updated_at, "updated_at"))

    @classmethod
    def new_local(
        cls,
        setting_key: str,
        value_json: str,
        *,
        schema_version: int = 1,
        updated_at: datetime | None = None,
    ) -> Setting:
        return cls(
            setting_key=setting_key,
            value_json=value_json,
            schema_version=schema_version,
            updated_at=updated_at if updated_at is not None else datetime.now(UTC),
        )
