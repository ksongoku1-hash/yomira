from sqlalchemy import BigInteger, CheckConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import PersistenceBase


class SettingRow(PersistenceBase):
    __tablename__ = "settings"
    __table_args__ = (
        CheckConstraint(
            "typeof(setting_key) = 'text' AND length(setting_key) > 0",
            name="setting_key_nonempty",
        ),
        CheckConstraint(
            "typeof(value_json) = 'text' AND json_valid(value_json) = 1",
            name="value_json_valid",
        ),
        CheckConstraint(
            "typeof(schema_version) = 'integer' AND schema_version > 0",
            name="schema_version_positive_integer",
        ),
        CheckConstraint(
            "typeof(updated_at_us) = 'integer'",
            name="updated_at_integer",
        ),
    )

    setting_key: Mapped[str] = mapped_column(Text(), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text(), nullable=False)
    schema_version: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    updated_at_us: Mapped[int] = mapped_column(BigInteger(), nullable=False)
