import asyncio
from pathlib import Path
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import select
from test_cover_assets import _fixture, _png  # pyright: ignore[reportPrivateUsage]

from app.domain.metadata import MetadataAuthority, MetadataField
from app.domain.series_source import SeriesSource
from app.domain.source import Source, SourceHealthStatus
from app.errors import ErrorCode
from app.persistence.database import SQLiteDatabase
from app.persistence.disk_budget import DiskBudgetManager
from app.persistence.mappers.series_source import series_source_to_row
from app.persistence.mappers.source import source_to_row
from app.persistence.models.asset import AssetRow
from app.services.cover_assets import MAX_COVER_BYTES, CoverAssetError, CoverAssetService
from app.services.library import LibraryService
from app.services.metadata import MetadataService
from app.services.thumbnails import ThumbnailVariant


def _budget(service: CoverAssetService) -> DiskBudgetManager:
    return service._disk_budget  # pyright: ignore[reportPrivateUsage]


def _pending(service: CoverAssetService) -> UUID | None:
    return service._pending_reservation  # pyright: ignore[reportPrivateUsage]


def _add_conflicting_source(database: SQLiteDatabase, series_id: UUID) -> None:
    source = Source.new_local(
        "yomira.mock", "1.0.0", "Other fixture", True, SourceHealthStatus.HEALTHY
    )
    association = SeriesSource.new_local(
        series_id,
        source.source_id,
        remote_id="other-series",
        remote_cover_url="https://source.example/other-cover.png",
    )
    with database.write_session() as session:
        session.add(source_to_row(source))
        session.flush()
        session.add(series_source_to_row(association))


@pytest.mark.parametrize("cancel_count", [1, 3])
def test_cancel_during_cover_reservation_keeps_ownership_until_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_count: int
) -> None:
    database, series, service, http, requests = _fixture(tmp_path, _png())
    budget = _budget(service)
    reserve = budget.reserve_bounded_operation
    started, finish = Event(), Event()

    def blocked_reserve(reservation_id: UUID, *, page_bytes: int, cbz_bytes: int = 0) -> None:
        reserve(reservation_id, page_bytes=page_bytes, cbz_bytes=cbz_bytes)
        started.set()
        assert finish.wait(5), "Reservation thread did not quiesce"

    monkeypatch.setattr(budget, "reserve_bounded_operation", blocked_reserve)

    async def exercise() -> None:
        task = asyncio.create_task(service.ensure_cover(series.series_id))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            for _ in range(cancel_count):
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done()
            assert _pending(service) is not None
            assert budget.snapshot().reserved_bytes == MAX_COVER_BYTES
            assert not requests
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert _pending(service) is None
            assert budget.snapshot().reserved_bytes == 0
        finally:
            finish.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await service.close()
            await http.close()

    try:
        asyncio.run(exercise())
        current = LibraryService(database).get_series(series.series_id)
        assert current is not None and current.cover_asset_id is None
        assert not tuple((tmp_path / "library" / "covers").glob("*"))
    finally:
        finish.set()
        database.close()


def test_repeated_cancel_during_cover_write_retains_admission_until_quiescence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, series, service, http, requests = _fixture(tmp_path, _png())
    persist = service._persist  # pyright: ignore[reportPrivateUsage]
    started, finish, completed = Event(), Event(), Event()

    def blocked_persist(
        series_id: UUID, url: str, body: bytes, mime: str, reservation_id: UUID
    ) -> bool:
        started.set()
        try:
            assert finish.wait(5), "Cover writer did not quiesce"
            return persist(series_id, url, body, mime, reservation_id)
        finally:
            completed.set()

    monkeypatch.setattr(service, "_persist", blocked_persist)

    async def exercise() -> None:
        first = asyncio.create_task(service.ensure_cover(series.series_id))
        second: asyncio.Task[bool] | None = None
        try:
            assert await asyncio.to_thread(started.wait, 5)
            for _ in range(3):
                first.cancel()
                await asyncio.sleep(0)
            second = asyncio.create_task(service.ensure_cover(series.series_id))
            await asyncio.sleep(0)
            assert not first.done() and not second.done()
            assert not completed.is_set()
            assert len(requests) == 1
            assert _budget(service).snapshot().reserved_bytes == MAX_COVER_BYTES
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert completed.is_set()
            assert await second is False
            assert len(requests) == 1
            assert _pending(service) is None
            assert _budget(service).snapshot().reserved_bytes == 0
        finally:
            finish.set()
            for task in (first, second):
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            await service.close()
            await http.close()

    try:
        asyncio.run(exercise())
    finally:
        finish.set()
        database.close()


def test_cover_reservation_is_retained_while_another_storage_mutation_is_active(
    tmp_path: Path,
) -> None:
    body = _png()
    database, series, service, http, requests = _fixture(tmp_path, body)

    async def exercise() -> None:
        try:
            with database.storage_gate.mutation():
                assert await service.ensure_cover(series.series_id)
                assert _pending(service) is not None
                assert _budget(service).snapshot().reserved_bytes == MAX_COVER_BYTES - len(body)
                with pytest.raises(CoverAssetError) as captured:
                    await service.close()
                assert captured.value.code is ErrorCode.SYS_DISK_BUDGET_EXCEEDED
                assert captured.value.retryable
                assert _pending(service) is not None
            await service.close()
            assert _pending(service) is None
            assert _budget(service).snapshot().reserved_bytes == 0
            assert len(requests) == 1
        finally:
            await service.close()
            await http.close()

    try:
        asyncio.run(exercise())
    finally:
        database.close()


@pytest.mark.parametrize("change", ["null_override", "source_conflict", "source_changed"])
def test_cover_rechecks_metadata_authority_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    body = _png()
    database, series, service, http, requests = _fixture(tmp_path, body)
    publisher = service._publisher  # pyright: ignore[reportPrivateUsage]
    publish = publisher.publish_no_replace
    metadata = MetadataService(database)
    association = metadata.list_sources(series.series_id)[0]

    def publish_then_change(source: Path, destination: Path) -> None:
        publish(source, destination)
        if change == "null_override":
            assert metadata.set_user_override(series.series_id, MetadataField.COVER, None)
        elif change == "source_conflict":
            _add_conflicting_source(database, series.series_id)
        else:
            assert metadata.set_source_metadata(
                association.series_source_id,
                MetadataField.COVER,
                "https://source.example/new-cover.png",
            )

    monkeypatch.setattr(publisher, "publish_no_replace", publish_then_change)

    async def exercise() -> None:
        try:
            assert await service.ensure_cover(series.series_id) is False
            assert _budget(service).snapshot().reserved_bytes == 0
        finally:
            await service.close()
            await http.close()

    try:
        asyncio.run(exercise())
        assert len(requests) == 1
        current = LibraryService(database).get_series(series.series_id)
        assert current is not None and current.cover_asset_id is None
        with database.read_session() as session:
            assert not tuple(session.scalars(select(AssetRow)))
        originals = tuple((tmp_path / "library" / "covers").glob("*"))
        assert len(originals) == 1 and originals[0].read_bytes() == body
        resolutions = metadata.resolve_series(series.series_id)
        assert resolutions is not None
        cover = next(r for r in resolutions if r.field is MetadataField.COVER)
        if change == "null_override":
            assert cover.authority is MetadataAuthority.USER_OVERRIDE and cover.value is None
        elif change == "source_conflict":
            assert cover.source_conflict and cover.value is None
        else:
            assert cover.value == "https://source.example/new-cover.png"
    finally:
        database.close()


def test_initial_source_cover_conflict_prevents_download(tmp_path: Path) -> None:
    database, series, service, http, requests = _fixture(tmp_path, _png())
    _add_conflicting_source(database, series.series_id)

    async def exercise() -> None:
        try:
            assert await service.ensure_cover(series.series_id) is False
            assert _budget(service).snapshot().reserved_bytes == 0
        finally:
            await service.close()
            await http.close()

    try:
        asyncio.run(exercise())
        assert not requests
        assert not tuple((tmp_path / "library" / "covers").glob("*"))
    finally:
        database.close()


@pytest.mark.parametrize("operation", ["ensure_cover", "refresh"])
def test_cover_acquisition_requires_resource_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    database, series, service, http, requests = _fixture(tmp_path, _png())
    monkeypatch.setattr(service, "_admission_allowed", lambda: False)

    async def exercise() -> None:
        try:
            with pytest.raises(CoverAssetError) as captured:
                if operation == "refresh":
                    await service.refresh(series.series_id)
                else:
                    await service.ensure_cover(series.series_id)
            assert captured.value.code is ErrorCode.SYS_RESOURCE_MONITOR_UNAVAILABLE
            assert captured.value.retryable
            assert _budget(service).snapshot().reserved_bytes == 0
        finally:
            await service.close()
            await http.close()

    try:
        asyncio.run(exercise())
        assert not requests
        assert not tuple((tmp_path / "library" / "covers").glob("*"))
    finally:
        database.close()


def test_uncached_cover_thumbnail_requires_admission_and_cached_one_stays_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, series, service, http, requests = _fixture(tmp_path, _png())

    async def exercise() -> None:
        try:
            assert await service.ensure_cover(series.series_id)
            cached = await service.thumbnail(series.series_id, ThumbnailVariant.SMALL)
            assert cached is not None
            monkeypatch.setattr(service, "_admission_allowed", lambda: False)
            assert await service.thumbnail(series.series_id, ThumbnailVariant.SMALL) == cached
            with pytest.raises(CoverAssetError) as captured:
                await service.thumbnail(series.series_id, ThumbnailVariant.MEDIUM)
            assert captured.value.code is ErrorCode.SYS_RESOURCE_MONITOR_UNAVAILABLE
            assert captured.value.retryable
            assert len(requests) == 1
        finally:
            await service.close()
            await http.close()

    try:
        asyncio.run(exercise())
    finally:
        database.close()
