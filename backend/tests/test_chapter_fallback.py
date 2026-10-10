import asyncio
import base64
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from support.cbz import index_test_job
from support.sqlite import open_test_database

from app.connectors.fmd2 import FMD2_API_ORIGIN, Fmd2Connector
from app.domain.download_job import DownloadJobStatus
from app.errors import ErrorCode
from app.fmd2_bridge import FMD2_METADATA, FMD2_PUBLIC_SOURCES, fmd2_chapter_location
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.series import SeriesRow
from app.services.chapter_fallback import ChapterFallbackService, _chapter_number
from app.services.download_worker import DownloadWorkerResult
from app.services.jobs import DownloadJobService

TMO = "9185eb6c49324a849c7d7925a41ef3a3"
INMANGA = "03ae42ac9141443fa0711bd401c0203d"
NOVEL = "424747b0135c4240a902eb9cfcb11db9"
MANHWA = "K042a85566244b7e836679491ce67ot0"
ASURA = "7103ae6839ea46ec80cdfc2c4b37c803"


def reference(source: str, path: str = "/naruto") -> str:
    return "fmd2:v1:" + (source + "\n" + path).encode().hex()


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Capítulo 02", "2"),
        ("Capítulo: 2.50", "2.50"),
        ("Chapter 2,5", "2.5"),
        ("2.05", "2.05"),
        ("Cap. 1", "1"),
        ("Capítulo 0", "0"),
        ("Capítulo 2 extra", None),
        ("Volumen 2", None),
        ("Especial 2", None),
        ("Capítulo 2-3", None),
        ("2: otro título", None),
        ("Capítulo 2.5a", None),
    ],
)
def test_chapter_number_does_not_guess_specials(label, expected):
    number = _chapter_number(label)
    assert (str(number) if number is not None else None) == expected


async def setup(data_root: Path, *, options=None):
    options = options or {}
    database = open_test_database(data_root)
    MigrationRunner(database, data_root / "backups").upgrade_to_head()
    calls = []
    results = [
        {
            "source_id": site,
            "manga_ref": reference(site),
            "remote_url": FMD2_PUBLIC_SOURCES[site][1] + "/naruto",
            "title": "Naruto",
        }
        for site in (TMO, INMANGA, NOVEL, MANHWA, ASURA)
    ]
    if options.get("ambiguous"):
        results.insert(
            2,
            {
                **results[1],
                "manga_ref": reference(INMANGA, "/naruto-other"),
                "remote_url": "https://inmanga.com/naruto-other",
            },
        )
    if options.get("different_title"):
        results[1]["title"] = "Naruto Full Color"

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/search":
            return httpx.Response(200, json={"data": results})
        value = request.url.path.removeprefix("/v1/manga/").removesuffix("/chapters")
        source = bytes.fromhex(value.removeprefix("fmd2:v1:")).decode().split("\n")[0]
        if request.url.path.endswith("/chapters"):
            titles = ["Capítulo: 2.50"] if source == INMANGA else ["Capítulo 2,5"]
            if options.get("duplicate_chapter") and source == INMANGA:
                titles.append("Capítulo 2.5")
            return httpx.Response(
                200,
                json={
                    "chapters": [
                        {
                            "chapter_ref": reference(source, f"/read/{index}"),
                            "title": title,
                            "ordinal": index + 70,
                        }
                        for index, title in enumerate(titles)
                    ]
                },
            )
        title = "Naruto"
        author = "Masashi Kishimoto"
        if options.get("metadata_variant") and source == INMANGA:
            title = "Naruto Full Color"
        if options.get("different_author") and source == INMANGA:
            author = "Otro autor"
        if options.get("reordered_author") and source == INMANGA:
            author = "Kishimoto Masashi"
        return httpx.Response(
            200,
            json={
                "manga": {
                    "manga_ref": value,
                    "source_id": source,
                    "title": title,
                    "authors": author,
                    "artists": "",
                    "genres": "",
                    "status": "0",
                }
            },
        )

    client = httpx.AsyncClient(
        base_url=FMD2_API_ORIGIN,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer " + "a" * 64},
    )
    connector = Fmd2Connector(data_root / "fmd.exe", data_root / "staging", api_client=client)
    registry = SimpleNamespace(resolve=lambda connector_id: connector)
    service = DownloadJobService(database)
    metadata = connector.normalize_metadata(await connector.get_series(reference(TMO)))
    chapters = await connector.get_chapters(reference(TMO))
    summary = service.create_selected_jobs(
        connector=FMD2_METADATA,
        remote_series_id=reference(TMO),
        series_metadata=metadata,
        chapters=chapters,
        selected_remote_ids=(chapters[0].remote_id,),
    )[0]
    with database.write_session() as session:
        session.get(DownloadJobRow, str(summary.job.job_id)).status = "FAILED"
    result = DownloadWorkerResult(
        replace(summary.job, status=DownloadJobStatus.FAILED),
        summary.series_id,
        summary.chapter_id,
        0,
        None,
        ErrorCode.SRC_CONNECTOR_FAILED.value,
        "page_download",
        "trace",
        retryable=True,
    )
    fallback = ChapterFallbackService(database, registry)
    return database, client, connector, service, fallback, summary, result, calls


def test_fallback_keeps_canonical_chapter_and_durable_attempts_across_restart(tmp_path):
    async def exercise():
        database, client, connector, service, fallback, summary, result, _ = await setup(tmp_path)
        try:
            assert await fallback.recover(result)
            current = service.list_jobs().items
            assert len(current) == 1
            replacement = current[0]
            assert replacement.chapter_id == summary.chapter_id
            assert replacement.series_id == summary.series_id
            assert replacement.job.job_id != summary.job.job_id
            assert replacement.job.status is DownloadJobStatus.QUEUED
            assert replacement.job.priority.value == "HIGH"
            availability = service.chapter_availability(FMD2_METADATA.connector_id, reference(TMO))
            with database.read_session() as session:
                original = session.get(ChapterSourceRow, str(summary.job.chapter_source_id))
                assert original.remote_id in availability["busy_remote_ids"]
            assert service.list_jobs(archived=True).items[0].job.job_id == result.job.job_id
            assert not await fallback.recover(result)
            with database.read_session() as session:
                assert session.scalar(select(func.count()).select_from(ChapterRow)) == 1
                assert session.scalar(select(func.count()).select_from(SeriesRow)) == 1
                remote = session.get(
                    ChapterSourceRow, str(replacement.job.chapter_source_id)
                ).remote_id
            assert fmd2_chapter_location(remote)[1] == INMANGA

            # A provider changes its ordinal, but the physical source was already attempted.
            with database.write_session() as session:
                session.get(DownloadJobRow, str(replacement.job.job_id)).status = "FAILED"
                row = session.get(ChapterSourceRow, str(replacement.job.chapter_source_id))
                token = row.remote_id.split(":", 2)[2]
                payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
                payload = payload[:3] + [99]
                row.remote_id = "yomira-fmd2:v2:" + base64.urlsafe_b64encode(
                    json.dumps(payload).encode()
                ).decode().rstrip("=")
            restarted = ChapterFallbackService(
                database, SimpleNamespace(resolve=lambda _: connector)
            )
            failed = replace(result, job=replace(replacement.job, status=DownloadJobStatus.FAILED))
            assert await restarted.recover(failed)
            newest = service.list_jobs().items[0]
            with database.read_session() as session:
                remote = session.get(ChapterSourceRow, str(newest.job.chapter_source_id)).remote_id
            assert fmd2_chapter_location(remote)[1] == NOVEL
            with database.write_session() as session:
                session.get(DownloadJobRow, str(newest.job.job_id)).status = "FAILED"
            assert await restarted.recover(
                replace(failed, job=replace(newest.job, status=DownloadJobStatus.FAILED))
            )
            last = service.list_jobs().items[0]
            with database.write_session() as session:
                session.get(DownloadJobRow, str(last.job.job_id)).status = "FAILED"
            assert not await restarted.recover(
                replace(failed, job=replace(last.job, status=DownloadJobStatus.FAILED))
            )
            assert len(service.list_jobs().items) == 1
        finally:
            await client.aclose()
            database.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "option",
    [
        "ambiguous",
        "different_title",
        "metadata_variant",
        "different_author",
        "duplicate_chapter",
    ],
)
def test_fallback_rejects_unsafe_candidate_and_moves_to_next_source(tmp_path, option):
    async def exercise():
        database, client, _, service, fallback, _, result, _ = await setup(
            tmp_path, options={option: True}
        )
        try:
            assert await fallback.recover(result)
            job = service.list_jobs().items[0].job
            with database.read_session() as session:
                remote = session.get(ChapterSourceRow, str(job.chapter_source_id)).remote_id
            assert fmd2_chapter_location(remote)[1] == NOVEL
        finally:
            await client.aclose()
            database.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "state", ["cancelled", "archived", "busy", "completed", "disk", "special", "other_series"]
)
def test_fallback_preserves_user_actions_and_storage_failures(tmp_path, state):
    async def exercise():
        database, client, connector, service, fallback, summary, result, calls = await setup(
            tmp_path
        )
        try:
            if state == "other_series":
                service.import_library_series(
                    connector=FMD2_METADATA,
                    remote_series_id=reference(INMANGA),
                    series_metadata=connector.normalize_metadata(
                        await connector.get_series(reference(INMANGA))
                    ),
                )
            else:
                with database.write_session() as session:
                    row = session.get(DownloadJobRow, str(result.job.job_id))
                    if state == "cancelled":
                        row.cancel_requested_at_us = row.updated_at_us
                    elif state == "busy":
                        row.status = "QUEUED"
                    elif state == "completed":
                        row.status = "COMPLETED"
                    elif state == "special":
                        session.get(
                            ChapterRow, str(summary.chapter_id)
                        ).display_label = "Capítulo 2.5 Extra"
                if state == "archived":
                    service.archive_job(result.job.job_id)
                if state == "disk":
                    result = replace(result, error_code=ErrorCode.SYS_DISK_BUDGET_EXCEEDED.value)
            before = len(calls)
            recovered = await fallback.recover(result)
            if state == "other_series":
                assert recovered
                job = service.list_jobs().items[0].job
                with database.read_session() as session:
                    remote = session.get(ChapterSourceRow, str(job.chapter_source_id)).remote_id
                assert fmd2_chapter_location(remote)[1] == NOVEL
            else:
                assert not recovered
                assert len(calls) == before
        finally:
            await client.aclose()
            database.close()

    asyncio.run(exercise())


def test_dispatcher_enqueues_alternative_before_advancing_and_reports_search(tmp_path):
    async def exercise():
        from app.services.download_dispatcher import DownloadDispatcher
        from app.services.job_recovery import RecoveryCoordinator

        database, client, _, service, fallback, summary, result, _ = await setup(tmp_path)
        try:

            class Runner:
                async def run_job(self, job_id):
                    assert job_id == result.job.job_id
                    return result

            dispatcher = DownloadDispatcher(
                database=database,
                worker=Runner(),
                chapter_fallback=fallback,
                recovery_coordinator=RecoveryCoordinator(database, database.data_root_identity),
            )
            recovered = asyncio.Event()
            proceed = asyncio.Event()
            original = fallback.recover

            async def hold(result):
                recovered.set()
                await proceed.wait()
                return await original(result)

            fallback.recover = hold
            task = asyncio.create_task(dispatcher._run_one(summary.job.job_id))
            await recovered.wait()
            assert dispatcher.recovering_job_id == summary.job.job_id
            proceed.set()
            await task
            assert dispatcher.recovering_job_id is None
            assert dispatcher.source_wait == (0, 0)
            assert service.list_jobs().items[0].job.status is DownloadJobStatus.QUEUED
        finally:
            await client.aclose()
            database.close()

    asyncio.run(exercise())


def test_fallback_accepts_the_same_author_with_reversed_name_order(tmp_path):
    async def exercise():
        database, client, _, service, fallback, _, result, _ = await setup(
            tmp_path,
            options={"reordered_author": True},
        )
        try:
            assert await fallback.recover(result)
            job = service.list_jobs().items[0].job
            with database.read_session() as session:
                remote = session.get(ChapterSourceRow, str(job.chapter_source_id)).remote_id
            assert fmd2_chapter_location(remote)[1] == INMANGA
        finally:
            await client.aclose()
            database.close()

    asyncio.run(exercise())


def test_retrying_original_source_reuses_the_alternate_attempt_without_duplicates(tmp_path):
    async def exercise():
        database, client, connector, service, fallback, _, result, _ = await setup(tmp_path)
        try:
            assert await fallback.recover(result)
            alternative = service.list_jobs().items[0]
            retried = service.retry_job(result.job.job_id)
            assert retried.job.job_id == alternative.job.job_id and not retried.created
            chapters = await connector.get_chapters(reference(TMO))
            repeated = service.create_selected_jobs(
                connector=FMD2_METADATA,
                remote_series_id=reference(TMO),
                series_metadata=connector.normalize_metadata(
                    await connector.get_series(reference(TMO))
                ),
                chapters=chapters,
                selected_remote_ids=(chapters[0].remote_id,),
            )[0]
            assert repeated.job.job_id == alternative.job.job_id and not repeated.created
            with database.write_session() as session:
                session.get(DownloadJobRow, str(alternative.job.job_id)).status = "COMPLETED"
            index_test_job(database, alternative.job.job_id)
            retried = service.retry_job(result.job.job_id)
            assert retried.job.job_id == alternative.job.job_id
            assert retried.job.status is DownloadJobStatus.COMPLETED
            with database.read_session() as session:
                assert session.scalar(select(func.count()).select_from(DownloadJobRow)) == 2
        finally:
            await client.aclose()
            database.close()

    asyncio.run(exercise())
