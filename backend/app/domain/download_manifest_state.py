import re
from dataclasses import dataclass
from uuid import UUID

from app.domain.job_manifest import MAX_MANIFEST_BYTES

MAX_SQLITE_INTEGER = (1 << 63) - 1
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class DownloadManifestState:
    """Durable publication generation and intent for one job manifest."""

    job_id: UUID
    generation: int
    manifest_sha256: str
    manifest_size_bytes: int
    previous_sha256: str | None
    publication_pending: bool

    def __post_init__(self) -> None:
        if type(self.job_id) is not UUID or self.job_id.version != 4:
            raise ValueError("job_id must be a UUIDv4")
        if (
            type(self.generation) is not int
            or not 1 <= self.generation <= MAX_SQLITE_INTEGER
        ):
            raise ValueError("generation must be a positive SQLite integer")
        if (
            type(self.manifest_sha256) is not str
            or _SHA256_PATTERN.fullmatch(self.manifest_sha256) is None
        ):
            raise ValueError(
                "manifest_sha256 must be 64 lowercase hexadecimal characters"
            )
        if (
            type(self.manifest_size_bytes) is not int
            or not 1 <= self.manifest_size_bytes <= MAX_MANIFEST_BYTES
        ):
            raise ValueError("manifest_size_bytes is outside the supported manifest limit")
        if self.previous_sha256 is not None and (
            type(self.previous_sha256) is not str
            or _SHA256_PATTERN.fullmatch(self.previous_sha256) is None
        ):
            raise ValueError(
                "previous_sha256 must be 64 lowercase hexadecimal characters or None"
            )
        if type(self.publication_pending) is not bool:
            raise ValueError("publication_pending must be a boolean")
