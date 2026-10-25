import asyncio
import hashlib
import os
import sqlite3
import stat
from pathlib import Path
from uuid import UUID

import pytest
from support.sqlite import open_test_database
from test_online_reader import _memory_harness  # pyright: ignore[reportPrivateUsage]

from app.connectors.pages import PageFetchRequest
from app.persistence.disk_budget import DiskBudgetManager, DiskBudgetUnavailableError
from app.persistence.migrations.runner import MigrationRunner
from app.services.download_transport import MemoryPageResult, NetworkOrigin
from app.services.image_validation import ImageValidationPolicy
from app.services.online_image_cache import OnlineImageCache
from app.services.online_reader import OnlineReaderSessionService

KEY = hashlib.sha256(b"chapter-source").hexdigest()


def test_disk_cache_is_bounded_persistent_expires_and_can_be_refreshed(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    budget = DiskBudgetManager(database)
    now = [1.0]
    cache = OnlineImageCache(database, budget, max_bytes=6, clock=lambda: now[0])
    try:
        cache.put(KEY, 1, 4, b"111", "image/png")
        now[0] += 1
        cache.put(KEY, 2, 4, b"222", "image/png")
        now[0] += 1
        assert cache.get(KEY, 1, 4) == (b"111", "image/png")
        now[0] += 1
        cache.put(KEY, 3, 4, b"333", "image/png")
        assert cache.get(KEY, 2, 4) is None
        reopened = OnlineImageCache(database, budget, max_bytes=6, clock=lambda: now[0])
        assert reopened.get(KEY, 1, 4) == (b"111", "image/png")
        assert reopened.get(KEY, 1, 5) is None
        assert reopened.get(hashlib.sha256(b"other-source").hexdigest(), 1, 4) is None
        reopened.invalidate(KEY)
        assert reopened.get(KEY, 1, 4) is None
        reopened.put(KEY, 1, 4, b"new", "image/png")
        assert reopened.get(KEY, 1, 4) == (b"new", "image/png")
        now[0] += 7 * 24 * 60 * 60 + 1
        assert reopened.get(KEY, 1, 4) is None
    finally:
        database.close()


def test_cached_images_remain_readable_when_no_disk_growth_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    budget = DiskBudgetManager(database)
    cache = OnlineImageCache(database, budget)
    try:
        cache.put(KEY, 1, 1, b"cached", "image/png")

        def unavailable(*_args: object, **_kwargs: object) -> None:
            raise DiskBudgetUnavailableError("INSUFFICIENT_SPACE", free_bytes=0, required_bytes=1)

        monkeypatch.setattr(budget, "reserve_bounded_operation", unavailable)
        assert cache.get(KEY, 1, 1) == (b"cached", "image/png")
        cache.put(KEY, 1, 1, b"new", "image/png")
        assert cache.get(KEY, 1, 1) == (b"cached", "image/png")
    finally:
        database.close()


def test_online_reader_reopens_from_disk_and_forced_retry_renews_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        harness, first, _runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
        budget = DiskBudgetManager(harness.database)
        disk = OnlineImageCache(harness.database, budget)
        monkeypatch.setattr(first, "_disk_cache", disk)
        requests = 0
        original = harness.transport.fetch_memory

        async def fetch(
            request: PageFetchRequest,
            *,
            session_id: UUID,
            ordinal: int,
            allowed_origins: frozenset[NetworkOrigin],
        ) -> MemoryPageResult:
            nonlocal requests
            requests += 1
            return await original(
                request, session_id=session_id, ordinal=ordinal, allowed_origins=allowed_origins
            )

        monkeypatch.setattr(harness.transport, "fetch_memory", fetch)
        second = OnlineReaderSessionService(
            harness.database,
            source_pages=harness.source_pages,
            transport=harness.transport,
            image_policy=ImageValidationPolicy(1024, 100, 100, 10_000),
            disk_cache=OnlineImageCache(harness.database, budget),
        )
        try:
            await first.open(harness.chapter_id)
            image = await first.open_page(harness.chapter_id, 1)
            assert b"".join(image.iter_chunks()) == harness.body
            await first.close()
            assert requests == 1
            await second.open(harness.chapter_id)
            image = await second.open_page(harness.chapter_id, 1)
            assert b"".join(image.iter_chunks()) == harness.body and requests == 1
            await second.open(harness.chapter_id, restart=True)
            image = await second.open_page(harness.chapter_id, 1)
            assert b"".join(image.iter_chunks()) == harness.body and requests == 2
            await second.pause(harness.chapter_id)
            path = tmp_path / "cache/online-images/images.sqlite3"
            # An internally consistent hash still cannot bypass the core image validator.
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE images SET body=?,sha256=?",
                    (b"not an image", hashlib.sha256(b"not an image").hexdigest()),
                )
            await second.open(harness.chapter_id)
            image = await second.open_page(harness.chapter_id, 1)
            assert b"".join(image.iter_chunks()) == harness.body and requests == 3
        finally:
            await first.close()
            await second.close()
            await harness.close()

    asyncio.run(run())


def test_cache_does_not_follow_a_reparse_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    cache = OnlineImageCache(database, DiskBudgetManager(database))
    original = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        if path == tmp_path / "cache":
            return os.stat_result((stat.S_IFLNK, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        return original(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    try:
        assert cache.get(KEY, 1, 1) is None
        cache.put(KEY, 1, 1, b"image", "image/png")
        assert not (tmp_path / "cache").exists()
        with pytest.raises(OSError):
            cache.invalidate(KEY)
    finally:
        database.close()


def test_foreign_database_is_not_modified_by_cache(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    cache = OnlineImageCache(database, DiskBudgetManager(database))
    path = tmp_path / "cache/online-images/images.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE personal(value TEXT)")
        connection.execute("INSERT INTO personal VALUES ('keep')")
    before = path.read_bytes()
    try:
        assert cache.get(KEY, 1, 1) is None
        cache.put(KEY, 1, 1, b"image", "image/png")
        with pytest.raises(sqlite3.DatabaseError):
            cache.invalidate(KEY)
        assert path.read_bytes() == before
    finally:
        database.close()


def test_hardlinked_cache_is_not_read_or_modified(tmp_path: Path) -> None:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    cache = OnlineImageCache(database, DiskBudgetManager(database))
    try:
        cache.put(KEY, 1, 1, b"cached", "image/png")
        path = tmp_path / "cache/online-images/images.sqlite3"
        os.link(path, tmp_path / "linked.sqlite3")
        before = path.read_bytes()
        assert cache.get(KEY, 1, 1) is None
        cache.put(KEY, 1, 1, b"new", "image/png")
        with pytest.raises(OSError):
            cache.invalidate(KEY)
        assert path.read_bytes() == before
    finally:
        database.close()
