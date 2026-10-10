"""A real indexed archive for tests that need a completed, readable download."""

import base64
import hashlib
import zipfile
from uuid import UUID

from sqlalchemy import select

from app.persistence.database import SQLiteDatabase
from app.persistence.models.chapter_source import ChapterSourceRow
from app.persistence.models.download_job import DownloadJobRow
from app.persistence.models.download_page import DownloadPageRow
from app.services.cbz_builder import CBZBuildResult
from app.services.reader_asset import (
    V1_MAX_CBZ_BYTES,
    V1_MAX_PAGE_BYTES,
    V1_STREAM_CHUNK_BYTES,
    ReaderAssetLimits,
    ReaderAssetService,
)


def index_test_job(database: SQLiteDatabase, job_id: UUID) -> None:
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l5sAAAAASUVORK5CYII="
    )
    with database.write_session() as session:
        job = session.get(DownloadJobRow, str(job_id))
        assert job is not None
        source = session.get(ChapterSourceRow, job.chapter_source_id)
        assert source is not None
        chapter_id = UUID(source.chapter_id)
        job.status = "INDEXING"
        assert not session.scalar(
            select(DownloadPageRow).where(DownloadPageRow.job_id == str(job_id))
        )
        session.add(
            DownloadPageRow(
                job_id=str(job_id),
                ordinal=1,
                remote_reference="fixture",
                status="VALIDATED",
                size_bytes=len(image),
                sha256=hashlib.sha256(image).hexdigest(),
                mime="image/png",
                attempts=0,
            )
        )
    path = database.data_root_identity.canonical_path / "library/chapters" / f"{chapter_id}.cbz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ComicInfo.xml", "<ComicInfo><PageCount>1</PageCount></ComicInfo>")
        archive.writestr("001.png", image)
    body = path.read_bytes()
    reader = ReaderAssetService(
        database,
        data_root_identity=database.data_root_identity,
        limits=ReaderAssetLimits(
            max_cbz_bytes=V1_MAX_CBZ_BYTES,
            max_page_bytes=V1_MAX_PAGE_BYTES,
            chunk_bytes=V1_STREAM_CHUNK_BYTES,
        ),
    )
    reader.index_verified_cbz(
        job_id=job_id,
        chapter_id=chapter_id,
        result=CBZBuildResult(
            path=path, sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body), page_count=1
        ),
    )
