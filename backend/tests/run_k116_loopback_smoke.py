"""Reproducible, loopback-only K116 pipeline smoke with preserved evidence.

Run with the project Python from ``backend`` or the repository root. The run
creates a unique artifact directory below ``backend/test-artifacts`` and never
deletes it. This is a test harness, not production code.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import random
import string
import sys
import threading
import time
import zipfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from sqlalchemy import event

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
TESTS_ROOT = BACKEND_ROOT / "tests"
K116_PROFILE_PAGE_COUNT = 100
K116_HARNESS_WATCHDOG_SECONDS = 600
K116_FIRST_PAGE_CONCURRENCY_PROBE = "/api/v1/pages/mock-chapter-1/page-1"
K116_TEMPORARY_INVENTORY_SAMPLE_INTERVAL_SECONDS = 5.0
for import_root in (str(BACKEND_ROOT), str(TESTS_ROOT)):
    if import_root not in sys.path:
        sys.path.insert(0, import_root)

import httpx  # noqa: E402
import mock_manga_server as mock_fixture  # noqa: E402
from mock_manga_server import (  # noqa: E402
    MockMangaServer,
    MockScenario,
    _MockRequestHandler,  # pyright: ignore[reportPrivateUsage]
    _png_chunk,  # pyright: ignore[reportPrivateUsage]
    _small_png,  # pyright: ignore[reportPrivateUsage]
)
from support.sqlite import open_test_database  # noqa: E402

from app.connectors.metadata import ConnectorCapability, ConnectorMetadata  # noqa: E402
from app.connectors.mock import MockConnector  # noqa: E402
from app.connectors.pages import ConnectorPageReference, PageFetchRequest  # noqa: E402
from app.connectors.registry import ConnectorRegistration, ConnectorRegistry  # noqa: E402
from app.connectors.sandbox import ConnectorHostContext  # noqa: E402
from app.domain.download_job import DownloadJobStatus  # noqa: E402
from app.domain.job_pause import JobPauseController  # noqa: E402
from app.persistence.atomic_file_publisher import (  # noqa: E402
    ADR_0087_RETRY_POLICY,
    AtomicFilePublisher,
)
from app.persistence.migrations.runner import MigrationRunner  # noqa: E402
from app.persistence.models.asset import AssetRow  # noqa: E402
from app.persistence.models.chapter_cbz_asset import ChapterCbzAssetRow  # noqa: E402
from app.persistence.models.download_page import DownloadPageRow  # noqa: E402
from app.persistence.repositories.download_job import DownloadJobRepository  # noqa: E402
from app.secrets.vault import WindowsDpapiSecretVault  # noqa: E402
from app.services.cbz_builder import CBZBuilder  # noqa: E402
from app.services.download_dispatcher import (  # noqa: E402
    MAX_ACTIVE_JOBS,
    MAX_ACTIVE_JOBS_PER_SOURCE,
    DownloadDispatcher,
)
from app.services.download_transport import (  # noqa: E402
    DownloadTransport,
    DownloadTransportPolicy,
    NetworkOrigin,
)
from app.services.download_worker import DownloadWorker  # noqa: E402
from app.services.image_validation import ImageValidationPolicy  # noqa: E402
from app.services.job_recovery import RecoveryCoordinator  # noqa: E402
from app.services.jobs import DownloadJobService, DownloadJobSummary  # noqa: E402
from app.services.reader_asset import (  # noqa: E402
    V1_MAX_CBZ_BYTES,
    V1_MAX_PAGE_BYTES,
    V1_STREAM_CHUNK_BYTES,
    ReaderAssetLimits,
    ReaderAssetService,
)


class _ProcessMemoryCountersEx(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class _WindowsProcessSampler:
    """Sample current/peak working set and handle count using Win32 APIs."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("K116 smoke requires Windows for the target resource APIs")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self._kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        self._kernel32.GetProcessHandleCount.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        )
        self._kernel32.GetProcessHandleCount.restype = ctypes.c_int
        self._psapi.GetProcessMemoryInfo.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessMemoryCountersEx),
            ctypes.c_ulong,
        )
        self._psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        self._process = self._kernel32.GetCurrentProcess()

    def sample(self) -> dict[str, int]:
        counters = _ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        if not self._psapi.GetProcessMemoryInfo(
            self._process,
            ctypes.byref(counters),
            counters.cb,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        handles = ctypes.c_ulong()
        if not self._kernel32.GetProcessHandleCount(
            self._process,
            ctypes.byref(handles),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return {
            "working_set_bytes": int(counters.WorkingSetSize),
            "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
            "private_bytes": int(counters.PrivateUsage),
            "handle_count": int(handles.value),
        }


class _MetricSampler:
    def __init__(self, data_root: Path, *, interval_seconds: float = 0.25) -> None:
        self._data_root = data_root
        self._interval_seconds = interval_seconds
        self._process = _WindowsProcessSampler()
        self._stop = threading.Event()
        self._record_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name="yomira-k116-resource-sampler",
            daemon=False,
        )
        self.baseline: dict[str, int] | None = None
        self.peak: dict[str, int] = {}
        self._last_temporary_inventory_at: float | None = None
        self._last_temporary_inventory = (0, 0)
        self.peak_temporary_bytes = 0
        self.peak_temporary_file_count = 0
        self.samples = 0
        self.errors: list[str] = []
        self.record_wall_seconds = 0.0
        self.record_wall_peak_seconds = 0.0
        self.process_cpu_baseline_seconds: float | None = None
        self.process_cpu_final_seconds: float | None = None

    def start(self) -> None:
        self.process_cpu_baseline_seconds = time.process_time()
        self._record()
        self._thread.start()

    def stop(self) -> None:
        self._record()
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("K116 metric sampler failed to stop")
        self._record()
        self.process_cpu_final_seconds = time.process_time()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self._record()

    def _record(self) -> None:
        with self._record_lock:
            started = time.perf_counter()
            try:
                sample = self._process.sample()
                if (
                    self._last_temporary_inventory_at is None
                    or started - self._last_temporary_inventory_at
                    >= K116_TEMPORARY_INVENTORY_SAMPLE_INTERVAL_SECONDS
                ):
                    self._last_temporary_inventory = _temporary_inventory(
                        self._data_root / "temp"
                    )
                    self._last_temporary_inventory_at = started
                temporary_bytes, file_count = self._last_temporary_inventory
            except Exception as error:
                self.errors.append(type(error).__name__)
                return
            finally:
                elapsed = time.perf_counter() - started
                self.record_wall_seconds += elapsed
                self.record_wall_peak_seconds = max(
                    self.record_wall_peak_seconds,
                    elapsed,
                )
            if self.baseline is None:
                self.baseline = sample
            self.samples += 1
            for key, value in sample.items():
                self.peak[key] = max(self.peak.get(key, value), value)
            self.peak_temporary_bytes = max(self.peak_temporary_bytes, temporary_bytes)
            self.peak_temporary_file_count = max(
                self.peak_temporary_file_count,
                file_count,
            )


class _PhaseMetrics:
    """Test-only inclusive phase timers for HTTP, SQL, and CBZ work."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._engine: Any | None = None
        self.transport_calls = 0
        self.transport_success_bytes = 0
        self.transport_wall_seconds = 0.0
        self.transport_failure_count = 0
        self.cbz_builds = 0
        self.cbz_wall_seconds = 0.0
        self.sql_statement_count = 0
        self.sql_execution_wall_seconds = 0.0

    def instrument_transport(self, transport: DownloadTransport) -> None:
        original_download = transport.download

        async def measured_download(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            result: Any | None = None
            try:
                result = await original_download(*args, **kwargs)
                return result
            finally:
                elapsed = time.perf_counter() - started
                with self._lock:
                    self.transport_calls += 1
                    self.transport_wall_seconds += elapsed
                    if result is None:
                        self.transport_failure_count += 1
                    else:
                        self.transport_success_bytes += int(result.size_bytes)

        transport.download = measured_download  # type: ignore[method-assign]

    def instrument_cbz_builder(self, builder: CBZBuilder) -> None:
        original_build = builder.build

        def measured_build(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return original_build(*args, **kwargs)
            finally:
                with self._lock:
                    self.cbz_builds += 1
                    self.cbz_wall_seconds += time.perf_counter() - started

        builder.build = measured_build  # type: ignore[method-assign]

    def attach_database(self, engine: Any) -> None:
        self._engine = engine

        def before_execute(
            _connection: Any,
            _cursor: Any,
            _statement: str,
            _parameters: Any,
            context: Any,
            _executemany: bool,
        ) -> None:
            context._k116_sql_started = time.perf_counter()

        def after_execute(
            _connection: Any,
            _cursor: Any,
            _statement: str,
            _parameters: Any,
            context: Any,
            _executemany: bool,
        ) -> None:
            started = getattr(context, "_k116_sql_started", None)
            if started is None:
                return
            with self._lock:
                self.sql_statement_count += 1
                self.sql_execution_wall_seconds += time.perf_counter() - started

        self._before_sql = before_execute
        self._after_sql = after_execute
        event.listen(engine, "before_cursor_execute", before_execute)
        event.listen(engine, "after_cursor_execute", after_execute)

    def detach_database(self) -> None:
        if self._engine is None:
            return
        event.remove(self._engine, "before_cursor_execute", self._before_sql)
        event.remove(self._engine, "after_cursor_execute", self._after_sql)
        self._engine = None

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            return {
                "transport_calls": self.transport_calls,
                "transport_success_bytes": self.transport_success_bytes,
                "transport_wall_seconds_including_waits": self.transport_wall_seconds,
                "transport_failure_count": self.transport_failure_count,
                "cbz_builds": self.cbz_builds,
                "cbz_build_wall_seconds": self.cbz_wall_seconds,
                "sql_statement_count": self.sql_statement_count,
                "sql_execution_wall_seconds": self.sql_execution_wall_seconds,
            }


def _temporary_inventory(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    total_bytes = 0
    files = 0
    for directory, directory_names, file_names in os.walk(path, followlinks=False):
        base = Path(directory)
        directory_names[:] = [
            name for name in directory_names if not (base / name).is_symlink()
        ]
        for name in file_names:
            candidate = base / name
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if candidate.is_symlink() or not candidate.is_file():
                continue
            total_bytes += info.st_size
            files += 1
    return total_bytes, files


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(V1_STREAM_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _resolve_asset_path(data_root: Path, relative_path: str) -> Path:
    """Resolve the indexed asset path using the runtime's data-root layout."""
    return data_root / Path(*relative_path.split("/"))


class _LoopbackFixtureConnector(MockConnector):
    def __init__(self, *, connector_id: str, page_server_base_url: str) -> None:
        super().__init__(page_server_base_url=page_server_base_url)
        self._page_server_base_url = page_server_base_url.rstrip("/")
        self._fixture_metadata = ConnectorMetadata(
            connector_id=connector_id,
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

    @property
    def metadata(self) -> ConnectorMetadata:
        return self._fixture_metadata

    async def get_pages(
        self,
        remote_chapter_reference: str,
    ) -> tuple[ConnectorPageReference, ...]:
        if remote_chapter_reference != "mock-chapter-1":
            raise ValueError("unexpected fixture chapter reference")
        references = tuple(
            ConnectorPageReference(f"mock-chapter-1/page-{ordinal}")
            for ordinal in range(1, K116_PROFILE_PAGE_COUNT)
        )
        # Retain an intentional duplicate occurrence, as the base fixture does.
        return (*references, references[-1])

    def resolve_page(self, page_reference: ConnectorPageReference) -> PageFetchRequest:
        prefix = "mock-chapter-1/page-"
        if not page_reference.remote_reference.startswith(prefix):
            raise ValueError("unexpected fixture page reference")
        ordinal = page_reference.remote_reference.removeprefix(prefix)
        if not ordinal.isdecimal() or not 1 <= int(ordinal) < K116_PROFILE_PAGE_COUNT:
            raise ValueError("fixture page ordinal is outside the smoke profile")
        return PageFetchRequest(
            url=(
                f"{self._page_server_base_url}/api/v1/pages/"
                f"mock-chapter-1/page-{ordinal}"
            )
        )


def _large_valid_fixture_png() -> bytes:
    small_png = _small_png()
    text_payload_size = V1_STREAM_CHUNK_BYTES - len(small_png) - 12
    text_prefix = b"Yomira\0"
    random_source = random.Random("yomira-k116-100-page-fixture")
    text_data = "".join(
        random_source.choices(
            string.ascii_letters + string.digits,
            k=text_payload_size - len(text_prefix),
        )
    ).encode("ascii")
    text_payload = text_prefix + text_data
    payload = small_png[:-12] + _png_chunk(b"tEXt", text_payload) + small_png[-12:]
    if len(payload) != V1_STREAM_CHUNK_BYTES:
        raise AssertionError("synthetic PNG payload must be exactly one 64 KiB chunk")
    return payload


class _ExpandedFixtureResponses:
    """Expand only the in-process fixture's synthetic page list and page bytes."""

    def __init__(self) -> None:
        self._original = mock_fixture._normal_response  # pyright: ignore[reportPrivateUsage]
        self._payload = _large_valid_fixture_png()

    def __enter__(self) -> _ExpandedFixtureResponses:
        original = self._original
        payload = self._payload

        def expanded_response(
            path: str,
            query_string: str,
            *,
            page_count: int = 2,
            duplicate_pages: bool = False,
        ) -> tuple[int, str, bytes]:
            if path == "/api/v1/series/mock-series-1/chapters":
                return original(
                    path,
                    query_string,
                    page_count=K116_PROFILE_PAGE_COUNT,
                    duplicate_pages=duplicate_pages,
                )
            if path == "/api/v1/chapters/mock-chapter-1/pages":
                references = [
                    {"remote_reference": f"/api/v1/pages/mock-chapter-1/page-{ordinal}"}
                    for ordinal in range(1, K116_PROFILE_PAGE_COUNT)
                ]
                references.append(references[-1])
                return mock_fixture._json_response({"pages": references})  # pyright: ignore[reportPrivateUsage]
            page_prefix = "/api/v1/pages/mock-chapter-1/page-"
            if path.startswith(page_prefix):
                ordinal = path.removeprefix(page_prefix)
                if ordinal.isdecimal() and 1 <= int(ordinal) < K116_PROFILE_PAGE_COUNT:
                    return 200, "image/png", payload
            return original(
                path,
                query_string,
                page_count=page_count,
                duplicate_pages=duplicate_pages,
            )

        mock_fixture._normal_response = expanded_response  # type: ignore[method-assign]
        return self

    def __exit__(self, *_args: object) -> None:
        mock_fixture._normal_response = self._original  # type: ignore[method-assign]


class _FixtureHostConcurrency:
    """Observe fixture requests by Host header without changing app code."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active: dict[str, int] = defaultdict(int)
        self.peak: dict[str, int] = defaultdict(int)
        self.active_total = 0
        self.peak_total = 0
        self._original = _MockRequestHandler.do_GET

    def __enter__(self) -> _FixtureHostConcurrency:
        observer = self

        def counted_get(handler: Any) -> None:
            host_header = handler.headers.get("Host", "unknown")
            host = host_header.rsplit(":", maxsplit=1)[0].lower()
            with observer._lock:
                observer.active[host] += 1
                observer.peak[host] = max(
                    observer.peak[host],
                    observer.active[host],
                )
                observer.active_total += 1
                observer.peak_total = max(observer.peak_total, observer.active_total)
            try:
                observer._original(handler)
            finally:
                with observer._lock:
                    observer.active[host] -= 1
                    observer.active_total -= 1

        _MockRequestHandler.do_GET = counted_get  # type: ignore[method-assign]
        return self

    def __exit__(self, *_args: object) -> None:
        _MockRequestHandler.do_GET = self._original  # type: ignore[method-assign]


async def _read_job_statuses(database: Any, job_ids: tuple[Any, ...]) -> dict[str, str]:
    repository = DownloadJobRepository()
    statuses: dict[str, str] = {}
    with database.read_session() as session:
        for job_id in job_ids:
            job = repository.get(session, job_id)
            if job is not None:
                statuses[str(job_id)] = job.status.value
    return statuses


def _verify_pipeline_outputs(
    database: Any,
    data_root: Path,
    job_ids: tuple[Any, ...],
    *,
    expected_page_count: int,
) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    repository = DownloadJobRepository()
    with database.read_session() as session:
        for job_id in job_ids:
            job = repository.get(session, job_id)
            if job is None or job.status is not DownloadJobStatus.COMPLETED:
                raise AssertionError(f"fixture job {job_id} did not complete")
            rows = (
                session.query(DownloadPageRow)
                .filter_by(job_id=str(job_id))
                .order_by(DownloadPageRow.ordinal)
                .all()
            )
            link = session.query(ChapterCbzAssetRow).filter_by(
                download_job_id=str(job_id)
            ).one()
            asset = session.get(AssetRow, link.asset_id)
            if asset is None:
                raise AssertionError("completed fixture job has no indexed asset")
            if len(rows) != link.page_count or len(rows) != expected_page_count:
                raise AssertionError("mock fixture duplicate page occurrences were not retained")
            row_evidence: list[dict[str, Any]] = []
            page_hashes: list[str] = []
            for row in rows:
                if (
                    row.status != "VALIDATED"
                    or row.temporary_path is None
                    or row.sha256 is None
                    or row.size_bytes is None
                ):
                    raise AssertionError("page row lacks validated bytes/hash evidence")
                page_path = data_root / "temp" / Path(*row.temporary_path.split("/"))
                actual_hash, actual_size = _sha256_file(page_path)
                if actual_hash != row.sha256 or actual_size != row.size_bytes:
                    raise AssertionError("persisted page bytes differ from SQLite evidence")
                page_hashes.append(actual_hash)
                row_evidence.append(
                    {
                        "ordinal": row.ordinal,
                        "remote_reference": row.remote_reference,
                        "status": row.status,
                        "size_bytes": actual_size,
                        "sha256": actual_hash,
                        "temporary_path": row.temporary_path,
                    }
                )
            if rows[-2].remote_reference != rows[-1].remote_reference:
                raise AssertionError("fixture no longer contains duplicate page references")

            cbz_path = _resolve_asset_path(data_root, asset.relative_path)
            cbz_hash, cbz_size = _sha256_file(cbz_path)
            if cbz_hash != asset.sha256 or cbz_size != asset.size_bytes:
                raise AssertionError("published CBZ differs from indexed SHA-256/size")
            with zipfile.ZipFile(cbz_path, "r") as archive:
                if archive.testzip() is not None:
                    raise AssertionError("CBZ ZIP CRC validation failed")
                expected_names = ["ComicInfo.xml"] + [
                    f"{ordinal:03d}.png" for ordinal in range(1, expected_page_count + 1)
                ]
                if archive.namelist() != expected_names:
                    raise AssertionError("CBZ members/order differ from the page manifest")
                page_archive_hashes: list[str] = []
                for member in expected_names[1:]:
                    digest = hashlib.sha256()
                    with archive.open(member, "r") as page_stream:
                        while chunk := page_stream.read(V1_STREAM_CHUNK_BYTES):
                            digest.update(chunk)
                    page_archive_hashes.append(digest.hexdigest())
                if page_archive_hashes != page_hashes:
                    raise AssertionError("CBZ page bytes/hash order differs from validated pages")
                comic_info = ElementTree.fromstring(archive.read("ComicInfo.xml"))
                page_count = comic_info.findtext("PageCount")
                if page_count != str(expected_page_count):
                    raise AssertionError("ComicInfo page count differs from the retained fixture")

            evidence.append(
                {
                    "job_id": str(job_id),
                    "status": job.status.value,
                    "page_count": len(rows),
                    "duplicate_reference_preserved": True,
                    "pages": row_evidence,
                    "cbz_relative_path": asset.relative_path,
                    "cbz_size_bytes": cbz_size,
                    "cbz_sha256": cbz_hash,
                    "cbz_members": expected_names,
                    "cbz_crc_verified": True,
                }
            )
    return {"jobs": evidence, "completed_jobs": len(evidence)}


async def _run_smoke(
    run_directory: Path,
    host_observer: _FixtureHostConcurrency,
) -> dict[str, Any]:
    server = MockMangaServer(
        scenario=MockScenario.SLOW,
        concurrency_probe_path=K116_FIRST_PAGE_CONCURRENCY_PROBE,
    )
    data_root = run_directory / "data"
    database = open_test_database(data_root)
    MigrationRunner(database, data_root / "backups").upgrade_to_head()

    with _ExpandedFixtureResponses(), server:
        host, port = server.bound_address
        base_urls = (
            f"http://{host}:{port}",
            f"http://localhost:{port}",
        )
        connectors = (
            _LoopbackFixtureConnector(
                connector_id="yomira.mock.k116.a",
                page_server_base_url=base_urls[0],
            ),
            _LoopbackFixtureConnector(
                connector_id="yomira.mock.k116.b",
                page_server_base_url=base_urls[1],
            ),
        )
        registrations = tuple(
            ConnectorRegistration(
                metadata=connector.metadata,
                factory=lambda _context, selected=connector: selected,
                network_origins=frozenset({base_url}),
            )
            for connector, base_url in zip(connectors, base_urls, strict=True)
        )
        registry = ConnectorRegistry(
            context=ConnectorHostContext(secret_vault=WindowsDpapiSecretVault(data_root)),
            registrations=registrations,
        )
        service = DownloadJobService(database)
        selections: list[DownloadJobSummary] = []
        for connector in connectors:
            metadata = connector.normalize_metadata(
                await connector.get_series("mock-series-1")
            )
            chapters = await connector.get_chapters("mock-series-1")
            selections.append(
                service.create_selected_jobs(
                    connector=connector.metadata,
                    remote_series_id="mock-series-1",
                    series_metadata=metadata,
                    chapters=chapters,
                    selected_remote_ids=("mock-chapter-1",),
                )[0]
            )
        job_ids = tuple(selection.job.job_id for selection in selections)
        if len({selection.job.chapter_source_id for selection in selections}) != 2:
            raise AssertionError("fixture jobs must use distinct source admission slots")

        origins = frozenset(NetworkOrigin.from_url(value) for value in base_urls)
        http_transport = httpx.AsyncHTTPTransport(
            trust_env=False,
            retries=0,
            limits=httpx.Limits(
                max_connections=2,
                max_keepalive_connections=2,
            ),
        )
        timeout = httpx.Timeout(connect=10, read=30, write=10, pool=10)
        pause_controller = JobPauseController()
        recovery = RecoveryCoordinator(database, database.data_root_identity)
        reader = ReaderAssetService(
            database,
            data_root_identity=database.data_root_identity,
            limits=ReaderAssetLimits(
                max_cbz_bytes=V1_MAX_CBZ_BYTES,
                max_page_bytes=V1_MAX_PAGE_BYTES,
                chunk_bytes=V1_STREAM_CHUNK_BYTES,
            ),
        )
        publisher = AtomicFilePublisher(
            data_root_identity=database.data_root_identity,
            retry_policy=ADR_0087_RETRY_POLICY,
            sleep=time.sleep,
            clock=time.monotonic,
        )
        builder = CBZBuilder(
            publisher=publisher,
            max_archive_bytes=V1_MAX_CBZ_BYTES,
        )
        phase_metrics = _PhaseMetrics()
        phase_metrics.instrument_cbz_builder(builder)

        async with httpx.AsyncClient(
            transport=http_transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            downloader = DownloadTransport(
                client,
                DownloadTransportPolicy(
                    allowed_origins=origins,
                    max_bytes=V1_MAX_PAGE_BYTES,
                    chunk_size=V1_STREAM_CHUNK_BYTES,
                    connect_timeout_seconds=10,
                    read_timeout_seconds=30,
                    write_timeout_seconds=10,
                    pool_timeout_seconds=10,
                    max_redirects=5,
                    max_retry_after_seconds=60,
                    global_concurrency=2,
                    per_host_concurrency=1,
                ),
                data_root=data_root,
                data_root_identity=database.data_root_identity,
                test_transport=http_transport,
                storage_gate=database.storage_gate,
            )
            phase_metrics.instrument_transport(downloader)
            phase_metrics.attach_database(database.engine)
            worker = DownloadWorker(
                database=database,
                data_root_identity=database.data_root_identity,
                connector_registry=registry,
                recovery_coordinator=recovery,
                transport=downloader,
                image_validation_policy=ImageValidationPolicy(
                    max_bytes=V1_MAX_PAGE_BYTES,
                    max_width=40_000,
                    max_height=40_000,
                    max_pixels=100_000_000,
                ),
                cbz_builder=builder,
                file_publisher=publisher,
                reader_asset_service=reader,
                pause_controller=pause_controller,
            )
            dispatcher = DownloadDispatcher(
                database=database,
                worker=worker,
                recovery_coordinator=recovery,
                poll_interval_seconds=0.05,
                pause_controller=pause_controller,
            )
            metrics = _MetricSampler(data_root)
            metrics.start()
            started = time.perf_counter()
            await dispatcher.start()
            dispatcher.notify_new_jobs()
            deadline_seconds = K116_HARNESS_WATCHDOG_SECONDS
            deadline = time.monotonic() + deadline_seconds
            last_statuses: dict[str, str] = {}
            run_failure: BaseException | None = None
            wait_phase_seconds = 0.0
            dispatcher_stop_seconds = 0.0
            try:
                while time.monotonic() < deadline:
                    last_statuses = await _read_job_statuses(database, job_ids)
                    if len(last_statuses) == len(job_ids) and all(
                        status
                        in {
                            DownloadJobStatus.COMPLETED.value,
                            DownloadJobStatus.FAILED.value,
                            DownloadJobStatus.INTERRUPTED.value,
                            DownloadJobStatus.CANCELLED.value,
                        }
                        for status in last_statuses.values()
                    ):
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise TimeoutError(f"fixture jobs did not finish: {last_statuses}")
            except BaseException as error:
                run_failure = error
            finally:
                wait_phase_seconds = time.perf_counter() - started
                dispatcher_stop_started = time.perf_counter()
                await dispatcher.stop()
                dispatcher_stop_seconds = time.perf_counter() - dispatcher_stop_started
                metrics.stop()
                phase_metrics.detach_database()

        elapsed_seconds = wait_phase_seconds + dispatcher_stop_seconds
        try:
            statuses_after_stop = await _read_job_statuses(database, job_ids)
        except Exception as error:
            statuses_after_stop = {"snapshot_error": type(error).__name__}
        final_temporary_bytes, final_temporary_files = _temporary_inventory(
            data_root / "temp"
        )
        observed = {
            "pipeline_elapsed_seconds": elapsed_seconds,
            "status_wait_phase_seconds": wait_phase_seconds,
            "dispatcher_stop_seconds": dispatcher_stop_seconds,
            "deadline_seconds": deadline_seconds,
            "fixture_peak_concurrent_requests": server.peak_concurrency,
            "fixture_peak_requests_by_host": dict(host_observer.peak),
            "metric_sampling_interval_seconds": 0.25,
            "temporary_inventory_sample_interval_seconds": (
                K116_TEMPORARY_INVENTORY_SAMPLE_INTERVAL_SECONDS
            ),
            "metric_samples": metrics.samples,
            "metric_sampling_errors": metrics.errors,
            "metric_sampler_wall_seconds_total": metrics.record_wall_seconds,
            "metric_sampler_wall_seconds_max_per_sample": metrics.record_wall_peak_seconds,
            "metric_sampler_wall_share_of_pipeline": (
                metrics.record_wall_seconds / elapsed_seconds if elapsed_seconds else None
            ),
            "process_cpu_seconds_delta": (
                metrics.process_cpu_final_seconds - metrics.process_cpu_baseline_seconds
                if metrics.process_cpu_final_seconds is not None
                and metrics.process_cpu_baseline_seconds is not None
                else None
            ),
            "baseline_working_set_bytes": (
                metrics.baseline["working_set_bytes"] if metrics.baseline else None
            ),
            "sampled_peak_working_set_bytes": metrics.peak.get("working_set_bytes"),
            "sampled_peak_working_set_delta_bytes": (
                metrics.peak.get("working_set_bytes", 0)
                - (metrics.baseline.get("working_set_bytes", 0) if metrics.baseline else 0)
            ),
            "process_peak_working_set_bytes": metrics.peak.get("peak_working_set_bytes"),
            "sampled_peak_private_bytes": metrics.peak.get("private_bytes"),
            "sampled_peak_private_delta_bytes": (
                metrics.peak.get("private_bytes", 0)
                - (metrics.baseline.get("private_bytes", 0) if metrics.baseline else 0)
            ),
            "baseline_handle_count": (
                metrics.baseline["handle_count"] if metrics.baseline else None
            ),
            "sampled_peak_handle_count": metrics.peak.get("handle_count"),
            "sampled_peak_handle_delta": (
                metrics.peak.get("handle_count", 0)
                - (metrics.baseline.get("handle_count", 0) if metrics.baseline else 0)
            ),
            "sampled_peak_temporary_bytes": metrics.peak_temporary_bytes,
            "final_temporary_bytes": final_temporary_bytes,
            "sampled_peak_temporary_file_count": metrics.peak_temporary_file_count,
            "final_temporary_file_count": final_temporary_files,
            "phase_timings": phase_metrics.snapshot(),
            "job_statuses_at_deadline": last_statuses,
            "job_statuses_after_dispatcher_stop": statuses_after_stop,
            "failure_type": type(run_failure).__name__ if run_failure else None,
            "failure_message": str(run_failure) if run_failure else None,
        }
        (run_directory / "resource_observation.json").write_text(
            json.dumps(observed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if run_failure is not None:
            database.close()
            raise run_failure

        output_evidence = _verify_pipeline_outputs(
            database,
            data_root,
            job_ids,
            expected_page_count=K116_PROFILE_PAGE_COUNT,
        )
        database.close()
        return {
            "fixture_only": True,
            "internet_used": False,
            "server_scenario": MockScenario.SLOW.value,
            "fixture_url_hosts": ["127.0.0.1", "localhost"],
            "fixture_ephemeral_port": port,
            "job_ids": [str(job_id) for job_id in job_ids],
            "workload": {
                "jobs": len(job_ids),
                "pages_per_job": K116_PROFILE_PAGE_COUNT,
                "page_payload_bytes": V1_STREAM_CHUNK_BYTES,
                "expected_download_payload_bytes": (
                    len(job_ids) * K116_PROFILE_PAGE_COUNT * V1_STREAM_CHUNK_BYTES
                ),
                "total_page_occurrences": len(job_ids) * K116_PROFILE_PAGE_COUNT,
                "duplicate_occurrence_per_job": True,
            },
            "dispatcher_limits": {
                "global_active_jobs": MAX_ACTIVE_JOBS,
                "active_jobs_per_source": MAX_ACTIVE_JOBS_PER_SOURCE,
            },
            "transport_limits": {
                "global_downloads": 2,
                "downloads_per_host": 1,
                "max_page_bytes": V1_MAX_PAGE_BYTES,
                "stream_chunk_bytes": V1_STREAM_CHUNK_BYTES,
                "connect_timeout_seconds": 10,
                "read_timeout_seconds": 30,
                "write_timeout_seconds": 10,
                "pool_timeout_seconds": 10,
                "max_redirects": 5,
                "max_retry_after_seconds": 60,
                "retry_delays_seconds": [1, 3, 8],
                "max_manifest_pages": 10_000,
                "max_cbz_bytes": V1_MAX_CBZ_BYTES,
                "max_temporary_page_bytes_per_job": 2 * 1024 * 1024 * 1024,
            },
            "observed": observed,
            "integrity": output_evidence,
            "fixture_pipeline_result": "PASS",
            "k116_gate_result": "NO PASS",
            "k116_no_pass_reason": (
                "This is a one-run, two-job, 100-page-per-job fixture smoke. It provides "
                "measured pipeline and integrity evidence but does not establish sustained "
                "stress or memory-leak behavior required to close K116."
            ),
        }


def _new_run_directory() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    artifact_parent = Path(
        os.environ.get("YOMIRA_K116_BASETEMP", BACKEND_ROOT / "test-artifacts")
    )
    artifact_parent.mkdir(parents=True, exist_ok=True)
    run_directory = artifact_parent / f"k116-loopback-{stamp}"
    run_directory.mkdir(parents=True, exist_ok=False)
    return run_directory


def main() -> int:
    if os.name != "nt":
        raise SystemExit("This target-platform smoke requires Windows.")
    run_directory = _new_run_directory()
    report_path = run_directory / "metrics.json"
    report: dict[str, Any] = {
        "run_id": run_directory.name,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "host_platform": sys.platform,
        "python_version": sys.version,
        "artifact_directory": str(run_directory),
        "data_root": str(run_directory / "data"),
    }
    host_observer = _FixtureHostConcurrency()
    try:
        with host_observer:
            report.update(asyncio.run(_run_smoke(run_directory, host_observer)))
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        report["fixture_peak_requests_by_host"] = dict(host_observer.peak)
        report["fixture_global_peak_concurrent_requests"] = host_observer.peak_total
        if host_observer.peak_total != 2:
            report["fixture_pipeline_result"] = "FAIL"
            report["k116_gate_result"] = "NO PASS"
            raise AssertionError(
                "expected observed fixture concurrency 2 using the approved global/per-host caps"
            )
        if any(value > 1 for value in host_observer.peak.values()):
            report["fixture_pipeline_result"] = "FAIL"
            report["k116_gate_result"] = "NO PASS"
            raise AssertionError("per-host observed concurrency exceeded the approved cap of 1")
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "run_id": report["run_id"],
                    "artifact_directory": report["artifact_directory"],
                    "fixture_pipeline_result": report["fixture_pipeline_result"],
                    "k116_gate_result": report["k116_gate_result"],
                    "fixture_global_peak_concurrent_requests": (
                        report["fixture_global_peak_concurrent_requests"]
                    ),
                    "fixture_peak_requests_by_host": report[
                        "fixture_peak_requests_by_host"
                    ],
                    "observed": report.get("observed"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except BaseException as error:
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        report["fixture_pipeline_result"] = "FAIL"
        report["k116_gate_result"] = "NO PASS"
        report["failure_type"] = type(error).__name__
        report["failure_message"] = str(error)
        report["fixture_peak_requests_by_host"] = dict(host_observer.peak)
        report["fixture_global_peak_concurrent_requests"] = host_observer.peak_total
        report["k116_no_pass_reason"] = (
            "The pipeline run failed or exceeded its harness watchdog; K116 remains NO PASS."
        )
        observation_path = run_directory / "resource_observation.json"
        if observation_path.is_file():
            report["observed"] = json.loads(observation_path.read_text(encoding="utf-8"))
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "run_id": report["run_id"],
                    "artifact_directory": report["artifact_directory"],
                    "fixture_pipeline_result": report["fixture_pipeline_result"],
                    "k116_gate_result": report["k116_gate_result"],
                    "failure_type": report.get("failure_type"),
                    "failure_message": report.get("failure_message"),
                    "fixture_global_peak_concurrent_requests": (
                        report["fixture_global_peak_concurrent_requests"]
                    ),
                    "fixture_peak_requests_by_host": report[
                        "fixture_peak_requests_by_host"
                    ],
                    "observed": report.get("observed"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
