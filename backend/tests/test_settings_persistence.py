from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from support.sqlite import open_test_database

from app.domain import Chapter, Series, Setting
from app.persistence.base import PersistenceBase
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.chapter import chapter_to_row
from app.persistence.mappers.series import series_to_row
from app.persistence.mappers.setting import setting_from_row, setting_to_row
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models import SchemaHistoryRow, SettingRow

NOW = datetime(2026, 9, 26, 18, 30, 45, 765_432, tzinfo=UTC)
NOW_US = datetime_to_epoch_us(NOW)


def _create_database(
    tmp_path: Path,
    *,
    before_revision: str | None = None,
) -> tuple[SQLiteDatabase, Series, Chapter]:
    database = open_test_database(tmp_path)
    runner = MigrationRunner(database, tmp_path / "backups")
    if before_revision is None:
        runner.upgrade_to_head()
    else:
        with database.exclusive_connection() as connection:
            config = Config(str(runner.config_path))
            config.attributes["connection"] = connection
            command.upgrade(config, before_revision)
            connection.commit()

    series = Series.new_local("Keep settings migration data", "Existing", now=NOW)
    chapter = Chapter.new_local(
        series.series_id,
        "7",
        "Chapter 7",
        chapter_sort_key=(7, 0, 0),
    )
    with database.write_session() as session:
        if before_revision is None:
            session.add(series_to_row(series))
        else:
            session.execute(
                text(
                    "INSERT INTO series (series_id, canonical_title, display_title, "
                    "description, status, language, cover_asset_id, created_at_us, "
                    "updated_at_us) VALUES (:series_id, :canonical_title, :display_title, "
                    ":description, :status, :language, NULL, :created_at_us, :updated_at_us)"
                ),
                {
                    "series_id": str(series.series_id),
                    "canonical_title": series.canonical_title,
                    "display_title": series.display_title,
                    "description": series.description,
                    "status": series.status,
                    "language": series.language,
                    "created_at_us": datetime_to_epoch_us(series.created_at),
                    "updated_at_us": datetime_to_epoch_us(series.updated_at),
                },
            )
        session.flush()
        session.add(chapter_to_row(chapter))
    return database, series, chapter


def _insert_setting(
    session: Session,
    *,
    setting_key: str,
    value_json: str,
    schema_version: int | float,
    updated_at_us: int | float = NOW_US,
) -> None:
    session.execute(
        text(
            "INSERT INTO settings (setting_key, value_json, schema_version, updated_at_us) "
            "VALUES (:setting_key, :value_json, :schema_version, :updated_at_us)"
        ),
        {
            "setting_key": setting_key,
            "value_json": value_json,
            "schema_version": schema_version,
            "updated_at_us": updated_at_us,
        },
    )


def test_setting_preserves_versioned_json_and_normalizes_updated_time() -> None:
    local_time = datetime(
        2026,
        9,
        26,
        14,
        30,
        45,
        765_432,
        tzinfo=timezone(timedelta(hours=-4)),
    )
    raw_json = r'{ "library_root" : "D:\\Yomira\\Manga", "lan_enabled": false }'
    setting = Setting.new_local(
        "library_root",
        raw_json,
        schema_version=3,
        updated_at=local_time,
    )

    assert setting.setting_key == "library_root"
    assert setting.value_json == raw_json
    assert setting.schema_version == 3
    assert setting.updated_at == local_time.astimezone(UTC)


def test_setting_rejects_empty_key_bad_json_schema_version_and_naive_time() -> None:
    with pytest.raises(ValueError, match="setting_key must not be empty"):
        Setting.new_local("", "null")
    for invalid_json in ("", "not-json", "NaN", "Infinity"):
        with pytest.raises(ValueError, match="value_json must be valid finite JSON"):
            Setting.new_local("crop_mode", invalid_json)
    for invalid_version in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="schema_version must be a positive integer"):
            Setting.new_local(
                "reader_mode",
                '"page"',
                schema_version=cast(int, invalid_version),
            )
    with pytest.raises(ValueError, match="timezone information"):
        Setting.new_local(
            "reader_rtl",
            "false",
            updated_at=datetime(2026, 9, 26),
        )


def test_setting_mapper_round_trips_exact_json_text() -> None:
    raw_json = '{"retry_count":3,"timeout_seconds":0.1,"crop_mode":"SAFE"}'
    setting = Setting.new_local(
        "download_options",
        raw_json,
        schema_version=2,
        updated_at=NOW,
    )

    row = setting_to_row(setting)

    assert row.value_json == raw_json
    assert row.schema_version == 2
    assert row.updated_at_us == NOW_US
    assert setting_from_row(row) == setting


def test_settings_schema_preserves_unknown_keys_and_checks_json_version_and_time(
    tmp_path: Path,
) -> None:
    database, _, _ = _create_database(tmp_path)
    try:
        setting = Setting.new_local(
            "future_setting_key",
            '{"value":[1,true,"future"]}',
            schema_version=8,
            updated_at=NOW,
        )
        with database.write_session() as session:
            session.add(setting_to_row(setting))
        with database.read_session() as session:
            row = session.get(SettingRow, setting.setting_key)
            table_names = {table.name for table in PersistenceBase.metadata.sorted_tables}
        assert row is not None
        assert setting_from_row(row) == setting
        assert row.value_json == setting.value_json
        assert {"settings", "schema_history"} <= table_names

        invalid_rows: tuple[tuple[str, str, int | float, int | float], ...] = (
            ("", "null", 1, NOW_US),
            ("invalid_json", "not-json", 1, NOW_US),
            ("non_finite", "NaN", 1, NOW_US),
            ("bad_version", "null", 0, NOW_US),
            ("float_version", "null", 1.5, NOW_US),
            ("float_timestamp", "null", 1, 1.5),
        )
        for key, value_json, schema_version, updated_at_us in invalid_rows:
            with pytest.raises(IntegrityError):
                with database.write_session() as session:
                    _insert_setting(
                        session,
                        setting_key=key,
                        value_json=value_json,
                        schema_version=schema_version,
                        updated_at_us=updated_at_us,
                    )
    finally:
        database.close()


def test_schema_history_model_reads_audit_rows_and_triggers_remain_append_only(
    tmp_path: Path,
) -> None:
    database, _, _ = _create_database(tmp_path)
    try:
        with database.read_session() as session:
            first_event = session.get(SchemaHistoryRow, 1)
            trigger_names = {
                row[0]
                for row in session.connection()
                .exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                .all()
            }
        assert first_event is not None
        assert first_event.revision_id == "0001_schema_history"
        assert first_event.resulting_revision == "0001_schema_history"
        assert first_event.direction == "upgrade"
        assert {
            "schema_history_reject_update",
            "schema_history_reject_delete",
        } <= trigger_names

        with database.write_session() as session:
            with pytest.raises(IntegrityError, match="append-only"):
                session.execute(
                    text("UPDATE schema_history SET direction = 'downgrade' WHERE event_id = 1")
                )
        with database.write_session() as session:
            with pytest.raises(IntegrityError, match="append-only"):
                session.execute(text("DELETE FROM schema_history WHERE event_id = 1"))
    finally:
        database.close()


def test_settings_migration_preserves_existing_chapters_and_audit_history(
    tmp_path: Path,
) -> None:
    database, _, chapter = _create_database(
        tmp_path,
        before_revision="0010_reader_progress",
    )
    try:
        report = MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
        setting = Setting.new_local(
            "log_level",
            '"INFO"',
            schema_version=1,
            updated_at=NOW,
        )
        with database.write_session() as session:
            session.add(setting_to_row(setting))
        with database.read_session() as session:
            saved_chapter_label = session.execute(
                text("SELECT chapter_raw_label FROM chapters WHERE chapter_id = :chapter_id"),
                {"chapter_id": str(chapter.chapter_id)},
            ).scalar_one()
            last_event = session.get(SchemaHistoryRow, 23)
            foreign_key_errors = (
                session.connection().exec_driver_sql("PRAGMA foreign_key_check").all()
            )

        assert report.previous_revision == "0010_reader_progress"
        assert report.current_revision == "0023_whole_manga_export_size"
        assert report.changed is True
        assert report.backup_path is not None and report.backup_path.is_file()
        assert saved_chapter_label == "7"
        assert last_event is not None
        assert last_event.revision_id == "0023_whole_manga_export_size"
        assert foreign_key_errors == []
    finally:
        database.close()
