"""
The append-only `job_events` table.

Two ordering notes:
  * `seq` is a database-assigned, monotonically increasing number. We order the
    log by it rather than by timestamp, because wall-clock times can tie or even
    go backwards; `seq` gives a stable total order.
  * There is intentionally no UPDATE/DELETE path for this table anywhere in the
    code — events are only ever inserted.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class JobEventModel(Base):
    __tablename__ = "job_events"

    # database-assigned order key (identity/autoincrement)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
