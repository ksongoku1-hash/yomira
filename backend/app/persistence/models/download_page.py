from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase

DOWNLOAD_PAGE_SHA256_CHECK = (
    "typeof(sha256) = 'text' "
    "AND length(sha256) = 64 "
    "AND sha256 = lower(sha256) "
    "AND sha256 NOT GLOB '*[^0-9a-f]*'"
)
DOWNLOAD_PAGE_TEMPORARY_PATH_CHECK = (
    "length(temporary_path) > 0 "
    "AND substr(temporary_path, 1, 1) <> '/' "
    "AND substr(temporary_path, -1, 1) <> '/' "
    "AND instr(temporary_path, char(92)) = 0 "
    "AND instr(temporary_path, ':') = 0 "
    "AND instr(temporary_path, '//') = 0 "
    "AND instr('/' || temporary_path || '/', '/../') = 0 "
    "AND instr('/' || temporary_path || '/', '/./') = 0 "
    "AND instr(temporary_path, char(0)) = 0 "
    "AND temporary_path NOT GLOB '*[<>:\"|?*]*'"
)


class DownloadPageRow(PersistenceBase):
    __tablename__ = "download_pages"
    __table_args__ = (
        CheckConstraint(
            "typeof(ordinal) = 'integer' AND ordinal > 0",
            name="ordinal_positive_integer",
        ),
        CheckConstraint(
            "typeof(remote_reference) = 'text' AND length(remote_reference) > 0",
            name="remote_reference_nonempty",
        ),
        CheckConstraint(
            "typeof(status) = 'text' AND length(status) > 0",
            name="status_nonempty",
        ),
        CheckConstraint(
            "temporary_path IS NULL OR (typeof(temporary_path) = 'text' AND "
            f"{DOWNLOAD_PAGE_TEMPORARY_PATH_CHECK})",
            name="temporary_path_safe",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR (typeof(size_bytes) = 'integer' AND size_bytes >= 0)",
            name="size_bytes_nonnegative_integer",
        ),
        CheckConstraint(
            f"sha256 IS NULL OR ({DOWNLOAD_PAGE_SHA256_CHECK})",
            name="sha256_valid",
        ),
        CheckConstraint(
            "(width IS NULL AND height IS NULL) OR "
            "(typeof(width) = 'integer' AND width > 0 AND "
            "typeof(height) = 'integer' AND height > 0)",
            name="dimensions_positive_pair",
        ),
        CheckConstraint(
            "mime IS NULL OR (typeof(mime) = 'text' AND length(mime) > 0)",
            name="mime_nonempty",
        ),
        CheckConstraint(
            "typeof(attempts) = 'integer' AND attempts >= 0",
            name="attempts_nonnegative_integer",
        ),
        Index("ux_download_pages_temporary_path", "temporary_path", unique=True),
        Index("ux_download_pages_temporary_path_key", "temporary_path_key", unique=True),
        Index("ix_download_pages_job_status", "job_id", "status"),
    )

    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("download_jobs.job_id", ondelete="RESTRICT", name="fk_download_pages_job_id"),
        primary_key=True,
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(BigInteger(), primary_key=True, nullable=False)
    remote_reference: Mapped[str] = mapped_column(Text(), nullable=False)
    temporary_path: Mapped[str | None] = mapped_column(Text(), nullable=True)
    temporary_path_key: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(Text(), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    width: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    height: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    mime: Mapped[str | None] = mapped_column(Text(), nullable=True)
    attempts: Mapped[int] = mapped_column(BigInteger(), nullable=False)
