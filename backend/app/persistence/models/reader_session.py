from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase
from app.persistence.models.download_page import DOWNLOAD_PAGE_SHA256_CHECK


class ReaderSessionRow(PersistenceBase):
    __tablename__ = "reader_sessions"
    __table_args__ = (
        CheckConstraint(
            "typeof(page_count) = 'integer' AND page_count BETWEEN 0 AND 10000",
            name="page_count_bounded",
        ),
        CheckConstraint(
            "state IN ('acquiring','complete','interrupted','failed','evicting','evicted')",
            name="state_supported",
        ),
        Index(
            "ux_reader_sessions_live_chapter",
            "chapter_id",
            unique=True,
            sqlite_where=text("state <> 'evicted'"),
        ),
        Index("ix_reader_sessions_lru", "last_access_us"),
    )

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chapter_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chapters.chapter_id", ondelete="RESTRICT"), nullable=False
    )
    chapter_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("chapter_sources.chapter_source_id", ondelete="RESTRICT"),
        nullable=False,
    )
    connector_id: Mapped[str] = mapped_column(Text(), nullable=False)
    connector_version: Mapped[str] = mapped_column(Text(), nullable=False)
    remote_reference: Mapped[str] = mapped_column(Text(), nullable=False)
    generation: Mapped[str | None] = mapped_column(String(36), nullable=True)
    page_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    state: Mapped[str] = mapped_column(Text(), nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    native_released: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    last_access_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)


class ReaderSessionPageRow(PersistenceBase):
    __tablename__ = "reader_session_pages"
    __table_args__ = (
        CheckConstraint(
            "typeof(ordinal) = 'integer' AND ordinal BETWEEN 1 AND 10000",
            name="ordinal_bounded",
        ),
        CheckConstraint(
            "typeof(attempt) = 'integer' AND attempt BETWEEN 1 AND 4", name="attempt_bounded"
        ),
        CheckConstraint("state IN ('downloading','publishing','ready')", name="state_supported"),
        CheckConstraint(
            "(state = 'downloading' AND sha256 IS NULL AND size_bytes IS NULL "
            "AND mime IS NULL AND width IS NULL AND height IS NULL) OR "
            "(state IN ('publishing','ready') AND sha256 IS NOT NULL AND size_bytes IS NOT NULL "
            "AND mime IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL)",
            name="verified_metadata_required",
        ),
        CheckConstraint(f"sha256 IS NULL OR ({DOWNLOAD_PAGE_SHA256_CHECK})", name="sha256_valid"),
        CheckConstraint(
            "size_bytes IS NULL OR (typeof(size_bytes) = 'integer' "
            "AND size_bytes BETWEEN 1 AND 67108864)",
            name="size_bounded",
        ),
        CheckConstraint(
            "mime IS NULL OR mime IN ('image/jpeg','image/png','image/webp')", name="mime_supported"
        ),
        CheckConstraint(
            "width IS NULL OR (typeof(width) = 'integer' AND width BETWEEN 1 AND 40000)",
            name="width_bounded",
        ),
        CheckConstraint(
            "height IS NULL OR (typeof(height) = 'integer' AND height BETWEEN 1 AND 40000)",
            name="height_bounded",
        ),
    )

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("reader_sessions.session_id", ondelete="RESTRICT"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(BigInteger(), primary_key=True)
    remote_reference: Mapped[str] = mapped_column(Text(), nullable=False)
    attempt: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    state: Mapped[str] = mapped_column(Text(), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    mime: Mapped[str | None] = mapped_column(Text(), nullable=True)
    width: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    height: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
