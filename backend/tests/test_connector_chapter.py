from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from app.connectors.chapter import ConnectorChapter


def test_connector_chapter_preserves_complex_decimal_identity() -> None:
    chapter = _chapter(
        remote_id="chapter-12.10",
        remote_label="12.10",
        chapter_raw_label="12.10",
        chapter_major=12,
        chapter_minor=10,
        chapter_sort_key=(12, 10, 0),
        display_label="12.10",
    )

    assert chapter.remote_id == "chapter-12.10"
    assert chapter.remote_label == "12.10"
    assert chapter.chapter_raw_label == "12.10"
    assert chapter.chapter_major == 12
    assert chapter.chapter_minor == 10
    assert chapter.chapter_sort_key == (12, 10, 0)


def test_connector_chapter_preserves_textual_extra_without_numeric_guessing() -> None:
    extra = _chapter(
        remote_id=None,
        remote_url="/chapter/extra-summer",
        remote_label="Extra verano",
        chapter_raw_label="Extra verano",
        chapter_major=None,
        chapter_minor=None,
        chapter_type="EXTRA",
        chapter_sort_key=(),
        display_label="Extra verano",
    )

    assert extra.chapter_raw_label == "Extra verano"
    assert extra.chapter_major is None
    assert extra.chapter_minor is None
    assert extra.chapter_type == "EXTRA"
    assert extra.chapter_sort_key == ()


def test_connector_chapter_preserves_open_variant_type_language_and_volume() -> None:
    chapter = _chapter(
        chapter_variant="  revised edition ",
        chapter_type="Future special type",
        chapter_sort_key=(12, 10, -2, 9_007_199_254_741_234),
        volume_number=4,
        language="x-future-language",
    )

    assert chapter.chapter_variant == "  revised edition "
    assert chapter.chapter_type == "Future special type"
    assert chapter.chapter_sort_key == (12, 10, -2, 9_007_199_254_741_234)
    assert chapter.volume_number == 4
    assert chapter.language == "x-future-language"


@pytest.mark.parametrize(
    ("remote_id", "remote_url", "message"),
    [
        (None, None, "remote_id or remote_url must be provided"),
        ("", "/chapter/1", "remote_id must not be empty"),
        ("chapter-1", "", "remote_url must not be empty"),
    ],
)
def test_connector_chapter_requires_nonempty_remote_identity(
    remote_id: str | None,
    remote_url: str | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _chapter(remote_id=remote_id, remote_url=remote_url)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("chapter_major", 12.1), ("chapter_minor", True), ("volume_number", False)],
)
def test_connector_chapter_rejects_float_and_boolean_components(
    field_name: str,
    value: object,
) -> None:
    invalid_value = cast(int | None, value)
    with pytest.raises(ValueError, match=f"{field_name} must be an integer or None"):
        _chapter(
            chapter_major=invalid_value if field_name == "chapter_major" else 12,
            chapter_minor=invalid_value if field_name == "chapter_minor" else 10,
            volume_number=invalid_value if field_name == "volume_number" else None,
        )


@pytest.mark.parametrize(
    "sort_key",
    [
        cast(tuple[int, ...], (12, 10.5)),
        cast(tuple[int, ...], (12, True)),
        cast(tuple[int, ...], [12, 10]),
    ],
)
def test_connector_chapter_rejects_noninteger_or_mutable_sort_keys(
    sort_key: tuple[int, ...],
) -> None:
    with pytest.raises(ValueError, match="chapter_sort_key must be a tuple of integers"):
        _chapter(chapter_sort_key=sort_key)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("chapter_raw_label", ""), ("display_label", "")],
)
def test_connector_chapter_requires_nonempty_labels(field_name: str, value: str) -> None:
    with pytest.raises(ValueError, match=f"{field_name} must not be empty"):
        if field_name == "chapter_raw_label":
            _chapter(chapter_raw_label=value)
        else:
            _chapter(display_label=value)


def test_connector_chapter_is_immutable() -> None:
    chapter = _chapter(chapter_sort_key=(12, 10, 0))

    with pytest.raises(FrozenInstanceError):
        chapter.chapter_raw_label = "changed"  # type: ignore[misc]


def _chapter(
    *,
    remote_id: str | None = "chapter-12.10",
    remote_url: str | None = None,
    remote_label: str | None = "Remote 12.10",
    chapter_raw_label: str = "12.10",
    chapter_major: int | None = 12,
    chapter_minor: int | None = 10,
    chapter_variant: str | None = None,
    chapter_type: str | None = None,
    chapter_sort_key: tuple[int, ...] = (12, 10, 0),
    volume_number: int | None = None,
    language: str | None = None,
    display_label: str = "12.10",
) -> ConnectorChapter:
    return ConnectorChapter(
        remote_id=remote_id,
        remote_url=remote_url,
        remote_label=remote_label,
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
