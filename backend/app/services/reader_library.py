"""Bounded personal reader data stored with the library, independent of image cache."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import UUID4, BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.series import SeriesRow
from app.persistence.models.setting import SettingRow

READER_LIBRARY_KEY = "reader.library.v1"
MAX_READER_WORKS = 200
MAX_READER_BOOKMARKS = 500
MAX_READER_IMPORTS = 200
_MAX_DOCUMENT_CHARACTERS = 2_000_000


class ReaderLibraryConflict(ValueError):
    """The operation cannot be completed without dropping data or valid references."""


class ReaderLibraryCorrupt(ValueError):
    """Existing reader data must remain untouched until it is repaired."""


class ReaderWorkPreferences(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    format: Literal["auto", "manga", "manhwa", "comic", "custom"]
    mode: Literal["single", "vertical", "double"]
    rtl: bool
    continuous: bool


class ReaderWorkProfile(ReaderWorkPreferences):
    series_id: UUID4 = Field(strict=False)


class ReaderBookmark(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    series_id: UUID4 = Field(strict=False)
    chapter_id: UUID4 = Field(strict=False)
    page_index: int = Field(ge=0, le=9999)
    chapter_label: str = Field(min_length=1, max_length=500)
    created_at: str = Field(min_length=20, max_length=32, pattern=r"^\d{4}-.+Z$")

    @field_validator("chapter_label")
    @classmethod
    def nonblank_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("chapter_label must not be blank")
        return value

    @field_validator("created_at")
    @classmethod
    def utc_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
            raise ValueError("created_at must be UTC")
        return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ReaderLibraryData(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    library_id: UUID4 = Field(strict=False)
    preferences: list[ReaderWorkProfile] = Field(max_length=MAX_READER_WORKS)
    bookmarks: list[ReaderBookmark] = Field(max_length=MAX_READER_BOOKMARKS)


class ReaderLibraryImport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    migration_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    preferences: list[ReaderWorkProfile] = Field(max_length=MAX_READER_WORKS)
    bookmarks: list[ReaderBookmark] = Field(max_length=MAX_READER_BOOKMARKS)


class _StoredLibrary(ReaderLibraryData):
    migrations: list[str] = Field(max_length=MAX_READER_IMPORTS)

    @field_validator("migrations")
    @classmethod
    def migration_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in values
        ):
            raise ValueError("Invalid migration ledger")
        return values


def _bookmark_key(bookmark: ReaderBookmark) -> tuple[UUID4, int]:
    return bookmark.chapter_id, bookmark.page_index


def _check_collection(data: ReaderLibraryData) -> None:
    works = {item.series_id for item in data.preferences} | {
        item.series_id for item in data.bookmarks
    }
    if (
        len(works) > MAX_READER_WORKS
        or len(data.preferences) > MAX_READER_WORKS
        or len(data.bookmarks) > MAX_READER_BOOKMARKS
        or len({item.series_id for item in data.preferences}) != len(data.preferences)
        or len({_bookmark_key(item) for item in data.bookmarks}) != len(data.bookmarks)
    ):
        raise ReaderLibraryConflict("Se alcanzó el límite de obras o marcadores del lector.")


class ReaderLibraryService:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    @staticmethod
    def _decode(row: SettingRow) -> _StoredLibrary:
        try:
            if row.schema_version != 1 or len(row.value_json) > _MAX_DOCUMENT_CHARACTERS:
                raise ValueError("Unsupported reader library document")
            data = _StoredLibrary.model_validate_json(row.value_json)
            _check_collection(data)
            return data
        except (ValidationError, ValueError, RecursionError, TypeError) as error:
            raise ReaderLibraryCorrupt(
                "Los datos guardados del lector no son válidos y se han conservado."
            ) from error

    @staticmethod
    def _public(data: _StoredLibrary) -> ReaderLibraryData:
        return ReaderLibraryData(
            library_id=data.library_id,
            preferences=data.preferences,
            bookmarks=data.bookmarks,
        )

    def get(self) -> ReaderLibraryData:
        with self._database.read_session() as session:
            row = session.get(SettingRow, READER_LIBRARY_KEY)
            if row is not None:
                return self._public(self._decode(row))
        return self._mutate(lambda _session, _data: False)

    def _mutate(self, operation: Callable[[Session, _StoredLibrary], bool]) -> ReaderLibraryData:
        with self._database.write_session() as session:
            # Serialize the read/merge/write even across two database handles.
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(SettingRow, READER_LIBRARY_KEY)
            data = (
                self._decode(row)
                if row is not None
                else _StoredLibrary(library_id=uuid4(), preferences=[], bookmarks=[], migrations=[])
            )
            changed = operation(session, data)
            _check_collection(data)
            if row is None or changed:
                value = json.dumps(data.model_dump(mode="json"), separators=(",", ":"))
                if len(value) > _MAX_DOCUMENT_CHARACTERS:
                    raise ReaderLibraryConflict(
                        "Los datos del lector superan su espacio reservado."
                    )
                if row is None:
                    row = SettingRow(setting_key=READER_LIBRARY_KEY, schema_version=1)
                    session.add(row)
                row.value_json = value
                row.updated_at_us = datetime_to_epoch_us(datetime.now(UTC))
            return self._public(data)

    @staticmethod
    def _references(
        session: Session,
        profiles: list[ReaderWorkProfile],
        bookmarks: list[ReaderBookmark],
    ) -> None:
        series_ids = {str(item.series_id) for item in profiles + bookmarks}
        existing: set[str] = (
            set(
                session.scalars(
                    select(SeriesRow.series_id).where(SeriesRow.series_id.in_(series_ids))
                )
            )
            if series_ids
            else set()
        )
        if existing != series_ids:
            raise ReaderLibraryConflict(
                "Alguna obra no pertenece a esta biblioteca. Los datos anteriores se conservan."
            )
        chapter_ids = {str(item.chapter_id) for item in bookmarks}
        chapters = (
            {
                item.chapter_id: item.series_id
                for item in session.scalars(
                    select(ChapterRow).where(ChapterRow.chapter_id.in_(chapter_ids))
                )
            }
            if chapter_ids
            else {}
        )
        if any(chapters.get(str(item.chapter_id)) != str(item.series_id) for item in bookmarks):
            raise ReaderLibraryConflict(
                "Algún capítulo no pertenece a la obra indicada. Los datos anteriores se conservan."
            )

    def put_preferences(
        self, series_id: UUID4, preferences: ReaderWorkPreferences
    ) -> ReaderLibraryData:
        profile = ReaderWorkProfile(series_id=series_id, **preferences.model_dump())

        def update(session: Session, data: _StoredLibrary) -> bool:
            self._references(session, [profile], [])
            data.preferences = [item for item in data.preferences if item.series_id != series_id]
            data.preferences.append(profile)
            return True

        return self._mutate(update)

    def put_bookmark(self, bookmark: ReaderBookmark) -> ReaderLibraryData:
        def update(session: Session, data: _StoredLibrary) -> bool:
            self._references(session, [], [bookmark])
            key = _bookmark_key(bookmark)
            data.bookmarks = [item for item in data.bookmarks if _bookmark_key(item) != key]
            data.bookmarks.append(bookmark)
            return True

        return self._mutate(update)

    def remove_bookmark(self, chapter_id: UUID4, page_index: int) -> ReaderLibraryData:
        if type(page_index) is not int or not 0 <= page_index <= 9999:
            raise ValueError("Invalid bookmark page")

        def update(_session: Session, data: _StoredLibrary) -> bool:
            before = len(data.bookmarks)
            data.bookmarks = [
                item
                for item in data.bookmarks
                if (item.chapter_id, item.page_index) != (chapter_id, page_index)
            ]
            return before != len(data.bookmarks)

        return self._mutate(update)

    def import_legacy(self, incoming: ReaderLibraryImport) -> ReaderLibraryData:
        def update(session: Session, data: _StoredLibrary) -> bool:
            if incoming.migration_id in data.migrations:
                return False
            if len(data.migrations) >= MAX_READER_IMPORTS:
                raise ReaderLibraryConflict("Se alcanzó el límite de importaciones del lector.")
            self._references(session, incoming.preferences, incoming.bookmarks)
            profiles = {item.series_id: item for item in data.preferences}
            markers = {_bookmark_key(item): item for item in data.bookmarks}
            for profile in incoming.preferences:
                profiles.setdefault(profile.series_id, profile)
            for bookmark in incoming.bookmarks:
                markers.setdefault(_bookmark_key(bookmark), bookmark)
            data.preferences = list(profiles.values())
            data.bookmarks = list(markers.values())
            data.migrations.append(incoming.migration_id)
            return True

        return self._mutate(update)
