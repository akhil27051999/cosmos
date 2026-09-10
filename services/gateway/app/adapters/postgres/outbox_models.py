"""
The `outbox` table.

`published_at` NULL means "not yet on the bus". The relay filters on it and sets
it once published. `seq` gives a stable oldest-first order (same reasoning as the
event log). A partial index on unpublished rows keeps the relay's poll cheap even
as the table grows.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class OutboxModel(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        # index only the rows the relay actually scans (published_at IS NULL)
        Index("ix_outbox_unpublished", "seq", postgresql_where=text("published_at IS NULL")),
    )

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
