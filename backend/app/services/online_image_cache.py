"""Disposable, bounded image cache, separate from library assets and reading progress."""

import hashlib
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
from uuid import uuid4

from app.persistence.database import SQLiteDatabase
from app.persistence.disk_budget import DiskBudgetManager

ONLINE_DISK_CACHE_BYTES = 1024 * 1024 * 1024
MAX_ONLINE_DISK_CACHE_BYTES = 8 * 1024 * 1024 * 1024
_MAX_PAGE_BYTES = 64 * 1024 * 1024
_MAX_ENTRIES = 1000
_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_ACCESS_REFRESH_SECONDS = 60
_SQLITE_OVERHEAD_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class OnlineImageCacheStatus:
    max_bytes: int
    used_bytes: int
    entries: int
    file_bytes: int


class OnlineImageCache:
    def __init__(
        self,
        database: SQLiteDatabase,
        budget: DiskBudgetManager,
        *,
        max_bytes: int = ONLINE_DISK_CACHE_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_ONLINE_DISK_CACHE_BYTES:
            raise ValueError("Invalid online disk cache size")
        if budget.database is not database:
            raise ValueError("Cache and disk budget must share the same data root")
        self._database = database
        self._budget = budget
        self._identity = database.data_root_identity
        self._root = self._identity.canonical_path / "cache" / "online-images"
        self._path = self._root / "images.sqlite3"
        self._max_bytes = max_bytes
        self._clock = clock
        self._lock = RLock()

    def _check_paths(self) -> None:
        self._identity.assert_current()
        paths = [
            self._root.parent,
            self._root,
            self._path,
            self._path.with_name("images.sqlite3-journal"),
            self._path.with_name("images.sqlite3-wal"),
            self._path.with_name("images.sqlite3-shm"),
        ]
        for path in paths:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise OSError("The online cache must not follow a reparse point")
            if path in paths[2:] and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise OSError("The online cache must own a regular, unlinked file")
            if (
                path == self._path
                and info.st_size > MAX_ONLINE_DISK_CACHE_BYTES + _SQLITE_OVERHEAD_BYTES
            ):
                raise OSError("The online cache exceeds its file limit")
            if not path.resolve().is_relative_to(self._identity.canonical_path):
                raise OSError("The online cache moved outside the data root")

    @contextmanager
    def _connection(self, reserve: int | None = 65536) -> Generator[sqlite3.Connection]:
        with self._lock:
            self._check_paths()
            if reserve is None:
                connection = sqlite3.connect(
                    self._path.as_uri() + "?mode=ro", uri=True, timeout=0.2
                )
                try:
                    self._validate_existing(connection)
                    yield connection
                finally:
                    connection.close()
                return
            reservation = uuid4()
            self._budget.reserve_bounded_operation(reservation, page_bytes=reserve)
            try:
                with self._database.storage_gate.mutation():
                    self._root.mkdir(parents=True, exist_ok=True)
                    self._check_paths()
                    connection = sqlite3.connect(self._path, timeout=0.2)
                    try:
                        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
                        if application_id == 0x594F4943:
                            self._validate_existing(connection)
                        else:
                            tables = connection.execute(
                                "SELECT count(*) FROM sqlite_master"
                            ).fetchone()[0]
                            if application_id != 0 or tables:
                                raise sqlite3.DatabaseError("Not an online image cache")
                            connection.execute("PRAGMA application_id=1498368323")
                        connection.execute("PRAGMA journal_mode=DELETE")
                        connection.execute("PRAGMA synchronous=NORMAL")
                        if connection.execute("PRAGMA page_size").fetchone()[0] != 4096:
                            raise sqlite3.DatabaseError("Unexpected cache page size")
                        # Include bounded SQLite overhead; the image payload has its own lower cap.
                        max_pages = (self._max_bytes + _SQLITE_OVERHEAD_BYTES) // 4096
                        connection.execute(f"PRAGMA max_page_count={max_pages}")
                        connection.execute("""CREATE TABLE IF NOT EXISTS chapters (
                            namespace TEXT PRIMARY KEY, generation INTEGER NOT NULL)""")
                        connection.execute("""CREATE TABLE IF NOT EXISTS images (
                            namespace TEXT NOT NULL, generation INTEGER NOT NULL,
                            ordinal INTEGER NOT NULL, body BLOB NOT NULL, mime TEXT NOT NULL,
                            page_count INTEGER NOT NULL,
                            sha256 TEXT NOT NULL, created REAL NOT NULL, accessed REAL NOT NULL,
                            PRIMARY KEY(namespace,generation,ordinal))""")
                        with connection:
                            yield connection
                    finally:
                        connection.close()
            finally:
                self._budget.release_bounded_operation(reservation)

    @staticmethod
    def _validate_existing(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA application_id").fetchone()[0] != 0x594F4943:
            raise sqlite3.DatabaseError("Not an online image cache")
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if tables != {"chapters", "images"}:
            raise sqlite3.DatabaseError("Unexpected online cache tables")
        for table, expected in (
            ("chapters", ("namespace", "generation")),
            (
                "images",
                (
                    "namespace",
                    "generation",
                    "ordinal",
                    "body",
                    "mime",
                    "page_count",
                    "sha256",
                    "created",
                    "accessed",
                ),
            ),
        ):
            columns = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
            if columns != expected:
                raise sqlite3.DatabaseError("Unexpected online cache schema")

    def status(self) -> OnlineImageCacheStatus:
        with self._lock:
            self._check_paths()
            if not self._path.exists():
                return OnlineImageCacheStatus(self._max_bytes, 0, 0, 0)
            with self._connection(None) as connection:
                used, count = connection.execute(
                    "SELECT coalesce(sum(length(body)),0),count(*) FROM images"
                ).fetchone()
            self._check_paths()
            return OnlineImageCacheStatus(self._max_bytes, used, count, self._path.stat().st_size)

    @contextmanager
    def changing_limit(self, max_bytes: int) -> Generator[None]:
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_ONLINE_DISK_CACHE_BYTES:
            raise ValueError("Invalid online disk cache size")
        with self._lock:
            previous = self._max_bytes
            state = self.status()
            while state.used_bytes > max_bytes or state.entries > _MAX_ENTRIES:
                # One bounded image deletion per transaction keeps its rollback journal reserved.
                with self._connection(_MAX_PAGE_BYTES * 3 + 65536) as connection:
                    row = connection.execute(
                        "SELECT rowid,length(body) FROM images ORDER BY accessed,rowid LIMIT 1"
                    ).fetchone()
                    if row is None:
                        raise sqlite3.DatabaseError("Inconsistent online cache usage")
                    if type(row[1]) is not int or not 0 <= row[1] <= _MAX_PAGE_BYTES:
                        raise sqlite3.DatabaseError("Invalid online cache image size")
                    connection.execute("DELETE FROM images WHERE rowid=?", (row[0],))
                state = self.status()
            self._max_bytes = max_bytes
            try:
                yield
            except BaseException:
                self._max_bytes = previous
                raise

    @staticmethod
    def _validate(namespace: str, ordinal: int, page_count: int) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", namespace) is None:
            raise ValueError("Invalid online cache identity")
        if (
            type(ordinal) is not int
            or type(page_count) is not int
            or not 1 <= ordinal <= page_count <= 10_000
        ):
            raise ValueError("Invalid online cache page")

    def get(self, namespace: str, ordinal: int, page_count: int) -> tuple[bytes, str] | None:
        self._validate(namespace, ordinal, page_count)
        try:
            with self._connection(None) as connection:
                row = connection.execute(
                    """SELECT rowid,body,mime,sha256,accessed FROM images
                    WHERE namespace=? AND generation=coalesce(
                        (SELECT generation FROM chapters WHERE namespace=?),0)
                    AND ordinal=? AND page_count=? AND created>=? AND length(body)<=?""",
                    (
                        namespace,
                        namespace,
                        ordinal,
                        page_count,
                        self._clock() - _MAX_AGE_SECONDS,
                        min(self._max_bytes, _MAX_PAGE_BYTES),
                    ),
                ).fetchone()
                if row is None:
                    return None
                rowid, body, mime, digest, accessed = row
                if not isinstance(body, bytes) or not isinstance(mime, str):
                    return None
                if hashlib.sha256(body).hexdigest() != digest:
                    return None
                # Repeated cache hits need no journal, space reservation, or writer lock.
                if (
                    isinstance(accessed, int | float)
                    and 0 <= self._clock() - accessed < _ACCESS_REFRESH_SECONDS
                ):
                    return body, mime
        except OSError, sqlite3.Error, RuntimeError:
            return None
        try:
            with self._connection() as connection:
                connection.execute(
                    "UPDATE images SET accessed=? WHERE rowid=?", (self._clock(), rowid)
                )
        except OSError, sqlite3.Error, RuntimeError:
            pass
        return body, mime

    def put(self, namespace: str, ordinal: int, page_count: int, body: bytes, mime: str) -> None:
        self._validate(namespace, ordinal, page_count)
        if not 0 < len(body) <= min(self._max_bytes, _MAX_PAGE_BYTES):
            return
        try:
            with self._connection(_MAX_PAGE_BYTES * 3 + 65536) as connection:
                generation = connection.execute(
                    "SELECT generation FROM chapters WHERE namespace=?", (namespace,)
                ).fetchone()
                epoch = generation[0] if generation else 0
                connection.execute(
                    "DELETE FROM images WHERE namespace=? AND generation=? AND ordinal=?",
                    (namespace, epoch, ordinal),
                )
                total, count = connection.execute(
                    "SELECT coalesce(sum(length(body)),0),count(*) FROM images"
                ).fetchone()
                for rowid, size in connection.execute(
                    "SELECT rowid,length(body) FROM images ORDER BY accessed,rowid"
                ).fetchall():
                    if total + len(body) <= self._max_bytes and count < _MAX_ENTRIES:
                        break
                    connection.execute("DELETE FROM images WHERE rowid=?", (rowid,))
                    total -= size
                    count -= 1
                now = self._clock()
                connection.execute(
                    "INSERT INTO images VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        namespace,
                        epoch,
                        ordinal,
                        body,
                        mime,
                        page_count,
                        hashlib.sha256(body).hexdigest(),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """DELETE FROM chapters WHERE namespace NOT IN
                    (SELECT namespace FROM images) AND namespace<>?""",
                    (namespace,),
                )
        except OSError, sqlite3.Error, RuntimeError:
            # Cache failures must not turn a successfully loaded page into a reader failure.
            return

    def invalidate(self, namespace: str) -> None:
        self._validate(namespace, 1, 1)
        # Advance a small generation row instead of journaling a whole chapter's image bytes.
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO chapters VALUES (?,1)
                ON CONFLICT(namespace) DO UPDATE SET generation=generation+1""",
                (namespace,),
            )
