"""
The `usage_ledger` table — per-tenant, per-period consumption counters.

One row per (tenant_id, period). The composite primary key makes the row
addressable for an atomic upsert-increment (see usage_ledger.py), so concurrent
submissions can't lose a count.
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class UsageRow(Base):
    __tablename__ = "usage_ledger"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # "YYYY-MM"
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
