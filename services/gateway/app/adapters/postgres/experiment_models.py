"""
The `experiments` table. Variants are stored as JSON — they're a small, nested,
schema-flexible list, so a JSON column beats a second table + join here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ExperimentModel(Base):
    __tablename__ = "experiments"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    description: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # list of {name, weight, config}
    variants: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
