"""
The `experiment_exposures` table — one row per (experiment, unit), so an exposure
is idempotent and `counts` measures unique units per variant.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ExposureRow(Base):
    __tablename__ = "experiment_exposures"

    experiment_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    unit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    variant: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
