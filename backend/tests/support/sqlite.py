from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from app.persistence.database import SQLiteDatabase, SQLiteOptions
    from app.persistence.windows_paths import DataRootIdentity


@dataclass(frozen=True, slots=True)
class SQLiteTestContext:
    """One canonical database path and one root identity for a test data root."""

    data_root: Path
    database_path: Path
    data_root_identity: DataRootIdentity

    def open(self, *, options: SQLiteOptions | None = None) -> SQLiteDatabase:
        from app.persistence.database import SQLiteDatabase

        database = SQLiteDatabase(
            self.database_path,
            options=options,
            data_root_identity=self.data_root_identity,
        )
        _CONTEXTS_BY_DATABASE[database] = self
        return database


_CONTEXTS_BY_ROOT: dict[str, SQLiteTestContext] = {}
_CONTEXTS_BY_DATABASE: WeakKeyDictionary[Any, SQLiteTestContext] = WeakKeyDictionary()


def sqlite_test_context(
    data_root: Path,
    *,
    create_database_directory: bool = True,
) -> SQLiteTestContext:
    """Capture one identity for a temporary root and derive its canonical DB path."""
    from app.persistence.windows_paths import DataRootIdentity

    data_root = Path(data_root)
    data_root.mkdir(parents=True, exist_ok=True)
    database_directory = data_root / "database"
    if create_database_directory:
        database_directory.mkdir(parents=True, exist_ok=True)

    root_key = os.path.normcase(str(data_root.resolve())).casefold()
    context = _CONTEXTS_BY_ROOT.get(root_key)
    if context is None:
        identity = DataRootIdentity.capture(data_root)
        canonical_root = Path(identity.canonical_path)
        context = SQLiteTestContext(
            data_root=canonical_root,
            database_path=canonical_root / "database" / "yomira.sqlite3",
            data_root_identity=identity,
        )
        _CONTEXTS_BY_ROOT[root_key] = context
    return context


def open_test_database(
    data_root: Path,
    *,
    options: SQLiteOptions | None = None,
    create_database_directory: bool = True,
) -> SQLiteDatabase:
    return sqlite_test_context(
        data_root,
        create_database_directory=create_database_directory,
    ).open(options=options)


def reopen_test_database(
    database: SQLiteDatabase,
    *,
    options: SQLiteOptions | None = None,
) -> SQLiteDatabase:
    """Reopen using the exact identity object that initialized this database."""
    return _CONTEXTS_BY_DATABASE[database].open(options=options)
