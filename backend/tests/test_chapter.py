from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from support.sqlite import open_test_database

from app.domain import Chapter, Series
from app.persistence.base import PersistenceBase
from app.persistence.mappers.chapter import chapter_from_row, chapter_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models import ChapterRow


def test_chapter_keeps_decimal_label_and_integer_sort_key_exact() -> None:
    series_id = uuid4()
    chapter = Chapter.new_local(
        series_id,
        "12.10",
        "12.10",
        chapter_major=12,
        chapter_minor=10,
        chapter_sort_key=(12, 10, 0),
    )
    extra = Chapter.new_local(
        series_id,
        "Extra verano",
        "Extra verano",
        chapter_sort_key=(),
        chapter_type="EXTRA",
    )

    assert chapter.chapter_id.version == 4
    assert chapter.chapter_raw_label == "12.10"
    assert chapter.chapter_major == 12
    assert chapter.chapter_minor == 10
    assert chapter.chapter_sort_key == (12, 10, 0)
    assert extra.chapter_raw_label == "Extra verano"
    assert extra.chapter_major is None
    assert extra.chapter_minor is None
    assert extra.chapter_type == "EXTRA"
    assert extra.chapter_sort_key == ()


def test_chapter_rejects_bad_ids_float_and_boolean_numeric_components() -> None:
    legacy_id = UUID("123e4567-e89b-12d3-a456-426614174000")
    with pytest.raises(ValueError, match="chapter_id must be a UUIDv4"):
        Chapter(
            chapter_id=legacy_id,
            series_id=uuid4(),
            chapter_raw_label="12.10",
            chapter_major=12,
            chapter_minor=10,
            chapter_variant=None,
            chapter_type=None,
            chapter_sort_key=(12, 10, 0),
            volume_number=None,
            language=None,
            display_label="12.10",
        )

    with pytest.raises(ValueError, match="chapter_major must be an integer"):
        Chapter.new_local(
            uuid4(),
            "12.10",
            "12.10",
            chapter_major=cast(int, 12.1),
            chapter_sort_key=(12, 10, 0),
        )

    with pytest.raises(ValueError, match="chapter_minor must be an integer"):
        Chapter.new_local(
            uuid4(),
            "12.10",
            "12.10",
            chapter_minor=True,
            chapter_sort_key=(12, 10, 0),
        )

    with pytest.raises(ValueError, match="chapter_sort_key must contain only integers"):
        Chapter.new_local(
            uuid4(),
            "12.10",
            "12.10",
            chapter_sort_key=cast(tuple[int, ...], (12, 10.0)),
        )

    with pytest.raises(ValueError, match="chapter_raw_label must not be empty"):
        Chapter.new_local(uuid4(), "", "display", chapter_sort_key=())


def test_chapter_mapper_round_trips_text_open_values_and_integer_sequence() -> None:
    chapter = Chapter.new_local(
        uuid4(),
        " 12.10 ",
        "  Display 12.10 ",
        chapter_major=12,
        chapter_minor=10,
        chapter_variant="  revised ",
        chapter_type="Future chapter type",
        chapter_sort_key=(12, 10, -2, 9_007_199_254_741_234),
        volume_number=4,
        language="x-future-language",
    )

    row = chapter_to_row(chapter)
    assert row.chapter_sort_key == "[12,10,-2,9007199254741234]"
    assert chapter_from_row(row) == chapter

    row.chapter_sort_key = "[12,10.5]"
    with pytest.raises(ValueError, match="JSON array of integers"):
        chapter_from_row(row)

    row.chapter_sort_key = "{}"
    with pytest.raises(ValueError, match="JSON array of integers"):
        chapter_from_row(row)

    row.chapter_sort_key = "not-json"
    with pytest.raises(ValueError, match="not valid JSON"):
        chapter_from_row(row)


def test_chapter_model_has_resolved_restrict_foreign_key() -> None:
    assert ChapterRow.__tablename__ == "chapters"
    sorted_table_names = {table.name for table in PersistenceBase.metadata.sorted_tables}
    assert {"series", "chapters"} <= sorted_table_names
    foreign_key = next(iter(ChapterRow.__table__.foreign_keys))
    assert foreign_key.target_fullname == "series.series_id"
    assert foreign_key.ondelete == "RESTRICT"


def _insert_chapter(
    session: Session,
    chapter_id: str,
    series_id: str,
    chapter_raw_label: str,
    chapter_major: int | float | None,
    chapter_minor: int | float | None,
    chapter_variant: str | None,
    chapter_type: str | None,
    chapter_sort_key: str,
    volume_number: int | float | None,
    language: str | None,
    display_label: str,
) -> None:
    session.execute(
        text(
            "INSERT INTO chapters (chapter_id, series_id, chapter_raw_label, chapter_major, "
            "chapter_minor, chapter_variant, chapter_type, chapter_sort_key, volume_number, "
            "language, display_label) VALUES (:chapter_id, :series_id, :chapter_raw_label, "
            ":chapter_major, :chapter_minor, :chapter_variant, :chapter_type, :chapter_sort_key, "
            ":volume_number, :language, :display_label)"
        ),
        {
            "chapter_id": chapter_id,
            "series_id": series_id,
            "chapter_raw_label": chapter_raw_label,
            "chapter_major": chapter_major,
            "chapter_minor": chapter_minor,
            "chapter_variant": chapter_variant,
            "chapter_type": chapter_type,
            "chapter_sort_key": chapter_sort_key,
            "volume_number": volume_number,
            "language": language,
            "display_label": display_label,
        },
    )


def test_chapter_migration_preserves_rows_and_enforces_integer_identity(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    try:
        report = runner.upgrade_to_head()
        assert report.current_revision == "0024_reader_sessions"

        series = Series.new_local("Series", "Series")
        first = Chapter.new_local(
            series.series_id,
            "12.10",
            "12.10",
            chapter_major=12,
            chapter_minor=10,
            chapter_sort_key=(12, 10, 0),
        )
        duplicate_label = Chapter.new_local(
            series.series_id,
            "12.10",
            "12.10 variant",
            chapter_major=12,
            chapter_minor=10,
            chapter_variant="alternate edition",
            chapter_sort_key=(12, 10, 1),
        )
        extra = Chapter.new_local(
            series.series_id,
            "Extra verano",
            "Extra verano",
            chapter_sort_key=(),
            chapter_type="EXTRA",
        )
        with database.write_session() as session:
            session.add(series_to_row(series))
            session.flush()
            session.add_all(
                [
                    chapter_to_row(first),
                    chapter_to_row(duplicate_label),
                    chapter_to_row(extra),
                ]
            )

        with database.read_session() as session:
            rows = session.execute(
                text(
                    "SELECT chapter_raw_label, chapter_major, chapter_minor, chapter_sort_key "
                    "FROM chapters WHERE series_id = :series_id ORDER BY chapter_id"
                ),
                {"series_id": str(series.series_id)},
            ).all()
            persisted = session.get(ChapterRow, str(first.chapter_id))
            index_names = {
                row[1]
                for row in session.connection()
                .exec_driver_sql("PRAGMA index_list('chapters')")
                .all()
            }
        assert len(rows) == 3
        assert any(tuple(row) == ("12.10", 12, 10, "[12,10,0]") for row in rows)
        assert any(tuple(row) == ("Extra verano", None, None, "[]") for row in rows)
        assert persisted is not None
        assert chapter_from_row(persisted) == first
        assert "ix_chapters_series_id" in index_names

        invalid_rows: tuple[
            tuple[str, str, str, int | float | None, int | float | None, str, int | float | None],
            ...,
        ] = (
            ("not-a-uuid", str(series.series_id), "label", None, None, "[]", None),
            (
                "123e4567-e89b-12d3-a456-426614174000",
                str(series.series_id),
                "label",
                None,
                None,
                "[]",
                None,
            ),
            (str(uuid4()).upper(), str(series.series_id), "label", None, None, "[]", None),
            (str(uuid4()), str(series.series_id), "", None, None, "[]", None),
            (str(uuid4()), str(series.series_id), "label", 12.5, None, "[]", None),
            (str(uuid4()), str(series.series_id), "label", None, 10.5, "[]", None),
            (str(uuid4()), str(series.series_id), "label", None, None, "{invalid", None),
            (str(uuid4()), str(series.series_id), "label", None, None, "{}", None),
            (str(uuid4()), str(series.series_id), "label", None, None, "[]", 1.5),
        )
        for chapter_id, series_id, raw_label, major, minor, sort_key, volume_number in invalid_rows:
            with pytest.raises(IntegrityError):
                with database.write_session() as session:
                    _insert_chapter(
                        session,
                        chapter_id,
                        series_id,
                        raw_label,
                        major,
                        minor,
                        None,
                        None,
                        sort_key,
                        volume_number,
                        None,
                        "display",
                    )

        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                _insert_chapter(
                    session,
                    str(uuid4()),
                    str(uuid4()),
                    "foreign series",
                    None,
                    None,
                    None,
                    None,
                    "[]",
                    None,
                    None,
                    "foreign series",
                )
        with database.write_session() as session:
            with pytest.raises(IntegrityError):
                session.execute(
                    text("DELETE FROM series WHERE series_id = :series_id"),
                    {"series_id": str(series.series_id)},
                )
    finally:
        database.close()
