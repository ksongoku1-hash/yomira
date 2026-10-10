"""Open preserved K116 fixtures in a fresh process and read an indexed page."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast
from uuid import UUID

BACKEND_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = BACKEND_ROOT / "tests"
for import_root in (str(BACKEND_ROOT), str(TESTS_ROOT)):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from support.sqlite import open_test_database  # noqa: E402

from app.domain.download_job import DownloadJobStatus  # noqa: E402
from app.persistence.models.chapter_cbz_asset import ChapterCbzAssetRow  # noqa: E402
from app.persistence.models.download_page import DownloadPageRow  # noqa: E402
from app.persistence.repositories.download_job import DownloadJobRepository  # noqa: E402
from app.services.reader_asset import (  # noqa: E402
    V1_MAX_CBZ_BYTES,
    V1_MAX_PAGE_BYTES,
    V1_STREAM_CHUNK_BYTES,
    ReaderAssetLimits,
    ReaderAssetService,
)


def verify(artifact_root: Path) -> dict[str, Any]:
    run_directories = sorted(
        path
        for path in artifact_root.glob("k116-loopback-*")
        if (path / "metrics.json").is_file()
    )
    if not run_directories:
        raise RuntimeError("No completed K116 smoke reports were found.")

    run_evidence: list[dict[str, Any]] = []
    for run_directory in run_directories:
        metrics_path = run_directory / "metrics.json"
        metrics: dict[str, Any] = json.loads(metrics_path.read_text(encoding="utf-8"))
        data_root = Path(metrics["data_root"])
        job_id_values: object = metrics.get("job_ids")
        if not isinstance(job_id_values, list):
            observed_value: object = metrics.get("observed", {})
            if not isinstance(observed_value, dict):
                raise RuntimeError("K116 observed metrics must be a JSON object.")
            observed = cast(dict[str, Any], observed_value)
            statuses_value: object = observed.get(
                "job_statuses_after_dispatcher_stop",
                {},
            )
            if not isinstance(statuses_value, dict):
                raise RuntimeError("K116 dispatcher statuses must be a JSON object.")
            statuses = cast(dict[str, Any], statuses_value)
            job_id_values = list(statuses)
        job_ids: list[str] = []
        for job_id_value in cast(list[object], job_id_values):
            if not isinstance(job_id_value, str):
                raise RuntimeError("K116 job IDs must be JSON strings.")
            job_ids.append(job_id_value)
        integrity_jobs = {
            item["job_id"]: item
            for item in metrics.get("integrity", {}).get("jobs", [])
        }
        database = open_test_database(data_root)
        jobs: list[dict[str, Any]] = []
        try:
            reader = ReaderAssetService(
                database,
                data_root_identity=database.data_root_identity,
                limits=ReaderAssetLimits(
                    max_cbz_bytes=V1_MAX_CBZ_BYTES,
                    max_page_bytes=V1_MAX_PAGE_BYTES,
                    chunk_bytes=V1_STREAM_CHUNK_BYTES,
                ),
            )
            job_repository = DownloadJobRepository()
            for job_id_text in job_ids:
                job_id = UUID(job_id_text)
                with database.read_session() as session:
                    job = job_repository.get(session, job_id)
                    link = session.query(ChapterCbzAssetRow).filter_by(
                        download_job_id=str(job_id)
                    ).one()
                    chapter_id = UUID(link.chapter_id)
                    expected_pages = link.page_count
                    page_rows = (
                        session.query(DownloadPageRow)
                        .filter_by(job_id=str(job_id))
                        .order_by(DownloadPageRow.ordinal)
                        .all()
                    )
                if job is None or job.status is not DownloadJobStatus.COMPLETED:
                    raise AssertionError(f"reopened job {job_id} is not COMPLETED")
                if len(page_rows) != expected_pages or any(
                    page.status != "VALIDATED"
                    or page.size_bytes is None
                    or page.sha256 is None
                    for page in page_rows
                ):
                    raise AssertionError(f"reopened page manifest is incomplete for {job_id}")
                chapter = reader.get_chapter(chapter_id)
                if chapter is None or chapter.page_count != expected_pages:
                    raise AssertionError(f"reopened reader index is missing for {job_id}")

                page_number = expected_pages
                stream = reader.open_page(chapter_id, page_number)
                page_bytes = b"".join(stream.iter_chunks())
                actual_sha256 = hashlib.sha256(page_bytes).hexdigest()
                original_job = integrity_jobs.get(str(job_id))
                expected_page = (
                    original_job["pages"][page_number - 1]
                    if original_job is not None
                    else {
                        "size_bytes": page_rows[page_number - 1].size_bytes,
                        "sha256": page_rows[page_number - 1].sha256,
                    }
                )
                if (
                    len(page_bytes) != expected_page["size_bytes"]
                    or actual_sha256 != expected_page["sha256"]
                ):
                    raise AssertionError(f"reopened reader bytes differ for {job_id}")
                jobs.append(
                    {
                        "job_id": str(job_id),
                        "status": job.status.value,
                        "chapter_id": str(chapter_id),
                        "reader_page_count": chapter.page_count,
                        "page_read_after_reopen": page_number,
                        "read_page_size_bytes": len(page_bytes),
                        "read_page_sha256": actual_sha256,
                        "matches_preclose_integrity_evidence": original_job is not None,
                        "matches_durable_manifest_checksum": True,
                    }
                )
        finally:
            database.close()
        run_evidence.append(
            {
                "metrics_path": str(metrics_path),
                "database_closed_by_smoke_before_this_process": True,
                "jobs": jobs,
            }
        )

    return {
        "verification_process_is_separate_from_smoke_process": True,
        "artifact_root": str(artifact_root),
        "verified_runs": len(run_evidence),
        "verified_jobs": sum(len(run["jobs"]) for run in run_evidence),
        "runs": run_evidence,
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_k116_reopen.py ARTIFACT_ROOT")
    artifact_root = Path(sys.argv[1]).resolve()
    report = verify(artifact_root)
    report_path = artifact_root / "reopen_verification.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"report_path": str(report_path), **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
