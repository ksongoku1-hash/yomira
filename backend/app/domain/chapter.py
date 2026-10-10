import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4


def chapter_number(label: str) -> Decimal | None:
    # Source ordinals are not necessarily the printed chapter number.
    text = unicodedata.normalize("NFKD", label).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c)).strip()
    match = re.fullmatch(
        r"(?:(?:capitulo|cap|chapter|ch|episodio|episode)\s*[.:#-]?\s*)?"
        r"(\d+(?:[.,]\d+)?)",
        text,
    )
    return Decimal(match[1].replace(",", ".")) if match else None


@dataclass(frozen=True, slots=True)
class Chapter:
    chapter_id: UUID
    series_id: UUID
    chapter_raw_label: str
    chapter_major: int | None
    chapter_minor: int | None
    chapter_variant: str | None
    chapter_type: str | None
    chapter_sort_key: tuple[int, ...]
    volume_number: int | None
    language: str | None
    display_label: str

    def __post_init__(self) -> None:
        for field_name in ("chapter_id", "series_id"):
            if getattr(self, field_name).version != 4:
                raise ValueError(f"{field_name} must be a UUIDv4")
        if not self.chapter_raw_label:
            raise ValueError("chapter_raw_label must not be empty")
        if not self.display_label:
            raise ValueError("display_label must not be empty")

        for field_name in ("chapter_major", "chapter_minor", "volume_number"):
            value = getattr(self, field_name)
            if value is not None and type(value) is not int:
                raise ValueError(f"{field_name} must be an integer or None")

        try:
            sort_key = tuple(self.chapter_sort_key)
        except TypeError as error:
            raise ValueError("chapter_sort_key must be a sequence of integers") from error
        if any(type(value) is not int for value in sort_key):
            raise ValueError("chapter_sort_key must contain only integers")
        object.__setattr__(self, "chapter_sort_key", sort_key)

    @classmethod
    def new_local(
        cls,
        series_id: UUID,
        chapter_raw_label: str,
        display_label: str,
        *,
        chapter_sort_key: tuple[int, ...],
        chapter_major: int | None = None,
        chapter_minor: int | None = None,
        chapter_variant: str | None = None,
        chapter_type: str | None = None,
        volume_number: int | None = None,
        language: str | None = None,
        chapter_id: UUID | None = None,
    ) -> Chapter:
        return cls(
            chapter_id=chapter_id if chapter_id is not None else uuid4(),
            series_id=series_id,
            chapter_raw_label=chapter_raw_label,
            chapter_major=chapter_major,
            chapter_minor=chapter_minor,
            chapter_variant=chapter_variant,
            chapter_type=chapter_type,
            chapter_sort_key=chapter_sort_key,
            volume_number=volume_number,
            language=language,
            display_label=display_label,
        )
