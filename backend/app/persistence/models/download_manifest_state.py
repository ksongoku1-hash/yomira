from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.job_manifest import MAX_MANIFEST_BYTES
from app.persistence.base import PersistenceBase

DOWNLOAD_MANIFEST_SHA256_CHECK = (
    "typeof(manifest_sha256) = 'text' "
    "AND length(manifest_sha256) = 64 "
    "AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'"
)
DOWNLOAD_MANIFEST_PREVIOUS_SHA256_CHECK = (
    "previous_sha256 IS NULL OR ("
    "typeof(previous_sha256) = 'text' "
    "AND length(previous_sha256) = 64 "
    "AND previous_sha256 NOT GLOB '*[^0-9a-f]*')"
)


class DownloadManifestStateRow(PersistenceBase):
    __tablename__ = "download_manifest_state"
    __table_args__ = (
        CheckConstraint(
            "typeof(generation) = 'integer' "
            "AND generation BETWEEN 1 AND 9223372036854775807",
            name="generation_positive_sqlite_integer",
        ),
        CheckConstraint(
            DOWNLOAD_MANIFEST_SHA256_CHECK,
            name="manifest_sha256_valid",
        ),
        CheckConstraint(
            "typeof(manifest_size_bytes) = 'integer' "
            f"AND manifest_size_bytes BETWEEN 1 AND {MAX_MANIFEST_BYTES}",
            name="manifest_size_bounded_integer",
        ),
        CheckConstraint(
            DOWNLOAD_MANIFEST_PREVIOUS_SHA256_CHECK,
            name="previous_sha256_valid",
        ),
        CheckConstraint(
            "typeof(publication_pending) = 'integer' AND publication_pending IN (0, 1)",
            name="publication_pending_boolean",
        ),
    )

    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "download_jobs.job_id",
            ondelete="RESTRICT",
            name="fk_download_manifest_state_job_id",
        ),
        primary_key=True,
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_size_bytes: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    previous_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    publication_pending: Mapped[bool] = mapped_column(Boolean(), nullable=False)
