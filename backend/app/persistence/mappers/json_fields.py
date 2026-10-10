import json
from typing import cast


def encode_optional_string_tuple(value: tuple[str, ...] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))


def decode_optional_string_tuple(value: str | None, field_name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    try:
        decoded: object = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{field_name} must be valid JSON") from error
    if not isinstance(decoded, list):
        raise ValueError(f"{field_name} must be a JSON array of strings")

    items: list[str] = []
    for item in cast(list[object], decoded):
        if type(item) is not str:
            raise ValueError(f"{field_name} must be a JSON array of strings")
        items.append(item)
    return tuple(items)
