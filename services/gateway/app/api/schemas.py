"""
API schemas (DTOs) — the shapes that cross the HTTP boundary.

These Pydantic models are SEPARATE from the domain `Job` on purpose. They are
the public contract with clients; the domain object is internal. Keeping them
apart means you can evolve the API without leaking internal fields, and Pydantic
gives you request validation and response serialisation for free.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.job import Job, JobStatus


class JobCreate(BaseModel):
    """Request body for POST /v1/jobs."""

    # `payload` is whatever work the caller wants done. We require at least one
    # key so empty submissions are rejected by validation (=> 422) automatically.
    payload: dict = Field(..., min_length=1, description="the work to perform")


class JobTransition(BaseModel):
    """Request body for POST /v1/jobs/{id}/transition."""

    to: JobStatus = Field(..., description="target status")
    # The client tells us which version it based this change on. That's what
    # makes the write safe under concurrency (optimistic concurrency control).
    expected_version: int = Field(..., ge=1, description="version the client last saw")


class JobOut(BaseModel):
    """Response body representing a job."""

    model_config = ConfigDict(from_attributes=True)  # allow building from an object

    id: uuid.UUID
    status: JobStatus
    version: int
    idempotency_key: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, job: Job) -> "JobOut":
        return cls.model_validate(job)
