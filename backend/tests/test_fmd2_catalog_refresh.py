from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest

from app.connectors.http_client import ConnectorHttpSession
from app.services.fmd2_catalog_refresh import FMD2_CATALOG_ORIGINS, Fmd2CatalogRefreshService

INVENTARIO = "422d5e83e2ae4b3b84742122b00926ef"
RAGNAROK = "36b0f19e144a4642a2839ec9cb728c3a"
HOSTS = ("inventariooculto.com", "ragnarokscanlation.org")
NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]
SCHEMA = (
    "CREATE TABLE masterlist (link TEXT NOT NULL PRIMARY KEY,title TEXT,alttitles TEXT,"
    "authors TEXT,artists TEXT,genres TEXT,status TEXT,summary TEXT,numchapter INTEGER,jdn INTEGER)"
)


def _html(
    slugs: tuple[str, ...] = ("old",), *, next_page: int | None = None,
    previous: str | None = None,
) -> bytes:
    cards = "".join(
        '<div class="page-item-detail manga"><div class="post-title"><h3 class="h5">'
        f'<a href="/manga/{slug}/">{slug.title()}</a></h3></div></div>' for slug in slugs
    )
    following = (
        '<div class="nav-previous"><a href="/manga/page/'
        f'{next_page}/">Older Posts</a></div>' if next_page is not None else ""
    )
    back = f'<div class="nav-next"><a href="{previous}">Newer posts</a></div>' if previous else ""
    return (
        '<html><body><div class="c-tabs-item"><div class="page-content-listing">'
        + cards + '</div><nav class="navigation paging-navigation"><div class="nav-links">'
        + following + back + '</div></nav></div><footer><a href="/manga/promo/">'
        'Advertisement</a><a href="https://evil.example/manga/page/9/">Next</a></footer>'
        '</body></html>'
    ).encode()


def _seed(directory: Path, source_id: str = INVENTARIO, *, count: int = 1) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{source_id}.db"
    with closing(sqlite3.connect(path)) as database:
        database.execute(SCHEMA)
        database.executemany(
            "INSERT INTO masterlist(link,title) VALUES (?,?)",
            [(f"/manga/old-{number}/", f"Old {number}") for number in range(count)],
        )
        database.commit()
    return path


def _entries(path: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        assert database.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert len(database.execute("PRAGMA table_info(masterlist)").fetchall()) == 10
        return cast(list[tuple[str, str]], database.execute(
            "SELECT link,title FROM masterlist ORDER BY link"
        ).fetchall())


def _record(path: Path, source_id: str = INVENTARIO) -> dict[str, object]:
    state = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    sources = state["sources"]
    assert isinstance(sources, dict)
    record = cast(dict[str, object], sources)[source_id]
    assert isinstance(record, dict)
    return cast(dict[str, object], record)


class _Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


class _Harness:
    def __init__(self, root: Path) -> None:
        self.clock = _Clock()
        self.calls: list[httpx.Request] = []
        self.pages: dict[tuple[str, str], tuple[int, bytes]] = {
            (host, "/manga/"): (200, _html()) for host in HOSTS
        }
        self.handler: Handler | None = None
        self.session = ConnectorHttpSession(test_transport=httpx.MockTransport(self.respond))
        self.client = self.session.scoped_client(FMD2_CATALOG_ORIGINS)
        self.directory = root / "catalogs"
        self.state_path = root / "refresh.json"
        self.service = Fmd2CatalogRefreshService(
            self.client, self.directory, self.state_path, clock=self.clock
        )

    async def respond(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.method == "GET" and request.url.host in HOSTS
        assert not request.url.query and not request.url.fragment
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert "user-agent" not in request.headers
        if self.handler is not None:
            return await self.handler(request)
        status, body = self.pages.get((request.url.host, request.url.path), (404, b""))
        return httpx.Response(status, content=body, headers={
            "content-type": "text/html; charset=utf-8", "set-cookie": "ignored=1",
        })


@asynccontextmanager
async def _harness(root: Path) -> AsyncGenerator[_Harness]:
    harness = _Harness(root)
    try:
        yield harness
    finally:
        await harness.service.close()
        await harness.session.close()


@pytest.fixture(autouse=True)
def _fast_page_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.fmd2_catalog_refresh._PAGE_PAUSE_SECONDS", 0)


def test_second_page_adds_a_title_and_concurrent_calls_share_daily_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.fmd2_catalog_refresh._PAGE_PAUSE_SECONDS", 0)

    async def run() -> None:
        async with _harness(tmp_path) as h:
            _seed(h.directory)
            h.pages[(HOSTS[0], "/manga/")] = (200, _html(next_page=2))
            h.pages[(HOSTS[0], "/manga/page/2/")] = (200, _html(("new",), previous="/manga/"))
            await asyncio.gather(*(h.service.refresh_due() for _ in range(10)))
            target = h.directory / f"{INVENTARIO}.db"
            assert _entries(target) == [("/manga/new/", "New"), ("/manga/old/", "Old")]
            assert len(h.calls) == 3
            saved = _record(h.state_path)
            assert saved["updated_at"] == NOW.isoformat().replace("+00:00", "Z")
            assert saved["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
            assert saved["retry_at"] is None
            restored = Fmd2CatalogRefreshService(
                h.client, h.directory, h.state_path, clock=h.clock
            )
            try:
                h.clock.value += timedelta(hours=23, minutes=59)
                await restored.refresh_due()
                assert len(h.calls) == 3
                h.clock.value += timedelta(minutes=1)
                await restored.refresh_due()
                assert len(h.calls) == 6
            finally:
                await restored.close()
            assert not list(tmp_path.rglob(".yomira-catalog-*"))

    asyncio.run(run())


@pytest.mark.parametrize("status", [403, 429, 503])
def test_failed_refresh_preserves_last_success_and_persists_cooldown(
    tmp_path: Path, status: int,
) -> None:
    async def run() -> None:
        async with _harness(tmp_path) as h:
            await h.service.refresh_due()
            target = h.directory / f"{INVENTARIO}.db"
            original, saved = target.read_bytes(), _record(h.state_path)
            h.clock.value += timedelta(days=1)
            h.pages = {(host, "/manga/"): (status, b"blocked") for host in HOSTS}
            await h.service.refresh_due()
            assert target.read_bytes() == original
            failed = _record(h.state_path)
            assert failed["updated_at"] == saved["updated_at"]
            assert failed["sha256"] == saved["sha256"]
            retry = failed["retry_at"]
            assert isinstance(retry, str)
            assert datetime.fromisoformat(retry) == h.clock.value + timedelta(hours=1)
            restored = Fmd2CatalogRefreshService(
                h.client, h.directory, h.state_path, clock=h.clock
            )
            try:
                before = len(h.calls)
                await asyncio.gather(*(restored.refresh_due() for _ in range(8)))
                h.clock.value += timedelta(minutes=59)
                await restored.refresh_due()
                assert len(h.calls) == before
                h.clock.value += timedelta(minutes=1)
                await restored.refresh_due()
                assert len(h.calls) == before + 2
            finally:
                await restored.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [
    "second_page", "empty", "footer", "truncated", "bad_title", "external_title",
    "external_page", "cycle", "gap", "duplicate_page", "page_limit", "title_limit",
    "too_large", "non_utf8", "network",
    "no_navigation", "no_previous", "wrong_previous",
])
def test_incomplete_or_untrusted_directory_preserves_previous_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    monkeypatch.setattr("app.services.fmd2_catalog_refresh._PAGE_PAUSE_SECONDS", 0)

    async def run() -> None:
        async with _harness(tmp_path) as h:
            target = _seed(h.directory)
            original = target.read_bytes()
            body = _html()
            if failure in {
                "second_page", "cycle", "gap", "duplicate_page", "page_limit",
                "no_previous", "wrong_previous",
            }:
                body = _html(next_page=3 if failure == "gap" else 2)
                h.pages[(HOSTS[0], "/manga/page/2/")] = (
                    503 if failure == "second_page" else 200,
                    _html(("old" if failure == "duplicate_page" else "new",),
                          next_page=2 if failure == "cycle" else (
                              3 if failure == "page_limit" else None
                          ), previous=(
                              None if failure == "no_previous" else (
                                  "/manga/page/2/" if failure == "wrong_previous" else "/manga/"
                              )
                          )),
                )
            if failure == "empty":
                body = b"<html><body></body></html>"
            elif failure == "footer":
                body = b'<html><footer><a href="/manga/promo/">Promo</a></footer></html>'
            elif failure == "truncated":
                body = body.removesuffix(b"</body></html>")
            elif failure == "no_navigation":
                body = body.replace(b'<nav class="navigation paging-navigation">', b'<div>')
                body = body.replace(b'</nav>', b'</div>')
            elif failure == "bad_title":
                body = body.replace(b">Old</a>", b">" + b"a" * 201 + b"</a>")
            elif failure == "external_title":
                body = body.replace(b'href="/manga/old/"', b'href="https://evil.example/manga/old/"')
            elif failure == "external_page":
                body = _html(next_page=2).replace(
                    b'href="/manga/page/2/"', b'href="https://evil.example/manga/page/2/"'
                )
            elif failure == "page_limit":
                monkeypatch.setattr("app.services.fmd2_catalog_refresh._MAX_PAGES", 2)
            elif failure == "title_limit":
                monkeypatch.setattr("app.services.fmd2_catalog_refresh._MAX_TITLES", 1)
                body = _html(("old", "new"))
            elif failure == "too_large":
                body = body + b" " * (2 * 1024 * 1024)
            elif failure == "non_utf8":
                body = b"\xff" + body
            elif failure == "network":
                async def fail(request: httpx.Request) -> httpx.Response:
                    raise httpx.ReadTimeout("simulated", request=request)
                h.handler = fail
            h.pages[(HOSTS[0], "/manga/")] = (200, body)
            await h.service.refresh_due()
            assert target.read_bytes() == original
            record = _record(h.state_path)
            assert record["updated_at"] is None and record["sha256"] is None
            assert record["retry_at"] is not None
            assert not list(tmp_path.rglob(".yomira-catalog-*"))

    asyncio.run(run())


def test_catalog_cannot_silently_shrink(tmp_path: Path) -> None:
    async def run() -> None:
        async with _harness(tmp_path) as h:
            target = _seed(h.directory, count=3)
            original = target.read_bytes()
            h.pages[(HOSTS[0], "/manga/")] = (200, _html(("old", "new")))
            await h.service.refresh_due()
            assert target.read_bytes() == original
            assert _record(h.state_path)["updated_at"] is None

    asyncio.run(run())


def test_translation_cards_remain_part_of_the_directory(tmp_path: Path) -> None:
    async def run() -> None:
        async with _harness(tmp_path) as h:
            h.pages[(HOSTS[1], "/manga/")] = (200, _html(("jjk-translation",)).replace(
                b'class="page-item-detail manga"', b'class="page-item-detail text"'
            ).replace(b'/manga/jjk-translation/', b'/series/jjk-translation/'))
            await h.service.refresh_due()
            assert _entries(h.directory / f"{RAGNAROK}.db") == [
                ("/series/jjk-translation/", "Jjk-Translation")
            ]

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["corrupt", "zero", "wrong_table", "empty_masterlist"])
def test_complete_directory_repairs_an_unusable_catalog(tmp_path: Path, kind: str) -> None:
    async def run() -> None:
        async with _harness(tmp_path) as h:
            h.directory.mkdir()
            target = h.directory / f"{INVENTARIO}.db"
            if kind in {"corrupt", "zero"}:
                target.write_bytes(b"broken SQLite" if kind == "corrupt" else b"")
            else:
                with closing(sqlite3.connect(target)) as database:
                    database.execute(
                        "CREATE TABLE other(value TEXT)" if kind == "wrong_table" else SCHEMA
                    )
                    database.commit()
            await h.service.refresh_due()
            assert _entries(target) == [("/manga/old/", "Old")]
            assert _record(h.state_path)["sha256"] == (
                hashlib.sha256(target.read_bytes()).hexdigest()
            )
            assert not list(tmp_path.rglob(".yomira-catalog-*"))

    asyncio.run(run())


def test_failed_network_preserves_even_a_corrupt_previous_catalog(tmp_path: Path) -> None:
    async def run() -> None:
        async with _harness(tmp_path) as h:
            h.directory.mkdir()
            target = h.directory / f"{INVENTARIO}.db"
            target.write_bytes(b"keep these corrupt bytes")
            h.pages[(HOSTS[0], "/manga/")] = (429, b"blocked")
            await h.service.refresh_due()
            assert target.read_bytes() == b"keep these corrupt bytes"
            assert _record(h.state_path)["sha256"] is None

    asyncio.run(run())


def test_digest_mismatch_repairs_a_catalog_even_within_daily_ttl(tmp_path: Path) -> None:
    async def run() -> None:
        async with _harness(tmp_path) as h:
            await h.service.refresh_due()
            target = h.directory / f"{INVENTARIO}.db"
            target.write_bytes(b"damaged after the last successful refresh")
            before = len(h.calls)
            await h.service.refresh_due()
            assert len(h.calls) == before + 1
            assert _entries(target) == [("/manga/old/", "Old")]
            assert _record(h.state_path)["sha256"] == (
                hashlib.sha256(target.read_bytes()).hexdigest()
            )

    asyncio.run(run())


@pytest.mark.parametrize("kind", [
    "invalid", "oversized", "unknown_source", "naive", "future", "future_retry",
])
def test_untrusted_state_cannot_suppress_refresh(tmp_path: Path, kind: str) -> None:
    async def run() -> None:
        state_path = tmp_path / "refresh.json"
        if kind in {"invalid", "oversized"}:
            state_path.write_text("not json" if kind == "invalid" else " " * 16_385)
        else:
            state = {
                "version": 1,
                "sources": {
                    "0" * 32 if kind == "unknown_source" else INVENTARIO: {
                        "updated_at": (NOW + timedelta(days=1)).isoformat() if kind == "future"
                        else NOW.replace(tzinfo=None).isoformat() if kind == "naive" else None,
                        "sha256": "a" * 64 if kind in {"naive", "future"} else None,
                        "retry_at": (NOW + timedelta(days=1)).isoformat()
                        if kind == "future_retry" else None,
                    },
                },
            }
            state_path.write_text(json.dumps(state))
        async with _harness(tmp_path) as h:
            await h.service.refresh_due()
            assert len(h.calls) == 2
            assert _entries(h.directory / f"{INVENTARIO}.db") == [("/manga/old/", "Old")]
            assert _record(h.state_path)["updated_at"] is not None

    asyncio.run(run())


@pytest.mark.parametrize("destination", ["catalog", "state"])
def test_failed_publication_rolls_back_catalog_and_keeps_previous_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destination: str,
) -> None:
    async def run() -> None:
        async with _harness(tmp_path) as h:
            await h.service.refresh_due()
            target = h.directory / f"{INVENTARIO}.db"
            original, saved = target.read_bytes(), _record(h.state_path)
            h.clock.value += timedelta(days=1)
            h.pages[(HOSTS[0], "/manga/")] = (200, _html(("old", "new")))
            replace = Path.replace
            state_replacements = 0

            def fail_publish(path: Path, output: str | Path) -> Path:
                nonlocal state_replacements
                if Path(output) == h.state_path:
                    state_replacements += 1
                    if destination == "state" and state_replacements == 2:
                        raise OSError("simulated state publication failure")
                if destination == "catalog" and Path(output) == target and path.suffix == ".tmp":
                    raise OSError("simulated catalog publication failure")
                return replace(path, output)

            monkeypatch.setattr(Path, "replace", fail_publish)
            await h.service.refresh_due()
            assert target.read_bytes() == original
            after = _record(h.state_path)
            assert after["updated_at"] == saved["updated_at"]
            assert after["sha256"] == saved["sha256"]
            assert after["retry_at"] is not None
            assert not list(tmp_path.rglob(".yomira-catalog-*"))

    asyncio.run(run())


def test_cancellation_during_page_two_preserves_catalog_and_restart_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.fmd2_catalog_refresh._PAGE_PAUSE_SECONDS", 0)

    async def run() -> None:
        async with _harness(tmp_path) as h:
            target = _seed(h.directory)
            original = target.read_bytes()
            entered = asyncio.Event()

            async def wait_on_page_two(request: httpx.Request) -> httpx.Response:
                if request.url.host == HOSTS[0] and request.url.path.endswith("/2/"):
                    entered.set()
                    await asyncio.Event().wait()
                return httpx.Response(200, content=_html(next_page=2),
                                      headers={"content-type": "text/html"})

            h.handler = wait_on_page_two
            task = asyncio.create_task(h.service.refresh_due())
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert target.read_bytes() == original
            assert _record(h.state_path)["updated_at"] is None
            inventory_calls = len([request for request in h.calls if request.url.host == HOSTS[0]])
            h.handler = None
            restored = Fmd2CatalogRefreshService(
                h.client, h.directory, h.state_path, clock=h.clock
            )
            try:
                await restored.refresh_due()
                assert len([request for request in h.calls if request.url.host == HOSTS[0]]) == (
                    inventory_calls
                )
            finally:
                await restored.close()

    asyncio.run(run())


@pytest.mark.parametrize("background", [False, True])
def test_start_is_immediate_and_close_cancels_active_io_without_closing_shared_session(
    tmp_path: Path, background: bool,
) -> None:
    async def run() -> None:
        async with _harness(tmp_path) as h:
            target = _seed(h.directory)
            original = target.read_bytes()
            entered = asyncio.Event()

            async def wait_forever(_request: httpx.Request) -> httpx.Response:
                entered.set()
                await asyncio.Event().wait()
                raise AssertionError("Cancelled request cannot finish")

            h.handler = wait_forever
            manual: asyncio.Task[None] | None = None
            if background:
                await asyncio.wait_for(h.service.start(), timeout=0.1)
                await h.service.start()
            else:
                manual = asyncio.create_task(h.service.refresh_due())
            await asyncio.wait_for(entered.wait(), timeout=2)
            await asyncio.wait_for(h.service.close(), timeout=1)
            if manual is not None:
                with pytest.raises(asyncio.CancelledError):
                    await manual
            assert not h.session.is_closed and target.read_bytes() == original
            assert _record(h.state_path)["retry_at"] is not None
            before = len(h.calls)
            await h.service.close()
            await h.service.start()
            await h.service.refresh_due()
            assert len(h.calls) == before

    asyncio.run(run())


def test_real_page_pause_is_one_second(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pauses: list[float] = []
    original_sleep = asyncio.sleep

    async def quick_sleep(delay: float) -> None:
        pauses.append(delay)
        await original_sleep(0)

    monkeypatch.setattr("app.services.fmd2_catalog_refresh.asyncio.sleep", quick_sleep)
    monkeypatch.setattr("app.services.fmd2_catalog_refresh._PAGE_PAUSE_SECONDS", 1.0)

    async def run() -> None:
        async with _harness(tmp_path) as h:
            h.pages[(HOSTS[0], "/manga/")] = (200, _html(next_page=2))
            h.pages[(HOSTS[0], "/manga/page/2/")] = (200, _html(("new",), previous="/manga/"))
            await h.service.refresh_due()
            assert pauses == [1.0, 1.0]

    asyncio.run(run())
