"""
The `job_read_model` table — the denormalized CQRS read side.

Physically separate from the write tables (jobs/job_events). In a bigger system
it could live in a different database entirely; here it's another Postgres table,
kept eventually-consistent by the projector.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class JobReadModelRow(Base):
    __tablename__ = "job_read_model"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Stage 6: carried through from the event so the read side is tenant-scoped.
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, default="public")
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
