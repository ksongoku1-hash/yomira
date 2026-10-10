from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase


class ReaderProgressRow(PersistenceBase):
    __tablename__ = "reader_progress"
    __table_args__ = (
        CheckConstraint(
            "typeof(page_index) = 'integer' AND page_index >= 0",
            name="page_index_nonnegative_integer",
        ),
        CheckConstraint(
            "typeof(percentage_basis_points) = 'integer' "
            "AND percentage_basis_points BETWEEN 0 AND 10000",
            name="percentage_basis_points_range",
        ),
        CheckConstraint(
            "typeof(updated_at_us) = 'integer'",
            name="updated_at_integer",
        ),
        ForeignKeyConstraint(
            ["series_id", "chapter_id"],
            ["chapters.series_id", "chapters.chapter_id"],
            name="fk_reader_progress_series_chapter",
            ondelete="RESTRICT",
        ),
        Index("ix_reader_progress_chapter_id", "chapter_id"),
        Index(
            "ix_reader_progress_saved_recent",
            text("updated_at_us DESC"),
            text("series_id ASC"),
            text("chapter_id ASC"),
        ),
    )

    series_id: Mapped[str] = mapped_column(String(36), primary_key=True, nullable=False)
    chapter_id: Mapped[str] = mapped_column(String(36), primary_key=True, nullable=False)
    page_index: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    percentage_basis_points: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    updated_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
