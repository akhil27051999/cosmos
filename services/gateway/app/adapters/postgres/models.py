"""
The Postgres table definition (SQLAlchemy ORM model).

IMPORTANT distinction: `JobModel` here is *not* the same thing as the domain
`Job`. This is a persistence detail — how a job is laid out in a database table.
The domain `Job` is the business object. The repository (repository.py)
translates between the two. Keeping them separate means the DB schema can change
without touching business rules, and vice-versa.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class JobModel(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        # Stage 6: idempotency is now scoped PER TENANT — the race-proof backstop
        # is a unique constraint on (tenant_id, idempotency_key), so two different
        # tenants may reuse the same key string without colliding, while a single
        # tenant still can't create duplicates under a repeated key.
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_jobs_tenant_idempotency_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # Stage 6: the isolation key. Indexed because every tenant-scoped read filters
    # on it (get/list), and it's half of the idempotency uniqueness above.
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # payload moved to MongoDB in Stage 2 — this table is metadata-only now.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Note: SQLAlchemy can auto-manage a version column via
    # `__mapper_args__ = {"version_id_col": version}`. We deliberately DON'T use
    # it here, because our domain `Job` already owns the version (it bumps it on
    # each transition). Instead the repository does an explicit conditional
    # UPDATE (`WHERE version = expected`) — see repository.py. That keeps the
    # Postgres and in-memory adapters behaving identically and makes the OCC
    # mechanism visible rather than magic.
