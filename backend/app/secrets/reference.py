"""Opaque identifiers used to refer to secrets without containing their values."""

from dataclasses import dataclass, field
from uuid import UUID, uuid4

_REFERENCE_PREFIX = "yomira-secret:v1:"


@dataclass(frozen=True, slots=True)
class SecretReference:
    id: UUID = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.id) is not UUID:
            raise TypeError("id must be a UUID")

    @classmethod
    def generate(cls) -> SecretReference:
        return cls(uuid4())

    @classmethod
    def parse(cls, value: str) -> SecretReference:
        if type(value) is not str or not value.startswith(_REFERENCE_PREFIX):
            raise ValueError("Invalid secret reference.")
        identifier_text = value[len(_REFERENCE_PREFIX) :]
        try:
            identifier = UUID(identifier_text)
        except (AttributeError, ValueError) as error:
            raise ValueError("Invalid secret reference.") from error
        if str(identifier) != identifier_text:
            raise ValueError("Invalid secret reference.")
        return cls(identifier)

    @property
    def value(self) -> str:
        return f"{_REFERENCE_PREFIX}{self.id}"

    def __repr__(self) -> str:
        return "SecretReference(<opaque>)"
