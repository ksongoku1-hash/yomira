from uuid import UUID

from app.domain.export_job import ExportJob, ExportJobStatus
from app.persistence.mappers.timestamps import datetime_to_epoch_us, epoch_us_to_datetime
from app.persistence.models.export_job import ExportJobRow


def export_job_to_row(job: ExportJob) -> ExportJobRow:
    return ExportJobRow(
        export_id=str(job.export_id),
        idempotency_key_hash=job.idempotency_key_hash,
        request_fingerprint=job.request_fingerprint,
        asset_id=str(job.asset_id),
        target_id=job.target_id,
        target_label=job.target_label,
        file_system=job.file_system,
        destination_name=job.destination_name,
        expected_size_bytes=job.expected_size_bytes,
        expected_sha256=job.expected_sha256,
        status=job.status.value,
        created_at_us=datetime_to_epoch_us(job.created_at),
        updated_at_us=datetime_to_epoch_us(job.updated_at),
        started_at_us=(
            datetime_to_epoch_us(job.started_at) if job.started_at is not None else None
        ),
        terminal_at_us=(
            datetime_to_epoch_us(job.terminal_at) if job.terminal_at is not None else None
        ),
        trace_id=job.trace_id,
        error_code=job.error_code,
        failure_stage=job.failure_stage,
        retryable=job.retryable,
        cause_type=job.cause_type,
    )


def export_job_from_row(row: ExportJobRow) -> ExportJob:
    return ExportJob(
        export_id=UUID(row.export_id),
        idempotency_key_hash=row.idempotency_key_hash,
        request_fingerprint=row.request_fingerprint,
        asset_id=UUID(row.asset_id),
        target_id=row.target_id,
        target_label=row.target_label,
        file_system=row.file_system,
        destination_name=row.destination_name,
        expected_size_bytes=row.expected_size_bytes,
        expected_sha256=row.expected_sha256,
        status=ExportJobStatus(row.status),
        created_at=epoch_us_to_datetime(row.created_at_us),
        updated_at=epoch_us_to_datetime(row.updated_at_us),
        started_at=epoch_us_to_datetime(row.started_at_us)
        if row.started_at_us is not None
        else None,
        terminal_at=(
            epoch_us_to_datetime(row.terminal_at_us) if row.terminal_at_us is not None else None
        ),
        trace_id=row.trace_id,
        error_code=row.error_code,
        failure_stage=row.failure_stage,
        retryable=row.retryable,
        cause_type=row.cause_type,
    )
