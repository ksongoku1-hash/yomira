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

import app.services.online_image_cache as cache_module
from app.connectors.pages import PageFetchRequest
from app.persistence.disk_budget import DiskBudgetManager, DiskBudgetUnavailableError
from app.persistence.migrations.runner import MigrationRunner
from app.services.download_transport import MemoryPageResult, NetworkOrigin
from app.services.image_validation import ImageValidationPolicy
from app.services.online_image_cache import OnlineImageCache
from app.services.online_reader import OnlineReaderSessionService

KEY = hashlib.sha256(b"chapter-source").hexdigest()


def test_recent_disk_hits_do_not_reserve_space_or_write_access_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    budget = DiskBudgetManager(database)
    now = [1.0]
    cache = OnlineImageCache(database, budget, clock=lambda: now[0])
    try:
        cache.put(KEY, 1, 1, b"cached", "image/png")
        reservations: list[int] = []
        original = budget.reserve_bounded_operation

        def reserve(reservation_id: UUID, *, page_bytes: int, cbz_bytes: int = 0) -> None:
            reservations.append(page_bytes)
            original(reservation_id, page_bytes=page_bytes, cbz_bytes=cbz_bytes)

        monkeypatch.setattr(budget, "reserve_bounded_operation", reserve)
        now[0] += 10
        assert cache.get(KEY, 1, 1) == (b"cached", "image/png")
        assert reservations == []
        now[0] += 60
        assert cache.get(KEY, 1, 1) == (b"cached", "image/png")
        assert reservations == [65536]
    finally:
        database.close()


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
        now[0] += 61
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


def test_status_does_not_create_an_empty_cache_and_limit_changes_are_bounded(
    tmp_path: Path,
) -> None:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    cache = OnlineImageCache(database, DiskBudgetManager(database))
    try:
        state = cache.status()
        assert state.used_bytes == state.entries == state.file_bytes == 0
        assert not (tmp_path / "cache").exists()
        with cache.changing_limit(2 * 1024**3):
            pass
        assert cache.status().max_bytes == 2 * 1024**3
        for invalid in (0, -1, True, 8 * 1024**3 + 1):
            with pytest.raises(ValueError):
                with cache.changing_limit(invalid):
                    pass
        assert not (tmp_path / "cache").exists()
    finally:
        database.close()


def test_reducing_payload_budget_preserves_physical_cache_and_persistence_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    cache = OnlineImageCache(database, DiskBudgetManager(database), max_bytes=6)
    try:
        cache.put(KEY, 1, 2, b"111", "image/png")
        cache.put(KEY, 2, 2, b"222", "image/png")
        # Scale the overhead down to exercise physical-file-vs-payload limits inexpensively.
        monkeypatch.setattr(cache_module, "_SQLITE_OVERHEAD_BYTES", 4096)
        before = cache.status()
        assert before.file_bytes > 3 + 4096
        with cache.changing_limit(3):
            assert cache.status().used_bytes == 3
        assert cache.status().max_bytes == 3
        assert cache.get(KEY, 2, 2) == (b"222", "image/png")
        assert cache.status().file_bytes > 3 + 4096
        with pytest.raises(RuntimeError, match="commit failed"):
            with cache.changing_limit(20):
                raise RuntimeError("commit failed")
        assert cache.status().max_bytes == 3
    finally:
        database.close()


def test_eviction_reserves_bounded_journal_and_leaves_limit_unchanged_on_budget_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    budget = DiskBudgetManager(database)
    cache = OnlineImageCache(database, budget, max_bytes=6)
    try:
        cache.put(KEY, 1, 2, b"111", "image/png")
        cache.put(KEY, 2, 2, b"222", "image/png")
        reservations: list[int] = []

        def unavailable(_reservation_id: UUID, *, page_bytes: int, cbz_bytes: int = 0) -> None:
            reservations.append(page_bytes + cbz_bytes)
            raise DiskBudgetUnavailableError("INSUFFICIENT_SPACE", free_bytes=0, required_bytes=1)

        monkeypatch.setattr(budget, "reserve_bounded_operation", unavailable)
        with pytest.raises(DiskBudgetUnavailableError):
            with cache.changing_limit(3):
                pass
        assert reservations == [3 * 64 * 1024 * 1024 + 65536]
        assert cache.status().max_bytes == 6
        assert cache.status().used_bytes == 6
    finally:
        database.close()


@pytest.mark.parametrize("foreign", [True, False])
def test_limit_change_never_rewrites_foreign_or_malformed_cache(
    tmp_path: Path, foreign: bool
) -> None:
    database = open_test_database(tmp_path)
    MigrationRunner(database, tmp_path / "backups").upgrade_to_head()
    cache = OnlineImageCache(database, DiskBudgetManager(database))
    path = tmp_path / "cache/online-images/images.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        if not foreign:
            connection.execute("PRAGMA application_id=1498368323")
        connection.execute("CREATE TABLE personal(value TEXT)")
        connection.execute("INSERT INTO personal VALUES ('keep')")
    before = path.read_bytes()
    try:
        with pytest.raises(sqlite3.DatabaseError):
            with cache.changing_limit(128 * 1024**2):
                pass
        assert path.read_bytes() == before
    finally:
        database.close()
