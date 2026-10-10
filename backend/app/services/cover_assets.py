"""Core-owned acquisition and offline thumbnails of public source covers."""

import asyncio
import hashlib
import stat
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from PIL import Image, ImageOps
from sqlalchemy import select

from app.connectors.errors import SourceConnectorError, SourceConnectorOperation
from app.connectors.http_client import MAX_CONNECTOR_HTML_BYTES, ConnectorHttpSession
from app.connectors.mangadex import MANGADEX_METADATA, MANGADEX_USER_AGENT, MangaDexLookup
from app.connectors.metadata import ConnectorCapability
from app.connectors.registry import ConnectorRegistry
from app.domain.asset import Asset, AssetType
from app.domain.metadata import MetadataAuthority, MetadataField, resolve_series_metadata
from app.errors import ErrorCode
from app.fmd2_bridge import FMD2_METADATA
from app.persistence.atomic_file_publisher import AtomicFilePublisher
from app.persistence.database import SQLiteDatabase
from app.persistence.disk_budget import DiskBudgetManager, DiskBudgetUnavailableError
from app.persistence.filesystem_retry import RetryingFileStream
from app.persistence.managed_directories import ensure_managed_directory
from app.persistence.mappers.asset import asset_from_row, asset_to_row
from app.persistence.models.asset import AssetRow
from app.persistence.models.source import SourceRow
from app.persistence.repositories.metadata import MetadataRepository
from app.persistence.repositories.series import SeriesRepository
from app.services.external_metadata import ExternalMetadataService
from app.services.image_validation import ImageValidationPolicy, validate_image_file
from app.services.metadata import MetadataService
from app.services.reader_asset import ReaderAssetService
from app.services.thumbnails import Thumbnail, ThumbnailService, ThumbnailVariant
from app.tracing import create_trace_id, current_trace_id

MAX_COVER_BYTES = MAX_CONNECTOR_HTML_BYTES
MAX_PRESERVED_COVER_ATTEMPTS = 3


class CoverAssetError(RuntimeError):
    """A core failure with an existing error code and no remote URL in its message."""

    def __init__(self, code: ErrorCode, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__("No se pudo completar la operación de portada.")


async def _completed_io[T](operation: Callable[[], T]) -> T:
    """Cancellation must not release admission while its thread still mutates disk."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class CoverAssetService:
    def __init__(
        self,
        *,
        database: SQLiteDatabase,
        registry: ConnectorRegistry,
        http_session: ConnectorHttpSession,
        reader: ReaderAssetService,
        disk_budget: DiskBudgetManager,
        publisher: AtomicFilePublisher,
        admission_allowed: Callable[[], bool],
    ) -> None:
        self._database = database
        self._registry = registry
        self._http_session = http_session
        self._reader = reader
        self._disk_budget = disk_budget
        self._publisher = publisher
        self._admission_allowed = admission_allowed
        self._metadata = MetadataService(database)
        self._external = ExternalMetadataService(database, registry)
        self._series = SeriesRepository()
        self._repository = MetadataRepository()
        self._thumbnails = ThumbnailService()
        # ponytail: one cover import at a time; split admission only if latency demands it.
        self._admission = asyncio.Semaphore(1)
        self._decode_lock = threading.Lock()
        self._pending_reservation: UUID | None = None
        self._policy = ImageValidationPolicy(
            max_bytes=MAX_COVER_BYTES,
            max_width=40_000,
            max_height=40_000,
            max_pixels=100_000_000,
        )

    def _require_admission(self) -> None:
        if not self._admission_allowed():
            raise CoverAssetError(
                code=ErrorCode.SYS_RESOURCE_MONITOR_UNAVAILABLE,
                retryable=True,
            )

    async def refresh(self, series_id: UUID) -> None:
        """Refresh associated source fichas, preserving every local value and override."""
        async with self._admission:
            self._require_admission()
            associations = await _completed_io(lambda: self._metadata.list_sources(series_id))
            with self._database.read_session() as session:
                connector_ids = {
                    UUID(row.source_id): row.connector_id
                    for row in session.scalars(
                        select(SourceRow).where(
                            SourceRow.source_id.in_([str(s.source_id) for s in associations]),
                            SourceRow.enabled.is_(True),
                        )
                    )
                }
            for source in associations:
                self._require_admission()
                connector_id = connector_ids.get(source.source_id)
                if (
                    source.remote_id is None
                    or connector_id is None
                    or connector_id not in self._registry.connector_ids
                ):
                    continue
                connector = self._registry.resolve(
                    connector_id, required_capability=ConnectorCapability.METADATA
                )
                raw = await connector.get_series(source.remote_id)
                metadata = connector.normalize_metadata(raw)
                await _completed_io(
                    lambda source=source, metadata=metadata: self._metadata.refresh_source(
                        source.series_source_id, metadata
                    )
                )
            await self._ensure_cover(series_id)

    async def ensure_cover(self, series_id: UUID) -> bool:
        async with self._admission:
            return await self._ensure_cover(series_id)

    async def catalog_cover(self, title: str) -> str | None:
        async with self._admission:
            return await self._external.cover_for_title(title)

    async def external_candidates(self, series_id: UUID) -> MangaDexLookup:
        async with self._admission:
            self._require_admission()
            return await self._external.find_candidates(series_id)

    async def apply_external(self, series_id: UUID, remote_id: str) -> None:
        async with self._admission:
            self._require_admission()
            await self._external.apply(series_id, remote_id)
            await self._ensure_cover(series_id)

    def _candidate(self, series_id: UUID, *, catalog_only: bool = False) -> tuple[str, str] | None:
        with self._database.read_session() as session:
            series = self._series.get(session, series_id)
            if series is None:
                return None
            sources = self._repository.list_series_sources(session, series_id)
            overrides = self._repository.list_overrides(session, series_id)
            cover = next(
                r
                for r in resolve_series_metadata(series, sources, overrides)
                if r.field is MetadataField.COVER
            )
            if cover.authority is not MetadataAuthority.SOURCE_METADATA or not isinstance(
                cover.value, str
            ):
                return None
            for source in sources:
                if catalog_only:
                    if (
                        source.source_connector_id != MANGADEX_METADATA.connector_id
                        or source.remote_cover_url is None
                    ):
                        continue
                elif (
                    source.source_id not in cover.effective_source_ids
                    or source.remote_cover_url != cover.value
                ):
                    continue
                row = session.get(SourceRow, str(source.source_id))
                if (
                    row is not None
                    and row.enabled
                    and row.connector_id in self._registry.connector_ids
                ):
                    return row.connector_id, source.remote_cover_url or cover.value
        return None

    async def _ensure_cover(self, series_id: UUID) -> bool:
        self._require_admission()
        await self._external.ensure_missing(series_id)
        candidate = await _completed_io(lambda: self._candidate(series_id))
        if candidate is None:
            await self._external.ensure_source_missing(series_id)
            candidate = await _completed_io(lambda: self._candidate(series_id))
        if candidate is None:
            return False
        self._require_admission()
        await _completed_io(self._release_pending)
        reservation_id = uuid4()
        self._pending_reservation = reservation_id
        connector_id, url = candidate
        try:
            await _completed_io(
                lambda: self._disk_budget.reserve_bounded_operation(
                    reservation_id, page_bytes=MAX_COVER_BYTES
                )
            )
            client = self._http_session.scoped_client(
                self._registry.get_network_origins(connector_id)
            )
            try:
                response = (
                    await client.get_image(url + ".512.jpg", user_agent=MANGADEX_USER_AGENT)
                    if connector_id == MANGADEX_METADATA.connector_id
                    else await client.get_image(
                        url,
                        referer=(
                            "https://www.manhwaweb.com/"
                            if connector_id == FMD2_METADATA.connector_id
                            and urlsplit(url).hostname in {"img1mw.xyz", "img2mw.xyz"}
                            else None
                        ),
                    )
                )
            except SourceConnectorError:
                if connector_id == MANGADEX_METADATA.connector_id:
                    raise
                fallback = await _completed_io(
                    lambda: self._candidate(series_id, catalog_only=True)
                )
                if fallback is None:
                    raise
                connector_id, url = fallback
                client = self._http_session.scoped_client(
                    self._registry.get_network_origins(connector_id)
                )
                response = await client.get_image(url + ".512.jpg", user_agent=MANGADEX_USER_AGENT)
            if response.status_code != 200:
                raise SourceConnectorError(
                    code=ErrorCode.SRC_CONNECTOR_FAILED,
                    operation=SourceConnectorOperation.GET_SERIES,
                    retryable=response.status_code in {429, 500, 502, 503, 504},
                )
            self._require_admission()
            return await _completed_io(
                lambda: self._persist(
                    series_id, url, response.body, response.content_type or "", reservation_id
                )
            )
        except DiskBudgetUnavailableError:
            raise CoverAssetError(ErrorCode.SYS_DISK_BUDGET_EXCEEDED, retryable=True) from None
        finally:
            await _completed_io(self._try_release_pending)

    def _try_release_pending(self) -> None:
        if self._pending_reservation is not None:
            try:
                self._disk_budget.release(self._pending_reservation, quiescent=True)
            except RuntimeError:
                return  # Retain the ID and reservation until all shared mutations quiesce.
            self._pending_reservation = None

    def _release_pending(self) -> None:
        self._try_release_pending()
        if self._pending_reservation is not None:
            raise CoverAssetError(
                code=ErrorCode.SYS_DISK_BUDGET_EXCEEDED,
                retryable=True,
            )

    def _check_parents(self, path: Path) -> None:
        identity = self._database.data_root_identity
        identity.assert_current()
        root = identity.canonical_path
        relative = path.relative_to(root)
        current = root
        for component in relative.parts[:-1]:
            current /= component
            information = current.lstat()
            if (
                not stat.S_ISDIR(information.st_mode)
                or stat.S_ISLNK(information.st_mode)
                or getattr(information, "st_file_attributes", 0) & 0x0400
            ):
                raise OSError("Cover parent is not a regular managed directory")
        identity.assert_current()

    def _persist(
        self, series_id: UUID, url: str, body: bytes, mime: str, reservation_id: UUID
    ) -> bool:
        trace_id = current_trace_id() or create_trace_id()
        root = self._database.data_root_identity.canonical_path
        directory = ensure_managed_directory(
            f"temp/covers/{series_id}",
            data_root_identity=self._database.data_root_identity,
            storage_gate=self._database.storage_gate,
            component_name="cover_assets",
            trace_id=trace_id,
        )
        target_directory = ensure_managed_directory(
            "library/covers",
            data_root_identity=self._database.data_root_identity,
            storage_gate=self._database.storage_gate,
            component_name="cover_assets",
            trace_id=trace_id,
        )
        digest = hashlib.sha256(body).hexdigest()
        staged: Path | None = None
        for attempt in range(1, MAX_PRESERVED_COVER_ATTEMPTS + 1):
            path = directory / f"attempt-{attempt}.part"
            self._check_parents(path)
            if path.exists() or path.is_symlink():
                probe = Asset.new_local(
                    AssetType.COVER,
                    path.relative_to(root).as_posix(),
                    digest,
                    len(body),
                    verified_at=datetime.now(UTC),
                )
                try:
                    with self._reader.open_verified_cover(probe, max_bytes=MAX_COVER_BYTES):
                        pass
                except Exception:
                    continue
            else:
                stream = RetryingFileStream.create(
                    path,
                    component="cover_assets",
                    stage="cover_stage",
                    trace_id=trace_id,
                    storage_gate=self._database.storage_gate,
                    validate_path=lambda path=path: self._check_parents(path),
                )
                try:
                    self._disk_budget.write_reserved(
                        reservation_id,
                        category="pages",
                        requested_bytes=len(body),
                        write=lambda stream=stream: stream.write(body),
                    )
                    stream.flush()
                    stream.sync()
                finally:
                    stream.close()
            staged = path
            break
        if staged is None:
            raise ValueError("Preserved cover attempts require recovery before another import")
        self._check_parents(staged)
        with self._decode_lock:
            validated = validate_image_file(staged, mime, policy=self._policy)
        # A fixed slot also bounds unindexed originals after a DB failure; never overwrite it.
        target = target_directory / f"{series_id}.cover"
        self._check_parents(target)
        if not target.exists():
            self._publisher.publish_no_replace(staged, target)
        asset = Asset.new_local(
            AssetType.COVER,
            target.relative_to(root).as_posix(),
            validated.sha256,
            validated.size_bytes,
            verified_at=datetime.now(UTC),
        )
        with self._reader.open_verified_cover(asset, max_bytes=MAX_COVER_BYTES):
            pass
        with self._database.write_session() as session:
            current = self._series.get(session, series_id)
            if current is None:
                return False
            sources = self._repository.list_series_sources(session, series_id)
            overrides = self._repository.list_overrides(session, series_id)
            cover = next(
                r
                for r in resolve_series_metadata(current, sources, overrides)
                if r.field is MetadataField.COVER
            )
            catalog_cover = any(
                source.source_connector_id == MANGADEX_METADATA.connector_id
                and source.remote_cover_url == url
                for source in sources
            )
            if cover.authority is not MetadataAuthority.SOURCE_METADATA or (
                cover.value != url and not catalog_cover
            ):
                return False
            session.add(asset_to_row(asset))
            session.flush()

            self._series.update(
                session,
                replace(current, cover_asset_id=asset.asset_id, updated_at=datetime.now(UTC)),
            )
        return True

    async def thumbnail(self, series_id: UUID, variant: ThumbnailVariant) -> Thumbnail | None:
        return await _completed_io(lambda: self._thumbnail(series_id, variant))

    def _thumbnail(self, series_id: UUID, variant: ThumbnailVariant) -> Thumbnail | None:
        with self._decode_lock:
            with self._database.read_session() as session:
                current = self._series.get(session, series_id)
                if current is None:
                    return None
                cover = next(
                    r
                    for r in resolve_series_metadata(
                        current,
                        self._repository.list_series_sources(session, series_id),
                        self._repository.list_overrides(session, series_id),
                    )
                    if r.field is MetadataField.COVER
                )
                if not isinstance(cover.value, UUID):
                    return None
                row = session.get(AssetRow, str(cover.value))
                if row is None:
                    return None
                asset = asset_from_row(row)
            with self._reader.open_verified_cover(asset, max_bytes=MAX_COVER_BYTES) as stream:
                cached = self._thumbnails.get_cached(asset.sha256, variant)
                if cached is not None:
                    return cached
                self._require_admission()
                with Image.open(stream) as original:
                    width, height = original.size
                    if (
                        width > self._policy.max_width
                        or height > self._policy.max_height
                        or width * height > self._policy.max_pixels
                    ):
                        raise ValueError("Cover exceeds decoded image bounds")
                    with ImageOps.exif_transpose(original) as oriented:
                        with oriented.convert(
                            "RGBA" if "A" in oriented.getbands() else "RGB"
                        ) as image:
                            return self._thumbnails.generate(image, asset.sha256, variant)

    async def close(self) -> None:
        async with self._admission:
            await _completed_io(self._release_pending)
