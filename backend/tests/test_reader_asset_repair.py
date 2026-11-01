import asyncio
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from support.cbz import index_test_job
from test_online_reader import _memory_harness  # pyright: ignore[reportPrivateUsage]
from test_reader_asset import PNG_BYTES, _fixture  # pyright: ignore[reportPrivateUsage]

from app.domain.download_job import DownloadJobStatus
from app.persistence.atomic_file_publisher import ADR_0087_RETRY_POLICY, AtomicFilePublisher
from app.persistence.mappers.asset import asset_to_row
from app.persistence.models.asset import AssetRow
from app.persistence.models.chapter_cbz_asset import ChapterCbzAssetRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.repositories.chapter_cbz_asset import ChapterCbzAssetRepository
from app.services.cbz_builder import CBZBuilder
from app.services.download_worker import DownloadWorker
from app.services.image_validation import ImageValidationPolicy
from app.services.job_recovery import RecoveryCoordinator
from app.services.jobs import DownloadJobService
from app.services.reader_asset import ReaderAssetError


@pytest.mark.parametrize("operation", ["page", "export"])
@pytest.mark.parametrize("damage", ["hash-mismatch", "missing-file"])
def test_unavailable_archive_allows_a_verified_replacement_without_losing_history_or_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, damage: str
) -> None:
    async def exercise() -> None:
        h, online, runtime, _closed = await _memory_harness(tmp_path, monkeypatch)
        try:
            jobs = DownloadJobService(h.database)
            chapters = await h.provider.get_chapters("mock-series-1")
            metadata = h.provider.normalize_metadata(await h.provider.get_series("mock-series-1"))
            original = jobs.create_selected_jobs(
                connector=h.provider.metadata,
                remote_series_id="mock-series-1",
                series_metadata=metadata,
                chapters=chapters,
                selected_remote_ids=("mock-chapter-1",),
            )[0]
            index_test_job(h.database, original.job.job_id)
            reader = runtime.reader_asset_service
            progress = runtime.reader_progress_service
            assert reader is not None and progress is not None
            saved = progress.save(h.chapter_id, 0)
            repository = ChapterCbzAssetRepository()
            with h.database.read_session() as session:
                linked = repository.get_for_chapter(session, h.chapter_id)
                assert linked is not None
                old_asset = linked[1]
            path = h.database.data_root_identity.canonical_path / old_asset.relative_path
            corrupted = b"X" + path.read_bytes()[1:]
            if damage == "missing-file":
                path.unlink()
            else:
                path.write_bytes(corrupted)
            message = "unavailable" if damage == "missing-file" else "hash changed"
            with pytest.raises(ReaderAssetError, match=message):
                if operation == "page":
                    reader.open_page(h.chapter_id, 1)
                else:
                    next(reader.iter_verified_chapter_pages(h.chapter_id))
            assert reader.get_chapter(h.chapter_id) is None
            with h.database.read_session() as session:
                if damage == "hash-mismatch":
                    assert repository.get_for_chapter(session, h.chapter_id) is None
                assert session.get(AssetRow, str(old_asset.asset_id)) is not None
                historical_job = session.get(DownloadJobRow, str(original.job.job_id))
                assert historical_job is not None and historical_job.status == "COMPLETED"
            replacement = jobs.create_selected_jobs(
                connector=h.provider.metadata,
                remote_series_id="mock-series-1",
                series_metadata=metadata,
                chapters=chapters,
                selected_remote_ids=("mock-chapter-1",),
            )[0]
            assert replacement.created and replacement.job.job_id != original.job.job_id
            assert replacement.chapter_id == h.chapter_id
            with h.database.read_session() as session:
                assert repository.get_for_chapter(session, h.chapter_id) is None
            identity = h.database.data_root_identity
            publisher = AtomicFilePublisher(
                data_root_identity=identity,
                retry_policy=ADR_0087_RETRY_POLICY,
                sleep=time.sleep,
                clock=time.monotonic,
                storage_gate=h.database.storage_gate,
            )
            worker = DownloadWorker(
                database=h.database,
                data_root_identity=identity,
                connector_registry=h.source_pages._registry,  # pyright: ignore[reportPrivateUsage]
                recovery_coordinator=RecoveryCoordinator(h.database, identity),
                transport=h.transport,
                image_validation_policy=ImageValidationPolicy(1024, 100, 100, 10_000),
                cbz_builder=CBZBuilder(publisher=publisher, max_archive_bytes=2_147_483_648),
                file_publisher=publisher,
                reader_asset_service=reader,
            )
            budget = h.transport._disk_budget_manager  # pyright: ignore[reportPrivateUsage]
            assert budget is not None
            budget.reserve(replacement.job.job_id)
            h.provider.release.set()
            try:
                result = await worker.run_job(replacement.job.job_id)
            finally:
                budget.release(replacement.job.job_id, quiescent=True)
            assert result.job.status is DownloadJobStatus.COMPLETED
            stream = reader.open_page(h.chapter_id, 1)
            assert b"".join(stream.iter_chunks()) == h.body
            if damage == "missing-file":
                assert not path.exists()
            else:
                assert path.read_bytes() == corrupted
            assert progress.get(h.chapter_id) == saved
            with h.database.read_session() as session:
                assert session.get(AssetRow, str(old_asset.asset_id)) is not None
                historical_job = session.get(DownloadJobRow, str(original.job.job_id))
                assert historical_job is not None and historical_job.status == "COMPLETED"
        finally:
            await online.close()
            await h.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("error", [PermissionError("temporarily locked"), OSError("unavailable")])
def test_temporary_file_error_keeps_the_verified_chapter_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    fixture = _fixture(tmp_path)
    try:
        fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=fixture.result,
        )
        original_open = Path.open

        def denied(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == fixture.cbz_path:
                raise error
            return cast(Any, original_open)(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", denied)
            with pytest.raises(ReaderAssetError):
                fixture.service.open_page(fixture.chapter.chapter_id, 1)
        assert fixture.service.get_chapter(fixture.chapter.chapter_id) is not None
        stream = fixture.service.open_page(fixture.chapter.chapter_id, 1)
        assert b"".join(stream.iter_chunks()) == PNG_BYTES
    finally:
        fixture.database.close()


def test_a_file_changing_during_hashing_keeps_the_chapter_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    try:
        fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=fixture.result,
        )
        fixture.cbz_path.write_bytes(b"X" + fixture.cbz_path.read_bytes()[1:])
        original_stat = os.fstat
        calls = 0

        def changing_stat(descriptor: int) -> Any:
            nonlocal calls
            result = original_stat(descriptor)
            calls += 1
            return result if calls == 1 else SimpleNamespace(
                st_size=result.st_size,
                st_mtime_ns=result.st_mtime_ns + 1,
                st_ctime_ns=result.st_ctime_ns,
            )

        with monkeypatch.context() as patch:
            patch.setattr(os, "fstat", changing_stat)
            with pytest.raises(ReaderAssetError, match="changed during verification"):
                fixture.service.open_page(fixture.chapter.chapter_id, 1)
        assert fixture.service.get_chapter(fixture.chapter.chapter_id) is not None
    finally:
        fixture.database.close()


def test_hash_mismatch_does_not_remove_a_concurrently_replaced_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    try:
        indexed = fixture.service.index_verified_cbz(
            job_id=fixture.job.job_id,
            chapter_id=fixture.chapter.chapter_id,
            result=fixture.result,
        )
        replacement = replace(
            indexed.asset, asset_id=uuid4(), relative_path="library/replacement.cbz"
        )
        replacement_path = tmp_path / replacement.relative_path
        replacement_path.write_bytes(fixture.cbz_path.read_bytes())
        fixture.cbz_path.write_bytes(b"X" + fixture.cbz_path.read_bytes()[1:])
        original_open = fixture.service._open_verified_asset  # pyright: ignore[reportPrivateUsage]

        def replace_before_verification(*args: Any, **kwargs: Any) -> Any:
            with fixture.database.write_session() as session:
                session.add(asset_to_row(replacement))
                session.flush()
                link = session.get(ChapterCbzAssetRow, str(fixture.chapter.chapter_id))
                assert link is not None
                link.asset_id = str(replacement.asset_id)
            return original_open(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(fixture.service, "_open_verified_asset", replace_before_verification)
            with pytest.raises(ReaderAssetError, match="hash changed"):
                fixture.service.open_page(fixture.chapter.chapter_id, 1)
        with fixture.database.read_session() as session:
            linked = ChapterCbzAssetRepository().get_for_chapter(
                session, fixture.chapter.chapter_id
            )
            assert linked is not None and linked[1].asset_id == replacement.asset_id
        stream = fixture.service.open_page(fixture.chapter.chapter_id, 1)
        assert b"".join(stream.iter_chunks()) == PNG_BYTES
    finally:
        fixture.database.close()
