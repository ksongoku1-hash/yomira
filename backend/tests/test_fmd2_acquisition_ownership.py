from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from app.connectors.errors import SourceConnectorError
from app.fmd2_bridge import FMD2_API_ORIGIN, FMD2_PUBLIC_SOURCES, Fmd2Connector


def _chapter() -> str:
    payload = json.dumps(["/en/test/list", "/en/test/chapter", 1]).encode()
    return "yomira-fmd2:v1:" + base64.urlsafe_b64encode(payload).decode().rstrip("=")


class _Engine:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.posts: list[UUID] = []
        self.deleted: list[UUID] = []
        self.completed: set[UUID] = set()
        self.cancelled: set[UUID] = set()
        self.acknowledge_cancel = True
        self.lose_create_response = False
        self.hold_create_response = False
        self.release_create_response = asyncio.Event()
        self.hold_cancel_response = False
        self.cancelling = asyncio.Event()
        self.release_cancel_response = asyncio.Event()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sources":
            return httpx.Response(
                200,
                json={
                    "api_version": "v1",
                    "source_policy": "public-allowlist-v1",
                    "data": [
                        {
                            "source_id": source,
                            "name": name,
                            "base_url": origin,
                            "access": "public",
                            "catalog_ready": True,
                        }
                        for source, (name, origin, _language) in FMD2_PUBLIC_SOURCES.items()
                    ],
                },
            )
        if request.method == "POST":
            job = UUID(json.loads(request.content)["request_id"])
            self.posts.append(job)
            self.started.set()
            if self.hold_create_response:
                await self.release_create_response.wait()
            if self.lose_create_response:
                raise httpx.ReadError("synthetic lost acknowledgement", request=request)
            return httpx.Response(202, json={"job_id": str(job)})
        job = UUID(request.url.path.rsplit("/", 1)[1])
        if request.method == "DELETE":
            self.deleted.append(job)
            self.cancelling.set()
            if self.hold_cancel_response:
                await self.release_cancel_response.wait()
            if self.acknowledge_cancel:
                self.cancelled.add(job)
            return httpx.Response(200, json={"job_id": str(job)})
        status = "completed" if job in self.completed else "running"
        if job in self.cancelled:
            status = "cancelled"
        return httpx.Response(
            200,
            json={
                "job_id": str(job),
                "status": status,
                "files": ["001.png"] if status == "completed" else [],
                "completed_pages": 1 if status == "completed" else 0,
                "total_pages": 1,
            },
        )


def _connector(tmp_path: Path, engine: _Engine) -> Fmd2Connector:
    client = httpx.AsyncClient(
        base_url=FMD2_API_ORIGIN,
        transport=httpx.MockTransport(engine.handle),
        trust_env=False,
        follow_redirects=False,
    )
    return Fmd2Connector(tmp_path / "fmd.exe", tmp_path / "staging", api_client=client)


@pytest.mark.parametrize("restored", [False, True])
def test_missing_native_attempt_requires_durable_restored_ownership(
    tmp_path: Path, restored: bool
) -> None:
    async def exercise() -> None:
        engine = _Engine()

        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/sources":
                return await engine.handle(request)
            return httpx.Response(404, json={"error": {"code": "not_found"}})

        client = httpx.AsyncClient(
            base_url=FMD2_API_ORIGIN, transport=httpx.MockTransport(handle), trust_env=False
        )
        connector = Fmd2Connector(tmp_path / "fmd.exe", tmp_path / "staging", api_client=client)
        job = uuid4()
        directory = tmp_path / "staging" / str(job)
        directory.mkdir(parents=True)
        page = directory / "001.png"
        page.write_bytes(b"owned fixture")
        try:
            if restored:
                connector.track_abandoned_acquisition(job)
                assert await connector.connection_available()
                await connector.release_reader_staging(job, restored=True)
                assert not directory.exists()
            else:
                with pytest.raises(SourceConnectorError):
                    await connector.release_reader_staging(job)
                assert page.read_bytes() == b"owned fixture"
        finally:
            await connector.close()

    asyncio.run(exercise())


def test_running_reader_staging_cannot_be_deleted(tmp_path: Path) -> None:
    async def exercise() -> None:
        engine = _Engine()
        connector = _connector(tmp_path, engine)
        job = uuid4()
        directory = tmp_path / "staging" / str(job)
        directory.mkdir(parents=True)
        page = directory / "001.png"
        page.write_bytes(b"owned fixture")
        try:
            with pytest.raises(ValueError, match="terminal"):
                await connector.release_reader_staging(job, restored=True)
            assert page.read_bytes() == b"owned fixture"
        finally:
            await connector.close()

    asyncio.run(exercise())


def test_connection_probe_preserves_owned_acquisition(tmp_path: Path) -> None:
    async def exercise() -> None:
        engine = _Engine()
        connector = _connector(tmp_path, engine)
        job = uuid4()
        acquisition = asyncio.create_task(connector.acquire_pages(job, _chapter(), "Chapter"))
        try:
            await asyncio.wait_for(engine.started.wait(), 2)
            assert await connector.connection_available()
            assert not acquisition.done() and engine.deleted == []
            engine.completed.add(job)
            assert len(await asyncio.wait_for(acquisition, 2)) == 1
        finally:
            if not acquisition.done():
                acquisition.cancel()
                await asyncio.gather(acquisition, return_exceptions=True)
            await connector.close()

    asyncio.run(exercise())


def test_owner_cancellation_releases_only_that_job_and_admits_waiter(tmp_path: Path) -> None:
    async def exercise() -> None:
        engine = _Engine()
        connector = _connector(tmp_path, engine)
        first_id, second_id = uuid4(), uuid4()
        first = asyncio.create_task(connector.acquire_pages(first_id, _chapter(), "First"))
        second: asyncio.Task[object] | None = None
        try:
            await asyncio.wait_for(engine.started.wait(), 2)
            second = asyncio.create_task(connector.acquire_pages(second_id, _chapter(), "Second"))
            engine.completed.add(second_id)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert await asyncio.wait_for(second, 2)
            assert engine.deleted == [first_id] and engine.posts == [first_id, second_id]
        finally:
            tasks = [task for task in (first, second) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await connector.close()

    asyncio.run(exercise())


def test_concurrent_recovery_probes_do_not_duplicate_cancellation(tmp_path: Path) -> None:
    async def exercise() -> None:
        engine = _Engine()
        engine.hold_cancel_response = True
        connector = _connector(tmp_path, engine)
        job = uuid4()
        acquisition = asyncio.create_task(connector.acquire_pages(job, _chapter(), "Chapter"))
        probes: list[asyncio.Task[bool]] = []
        try:
            await asyncio.wait_for(engine.started.wait(), 2)
            acquisition.cancel()
            await asyncio.wait_for(engine.cancelling.wait(), 2)
            probes = [asyncio.create_task(connector.connection_available()) for _ in range(2)]
            await asyncio.sleep(0.05)
            assert not any(probe.done() for probe in probes) and engine.deleted == [job]
            engine.release_cancel_response.set()
            with pytest.raises(asyncio.CancelledError):
                await acquisition
            assert await asyncio.wait_for(asyncio.gather(*probes), 2) == [True, True]
            assert engine.deleted == [job]
        finally:
            engine.release_cancel_response.set()
            if not acquisition.done():
                acquisition.cancel()
            await asyncio.gather(acquisition, *probes, return_exceptions=True)
            await connector.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("cancel_waiter", [False, True])
def test_next_acquisition_waits_without_cancelling_owner(
    tmp_path: Path, cancel_waiter: bool
) -> None:
    async def exercise() -> None:
        engine = _Engine()
        connector = _connector(tmp_path, engine)
        first_id, second_id = uuid4(), uuid4()
        first = asyncio.create_task(connector.acquire_pages(first_id, _chapter(), "First"))
        second: asyncio.Task[object] | None = None
        try:
            await asyncio.wait_for(engine.started.wait(), 2)
            engine.started.clear()
            second = asyncio.create_task(connector.acquire_pages(second_id, _chapter(), "Second"))
            await asyncio.sleep(0.05)
            assert engine.posts == [first_id] and engine.deleted == []
            assert await connector.connection_available()
            if cancel_waiter:
                second.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await second
                assert engine.deleted == []
            engine.completed.add(first_id)
            assert len(await asyncio.wait_for(first, 2)) == 1
            if not cancel_waiter:
                await asyncio.wait_for(engine.started.wait(), 2)
                assert engine.posts == [first_id, second_id] and engine.deleted == []
                engine.completed.add(second_id)
                assert await asyncio.wait_for(second, 2)
        finally:
            tasks = [task for task in (first, second) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await connector.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("interruption", ["read_error", "cancellation"])
def test_lost_create_acknowledgement_settles_the_native_attempt(
    tmp_path: Path, interruption: str
) -> None:
    async def exercise() -> None:
        engine = _Engine()
        engine.lose_create_response = interruption == "read_error"
        engine.hold_create_response = interruption == "cancellation"
        connector = _connector(tmp_path, engine)
        job = uuid4()
        try:
            if interruption == "read_error":
                with pytest.raises(SourceConnectorError):
                    await connector.acquire_pages(job, _chapter(), "Chapter")
            else:
                acquisition = asyncio.create_task(
                    connector.acquire_pages(job, _chapter(), "Chapter")
                )
                await asyncio.wait_for(engine.started.wait(), 2)
                acquisition.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await acquisition
            assert engine.posts == [job] and engine.deleted == [job]
            assert await connector.connection_available()
        finally:
            await connector.close()

    asyncio.run(exercise())


def test_engine_start_failure_does_not_leave_a_phantom_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        engine = _Engine()
        connector = _connector(tmp_path, engine)
        monkeypatch.setattr(connector, "_owns_process", True)

        async def unavailable() -> None:
            raise RuntimeError("synthetic startup failure")

        monkeypatch.setattr(connector, "start", unavailable)
        try:
            with pytest.raises(SourceConnectorError):
                await connector.acquire_pages(uuid4(), _chapter(), "Chapter")
            assert engine.posts == [] and engine.deleted == []
            monkeypatch.setattr(connector, "_owns_process", False)
            assert await connector.connection_available()
            job = uuid4()
            engine.completed.add(job)
            assert len(await connector.acquire_pages(job, _chapter(), "Chapter")) == 1
            assert engine.posts == [job] and engine.deleted == []
        finally:
            await connector.close()

    asyncio.run(exercise())


def test_unconfirmed_cancellation_blocks_admission_until_terminal(tmp_path: Path) -> None:
    async def exercise() -> None:
        engine = _Engine()
        engine.acknowledge_cancel = False
        connector = _connector(tmp_path, engine)
        first_id, second_id = uuid4(), uuid4()
        first = asyncio.create_task(connector.acquire_pages(first_id, _chapter(), "First"))
        try:
            await asyncio.wait_for(engine.started.wait(), 2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            with pytest.raises(SourceConnectorError):
                await connector.acquire_pages(second_id, _chapter(), "Second")
            assert engine.posts == [first_id]
            assert not await connector.connection_available()
            engine.cancelled.add(first_id)
            assert await connector.connection_available()
            engine.completed.add(second_id)
            assert len(await connector.acquire_pages(second_id, _chapter(), "Second")) == 1
            assert engine.posts == [first_id, second_id]
        finally:
            if not first.done():
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
            await connector.close()

    asyncio.run(exercise())
