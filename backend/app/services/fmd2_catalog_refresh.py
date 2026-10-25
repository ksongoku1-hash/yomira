"""Refresh two public FMD2 indexes without blocking startup or publishing partial lists."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.http_client import ConnectorHttpClient

_SOURCES = {
    "422d5e83e2ae4b3b84742122b00926ef": "https://inventariooculto.com",
    "36b0f19e144a4642a2839ec9cb728c3a": "https://ragnarokscanlation.org",
}
FMD2_CATALOG_ORIGINS = frozenset(_SOURCES.values())
_DAY = timedelta(days=1)
_COOLDOWN = timedelta(hours=1)
_PAGE_PAUSE_SECONDS = 1.0
_MAX_PAGES = 20
_MAX_TITLES = 10_000
_MAX_HTML_BYTES = 2 * 1024 * 1024
_MAX_CATALOG_BYTES = 32 * 1024 * 1024
_MAX_STATE_BYTES = 16 * 1024
_COLUMNS = (
    "link", "title", "alttitles", "authors", "artists", "genres", "status", "summary",
    "numchapter", "jdn",
)
_SCHEMA = (
    "CREATE TABLE masterlist (link TEXT NOT NULL PRIMARY KEY,title TEXT,alttitles TEXT,"
    "authors TEXT,artists TEXT,genres TEXT,status TEXT,summary TEXT,numchapter INTEGER,jdn INTEGER)"
)
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
    "source", "track", "wbr",
})
logger = logging.getLogger("yomira.fmd2.catalog_refresh")


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    updated_at: datetime | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    retry_at: datetime | None = None

    @field_validator("updated_at", "retry_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Index timestamps require a timezone")
        return value


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    sources: dict[str, _Record] = Field(default_factory=dict, max_length=2)

    @field_validator("sources")
    @classmethod
    def require_known_sources(cls, value: dict[str, _Record]) -> dict[str, _Record]:
        if not value.keys() <= _SOURCES.keys():
            raise ValueError("Unknown public index")
        return value


def _safe_path(href: str, origin: str, *, page: bool = False) -> str:
    if (
        not href or len(href) > 1024 or href.startswith("//")
        or any(character.isspace() or ord(character) < 32 for character in href)
        or "\\" in href
    ):
        raise ValueError("Invalid directory link")
    parsed = urlsplit(href)
    if parsed.query or parsed.fragment:
        raise ValueError("Directory links cannot contain queries")
    if parsed.scheme or parsed.netloc:
        if f"{parsed.scheme}://{parsed.netloc}" != origin:
            raise ValueError("Directory links must stay on their source")
    elif not href.startswith("/"):
        raise ValueError("Directory links must be source-relative")
    if re.search(r"%(?![0-9a-fA-F]{2})", parsed.path):
        raise ValueError("Invalid path encoding")
    decoded = unquote(parsed.path, errors="strict")
    pattern = r"/manga/(?:page/[1-9][0-9]*/)?" if page else r"/(?:manga|series)/[^/?#\\]+/"
    if (
        len(parsed.path) > 512 or re.fullmatch(pattern, decoded) is None
        or any(part in {".", ".."} for part in decoded.split("/"))
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in decoded)
    ):
        raise ValueError("Invalid directory path")
    return parsed.path


@dataclass
class _Anchor:
    href: str
    role: Literal["title", "next", "previous"]
    text: list[str] = field(default_factory=list[str])


class _Directory(HTMLParser):
    def __init__(self, origin: str) -> None:
        super().__init__(convert_charrefs=True)
        self.origin = origin
        self.entries: dict[str, str] = {}
        self.next_paths: set[str] = set()
        self.previous_paths: set[str] = set()
        self._frames: list[tuple[str, frozenset[str]]] = []
        self._anchor: _Anchor | None = None
        self._item_depth: int | None = None
        self._item_has_title = False
        self._listing_seen = False
        self._navigation_seen = False
        self._html_closed = False
        self._nodes = 0

    def _within(self, class_name: str) -> bool:
        return any(class_name in classes for _, classes in self._frames)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._nodes += 1
        if self._nodes > 100_000 or len(self._frames) > 128:
            raise ValueError("Directory structure exceeds its limits")
        attributes = dict(attrs)
        classes = frozenset((attributes.get("class") or "").split())
        if "page-content-listing" in classes and self._within("c-tabs-item"):
            if self._listing_seen:
                raise ValueError("Ambiguous directory listing")
            self._listing_seen = True
        if (
            tag == "nav" and {"navigation", "paging-navigation"} <= classes
            and self._within("c-tabs-item")
        ):
            if self._navigation_seen:
                raise ValueError("Ambiguous directory navigation")
            self._navigation_seen = True
        if (
            "page-item-detail" in classes
            and self._within("c-tabs-item") and self._within("page-content-listing")
        ):
            if self._item_depth is not None:
                raise ValueError("Nested directory cards")
            self._item_depth = len(self._frames) + 1
            self._item_has_title = False
        if tag == "a":
            role: Literal["title", "next", "previous"] | None = None
            if self._item_depth is not None and self._within("post-title"):
                role = "title"
            elif (
                self._within("c-tabs-item") and self._within("paging-navigation")
                and self._within("nav-links")
            ):
                if self._within("nav-previous"):
                    role = "next"
                elif self._within("nav-next"):
                    role = "previous"
            if role is not None:
                if self._anchor is not None:
                    raise ValueError("Nested directory anchors")
                self._anchor = _Anchor(attributes.get("href") or "", role)
        if tag not in _VOID_TAGS:
            self._frames.append((tag, classes))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor.text.append(data)
            if sum(map(len, self._anchor.text)) > 4096:
                raise ValueError("Directory anchor is too long")

    def handle_endtag(self, tag: str) -> None:
        indexes = [index for index, frame in enumerate(self._frames) if frame[0] == tag]
        if not indexes:
            return
        index = indexes[-1]
        if any(
            frame_tag == "a" or "page-content-listing" in classes
            or "page-item-detail" in classes
            for frame_tag, classes in self._frames[index + 1:]
        ):
            raise ValueError("Incomplete directory structure")
        if tag == "a" and self._anchor is not None:
            anchor, self._anchor = self._anchor, None
            path = _safe_path(anchor.href, self.origin, page=anchor.role != "title")
            if anchor.role == "title":
                title = " ".join("".join(anchor.text).split())
                if (
                    not 1 <= len(title) <= 200 or self._item_has_title
                    or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in title)
                    or path in self.entries
                ):
                    raise ValueError("Invalid or duplicate directory title")
                self.entries[path] = title
                self._item_has_title = True
            elif anchor.role == "next":
                self.next_paths.add(path)
            else:
                self.previous_paths.add(path)
        if self._item_depth == index + 1:
            if not self._item_has_title:
                raise ValueError("Directory card has no title")
            self._item_depth = None
        del self._frames[index:]
        if tag == "html":
            self._html_closed = True

    def finish(self, body: bytes) -> None:
        if len(body) > _MAX_HTML_BYTES:
            raise ValueError("Directory HTML exceeds its limit")
        self.feed(body.decode("utf-8-sig"))
        self.close()
        if (
            not self._listing_seen or not self._navigation_seen
            or not self._html_closed or not self.entries
            or self._item_depth is not None or self._anchor is not None
        ):
            raise ValueError("Empty or incomplete directory")


class Fmd2CatalogRefreshService:
    def __init__(
        self, client: ConnectorHttpClient, catalog_directory: Path, state_path: Path,
        *, clock: Callable[[], datetime] | None = None,
    ) -> None:
        if client.allowed_origins != FMD2_CATALOG_ORIGINS:
            raise ValueError("Catalog refresh requires the two exact public origins")
        if not catalog_directory.is_absolute() or not state_path.is_absolute():
            raise ValueError("Catalog and state paths must be absolute")
        self._client = client
        self._directory = catalog_directory
        self._state_path = state_path
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._active: asyncio.Task[object] | None = None
        self._closed = False
        self._requested = False
        self._state = _State()
        now = self._now()
        try:
            if state_path.is_file() and not state_path.is_symlink():
                with state_path.open("rb") as file:
                    raw = file.read(_MAX_STATE_BYTES + 1)
                if len(raw) <= _MAX_STATE_BYTES:
                    state = _State.model_validate_json(raw)
                    if all(
                        (record.updated_at is None or record.updated_at <= now)
                        and (record.retry_at is None or record.retry_at <= now + _COOLDOWN)
                        for record in state.sources.values()
                    ):
                        self._state = state
        except OSError, ValueError, ValidationError:
            pass

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("Catalog refresh clock requires a timezone")
        return now.astimezone(UTC)

    async def start(self) -> None:
        if self._closed:
            return
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="yomira.fmd2.catalog_refresh")

    async def close(self) -> None:
        self._closed = True
        tasks = {
            task for task in (self._task, self._active)
            if task is not None and task is not asyncio.current_task()
        }
        self._task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        async with self._lock:
            pass

    async def _run(self) -> None:
        while not self._closed:
            await self.refresh_due()
            deadlines = [
                record.retry_at or (record.updated_at + _DAY if record.updated_at else self._now())
                for record in self._state.sources.values()
            ]
            delay = min((deadline - self._now()).total_seconds() for deadline in deadlines)
            await asyncio.sleep(max(1.0, min(delay, _COOLDOWN.total_seconds())))

    async def refresh_due(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._active = asyncio.current_task()
            try:
                for source_id, origin in _SOURCES.items():
                    if self._closed:
                        return
                    now = self._now()
                    record = self._state.sources.get(source_id, _Record())
                    if record.retry_at is not None and record.retry_at > now:
                        continue
                    target = self._directory / f"{source_id}.db"
                    if (
                        record.updated_at is not None and now - _DAY < record.updated_at <= now
                        and record.sha256 is not None and self._matches(target, record.sha256)
                    ):
                        continue
                    self._state.sources[source_id] = record.model_copy(
                        update={"retry_at": now + _COOLDOWN}
                    )
                    try:
                        self._save_state(self._state)
                        async with asyncio.timeout(180):
                            entries = await self._collect(origin)
                        self._publish(source_id, entries)
                    except asyncio.CancelledError:
                        raise
                    except OSError, ValueError, sqlite3.Error, SourceConnectorError, TimeoutError:
                        logger.warning(
                            "fmd2.catalog_refresh.failed", extra={"source_id": source_id}
                        )
            finally:
                self._active = None

    @staticmethod
    def _matches(path: Path, digest: str) -> bool:
        try:
            return (
                not path.is_symlink() and path.is_file()
                and path.stat().st_size <= _MAX_CATALOG_BYTES
                and hashlib.sha256(path.read_bytes()).hexdigest() == digest
            )
        except OSError:
            return False

    async def _collect(self, origin: str) -> dict[str, str]:
        path = "/manga/"
        visited: set[str] = set()
        entries: dict[str, str] = {}
        while True:
            if path in visited or len(visited) >= _MAX_PAGES:
                raise ValueError("Directory pagination is cyclic or exceeds its limit")
            if self._requested:
                await asyncio.sleep(_PAGE_PAUSE_SECONDS)
            visited.add(path)
            self._requested = True
            response = await self._client.get_html(
                origin + path, operation=SourceConnectorOperation.SEARCH
            )
            final = urlsplit(response.final_url)
            if (
                response.status_code != 200
                or response.content_type not in {"text/html", "application/xhtml+xml"}
                or f"{final.scheme}://{final.netloc}" != origin
                or final.path != path or final.query or final.fragment
            ):
                raise ValueError("Directory response was rejected")
            page = _Directory(origin)
            page.finish(response.body)
            if entries.keys() & page.entries.keys():
                raise ValueError("Directory repeated titles across pages")
            entries.update(page.entries)
            if len(entries) > _MAX_TITLES:
                raise ValueError("Directory title limit exceeded")
            current = len(visited)
            previous_numbers = {
                1 if previous == "/manga/" else int(previous.split("/")[-2])
                for previous in page.previous_paths
            }
            if previous_numbers != ({current - 1} if current > 1 else set()):
                raise ValueError("Invalid previous directory page")
            if not page.next_paths:
                return entries
            if len(page.next_paths) != 1:
                raise ValueError("Ambiguous next directory page")
            path = next(iter(page.next_paths))
            if path == "/manga/" or int(path.split("/")[-2]) != current + 1:
                raise ValueError("Directory pagination is not contiguous")

    @staticmethod
    def _temporary(directory: Path, suffix: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise ValueError("Catalog directories cannot be symbolic links")
        with NamedTemporaryFile(dir=directory, prefix=".yomira-catalog-", suffix=suffix,
                                delete=False) as file:
            return Path(file.name)

    def _state_temporary(self, state: _State) -> Path:
        if self._state_path.is_symlink():
            raise ValueError("Catalog state cannot be a symbolic link")
        content = state.model_dump_json().encode("utf-8")
        if len(content) > _MAX_STATE_BYTES:
            raise ValueError("Catalog state exceeds its limit")
        temporary = self._temporary(self._state_path.parent, ".json.tmp")
        try:
            temporary.write_bytes(content)
            return temporary
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _save_state(self, state: _State) -> None:
        temporary = self._state_temporary(state)
        try:
            temporary.replace(self._state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _publish(self, source_id: str, entries: dict[str, str]) -> None:
        target = self._directory / f"{source_id}.db"
        if target.is_symlink():
            raise ValueError("Catalog cannot be a symbolic link")
        backup: Path | None = None
        temporary = self._temporary(self._directory, ".db.tmp")
        state_temporary: Path | None = None
        rollback_failed = False
        try:
            if target.exists():
                if target.stat().st_size > _MAX_CATALOG_BYTES:
                    raise ValueError("Previous catalog exceeds its limit")
                backup = self._temporary(self._directory, ".db.backup")
                shutil.copyfile(target, backup)
                previous_count: int | None = None
                try:
                    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as old:
                        columns = tuple(
                            row[1] for row in old.execute("PRAGMA table_info(masterlist)")
                        )
                        integrity = old.execute("PRAGMA quick_check").fetchone()
                        row = old.execute("SELECT count(*) FROM masterlist").fetchone()
                        if columns == _COLUMNS and integrity == ("ok",) and row is not None:
                            previous_count = int(row[0])
                except sqlite3.Error:
                    pass
                # shortcut: indexes never shrink; review legitimate removals before allowing it.
                if previous_count is not None and len(entries) < previous_count:
                    raise ValueError("Directory is smaller than the previous catalog")
            with closing(sqlite3.connect(temporary)) as database:
                database.execute(_SCHEMA)
                database.executemany(
                    "INSERT INTO masterlist(link,title) VALUES (?,?)", sorted(entries.items())
                )
                database.commit()
                columns = tuple(row[1] for row in database.execute("PRAGMA table_info(masterlist)"))
                integrity = database.execute("PRAGMA quick_check").fetchone()
                count = database.execute("SELECT count(*) FROM masterlist").fetchone()
                if columns != _COLUMNS or integrity != ("ok",) or count != (len(entries),):
                    raise ValueError("Invalid generated catalog")
            if temporary.stat().st_size > _MAX_CATALOG_BYTES:
                raise ValueError("Generated catalog exceeds its limit")
            record = _Record(
                updated_at=self._now(), sha256=hashlib.sha256(temporary.read_bytes()).hexdigest()
            )
            state = self._state.model_copy(deep=True)
            state.sources[source_id] = record
            state_temporary = self._state_temporary(state)
            temporary.replace(target)
            try:
                state_temporary.replace(self._state_path)
            except OSError:
                try:
                    if backup is not None:
                        backup.replace(target)
                        backup = None
                    else:
                        target.unlink()
                except OSError:
                    rollback_failed = True
                    raise
                raise
            self._state = state
        finally:
            for candidate in (temporary, state_temporary, None if rollback_failed else backup):
                if candidate is not None:
                    with suppress(OSError):
                        candidate.unlink(missing_ok=True)
