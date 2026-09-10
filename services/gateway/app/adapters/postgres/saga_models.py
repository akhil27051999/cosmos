"""
The `sagas` table. Steps + context are JSON (small, nested, schema-flexible), so
the whole instance is one row — read it back and the engine can resume.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SagaModel(Base):
    __tablename__ = "sagas"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    steps: Mapped[list] = mapped_column(JSON, nullable=False)     # [{name, status}]
    context: Mapped[dict] = mapped_column(JSON, nullable=False)
    current_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
