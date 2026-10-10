from uuid import UUID

from app.domain.download_job import DownloadJob, DownloadJobStatus, DownloadPriority
from app.persistence.mappers.timestamps import datetime_to_epoch_us, epoch_us_to_datetime
from app.persistence.models.download_job import DownloadJobRow


def download_job_to_row(job: DownloadJob) -> DownloadJobRow:
    return DownloadJobRow(
        job_id=str(job.job_id),
        chapter_source_id=str(job.chapter_source_id),
        status=job.status.value,
        priority=job.priority.value,
        created_at_us=datetime_to_epoch_us(job.created_at),
        updated_at_us=datetime_to_epoch_us(job.updated_at),
        cancel_requested_at_us=(
            datetime_to_epoch_us(job.cancel_requested_at)
            if job.cancel_requested_at is not None
            else None
        ),
        interrupted_from_status=(
            job.interrupted_from_status.value
            if job.interrupted_from_status is not None
            else None
        ),
        recovery_blocked=job.recovery_blocked,
    )


def download_job_from_row(row: DownloadJobRow) -> DownloadJob:
    return DownloadJob(
        job_id=UUID(row.job_id),
        chapter_source_id=UUID(row.chapter_source_id),
        status=DownloadJobStatus(row.status),
        priority=DownloadPriority(row.priority),
        created_at=epoch_us_to_datetime(row.created_at_us),
        updated_at=epoch_us_to_datetime(row.updated_at_us),
        cancel_requested_at=(
            epoch_us_to_datetime(row.cancel_requested_at_us)
            if row.cancel_requested_at_us is not None
            else None
        ),
        interrupted_from_status=(
            DownloadJobStatus(row.interrupted_from_status)
            if row.interrupted_from_status is not None
            else None
        ),
        recovery_blocked=row.recovery_blocked,
    )
