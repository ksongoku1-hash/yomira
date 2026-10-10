import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.timestamps import normalise_utc
from app.errors import ErrorCode

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_EXPORT_BYTES = 8 * 1024 * 1024 * 1024


class ExportJobStatus(StrEnum):
    QUEUED = "QUEUED"
    EXPORTING = "EXPORTING"
    EXPORTED = "EXPORTED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"

    @property
    def is_terminal(self) -> bool:
        return self not in {self.QUEUED, self.EXPORTING}


@dataclass(frozen=True, slots=True)
class ExportJob:
    export_id: UUID
    idempotency_key_hash: str
    request_fingerprint: str
    asset_id: UUID
    target_id: str
    target_label: str
    file_system: str
    destination_name: str
    expected_size_bytes: int
    expected_sha256: str
    status: ExportJobStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    terminal_at: datetime | None
    trace_id: str
    error_code: str | None = None
    failure_stage: str | None = None
    retryable: bool | None = None
    cause_type: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("export_id", "asset_id"):
            value = getattr(self, field_name)
            if type(value) is not UUID or value.version != 4:
                raise ValueError(f"{field_name} must be a UUIDv4")
        try:
            object.__setattr__(self, "status", ExportJobStatus(self.status))
        except (TypeError, ValueError) as error:
            raise ValueError("status must be a supported ExportJobStatus") from error

        for field_name in ("idempotency_key_hash", "request_fingerprint", "expected_sha256"):
            value = getattr(self, field_name)
            if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")

        _validate_bounded_text(self.target_id, "target_id", 128)
        _validate_bounded_text(self.target_label, "target_label", 128)
        _validate_bounded_text(self.file_system, "file_system", 32)
        _validate_bounded_text(self.destination_name, "destination_name", 255)
        if any(character in self.destination_name for character in ("/", "\\", ":")):
            raise ValueError("destination_name must be a single relative filename")
        if self.destination_name in {".", ".."}:
            raise ValueError("destination_name must not be a dot component")
        if (
            type(self.expected_size_bytes) is not int
            or not 1 <= self.expected_size_bytes <= _MAX_EXPORT_BYTES
        ):
            raise ValueError("expected_size_bytes must be within the export size limit")

        object.__setattr__(self, "created_at", normalise_utc(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", normalise_utc(self.updated_at, "updated_at"))
        if self.started_at is not None:
            object.__setattr__(self, "started_at", normalise_utc(self.started_at, "started_at"))
        if self.terminal_at is not None:
            object.__setattr__(self, "terminal_at", normalise_utc(self.terminal_at, "terminal_at"))
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at must not precede created_at")
        if self.started_at is not None and self.started_at > self.updated_at:
            raise ValueError("started_at must not follow updated_at")
        if self.terminal_at is not None and self.terminal_at > self.updated_at:
            raise ValueError("terminal_at must not follow updated_at")
        if self.status.is_terminal != (self.terminal_at is not None):
            raise ValueError("terminal_at must be present only for terminal jobs")

        if type(self.trace_id) is not str:
            raise ValueError("trace_id must be a canonical UUID")
        try:
            trace_uuid = UUID(self.trace_id)
        except ValueError as error:
            raise ValueError("trace_id must be a canonical UUID") from error
        if trace_uuid.version != 4 or str(trace_uuid) != self.trace_id:
            raise ValueError("trace_id must be a canonical UUIDv4")

        failure_values = (
            self.error_code,
            self.failure_stage,
            self.retryable,
            self.cause_type,
        )
        if self.status in {ExportJobStatus.FAILED, ExportJobStatus.INTERRUPTED}:
            if (
                self.error_code is None
                or self.failure_stage is None
                or self.retryable is None
                or self.cause_type is None
            ):
                raise ValueError("failed and interrupted jobs require sanitized failure details")
            try:
                parsed_error_code = ErrorCode(self.error_code)
            except ValueError as error:
                raise ValueError("error_code must be a registered Yomira error") from error
            if self.status is ExportJobStatus.INTERRUPTED:
                if parsed_error_code is not ErrorCode.SYS_STARTUP_FAILED:
                    raise ValueError("interrupted jobs require the startup recovery error code")
            elif not parsed_error_code.value.startswith("EXP_"):
                raise ValueError("failed jobs require an export error code")
            _validate_bounded_text(self.failure_stage, "failure_stage", 64)
            _validate_bounded_text(self.cause_type, "cause_type", 64)
            if type(self.retryable) is not bool:
                raise ValueError("retryable must be a boolean for failed jobs")
        elif any(value is not None for value in failure_values):
            raise ValueError("successful or active jobs must not contain failure details")


def _validate_bounded_text(value: object, name: str, maximum: int) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{name} must contain between 1 and {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters")
