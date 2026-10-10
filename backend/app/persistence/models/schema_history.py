from sqlalchemy import BigInteger, CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase


class SchemaHistoryRow(PersistenceBase):
    __tablename__ = "schema_history"
    __table_args__ = (
        CheckConstraint("direction IN ('upgrade', 'downgrade')", name="direction"),
        {"sqlite_autoincrement": True},
    )

    event_id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    revision_id: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    resulting_revision: Mapped[str] = mapped_column(String(32), nullable=False)
    applied_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
