from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase


class DownloadJobArchiveRow(PersistenceBase):
    __tablename__ = "download_job_archives"
    __table_args__ = (
        CheckConstraint(
            "typeof(archived_at_us) = 'integer' AND archived_at_us >= 0",
            name="archived_at_us_nonnegative_integer",
        ),
    )

    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("download_jobs.job_id", ondelete="RESTRICT", name="fk_job_archives_job_id"),
        primary_key=True,
        nullable=False,
    )
    archived_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
