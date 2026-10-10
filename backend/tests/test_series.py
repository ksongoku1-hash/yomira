from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from support.sqlite import open_test_database

from app.domain import Series
from app.persistence.base import PersistenceBase
from app.persistence.mappers.series import series_from_row, series_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models import SeriesRow


def test_new_local_series_preserves_text_and_normalizes_aware_timestamps() -> None:
    local_time = datetime(
        2026,
        1,
        2,
        3,
        4,
        5,
        600_700,
        tzinfo=timezone(timedelta(hours=3)),
    )
    series = Series.new_local(
        "  Canonical title  ",
        " Display title ",
        description="Description\nkept as provided",
        status="Publishing",
        language="pt-BR",
        now=local_time,
    )

    assert series.series_id.version == 4
    assert series.canonical_title == "  Canonical title  "
    assert series.display_title == " Display title "
    assert series.description == "Description\nkept as provided"
    assert series.status == "Publishing"
    assert series.language == "pt-BR"
    assert series.cover_asset_id is None
    assert series.created_at == local_time.astimezone(UTC)
    assert series.updated_at == series.created_at


def test_series_rejects_invalid_identity_title_and_timestamp() -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    legacy_id = UUID("123e4567-e89b-12d3-a456-426614174000")
    with pytest.raises(ValueError, match="series_id must be a UUIDv4"):
        Series(
            series_id=legacy_id,
            canonical_title="Canonical",
            display_title="Display",
            description=None,
            status=None,
            language=None,
            cover_asset_id=None,
            created_at=now,
            updated_at=now,
        )

    with pytest.raises(ValueError, match="canonical_title must not be empty"):
        Series.new_local("", "Display", now=now)

    with pytest.raises(ValueError, match="timezone information"):
        Series.new_local("Canonical", "Display", now=datetime(2026, 1, 2))

    with pytest.raises(ValueError, match="must not precede"):
        Series(
            series_id=uuid4(),
            canonical_title="Canonical",
            display_title="Display",
            description=None,
            status=None,
            language=None,
            cover_asset_id=None,
            created_at=now,
            updated_at=now - timedelta(microseconds=1),
        )


def test_series_cover_asset_fk_is_resolvable_and_restrictive() -> None:
    assert SeriesRow.__tablename__ == "series"
    sorted_table_names = {table.name for table in PersistenceBase.metadata.sorted_tables}
    assert {
        "series",
        "sources",
        "connector_health",
        "series_sources",
        "chapters",
        "chapter_sources",
        "assets",
    } <= (sorted_table_names)
    assert SeriesRow.__table__.c.cover_asset_id.nullable is True
    foreign_key = next(
        foreign_key
        for foreign_key in SeriesRow.__table__.foreign_keys
        if foreign_key.parent.name == "cover_asset_id"
    )
    assert foreign_key.target_fullname == "assets.asset_id"
    assert foreign_key.ondelete == "RESTRICT"


def test_series_mapper_round_trips_microseconds_and_optional_content() -> None:
    created_at = datetime(1969, 12, 31, 23, 59, 59, 123_456, tzinfo=UTC)
    updated_at = datetime(2026, 2, 3, 4, 5, 6, 789_123, tzinfo=UTC)
    series = Series(
        series_id=uuid4(),
        canonical_title="Raw canonical title",
        display_title="Raw display title",
        description="Description",
        status="Unrecognized future state",
        language="x-future-language",
        cover_asset_id=uuid4(),
        created_at=created_at,
        updated_at=updated_at,
    )

    restored = series_from_row(series_to_row(series))

    assert restored == series


def test_series_migration_preserves_optional_text_and_enforces_constraints(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    try:
        report = runner.upgrade_to_head()
        assert report.current_revision == "0023_whole_manga_export_size"

        now_us = 1_767_321_845_600_700
        valid_id = str(uuid4())
        with database.write_session() as session:
            session.execute(
                text(
                    "INSERT INTO series (series_id, canonical_title, display_title, "
                    "description, status, language, cover_asset_id, created_at_us, updated_at_us) "
                    "VALUES (:series_id, :canonical_title, :display_title, :description, "
                    ":status, :language, NULL, :created_at_us, :updated_at_us)"
                ),
                {
                    "series_id": valid_id,
                    "canonical_title": " Canonical ",
                    "display_title": "Display\nTitle",
                    "description": "Raw description",
                    "status": "Future status value",
                    "language": "x-source-defined",
                    "created_at_us": now_us,
                    "updated_at_us": now_us,
                },
            )

        with database.read_session() as session:
            row = session.execute(
                text(
                    "SELECT canonical_title, display_title, description, status, language "
                    "FROM series WHERE series_id = :series_id"
                ),
                {"series_id": valid_id},
            ).one()
        assert tuple(row) == (
            " Canonical ",
            "Display\nTitle",
            "Raw description",
            "Future status value",
            "x-source-defined",
        )

        second_series = series_to_row(
            Series.new_local(
                " Canonical ",
                "Second display title",
                now=datetime(2026, 1, 2, tzinfo=UTC),
            )
        )
        with database.write_session() as session:
            session.add(second_series)
        with database.read_session() as session:
            matching_titles = session.execute(
                text("SELECT count(*) FROM series WHERE canonical_title = ' Canonical '")
            ).scalar_one()
        assert matching_titles == 2

        invalid_rows: tuple[tuple[str, str, str, int | float, int | float], ...] = (
            ("not-a-uuid", "Canonical", "Display", now_us, now_us),
            ("123e4567-e89b-12d3-a456-426614174000", "Canonical", "Display", now_us, now_us),
            (str(uuid4()).upper(), "Canonical", "Display", now_us, now_us),
            (str(uuid4()), "", "Display", now_us, now_us),
            (str(uuid4()), "Canonical", "", now_us, now_us),
            (str(uuid4()), "Canonical", "Display", now_us, now_us - 1),
            (str(uuid4()), "Canonical", "Display", 1.5, 1.5),
        )
        for series_id, canonical_title, display_title, created_at_us, updated_at_us in invalid_rows:
            with pytest.raises(IntegrityError):
                with database.write_session() as session:
                    session.execute(
                        text(
                            "INSERT INTO series (series_id, canonical_title, display_title, "
                            "created_at_us, updated_at_us) VALUES (:series_id, "
                            ":canonical_title, :display_title, :created_at_us, :updated_at_us)"
                        ),
                        {
                            "series_id": series_id,
                            "canonical_title": canonical_title,
                            "display_title": display_title,
                            "created_at_us": created_at_us,
                            "updated_at_us": updated_at_us,
                        },
                    )

        with database.read_session() as session:
            index_names = {
                row[1]
                for row in session.connection().exec_driver_sql("PRAGMA index_list('series')").all()
            }
        assert {"ix_series_canonical_title", "ix_series_updated_at_us"} <= index_names
    finally:
        database.close()
