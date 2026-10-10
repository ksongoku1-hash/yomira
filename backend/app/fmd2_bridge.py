"""Core-owned process and loopback acquisition for public FMD2 sources."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import stat
import subprocess
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import NoReturn, cast
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

import httpx

from app.connectors.chapter import ConnectorChapter
from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.metadata import ConnectorCapability, ConnectorMetadata
from app.connectors.pages import ConnectorPageReference, PageFetchRequest
from app.connectors.search import ConnectorSearchQuery, ConnectorSearchResult
from app.connectors.series_metadata import ConnectorSeriesMetadata
from app.domain.source import SourceHealthStatus
from app.errors import ErrorCode
from app.platform.windows_process_job import WindowsChildJob

FMD2_API_ORIGIN = "http://127.0.0.1:18873"
FMD2_COVER_ORIGIN = "https://swebtoon-phinf.pstatic.net"
FMD2_SOURCE_ID = "18f636ec7fdf47fabe95d940ad0b548f"
FMD2_PUBLIC_SOURCES = {
    FMD2_SOURCE_ID: ("WebToons", "https://www.webtoons.com", "en"),
    "97dcd409f781467eb2da4a8634e5a1d9": (
        "CapibaraTraductor",
        "https://capibaratraductor.com",
        "es",
    ),
    "9a0a1298ca044dd9886a3446092f285e": ("Cartel De Manhwas", "https://carteldemanhwas.net", "es"),
    "f31e7a36050c424291c839f0a02d92a4": ("Apoll Comics", "https://apollcomics.es", "es"),
    "7103ae6839ea46ec80cdfc2c4b37c803": ("Asura Scans", "https://asurascans.com", "en"),
    "9185eb6c49324a849c7d7925a41ef3a3": ("TuMangaOnline", "https://zonatmo.org", "es"),
    "424747b0135c4240a902eb9cfcb11db9": ("NovelCoolES", "https://es.novelcool.com", "es"),
    "03ae42ac9141443fa0711bd401c0203d": ("InManga", "https://inmanga.com", "es"),
    "K042a85566244b7e836679491ce67ot0": ("ManhwaWeb", "https://www.manhwaweb.com", "es"),
}
FMD2_SOURCE_COVER_ORIGINS: dict[str, frozenset[str]] = {
    FMD2_SOURCE_ID: frozenset({FMD2_COVER_ORIGIN}),
    "97dcd409f781467eb2da4a8634e5a1d9": frozenset(
        {"https://capibaratraductor.com", "https://r2.capibaratraductor.com"}
    ),
    "9a0a1298ca044dd9886a3446092f285e": frozenset({"https://carteldemanhwas.net"}),
    "f31e7a36050c424291c839f0a02d92a4": frozenset({"https://apollcomics.es"}),
    "7103ae6839ea46ec80cdfc2c4b37c803": frozenset(
        {"https://asurascans.com", "https://api.asurascans.com", "https://cdn.asurascans.com"}
    ),
    "9185eb6c49324a849c7d7925a41ef3a3": frozenset({"https://zonatmo.org"}),
    "424747b0135c4240a902eb9cfcb11db9": frozenset(
        {"https://es.novelcool.com", "https://img.novelcool.com"}
    ),
    "03ae42ac9141443fa0711bd401c0203d": frozenset(
        {"https://inmanga.com", "https://cdn1.intomanga.com", "https://assets.intomanga.com"}
    ),
    "K042a85566244b7e836679491ce67ot0": frozenset(
        {"https://www.manhwaweb.com", "https://img1mw.xyz", "https://img2mw.xyz"}
    ),
}
FMD2_COVER_ONLY_ORIGINS: frozenset[str] = frozenset(
    origin for origins in FMD2_SOURCE_COVER_ORIGINS.values() for origin in origins
)
FMD2_NETWORK_ORIGINS: frozenset[str] = frozenset({FMD2_API_ORIGIN}) | FMD2_COVER_ONLY_ORIGINS
FMD2_METADATA = ConnectorMetadata(
    connector_id="yomira.fmd2.webtoons",
    connector_version="1.0.0",
    capabilities=frozenset(
        {
            ConnectorCapability.SEARCH,
            ConnectorCapability.METADATA,
            ConnectorCapability.CHAPTER_LIST,
            ConnectorCapability.PAGE_LIST,
        }
    ),
)

_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_TEXT = 100_000
_MAX_CHAPTERS = 10_000
_MAX_PAGES = 10_000
_MAX_STAGE_BYTES = 512 * 1024 * 1024
_STALLED_ACQUISITION_SECONDS = 180
_FMD_REFERENCE = re.compile(r"^fmd2:v1:[0-9a-fA-F]{2,8192}$")
_CHAPTER_REFERENCE = re.compile(r"^yomira-fmd2:v[123]:[A-Za-z0-9_-]{1,16384}$")
_PAGE_REFERENCE = re.compile(
    r"^fmd2-page:v1:(?P<job>[0-9a-f-]{36}):(?P<file>[0-9]{1,6}\.(?:jpg|jpeg|png|webp))$"
)
_PAGE_FILE = re.compile(r"^(?P<number>[0-9]{1,6})\.(?:jpg|jpeg|png|webp)$", re.IGNORECASE)


def _raise(
    code: ErrorCode,
    operation: SourceConnectorOperation,
    *,
    retryable: bool = False,
) -> NoReturn:
    if code is ErrorCode.SRC_CONNECTOR_FAILED:
        raise SourceConnectorError(code=code, operation=operation, retryable=retryable)
    raise SourceConnectorError(code=code, operation=operation)


def _object(value: object, operation: SourceConnectorOperation) -> dict[str, object]:
    if not isinstance(value, dict):
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    mapping = cast(dict[object, object], value)
    if len(mapping) > 128 or any(type(key) is not str for key in mapping):
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    return cast(dict[str, object], mapping)


def _text(value: object, operation: SourceConnectorOperation, *, limit: int = _MAX_TEXT) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > limit
        or "\x00" in value
        or any(ord(character) < 0x20 and character not in "\t\n\r" for character in value)
    ):
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    return value.strip()


def _reference(value: object, operation: SourceConnectorOperation) -> str:
    if type(value) is not str or _FMD_REFERENCE.fullmatch(value) is None:
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    return value.lower()


def _reference_parts(value: str, operation: SourceConnectorOperation) -> tuple[str, str]:
    value = _reference(value, operation)
    try:
        decoded = bytes.fromhex(value.removeprefix("fmd2:v1:")).decode("utf-8", errors="strict")
    except ValueError, UnicodeDecodeError:
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    source_id, separator, url = decoded.partition("\n")
    parsed = urlsplit(url)
    if (
        source_id not in FMD2_PUBLIC_SOURCES
        or not separator
        or not url.startswith("/")
        or url.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or "\\" in url
        or ".." in parsed.path
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in url)
    ):
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    if source_id == FMD2_SOURCE_ID and (
        not parsed.path.startswith("/en/") or parsed.path.startswith("/en/canvas/")
    ):
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    return source_id, url


def _reference_url(value: str, operation: SourceConnectorOperation) -> str:
    return _reference_parts(value, operation)[1]


def _pack_reference(url: str, source_id: str = FMD2_SOURCE_ID) -> str:
    return "fmd2:v1:" + (source_id + "\n" + url).encode("utf-8").hex()


def _remote_url(value: object, source_id: str, operation: SourceConnectorOperation) -> str:
    text = _text(value, operation, limit=4_096)
    parsed = urlsplit(text)
    expected_origin = FMD2_PUBLIC_SOURCES[source_id][1]
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != urlsplit(expected_origin).netloc.casefold()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    relative = parsed.path + ("?" + parsed.query if parsed.query else "")
    _reference_url(_pack_reference(relative, source_id), operation)
    return text


def _pack_chapter(
    manga_ref: str,
    chapter_ref: str,
    ordinal: int,
    operation: SourceConnectorOperation,
) -> str:
    manga_source, manga_url = _reference_parts(manga_ref, operation)
    chapter_source, chapter_url = _reference_parts(chapter_ref, operation)
    if manga_source != chapter_source:
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    body = json.dumps(
        [manga_source, manga_url, chapter_url],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    token = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    result = f"yomira-fmd2:v3:{token}"
    if len(result) > 512:
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    return result


def _unpack_chapter(value: str, operation: SourceConnectorOperation) -> tuple[str, str, int | None]:
    if type(value) is not str or _CHAPTER_REFERENCE.fullmatch(value) is None:
        _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation)
    token = value.split(":", 2)[2]
    try:
        body = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        payload: object = json.loads(body.decode("utf-8", errors="strict"))
    except ValueError, UnicodeDecodeError, json.JSONDecodeError:
        _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation)
    if not isinstance(payload, list):
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    values = cast(list[object], payload)
    stable = value.startswith("yomira-fmd2:v3:")
    if stable or value.startswith("yomira-fmd2:v2:"):
        if len(values) != (3 if stable else 4):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        source_id = _text(values[0], operation, limit=32)
        values = [
            _pack_reference(_text(values[1], operation, limit=4_096), source_id),
            _pack_reference(_text(values[2], operation, limit=4_096), source_id),
            None if stable else values[3],
        ]
    if len(values) != 3:
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    manga_value = _text(values[0], operation, limit=8_200)
    chapter_value = _text(values[1], operation, limit=8_200)
    manga_ref = manga_value if manga_value.startswith("fmd2:v1:") else _pack_reference(manga_value)
    chapter_ref = (
        chapter_value if chapter_value.startswith("fmd2:v1:") else _pack_reference(chapter_value)
    )
    manga_source, _ = _reference_parts(manga_ref, operation)
    chapter_source, _ = _reference_parts(chapter_ref, operation)
    if manga_source != chapter_source:
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    ordinal = values[2]
    if not stable and (type(ordinal) is not int or not 1 <= ordinal <= _MAX_CHAPTERS):
        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
    return manga_ref, chapter_ref, cast(int | None, ordinal)


def fmd2_stable_chapter_reference(remote_reference: str) -> str:
    """Recognize saved v1/v2 identities without making catalog position part of identity."""
    operation = SourceConnectorOperation.GET_CHAPTER
    manga_ref, chapter_ref, _ = _unpack_chapter(remote_reference, operation)
    return _pack_chapter(manga_ref, chapter_ref, 1, operation)


def fmd2_series_source(remote_reference: str) -> str:
    return _reference_parts(remote_reference, SourceConnectorOperation.GET_SERIES)[0]


def fmd2_chapter_location(remote_reference: str) -> tuple[str, str, str]:
    operation = SourceConnectorOperation.GET_CHAPTER
    manga_ref, chapter_ref, _ = _unpack_chapter(remote_reference, operation)
    source, chapter_url = _reference_parts(chapter_ref, operation)
    return manga_ref, source, chapter_url


def _chapter(
    remote_reference: str, title: str, ordinal: int | None, language: str = "en"
) -> ConnectorChapter:
    return ConnectorChapter(
        remote_id=remote_reference,
        remote_url=None,
        remote_label=title,
        chapter_raw_label=title,
        chapter_major=ordinal,
        chapter_minor=None,
        chapter_variant=None,
        chapter_type=None,
        chapter_sort_key=(ordinal,) if ordinal is not None else (0,),
        volume_number=None,
        language=language,
        display_label=title,
    )


class Fmd2Connector:
    """Starts FMD2 locally and adapts its loopback API to Yomira's connector contract."""

    def __init__(
        self,
        executable: Path,
        staging_root: Path,
        *,
        api_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not executable.is_absolute():
            raise ValueError("FMD2 executable path must be absolute")
        if not staging_root.is_absolute():
            raise ValueError("FMD2 staging root must be absolute")
        if api_client is not None and type(api_client) is not httpx.AsyncClient:
            raise TypeError("api_client must be an httpx.AsyncClient or None")
        self._executable = executable
        self._staging_root = staging_root
        self._owns_process = api_client is None
        self._token = secrets.token_urlsafe(48)
        self._client = api_client or httpx.AsyncClient(
            base_url=FMD2_API_ORIGIN,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )
        self._process: subprocess.Popen[bytes] | None = None
        self._process_job: WindowsChildJob | None = None
        self._control_client = api_client or httpx.AsyncClient(
            base_url=FMD2_API_ORIGIN,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )
        self._start_lock = asyncio.Lock()
        self._chapters: OrderedDict[str, ConnectorChapter] = OrderedDict()
        self._unsettled_acquisitions: set[UUID] = set()
        parsed_api = urlsplit(str(self._client.base_url))
        if (
            parsed_api.scheme != "http"
            or parsed_api.hostname != "127.0.0.1"
            or parsed_api.port is None
            or parsed_api.username is not None
            or parsed_api.password is not None
            or parsed_api.path not in {"", "/"}
            or parsed_api.query
            or parsed_api.fragment
        ):
            raise ValueError("FMD2 API must be a credential-free IPv4 loopback origin")

    @property
    def api_origin(self) -> str:
        return str(self._client.base_url).rstrip("/")

    @property
    def metadata(self) -> ConnectorMetadata:
        return FMD2_METADATA

    async def start(self) -> None:
        async with self._start_lock:
            if self._process is not None and self._process.poll() is None:
                return
            if not self._executable.is_file():
                raise RuntimeError("Configured FMD2 executable is not available.")
            if self._process_job is not None:
                self._process_job.close()
                self._process_job = None
            environment = os.environ.copy()
            environment.update(
                {
                    "YOMIRA_FMD2_API_TOKEN": self._token,
                    "YOMIRA_FMD2_API_PORT": "18873",
                    "YOMIRA_FMD2_STAGING_ROOT": str(self._staging_root),
                }
            )
            startupinfo = None
            creationflags = 0
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                creationflags = subprocess.CREATE_NO_WINDOW
            self._process = subprocess.Popen(
                [str(self._executable)],
                cwd=self._executable.parent,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
            deadline = time.monotonic() + 20.0
            try:
                if os.name == "nt":
                    self._process_job = WindowsChildJob(self._process.pid)
                while time.monotonic() < deadline:
                    if self._process.poll() is not None:
                        raise RuntimeError("FMD2 exited before its local API became ready.")
                    try:
                        sources = await self._request_json(
                            "GET",
                            "/v1/sources",
                            SourceConnectorOperation.HEALTH_CHECK,
                            ensure_started=False,
                        )
                        self._validate_sources(sources, SourceConnectorOperation.HEALTH_CHECK)
                        return
                    except SourceConnectorError:
                        await asyncio.sleep(0.25)
                raise RuntimeError("FMD2 local API did not become ready within 20 seconds.")
            except BaseException:
                await self._stop_process()
                raise

    async def close(self) -> None:
        await self._stop_process()
        if not self._client.is_closed:
            await self._client.aclose()
        if not self._control_client.is_closed:
            await self._control_client.aclose()

    async def _stop_process(self) -> None:
        process = self._process
        try:
            if process is not None and process.poll() is None:
                with suppress(Exception):
                    await self._request_json(
                        "POST",
                        "/v1/shutdown",
                        SourceConnectorOperation.HEALTH_CHECK,
                        ensure_started=False,
                    )
                try:
                    await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=8.0)
                except TimeoutError:
                    process.terminate()
                    try:
                        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=3.0)
                    except TimeoutError:
                        process.kill()
                        await asyncio.to_thread(process.wait)
        finally:
            self._process = None
            if self._process_job is not None:
                self._process_job.close()
                self._process_job = None

    async def connection_available(self) -> bool:
        """Probe the engine without requiring every unrelated catalog to be ready."""
        try:
            await self._settle_acquisitions()
            payload = await self._request_json(
                "GET", "/v1/sources", SourceConnectorOperation.HEALTH_CHECK
            )
            self._validate_sources(payload, SourceConnectorOperation.HEALTH_CHECK)
        except SourceConnectorError:
            return False
        return True

    async def health_check(self) -> SourceHealthStatus:
        try:
            payload = await self._request_json(
                "GET", "/v1/sources", SourceConnectorOperation.HEALTH_CHECK
            )
            self._validate_sources(payload, SourceConnectorOperation.HEALTH_CHECK)
        except SourceConnectorError:
            return SourceHealthStatus.DEGRADED
        sources = cast(list[dict[str, object]], payload["data"])
        if len(sources) != len(FMD2_PUBLIC_SOURCES) or any(
            source["catalog_ready"] is False for source in sources
        ):
            return SourceHealthStatus.DEGRADED
        return SourceHealthStatus.HEALTHY

    async def search(self, query: ConnectorSearchQuery) -> tuple[ConnectorSearchResult, ...]:
        operation = SourceConnectorOperation.SEARCH
        if type(query) is not ConnectorSearchQuery or not 1 <= len(query.query.strip()) <= 200:
            _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation)
        payload = await self._request_json(
            "POST",
            "/v1/search",
            operation,
            {"source_id": "*", "query": query.query.strip(), "limit": 50},
        )
        data = payload.get("data")
        if not isinstance(data, list):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        results_data = cast(list[object], data)
        if len(results_data) > 50:
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        results: list[ConnectorSearchResult] = []
        for value in results_data:
            item = _object(value, operation)
            source_id = item.get("source_id")
            if type(source_id) is not str or source_id not in FMD2_PUBLIC_SOURCES:
                _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
            manga_ref = _reference(item.get("manga_ref"), operation)
            if _reference_parts(manga_ref, operation)[0] != source_id:
                _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
            results.append(
                ConnectorSearchResult(
                    remote_id=manga_ref,
                    remote_url=_remote_url(item.get("remote_url"), source_id, operation),
                    remote_title=_text(item.get("title"), operation, limit=2_000),
                    source_label=FMD2_PUBLIC_SOURCES[source_id][0],
                )
            )
        return tuple(results)

    async def get_series(self, remote_series_reference: str) -> object:
        operation = SourceConnectorOperation.GET_SERIES
        manga_ref = _reference(remote_series_reference, operation)
        source_id, _ = _reference_parts(manga_ref, operation)
        payload = await self._request_json("GET", f"/v1/manga/{manga_ref}", operation)
        manga = _object(payload.get("manga"), operation)
        if (
            _reference(manga.get("manga_ref"), operation) != manga_ref
            or manga.get("source_id") != source_id
        ):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        return manga

    def normalize_metadata(self, raw_metadata: object) -> ConnectorSeriesMetadata:
        operation = SourceConnectorOperation.NORMALIZE_METADATA
        manga = _object(raw_metadata, operation)
        authors = [
            value
            for value in (manga.get("authors"), manga.get("artists"))
            if type(value) is str and value.strip()
        ]
        source_id = manga.get("source_id")
        source_cover_origins = (
            FMD2_SOURCE_COVER_ORIGINS.get(source_id) if type(source_id) is str else None
        )
        cover = manga.get("cover_url")
        if source_cover_origins is None:
            cover = None
        if type(cover) is str:
            try:
                parsed_cover = urlsplit(cover)
            except ValueError:
                cover = None
            else:
                origin = f"{parsed_cover.scheme}://{parsed_cover.netloc}".lower()
                if (
                    parsed_cover.scheme != "https"
                    or parsed_cover.username is not None
                    or parsed_cover.password is not None
                    or source_cover_origins is None
                    or origin not in source_cover_origins
                    or not parsed_cover.path.startswith("/")
                ):
                    cover = None
                else:
                    # WebToons transforms are rejected by Yomira's library cover fetcher;
                    # other sources may need query parameters such as TMO's version marker.
                    cover = parsed_cover._replace(
                        query="" if source_id == FMD2_SOURCE_ID else parsed_cover.query,
                        fragment="",
                    ).geturl()
        else:
            cover = None
        genre_value = manga.get("genres")
        genres: tuple[str, ...] | None = None
        if type(genre_value) is str:
            genres = tuple(part.strip() for part in genre_value.split(",") if part.strip())
        status_value = manga.get("status")
        if type(status_value) is str:
            status_value = {
                "0": "completed",
                "1": "ongoing",
                "2": "hiatus",
                "3": "cancelled",
            }.get(status_value.strip(), status_value)
        language = (
            FMD2_PUBLIC_SOURCES[source_id][2]
            if type(source_id) is str and source_id in FMD2_PUBLIC_SOURCES
            else "en"
        )
        return ConnectorSeriesMetadata(
            title=_text(manga.get("title"), operation, limit=2_000),
            cover_url=cover,
            author=" / ".join(dict.fromkeys(authors)) or None,
            description=(
                _text(manga.get("summary"), operation, limit=100_000)
                if manga.get("summary")
                else None
            ),
            genres=genres,
            status=status_value if type(status_value) is str else None,
            language=language,
        )

    async def get_chapters(self, remote_series_reference: str) -> tuple[ConnectorChapter, ...]:
        operation = SourceConnectorOperation.GET_CHAPTERS
        manga_ref = _reference(remote_series_reference, operation)
        manga_source, _ = _reference_parts(manga_ref, operation)
        payload = await self._request_json(
            "GET",
            f"/v1/manga/{manga_ref}/chapters",
            operation,
        )
        chapters = payload.get("chapters")
        if not isinstance(chapters, list):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        chapter_values = cast(list[object], chapters)
        if len(chapter_values) > _MAX_CHAPTERS:
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        result: list[ConnectorChapter] = []
        seen: set[str] = set()
        for value in chapter_values:
            item = _object(value, operation)
            chapter_ref = _reference(item.get("chapter_ref"), operation)
            if _reference_parts(chapter_ref, operation)[0] != manga_source:
                _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
            title = _text(item.get("title"), operation, limit=256)
            ordinal = item.get("ordinal")
            if type(ordinal) is not int or not 1 <= ordinal <= _MAX_CHAPTERS:
                _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
            remote_id = _pack_chapter(manga_ref, chapter_ref, ordinal, operation)
            if remote_id in seen:
                _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
            seen.add(remote_id)
            chapter = _chapter(remote_id, title, ordinal, FMD2_PUBLIC_SOURCES[manga_source][2])
            result.append(chapter)
            self._chapters[remote_id] = chapter
            self._chapters.move_to_end(remote_id)
            if len(self._chapters) > _MAX_CHAPTERS:
                self._chapters.popitem(last=False)
        return tuple(result)

    async def get_chapter(self, remote_chapter_reference: str) -> ConnectorChapter:
        operation = SourceConnectorOperation.GET_CHAPTER
        manga_ref, _chapter_ref, ordinal = _unpack_chapter(remote_chapter_reference, operation)
        cached = self._chapters.get(fmd2_stable_chapter_reference(remote_chapter_reference))
        if cached is not None:
            return replace(cached, remote_id=remote_chapter_reference)
        source_id = _reference_parts(manga_ref, operation)[0]
        return _chapter(
            remote_chapter_reference,
            f"Chapter {ordinal}" if ordinal is not None else "Capítulo",
            ordinal,
            FMD2_PUBLIC_SOURCES[source_id][2],
        )

    async def get_pages(self, remote_chapter_reference: str) -> tuple[ConnectorPageReference, ...]:
        _manga_ref, _chapter_ref, ordinal = _unpack_chapter(
            remote_chapter_reference,
            SourceConnectorOperation.GET_PAGES,
        )
        return await self.acquire_pages(
            uuid4(),
            remote_chapter_reference,
            f"Chapter {ordinal}" if ordinal is not None else "Capítulo",
        )

    async def acquire_pages(
        self,
        job_id: UUID,
        remote_chapter_reference: str,
        chapter_title: str,
    ) -> tuple[ConnectorPageReference, ...]:
        operation = SourceConnectorOperation.GET_PAGES
        if type(job_id) is not UUID or job_id.version != 4:
            _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation)
        manga_ref, chapter_ref, _ordinal = _unpack_chapter(
            remote_chapter_reference,
            operation,
        )
        title = _text(chapter_title, operation, limit=256)
        job_text = str(job_id)
        await self._settle_acquisitions()
        created_job = False
        try:
            created = await self._request_json(
                "POST",
                "/v1/downloads",
                operation,
                {
                    "request_id": job_text,
                    "manga_ref": manga_ref,
                    "chapter_ref": chapter_ref,
                    "chapter_title": title,
                },
                expected_statuses=(200, 202),
            )
            if created.get("job_id") != job_text:
                _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
            created_job = True
            self._unsettled_acquisitions.add(job_id)
            last_progress: object = None
            deadline = time.monotonic() + _STALLED_ACQUISITION_SECONDS
            while True:
                status = await self._request_json("GET", f"/v1/downloads/{job_text}", operation)
                state = status.get("status")
                if state == "completed":
                    self._unsettled_acquisitions.discard(job_id)
                    return self._completed_pages(status, job_id, operation)
                if state == "failed":
                    self._unsettled_acquisitions.discard(job_id)
                    _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation, retryable=True)
                if state == "cancelled":
                    self._unsettled_acquisitions.discard(job_id)
                    _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation)
                if state not in {"queued", "running"}:
                    _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
                progress = (status.get("completed_pages"), status.get("progress"))
                if progress != last_progress:
                    last_progress = progress
                    deadline = time.monotonic() + _STALLED_ACQUISITION_SECONDS
                elif time.monotonic() >= deadline:
                    _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation, retryable=True)
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(self._settle_acquisitions())
            raise
        except SourceConnectorError:
            if created_job:
                with suppress(SourceConnectorError):
                    await self._settle_acquisitions()
            raise

    async def _settle_acquisitions(self) -> None:
        """Do not admit another chapter while an abandoned native attempt may still run."""
        operation = SourceConnectorOperation.GET_PAGES
        for pending in tuple(self._unsettled_acquisitions):
            try:
                status = await self._request_json("GET", f"/v1/downloads/{pending}", operation)
            except SourceConnectorError:
                await self.cancel_download(pending)
                raise
            if status.get("status") not in {"completed", "failed", "cancelled"}:
                await self.cancel_download(pending)
                status = await self._request_json("GET", f"/v1/downloads/{pending}", operation)
                if status.get("status") not in {"completed", "failed", "cancelled"}:
                    _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation, retryable=True)
            self._unsettled_acquisitions.discard(pending)

    async def cancel_download(self, job_id: UUID) -> None:
        operation = SourceConnectorOperation.GET_PAGES
        if type(job_id) is not UUID or job_id.version != 4:
            _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation)
        await self._request_json(
            "DELETE", f"/v1/downloads/{job_id}", operation, expected_statuses=(200,)
        )

    def _stage_files(self, job_id: UUID) -> tuple[Path, ...]:
        if type(job_id) is not UUID or job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        root = self._staging_root.absolute()
        directory = root / str(job_id)
        if directory.resolve() != directory:
            raise ValueError("External staging must not traverse links")
        if not directory.exists():
            return ()
        files = tuple(directory.iterdir())
        if len(files) > _MAX_PAGES:
            raise ValueError("External staging has too many files")
        for path in (root, directory, *files):
            information = path.lstat()
            if (
                stat.S_ISLNK(information.st_mode)
                or getattr(information, "st_file_attributes", 0) & 0x0400
            ):
                raise ValueError("External staging contains a link or reparse point")
            if path not in (root, directory) and (
                not stat.S_ISREG(information.st_mode) or _PAGE_FILE.fullmatch(path.name) is None
            ):
                raise ValueError("External staging contains an unexpected entry")
        return files

    def existing_stage_bytes(self, job_id: UUID) -> int:
        total = sum(path.stat().st_size for path in self._stage_files(job_id))
        if total > _MAX_STAGE_BYTES:
            raise ValueError("External staging exceeds the acquisition budget")
        return total

    async def finalize_download(self, job_id: UUID) -> None:
        """Release raw pages only after the core has committed a verified CBZ."""
        status = await self._request_json(
            "GET", f"/v1/downloads/{job_id}", SourceConnectorOperation.GET_PAGES
        )
        if status.get("job_id") != str(job_id) or status.get("status") != "completed":
            raise ValueError("Only a completed provider job can release its staging")

        def release() -> None:
            files = self._stage_files(job_id)
            for path in files:
                path.unlink()
            directory = self._staging_root / str(job_id)
            if directory.exists():
                directory.rmdir()

        await asyncio.to_thread(release)

    def resolve_page(self, page_reference: ConnectorPageReference) -> PageFetchRequest:
        operation = SourceConnectorOperation.RESOLVE_PAGE
        if type(page_reference) is not ConnectorPageReference:
            _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation)
        match = _PAGE_REFERENCE.fullmatch(page_reference.remote_reference)
        if match is None:
            _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation)
        try:
            job_id = UUID(match.group("job"))
        except ValueError:
            _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation)
        if job_id.version != 4:
            _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation)
        file_name = match.group("file")
        return PageFetchRequest(
            url=f"{self.api_origin}/v1/downloads/{job_id}/files/{quote(file_name, safe='')}"
        )

    def _completed_pages(
        self,
        status: dict[str, object],
        job_id: UUID,
        operation: SourceConnectorOperation,
    ) -> tuple[ConnectorPageReference, ...]:
        files = status.get("files")
        completed = status.get("completed_pages")
        total = status.get("total_pages")
        if not isinstance(files, list):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        file_values = cast(list[object], files)
        if (
            not 1 <= len(file_values) <= _MAX_PAGES
            or type(completed) is not int
            or type(total) is not int
            or completed != total
            or total != len(file_values)
        ):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        numbered: list[tuple[int, str]] = []
        seen_numbers: set[int] = set()
        for name in file_values:
            if type(name) is not str:
                _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
            match = _PAGE_FILE.fullmatch(name)
            if match is None:
                _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
            number = int(match.group("number"))
            if number in seen_numbers:
                _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
            seen_numbers.add(number)
            numbered.append((number, name))
        numbered.sort()
        if [number for number, _name in numbered] != list(range(1, len(numbered) + 1)):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        return tuple(
            ConnectorPageReference(f"fmd2-page:v1:{job_id}:{name}") for _number, name in numbered
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        operation: SourceConnectorOperation,
        payload: object | None = None,
        *,
        expected_statuses: tuple[int, ...] = (200,),
        ensure_started: bool = True,
    ) -> dict[str, object]:
        if ensure_started and self._owns_process:
            if self._client.is_closed:
                _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation, retryable=True)
            if self._process is None or self._process.poll() is not None:
                try:
                    await self.start()
                except OSError, RuntimeError:
                    _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation, retryable=True)
        try:
            client = (
                self._control_client
                if path.startswith("/v1/downloads") or path in {"/v1/sources", "/v1/shutdown"}
                else self._client
            )
            async with client.stream(method, path, json=payload) as response:
                if response.status_code not in expected_statuses:
                    _raise(
                        ErrorCode.SRC_CONNECTOR_FAILED,
                        operation,
                        retryable=response.status_code == 429 or response.status_code >= 500,
                    )
                media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if media_type != "application/json":
                    _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
                length = response.headers.get("content-length")
                if length is not None and (not length.isdigit() or int(length) > _MAX_JSON_BYTES):
                    _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_JSON_BYTES:
                        _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        except httpx.RequestError:
            _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation, retryable=True)
        try:
            value: object = json.loads(bytes(body).decode("utf-8", errors="strict"))
        except UnicodeDecodeError, ValueError, RecursionError:
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        return _object(value, operation)

    @staticmethod
    def _validate_sources(
        payload: dict[str, object],
        operation: SourceConnectorOperation,
    ) -> None:
        data = payload.get("data")
        if (
            payload.get("api_version") != "v1"
            or payload.get("source_policy") != "public-allowlist-v1"
            or not isinstance(data, list)
        ):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        sources_data = cast(list[object], data)
        if len(sources_data) > len(FMD2_PUBLIC_SOURCES):
            _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
        seen: set[str] = set()
        for value in sources_data:
            item = _object(value, operation)
            source_id = item.get("source_id")
            if type(source_id) is not str or source_id not in FMD2_PUBLIC_SOURCES:
                _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
            name, origin, _language = FMD2_PUBLIC_SOURCES[source_id]
            base_url = item.get("base_url")
            if (
                source_id in seen
                or item.get("name") != name
                or type(base_url) is not str
                or base_url.rstrip("/") != origin
                or item.get("access") != "public"
                or type(item.get("catalog_ready")) is not bool
            ):
                _raise(ErrorCode.SRC_SCHEMA_CHANGED, operation)
            seen.add(source_id)
        if FMD2_SOURCE_ID not in seen:
            _raise(ErrorCode.SRC_CONNECTOR_FAILED, operation, retryable=True)
