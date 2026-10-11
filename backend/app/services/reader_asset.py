from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
import time
import zipfile
from collections import OrderedDict
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal, TypeGuard, cast
from uuid import UUID
from xml.etree import ElementTree as ET

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.domain.asset import Asset, AssetType, validate_relative_path
from app.domain.chapter_cbz_asset import ChapterCbzAsset
from app.domain.download_job import DownloadJobStatus
from app.domain.job_state_machine import assert_transition_allowed
from app.persistence.database import SQLiteDatabase
from app.persistence.mappers.asset import asset_to_row
from app.persistence.mappers.timestamps import datetime_to_epoch_us
from app.persistence.models.chapter import ChapterRow
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.download_manifest_state import DownloadManifestStateRow
from app.persistence.models.download_page import DownloadPageRow
from app.persistence.repositories.chapter_cbz_asset import (
    ChapterCbzAssetRepository,
    ReaderChapterRecord,
    ReaderPageRecord,
)
from app.persistence.windows_paths import DataRootIdentity
from app.services.cbz_builder import CBZBuildResult

DEFAULT_READER_PAGE_SIZE = 100
MAX_READER_PAGE_SIZE = 500
V1_MAX_CBZ_BYTES = 2_147_483_648
V1_MAX_PAGE_BYTES = 67_108_864
V1_STREAM_CHUNK_BYTES = 65_536

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def _is_sqlite_database(value: object) -> TypeGuard[SQLiteDatabase]:
    return isinstance(value, SQLiteDatabase)


class ReaderAssetError(RuntimeError):
    """Sanitized asset-indexing or reader-integrity failure."""


class ReaderAssetNotFound(ReaderAssetError):
    """The requested chapter or page is not available to the reader."""


class ReaderAssetConflict(ReaderAssetError):
    """A chapter already has a different immutable CBZ link."""


@dataclass(frozen=True, slots=True)
class ReaderAssetLimits:
    max_cbz_bytes: int
    max_page_bytes: int
    chunk_bytes: int

    def __post_init__(self) -> None:
        expected = {
            "max_cbz_bytes": V1_MAX_CBZ_BYTES,
            "max_page_bytes": V1_MAX_PAGE_BYTES,
            "chunk_bytes": V1_STREAM_CHUNK_BYTES,
        }
        for field_name, required_value in expected.items():
            value = getattr(self, field_name)
            if type(value) is not int or value != required_value:
                raise ValueError(f"{field_name} must equal its ADR-approved V1 limit")


@dataclass(frozen=True, slots=True)
class IndexedCbzAsset:
    asset: Asset
    page_count: int


@dataclass(frozen=True, slots=True)
class ReaderChapter:
    chapter_id: UUID
    series_id: UUID
    display_label: str
    page_count: int


@dataclass(frozen=True, slots=True)
class ReaderChapterPage:
    items: tuple[ReaderChapter, ...]
    total: int
    limit: int
    offset: int


class ReaderPageStream:
    """A verified page stream that owns its CBZ file handle until closed."""

    __slots__ = (
        "media_type",
        "etag",
        "size_bytes",
        "_archive",
        "_file_stream",
        "_member",
        "_chunk_bytes",
        "_closed",
    )

    def __init__(
        self,
        *,
        media_type: str,
        sha256: str,
        size_bytes: int,
        archive: zipfile.ZipFile,
        file_stream: BinaryIO,
        member: str,
        chunk_bytes: int,
    ) -> None:
        self.media_type = media_type
        self.etag = f'"{sha256}"'
        self.size_bytes = size_bytes
        self._archive = archive
        self._file_stream = file_stream
        self._member = member
        self._chunk_bytes = chunk_bytes
        self._closed = False

    def iter_chunks(self) -> Iterator[bytes]:
        try:
            with self._archive.open(self._member, mode="r") as member:
                emitted = 0
                while chunk := member.read(self._chunk_bytes):
                    emitted += len(chunk)
                    if emitted > self.size_bytes:
                        raise ReaderAssetError("The verified page changed while streaming.")
                    yield chunk
                if emitted != self.size_bytes:
                    raise ReaderAssetError("The verified page changed while streaming.")
        except ReaderAssetError:
            raise
        except Exception:
            raise ReaderAssetError("The verified page could not be streamed.") from None
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._archive.close()
        self._file_stream.close()


@dataclass(frozen=True, slots=True)
class _PageExpectation:
    ordinal: int
    size_bytes: int
    sha256: str
    mime: str


@dataclass(frozen=True, slots=True)
class _IndexSnapshot:
    job_updated_at_us: int
    page_expectations: tuple[_PageExpectation, ...]
    existing_asset: Asset | None
    existing_link: ChapterCbzAsset | None


class ReaderAssetService:
    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        data_root_identity: DataRootIdentity,
        library_root_identity: DataRootIdentity | None = None,
        limits: ReaderAssetLimits,
        repository: ChapterCbzAssetRepository | None = None,
    ) -> None:
        if not _is_sqlite_database(database):
            raise TypeError("database must be a SQLiteDatabase")
        if type(data_root_identity) is not DataRootIdentity:
            raise TypeError("data_root_identity must be a DataRootIdentity")
        if database.data_root_identity is not data_root_identity:
            raise ValueError("database and reader service must share one root identity")
        if (
            library_root_identity is not None
            and type(library_root_identity) is not DataRootIdentity
        ):
            raise TypeError("library_root_identity must be a DataRootIdentity or None")
        if type(limits) is not ReaderAssetLimits:
            raise TypeError("limits must be an explicit ReaderAssetLimits")
        self._database = database
        self._data_root_identity = data_root_identity
        self._library_root_identity = library_root_identity or data_root_identity
        self._limits = limits
        self._repository = repository or ChapterCbzAssetRepository()
        self._verified_files: OrderedDict[tuple[object, ...], float] = OrderedDict()
        self._verification_lock = threading.RLock()

    def index_verified_cbz(
        self,
        *,
        job_id: UUID,
        chapter_id: UUID,
        result: CBZBuildResult,
    ) -> IndexedCbzAsset:
        self._validate_uuid4(job_id, "job_id")
        self._validate_uuid4(chapter_id, "chapter_id")
        if type(result) is not CBZBuildResult:
            raise TypeError("result must be a CBZBuildResult returned after verification")
        if (
            type(result.page_count) is not int
            or result.page_count <= 0
            or type(result.size_bytes) is not int
            or result.size_bytes <= 0
            or type(result.sha256) is not str
            or _SHA256_PATTERN.fullmatch(result.sha256) is None
        ):
            raise ReaderAssetError("The verified CBZ result metadata is invalid.")
        if result.size_bytes > self._limits.max_cbz_bytes:
            raise ReaderAssetError("The CBZ exceeds the approved V1 size limit.")

        relative_path = self._relative_path_for_result(result.path)
        snapshot = self._read_index_snapshot(job_id, chapter_id)
        if snapshot.existing_link is not None:
            old_asset = snapshot.existing_asset
            old_link = snapshot.existing_link
            if (
                old_asset is None
                or old_link.download_job_id != job_id
                or old_link.page_count != result.page_count
                or old_asset.asset_type is not AssetType.CBZ
                or old_asset.relative_path != relative_path
                or old_asset.sha256 != result.sha256
                or old_asset.size_bytes != result.size_bytes
            ):
                raise ReaderAssetConflict("The chapter already has a different CBZ asset.")
            self._verify_archive_file(
                old_asset.relative_path,
                expected_sha256=old_asset.sha256,
                expected_size_bytes=old_asset.size_bytes,
                page_expectations=snapshot.page_expectations,
                expected_page_count=old_link.page_count,
            )
            return IndexedCbzAsset(old_asset, old_link.page_count)

        if not snapshot.page_expectations:
            raise ReaderAssetError("The job has no validated pages to index.")
        if result.page_count != len(snapshot.page_expectations):
            raise ReaderAssetError("The CBZ page count does not match the verified job pages.")

        self._verify_archive_file(
            relative_path,
            expected_sha256=result.sha256,
            expected_size_bytes=result.size_bytes,
            page_expectations=snapshot.page_expectations,
            expected_page_count=result.page_count,
        )

        indexed_at = datetime.now(UTC)
        asset = Asset.new_local(
            AssetType.CBZ,
            relative_path,
            result.sha256,
            result.size_bytes,
            created_at=indexed_at,
            verified_at=indexed_at,
        )
        link = ChapterCbzAsset.new_local(
            chapter_id,
            asset.asset_id,
            job_id,
            result.page_count,
            indexed_at=indexed_at,
        )
        updated_at_us = max(
            datetime_to_epoch_us(indexed_at),
            snapshot.job_updated_at_us,
        )

        assert_transition_allowed(DownloadJobStatus.INDEXING, DownloadJobStatus.COMPLETED)
        with self._database.write_session() as session:
            current = self._load_index_snapshot(session, job_id, chapter_id)
            if (
                current.existing_link is not None
                or current.job_updated_at_us != snapshot.job_updated_at_us
                or current.page_expectations != snapshot.page_expectations
            ):
                raise ReaderAssetConflict("The job changed while its CBZ was being verified.")
            session.add(asset_to_row(asset))
            self._repository.add(session, link)
            changed = cast(
                CursorResult[object],
                session.execute(
                    update(DownloadJobRow)
                    .where(
                        DownloadJobRow.job_id == str(job_id),
                        DownloadJobRow.status == DownloadJobStatus.INDEXING.value,
                        DownloadJobRow.updated_at_us == snapshot.job_updated_at_us,
                        DownloadJobRow.cancel_requested_at_us.is_(None),
                        DownloadJobRow.recovery_blocked.is_(False),
                    )
                    .values(
                        status=DownloadJobStatus.COMPLETED.value,
                        updated_at_us=updated_at_us,
                    )
                ),
            )
            if changed.rowcount != 1:
                raise ReaderAssetConflict("The job state changed before CBZ indexing committed.")
            session.flush()
        return IndexedCbzAsset(asset, result.page_count)

    def list_chapters_for_series(
        self,
        series_id: UUID,
        *,
        limit: int = DEFAULT_READER_PAGE_SIZE,
        offset: int = 0,
        order: Literal["asc", "desc"] = "asc",
    ) -> ReaderChapterPage:
        self._validate_uuid4(series_id, "series_id")
        if type(limit) is not int or not 1 <= limit <= MAX_READER_PAGE_SIZE:
            raise ValueError("limit must be between 1 and 500")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if order not in {"asc", "desc"}:
            raise ValueError("order must be asc or desc")
        with self._database.read_session() as session:
            records, total = self._repository.list_ready_chapters(
                session,
                series_id,
                limit=limit,
                offset=offset,
                descending=order == "desc",
            )
        return ReaderChapterPage(
            items=tuple(self._public_chapter(record) for record in records),
            total=total,
            limit=limit,
            offset=offset,
        )

    def get_chapter(self, chapter_id: UUID) -> ReaderChapter | None:
        self._validate_uuid4(chapter_id, "chapter_id")
        with self._database.read_session() as session:
            record = self._repository.get_reader_chapter(session, chapter_id)
        return self._public_chapter(record) if record is not None else None

    def get_ready_chapter_records_by_ids(
        self,
        chapter_ids: tuple[UUID, ...],
    ) -> tuple[ReaderChapterRecord, ...]:
        if (
            type(chapter_ids) is not tuple
            or not 1 <= len(chapter_ids) <= 10_000
            or any(
                type(chapter_id) is not UUID or chapter_id.version != 4
                for chapter_id in chapter_ids
            )
            or len(set(chapter_ids)) != len(chapter_ids)
        ):
            raise ValueError("chapter_ids must be unique UUIDv4 values within the bounded limit")
        with self._database.read_session() as session:
            records = self._repository.list_ready_chapters_by_ids(session, chapter_ids)
        if len(records) != len(chapter_ids):
            raise ReaderAssetNotFound("One or more selected chapters are not ready to export.")
        return records

    def open_page(self, chapter_id: UUID, page_number: int) -> ReaderPageStream:
        self._validate_uuid4(chapter_id, "chapter_id")
        if type(page_number) is not int or page_number <= 0:
            raise ReaderAssetNotFound("The requested reader page is unavailable.")
        with self._database.read_session() as session:
            chapter = self._repository.get_reader_chapter(session, chapter_id)
            if chapter is None or page_number > chapter.page_count:
                raise ReaderAssetNotFound("The requested reader page is unavailable.")
            page = self._repository.get_reader_page(session, chapter_id, page_number)
        if page is None:
            raise ReaderAssetNotFound("The requested reader page is unavailable.")
        if page.page_mime not in _MIME_EXTENSIONS:
            raise ReaderAssetError("The indexed page MIME type is unsupported.")
        if page.page_size_bytes > self._limits.max_page_bytes:
            raise ReaderAssetError("The page exceeds the approved V1 size limit.")

        stream = self._open_verified_asset(
            page.asset.relative_path,
            expected_sha256=page.asset.sha256,
            expected_size_bytes=page.asset.size_bytes,
            root_identity=self._library_root_identity,
        )
        archive: zipfile.ZipFile | None = None
        try:
            archive = zipfile.ZipFile(stream, mode="r")
            member_name = self._member_name(page.page_ordinal, page.page_mime)
            infos = archive.infolist()
            names = [info.filename for info in infos]
            expected_prefix = "ComicInfo.xml"
            if (
                len(names) != page.link.page_count + 1
                or len(names) != len(set(names))
                or not names
                or names[0] != expected_prefix
                or member_name not in names[1:]
            ):
                raise ReaderAssetError("The indexed CBZ structure is invalid.")
            info = archive.getinfo(member_name)
            if info.file_size != page.page_size_bytes:
                raise ReaderAssetError("The indexed page size does not match the CBZ.")
            self._verify_zip_member(
                archive,
                member_name,
                expected_sha256=page.page_sha256,
                expected_size_bytes=page.page_size_bytes,
            )
            return ReaderPageStream(
                media_type=page.page_mime,
                sha256=page.page_sha256,
                size_bytes=page.page_size_bytes,
                archive=archive,
                file_stream=stream,
                member=member_name,
                chunk_bytes=self._limits.chunk_bytes,
            )
        except ReaderAssetError:
            if archive is not None:
                archive.close()
            stream.close()
            raise
        except Exception:
            if archive is not None:
                archive.close()
            stream.close()
            raise ReaderAssetError("The indexed CBZ page could not be opened.") from None

    def iter_verified_chapter_pages(
        self,
        chapter_id: UUID,
    ) -> Generator[tuple[ReaderPageRecord, bytes]]:
        """Read a chapter's verified CBZ once and yield bounded pages in order."""
        self._validate_uuid4(chapter_id, "chapter_id")
        with self._database.read_session() as session:
            chapter = self._repository.get_reader_chapter(session, chapter_id)
            pages = self._repository.list_reader_pages(session, chapter_id)
        if chapter is None or not pages:
            raise ReaderAssetNotFound("The requested verified chapter is unavailable.")
        first_page = pages[0]
        if len(pages) != chapter.page_count or any(
            page.page_ordinal != ordinal
            or page.link.chapter_id != chapter.chapter_id
            or page.link.page_count != chapter.page_count
            or page.link.asset_id != first_page.link.asset_id
            or page.asset.asset_id != first_page.asset.asset_id
            or page.page_mime not in _MIME_EXTENSIONS
            or page.page_size_bytes > self._limits.max_page_bytes
            for ordinal, page in enumerate(pages, start=1)
        ):
            raise ReaderAssetError("The indexed chapter pages are incomplete or inconsistent.")

        stream = self._open_verified_asset(
            first_page.asset.relative_path,
            expected_sha256=first_page.asset.sha256,
            expected_size_bytes=first_page.asset.size_bytes,
            root_identity=self._library_root_identity,
        )
        archive: zipfile.ZipFile | None = None
        try:
            archive = zipfile.ZipFile(stream, mode="r")
            infos = archive.infolist()
            names = [info.filename for info in infos]
            expected_names = [
                "ComicInfo.xml",
                *(self._member_name(page.page_ordinal, page.page_mime) for page in pages),
            ]
            if names != expected_names or len(names) != len(set(names)):
                raise ReaderAssetError("The indexed CBZ structure is invalid.")
            comic_info = archive.getinfo("ComicInfo.xml")
            self._verify_comic_info_page_count(archive, comic_info, chapter.page_count)
            for page, member_name in zip(pages, expected_names[1:], strict=True):
                info = archive.getinfo(member_name)
                if info.file_size != page.page_size_bytes:
                    raise ReaderAssetError("The indexed page size does not match the CBZ.")
                with archive.open(info, mode="r") as member:
                    content = member.read(self._limits.max_page_bytes + 1)
                    if len(content) > self._limits.max_page_bytes or member.read(1):
                        raise ReaderAssetError("The page exceeds the approved V1 size limit.")
                if (
                    len(content) != page.page_size_bytes
                    or hashlib.sha256(content).hexdigest() != page.page_sha256
                ):
                    raise ReaderAssetError("The indexed page does not match its CBZ content.")
                yield page, content
        except ReaderAssetError:
            raise
        except Exception:
            raise ReaderAssetError("The indexed CBZ pages could not be read.") from None
        finally:
            if archive is not None:
                archive.close()
            stream.close()

    def _read_index_snapshot(self, job_id: UUID, chapter_id: UUID) -> _IndexSnapshot:
        with self._database.read_session() as session:
            return self._load_index_snapshot(session, job_id, chapter_id)

    def _load_index_snapshot(
        self,
        session: Session,
        job_id: UUID,
        chapter_id: UUID,
    ) -> _IndexSnapshot:
        job = session.get(DownloadJobRow, str(job_id))
        if job is None:
            raise ReaderAssetNotFound("The download job for this CBZ was not found.")
        chapter = session.get(ChapterRow, str(chapter_id))
        if chapter is None:
            raise ReaderAssetNotFound("The chapter for this CBZ was not found.")
        chapter_source_id = session.execute(
            select(ChapterSourceRow.chapter_id).where(
                ChapterSourceRow.chapter_source_id == job.chapter_source_id
            )
        ).scalar_one_or_none()
        if chapter_source_id != str(chapter_id):
            raise ReaderAssetError("The download job does not belong to this chapter.")
        if job.recovery_blocked:
            raise ReaderAssetError("The download job is blocked by recovery state.")
        if job.cancel_requested_at_us is not None:
            raise ReaderAssetError("The download job has a pending cancellation.")
        manifest_state = session.get(DownloadManifestStateRow, str(job_id))
        if manifest_state is not None and manifest_state.publication_pending:
            raise ReaderAssetError("The download job has pending manifest publication.")

        existing_link_asset = self._repository.get_for_chapter(session, chapter_id)
        existing_link: ChapterCbzAsset | None = None
        existing_asset: Asset | None = None
        if existing_link_asset is not None:
            existing_link, existing_asset = existing_link_asset
            if existing_link.download_job_id != job_id:
                raise ReaderAssetConflict("The chapter already has a CBZ from another job.")
            if job.status != DownloadJobStatus.COMPLETED.value:
                raise ReaderAssetConflict("The chapter already has a nonterminal CBZ link.")
        elif job.status != DownloadJobStatus.INDEXING.value:
            raise ReaderAssetError("Only an INDEXING download job can be published to the library.")

        rows = (
            session.execute(
                select(DownloadPageRow)
                .where(DownloadPageRow.job_id == str(job_id))
                .order_by(DownloadPageRow.ordinal.asc())
            )
            .scalars()
            .all()
        )
        expectations: list[_PageExpectation] = []
        for expected_ordinal, row in enumerate(rows, start=1):
            if (
                row.ordinal != expected_ordinal
                or row.status != "VALIDATED"
                or type(row.size_bytes) is not int
                or row.size_bytes <= 0
                or row.size_bytes > self._limits.max_page_bytes
                or type(row.sha256) is not str
                or _SHA256_PATTERN.fullmatch(row.sha256) is None
                or row.mime not in _MIME_EXTENSIONS
            ):
                raise ReaderAssetError("The job pages are incomplete or not fully validated.")
            expectations.append(
                _PageExpectation(
                    ordinal=row.ordinal,
                    size_bytes=row.size_bytes,
                    sha256=row.sha256,
                    mime=row.mime,
                )
            )
        return _IndexSnapshot(
            job_updated_at_us=job.updated_at_us,
            page_expectations=tuple(expectations),
            existing_asset=existing_asset,
            existing_link=existing_link,
        )

    def _verify_archive_file(
        self,
        relative_path: str,
        *,
        expected_sha256: str,
        expected_size_bytes: int,
        page_expectations: tuple[_PageExpectation, ...],
        expected_page_count: int,
    ) -> None:
        if expected_size_bytes > self._limits.max_cbz_bytes:
            raise ReaderAssetError("The CBZ exceeds the approved V1 size limit.")
        stream = self._open_verified_asset(
            relative_path,
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size_bytes,
            root_identity=self._library_root_identity,
        )
        try:
            with zipfile.ZipFile(stream, mode="r") as archive:
                infos = archive.infolist()
                expected_names = [
                    "ComicInfo.xml",
                    *(self._member_name(page.ordinal, page.mime) for page in page_expectations),
                ]
                names = [info.filename for info in infos]
                if (
                    expected_page_count != len(page_expectations)
                    or names != expected_names
                    or len(names) != len(set(names))
                ):
                    raise ReaderAssetError("The verified CBZ structure is inconsistent.")
                comic_info = archive.getinfo("ComicInfo.xml")
                self._verify_comic_info_page_count(archive, comic_info, expected_page_count)
                for page, info in zip(page_expectations, infos[1:], strict=True):
                    if info.file_size != page.size_bytes:
                        raise ReaderAssetError("The CBZ page size differs from its DB metadata.")
                    self._verify_zip_member(
                        archive,
                        info.filename,
                        expected_sha256=page.sha256,
                        expected_size_bytes=page.size_bytes,
                    )
        except ReaderAssetError:
            raise
        except Exception:
            raise ReaderAssetError("The CBZ could not be verified from disk.") from None
        finally:
            stream.close()

    def _verify_comic_info_page_count(
        self,
        archive: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        expected_page_count: int,
    ) -> None:
        if info.file_size > self._limits.max_cbz_bytes:
            raise ReaderAssetError("ComicInfo.xml exceeds the bounded CBZ size.")
        try:
            root_tag: str | None = None
            page_count_text: str | None = None
            page_count_seen = False
            element_stack: list[ET.Element] = []
            with archive.open(info, mode="r") as source:
                for event, element in ET.iterparse(source, events=("start", "end")):
                    if event == "start":
                        if root_tag is None:
                            root_tag = str(element.tag)
                        element_stack.append(element)
                        continue
                    if element.tag == "PageCount":
                        if page_count_seen:
                            raise ReaderAssetError("ComicInfo.xml has duplicate PageCount fields.")
                        page_count_seen = True
                        page_count_text = element.text
                    if not element_stack or element_stack[-1] is not element:
                        raise ReaderAssetError("ComicInfo.xml element nesting is invalid.")
                    element_stack.pop()
                    if element_stack:
                        element_stack[-1].remove(element)
            if root_tag != "ComicInfo" or page_count_text != str(expected_page_count):
                raise ReaderAssetError("ComicInfo.xml page count does not match the CBZ.")
        except ReaderAssetError:
            raise
        except Exception:
            raise ReaderAssetError("ComicInfo.xml could not be verified.") from None

    def _verify_zip_member(
        self,
        archive: zipfile.ZipFile,
        member_name: str,
        *,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> None:
        if expected_size_bytes > self._limits.max_page_bytes:
            raise ReaderAssetError("A CBZ page exceeds the approved V1 page limit.")
        hasher = hashlib.sha256()
        byte_count = 0
        try:
            with archive.open(member_name, mode="r") as member:
                while chunk := member.read(self._limits.chunk_bytes):
                    byte_count += len(chunk)
                    if byte_count > self._limits.max_page_bytes:
                        raise ReaderAssetError("A CBZ page exceeds the approved V1 page limit.")
                    hasher.update(chunk)
        except ReaderAssetError:
            raise
        except Exception:
            raise ReaderAssetError("A CBZ page failed integrity verification.") from None
        if byte_count != expected_size_bytes or hasher.hexdigest() != expected_sha256:
            raise ReaderAssetError("A CBZ page does not match its verified job metadata.")

    def open_verified_cover(self, asset: Asset, *, max_bytes: int) -> BinaryIO:
        """Open a typed, verified original without trusting a database path alone."""
        if (
            asset.asset_type is not AssetType.COVER
            or asset.verified_at is None
            or not 0 < asset.size_bytes <= max_bytes
        ):
            raise ReaderAssetError("The cover asset is unverified or exceeds its limit.")
        return self._open_verified_asset(
            asset.relative_path,
            expected_sha256=asset.sha256,
            expected_size_bytes=asset.size_bytes,
        )

    def _asset_location(
        self, relative_path: str, root_identity: DataRootIdentity | None
    ) -> tuple[Path, DataRootIdentity]:
        validate_relative_path(relative_path)
        identity = root_identity or self._data_root_identity
        if root_identity is self._library_root_identity and relative_path.startswith(
            "library/chapters/"
        ):
            identity = self._data_root_identity
        identity.assert_current()
        candidate = identity.canonical_path
        components = relative_path.split("/")
        for index, component in enumerate(components):
            candidate /= component
            info = candidate.lstat()
            if self._is_reparse_point(info):
                raise ReaderAssetError("The indexed asset path traverses a reparse point.")
            if index < len(components) - 1:
                if not stat.S_ISDIR(info.st_mode):
                    raise ReaderAssetError("The indexed asset parent is not a directory.")
            elif not stat.S_ISREG(info.st_mode):
                raise ReaderAssetError("The indexed asset is not a regular file.")
        return candidate, identity

    def asset_available(self, asset: Asset) -> bool:
        """Presence and size only; the reader still verifies contents before serving a page."""
        try:
            path, identity = self._asset_location(asset.relative_path, self._library_root_identity)
            available = path.stat().st_size == asset.size_bytes
            identity.assert_current()
            return available
        except OSError, ValueError, ReaderAssetError:
            return False

    def _open_verified_asset(
        self,
        relative_path: str,
        *,
        expected_sha256: str,
        expected_size_bytes: int,
        root_identity: DataRootIdentity | None = None,
    ) -> BinaryIO:
        try:
            candidate, identity = self._asset_location(relative_path, root_identity)
            stream = candidate.open("rb")
            descriptor_info = os.fstat(stream.fileno())
            if (
                self._is_reparse_point(descriptor_info)
                or not stat.S_ISREG(descriptor_info.st_mode)
                or descriptor_info.st_size != expected_size_bytes
                or descriptor_info.st_size > self._limits.max_cbz_bytes
            ):
                stream.close()
                raise ReaderAssetError("The indexed asset file identity or size changed.")
            key = (
                str(candidate),
                expected_sha256,
                descriptor_info.st_dev,
                descriptor_info.st_ino,
                descriptor_info.st_size,
                descriptor_info.st_mtime_ns,
                descriptor_info.st_ctime_ns,
            )
            with self._verification_lock:
                # shortcut: stat-preserving edits expire in 30s; pages keep their own hashes.
                if self._verified_files.get(key, 0) <= time.monotonic():
                    digest = hashlib.sha256()
                    byte_count = 0
                    while chunk := stream.read(self._limits.chunk_bytes):
                        digest.update(chunk)
                        byte_count += len(chunk)
                        if byte_count > self._limits.max_cbz_bytes:
                            stream.close()
                            raise ReaderAssetError(
                                "The indexed asset exceeds the approved V1 size limit."
                            )
                    if byte_count != expected_size_bytes or digest.hexdigest() != expected_sha256:
                        stream.close()
                        raise ReaderAssetError("The indexed asset hash or size changed.")
                    after = os.fstat(stream.fileno())
                    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                        descriptor_info.st_size,
                        descriptor_info.st_mtime_ns,
                        descriptor_info.st_ctime_ns,
                    ):
                        stream.close()
                        raise ReaderAssetError("The indexed asset changed during verification.")
                    self._verified_files[key] = time.monotonic() + 30
                self._verified_files.move_to_end(key)
                if len(self._verified_files) > 256:
                    self._verified_files.popitem(last=False)
            identity.assert_current()
            stream.seek(0)
            return stream
        except ReaderAssetError:
            raise
        except Exception:
            raise ReaderAssetError(
                "The indexed asset is unavailable under its configured root."
            ) from None

    def _relative_path_for_result(self, path: object) -> str:
        if not isinstance(path, Path) or not path.is_absolute():
            raise ReaderAssetError("The CBZ result path is invalid.")
        root = self._library_root_identity.canonical_path
        try:
            relative = path.relative_to(root)
        except ValueError:
            raise ReaderAssetError(
                "The CBZ result is outside the configured library root."
            ) from None
        value = "/".join(relative.parts)
        try:
            validate_relative_path(value)
        except ValueError:
            raise ReaderAssetError("The CBZ result path is not a safe relative path.") from None
        return value

    @staticmethod
    def _member_name(ordinal: int, mime: str) -> str:
        extension = _MIME_EXTENSIONS.get(mime)
        if extension is None:
            raise ReaderAssetError("The indexed page MIME type is unsupported.")
        return f"{ordinal:03d}.{extension}"

    @staticmethod
    def _is_reparse_point(info: os.stat_result) -> bool:
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        attributes = getattr(info, "st_file_attributes", 0)
        return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_attribute)

    @staticmethod
    def _validate_uuid4(value: UUID, field_name: str) -> None:
        if type(value) is not UUID or value.version != 4:
            raise ValueError(f"{field_name} must be a UUIDv4")

    @staticmethod
    def _public_chapter(record: ReaderChapterRecord) -> ReaderChapter:
        return ReaderChapter(
            chapter_id=record.chapter_id,
            series_id=record.series_id,
            display_label=record.display_label,
            page_count=record.page_count,
        )
