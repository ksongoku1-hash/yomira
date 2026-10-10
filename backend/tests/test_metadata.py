from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.domain.metadata import (
    MetadataAuthority,
    MetadataField,
    MetadataOverride,
    resolve_series_metadata,
    with_local_metadata_value,
    with_source_metadata_value,
)
from app.domain.series import Series
from app.domain.series_source import SeriesSource

NOW = datetime(2026, 9, 26, 14, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("field", "remote_field", "primary_value", "catalog_value"),
    (
        (MetadataField.COVER, "remote_cover_url", "https://source.invalid/cover.jpg",
         "https://catalog.invalid/cover.jpg"),
        (MetadataField.AUTHOR, "remote_author", "Source author", "Catalog author"),
        (MetadataField.DESCRIPTION, "remote_description", "Source text", "Catalog text"),
        (MetadataField.GENRES, "remote_genres", ("Action",), ("Drama",)),
        (MetadataField.STATUS, "remote_status", "ongoing", "completed"),
        (MetadataField.LANGUAGE, "remote_language", "es", "ja"),
    ),
)
def test_primary_metadata_precedes_catalog_and_keeps_both_candidates(
    field: MetadataField,
    remote_field: str,
    primary_value: str | tuple[str, ...],
    catalog_value: str | tuple[str, ...],
) -> None:
    series = Series.new_local("canonical", "Local title", now=NOW)
    primary = replace(
        SeriesSource.new_local(series.series_id, uuid4(), remote_id="primary"),
        source_display_name="MangaDex",
        **{remote_field: primary_value},
    )
    catalog = replace(
        SeriesSource.new_local(series.series_id, uuid4(), remote_id="catalog"),
        source_connector_id="yomira.metadata.mangadex",
        source_display_name="Original source",
        **{remote_field: catalog_value},
    )

    resolved = {
        item.field: item for item in resolve_series_metadata(series, (catalog, primary), ())
    }[field]

    assert resolved.value == primary_value
    assert resolved.authority is MetadataAuthority.SOURCE_METADATA
    assert resolved.effective_source_ids == (primary.source_id,)
    assert resolved.source_conflict is True
    assert {candidate.value for candidate in resolved.source_values} == {
        primary_value, catalog_value
    }
    assert series == Series.new_local(
        "canonical", "Local title", series_id=series.series_id, now=NOW
    )
    assert primary.remote_title is None


def test_catalog_fills_only_fields_without_a_primary_value() -> None:
    series = Series.new_local("canonical", "Local title", now=NOW)
    primary = replace(
        SeriesSource.new_local(
            series.series_id, uuid4(), remote_id="primary", remote_author="", remote_genres=()
        ),
        source_connector_id="yomira.metadata.mangadex-extra",
    )
    catalog = replace(
        SeriesSource.new_local(
            series.series_id,
            uuid4(),
            remote_id="catalog",
            remote_title="Catalog title",
            remote_author="Catalog author",
            remote_description="Catalog text",
            remote_cover_url="https://catalog.invalid/cover.jpg",
            remote_genres=("Action",),
        ),
        source_connector_id="yomira.metadata.mangadex",
    )

    resolved = {
        item.field: item for item in resolve_series_metadata(series, (primary, catalog), ())
    }

    for field in (MetadataField.COVER, MetadataField.DESCRIPTION):
        assert resolved[field].authority is MetadataAuthority.SOURCE_METADATA
        assert resolved[field].effective_source_ids == (catalog.source_id,)
        assert resolved[field].source_conflict is False
    assert resolved[MetadataField.COVER].value == "https://catalog.invalid/cover.jpg"
    assert resolved[MetadataField.DESCRIPTION].value == "Catalog text"
    assert resolved[MetadataField.AUTHOR].value == ""
    assert resolved[MetadataField.GENRES].value == ()
    assert resolved[MetadataField.AUTHOR].effective_source_ids == (primary.source_id,)
    assert resolved[MetadataField.GENRES].effective_source_ids == (primary.source_id,)
    assert resolved[MetadataField.TITLE].value == "Local title"
    assert resolved[MetadataField.TITLE].authority is MetadataAuthority.LOCAL_CANONICAL


def test_primary_conflict_does_not_select_an_agreeing_catalog() -> None:
    series = Series.new_local("canonical", "Local title", now=NOW)
    primary = SeriesSource.new_local(
        series.series_id, uuid4(), remote_id="primary", remote_author="First author"
    )
    other_primary = SeriesSource.new_local(
        series.series_id, uuid4(), remote_id="other", remote_author="Second author"
    )
    catalog = replace(
        SeriesSource.new_local(
            series.series_id, uuid4(), remote_id="catalog", remote_author="First author"
        ),
        source_connector_id="yomira.metadata.mangadex",
    )

    author = {
        item.field: item
        for item in resolve_series_metadata(series, (primary, other_primary, catalog), ())
    }[MetadataField.AUTHOR]

    assert author.value is None
    assert author.authority is None
    assert author.source_conflict is True
    assert len(author.source_values) == 3
    assert author.effective_source_ids == ()


def test_catalog_agreement_is_not_reported_as_a_primary_effective_source() -> None:
    series = Series.new_local("canonical", "Local title", now=NOW)
    primary = SeriesSource.new_local(
        series.series_id, uuid4(), remote_id="primary", remote_author="Shared author"
    )
    catalog = replace(
        SeriesSource.new_local(
            series.series_id, uuid4(), remote_id="catalog", remote_author="Shared author"
        ),
        source_connector_id="yomira.metadata.mangadex",
    )
    author = {
        item.field: item for item in resolve_series_metadata(series, (primary, catalog), ())
    }[MetadataField.AUTHOR]

    assert author.value == "Shared author"
    assert author.source_conflict is False
    assert len(author.source_values) == 2
    assert author.effective_source_ids == (primary.source_id,)


@pytest.mark.parametrize(
    ("field", "override_value"),
    (
        (MetadataField.AUTHOR, None),
        (MetadataField.AUTHOR, ""),
        (MetadataField.COVER, None),
        (MetadataField.GENRES, ()),
    ),
)
def test_catalog_does_not_replace_explicit_user_null_or_empty_values(
    field: MetadataField,
    override_value: str | tuple[str, ...] | None,
) -> None:
    series = Series.new_local("canonical", "Local title", now=NOW)
    catalog = replace(
        SeriesSource.new_local(
            series.series_id,
            uuid4(),
            remote_id="catalog",
            remote_author="Catalog author",
            remote_cover_url="https://catalog.invalid/cover.jpg",
            remote_genres=("Action",),
        ),
        source_connector_id="yomira.metadata.mangadex",
    )
    override = MetadataOverride.new_local(series.series_id, field, override_value, now=NOW)
    resolved = {
        item.field: item for item in resolve_series_metadata(series, (catalog,), (override,))
    }[field]

    assert resolved.value == override_value
    assert resolved.authority is MetadataAuthority.USER_OVERRIDE
    assert resolved.has_user_override is True
    assert len(resolved.source_values) == 1
    assert resolved.effective_source_ids == ()


def test_conflicting_catalogs_leave_the_field_unresolved() -> None:
    series = Series.new_local("canonical", "Local title", now=NOW)
    catalogs = tuple(
        replace(
            SeriesSource.new_local(
                series.series_id, uuid4(), remote_id=author, remote_author=author
            ),
            source_connector_id="yomira.metadata.mangadex",
        )
        for author in ("First catalog author", "Second catalog author")
    )
    author = {
        item.field: item for item in resolve_series_metadata(series, catalogs, ())
    }[MetadataField.AUTHOR]

    assert author.value is None
    assert author.authority is None
    assert author.source_conflict is True
    assert len(author.source_values) == 2
    assert author.effective_source_ids == ()


def test_metadata_resolves_each_field_by_authority_without_losing_candidates() -> None:
    series = Series.new_local(
        "canonical title",
        "Local title",
        description="Local description",
        genres=(),
        language="es",
        now=NOW,
    )
    source_one = SeriesSource.new_local(
        series.series_id,
        uuid4(),
        remote_id="one",
        remote_title="Source title one",
        remote_cover_url="https://one.invalid/cover.jpg",
        remote_author="Author shared",
        remote_description="Source description one",
        remote_genres=("Action",),
        remote_status="ongoing",
        remote_language="en",
        last_checked_at=NOW,
    )
    source_two = SeriesSource.new_local(
        series.series_id,
        uuid4(),
        remote_id="two",
        remote_title="Source title two",
        remote_cover_url="https://two.invalid/cover.jpg",
        remote_author="Author shared",
        remote_description="Source description two",
        remote_genres=("Action",),
        remote_status="completed",
        remote_language="en",
        last_checked_at=NOW,
    )
    overrides = (
        MetadataOverride.new_local(
            series.series_id,
            MetadataField.TITLE,
            "User title",
            now=NOW,
        ),
        MetadataOverride.new_local(
            series.series_id,
            MetadataField.DESCRIPTION,
            "",
            now=NOW,
        ),
        MetadataOverride.new_local(
            series.series_id,
            MetadataField.COVER,
            None,
            now=NOW,
        ),
    )

    resolved = {
        item.field: item
        for item in resolve_series_metadata(series, (source_two, source_one), overrides)
    }

    assert resolved[MetadataField.TITLE].value == "User title"
    assert resolved[MetadataField.TITLE].authority is MetadataAuthority.USER_OVERRIDE
    assert resolved[MetadataField.DESCRIPTION].value == ""
    assert resolved[MetadataField.DESCRIPTION].has_user_override is True
    assert resolved[MetadataField.COVER].value is None
    assert resolved[MetadataField.COVER].authority is MetadataAuthority.USER_OVERRIDE
    assert resolved[MetadataField.GENRES].value == ()
    assert resolved[MetadataField.GENRES].authority is MetadataAuthority.LOCAL_CANONICAL
    assert resolved[MetadataField.AUTHOR].value == "Author shared"
    assert resolved[MetadataField.AUTHOR].authority is MetadataAuthority.SOURCE_METADATA
    assert len(resolved[MetadataField.AUTHOR].effective_source_ids) == 2
    assert resolved[MetadataField.STATUS].value is None
    assert resolved[MetadataField.STATUS].source_conflict is True
    assert len(resolved[MetadataField.STATUS].source_values) == 2
    assert resolved[MetadataField.LANGUAGE].value == "es"
    assert resolved[MetadataField.LANGUAGE].authority is MetadataAuthority.LOCAL_CANONICAL


def test_metadata_missing_override_and_explicit_null_are_distinct() -> None:
    series = Series.new_local("canonical", "Local title", now=NOW)
    without_override = resolve_series_metadata(series, (), ())[0]
    with_null_override = resolve_series_metadata(
        series,
        (),
        (MetadataOverride.new_local(series.series_id, MetadataField.TITLE, None, now=NOW),),
    )[0]

    assert without_override.has_user_override is False
    assert without_override.value == "Local title"
    assert with_null_override.has_user_override is True
    assert with_null_override.value is None
    assert with_null_override.authority is MetadataAuthority.USER_OVERRIDE


def test_metadata_override_validates_the_value_type_for_its_field() -> None:
    series_id = uuid4()

    try:
        MetadataOverride.new_local(series_id, MetadataField.GENRES, "Action", now=NOW)
    except ValueError as error:
        assert "genres" in str(error)
    else:
        raise AssertionError("A non-sequence genre override must be rejected.")


def test_local_and_source_updates_preserve_unrelated_values_and_source_cover_urls() -> None:
    series = Series.new_local("canonical", "Local title", now=NOW)
    source = SeriesSource.new_local(
        series.series_id,
        uuid4(),
        remote_id="work-1",
        remote_title="Remote title",
        remote_author="Remote author",
        remote_cover_url="https://source.invalid/old-cover.jpg",
        last_checked_at=NOW,
    )

    updated_series = with_local_metadata_value(
        series,
        MetadataField.AUTHOR,
        "Local author",
        now=NOW,
    )
    updated_title = with_source_metadata_value(
        source,
        MetadataField.TITLE,
        "Refreshed title",
        checked_at=NOW,
    )
    updated_cover = with_source_metadata_value(
        updated_title,
        MetadataField.COVER,
        "https://source.invalid/new-cover.jpg",
        checked_at=NOW,
    )

    assert updated_series.display_title == "Local title"
    assert updated_series.author == "Local author"
    assert updated_title.remote_title == "Refreshed title"
    assert updated_title.remote_author == "Remote author"
    assert updated_cover.remote_cover_url == "https://source.invalid/new-cover.jpg"
    assert updated_cover.remote_title == "Refreshed title"
