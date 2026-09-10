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

from app.domain.document import JobDocument
from app.domain.events import JobEvent, JobEventType
from app.domain.experiment import ExperimentStatus
from app.domain.job import Job, JobStatus
from app.domain.read_model import ReadModelEntry


class JobCreate(BaseModel):
    """Request body for POST /v1/jobs."""

    # `payload` is whatever work the caller wants done. We require at least one
    # key so empty submissions are rejected by validation (=> 422) automatically.
    payload: dict = Field(..., min_length=1, description="the work to perform")


class JobEventOut(BaseModel):
    """One entry of a job's event log (GET /v1/jobs/{id}/events)."""

    model_config = ConfigDict(from_attributes=True)

    type: JobEventType
    at: datetime
    data: dict

    @classmethod
    def from_entity(cls, event: JobEvent) -> "JobEventOut":
        return cls.model_validate(event)


class JobDocumentOut(BaseModel):
    """A job's big document from MongoDB (GET /v1/jobs/{id}/document)."""

    model_config = ConfigDict(from_attributes=True)

    job_id: uuid.UUID
    payload: dict
    result: dict | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, doc: JobDocument) -> "JobDocumentOut":
        return cls.model_validate(doc)


class JobViewOut(BaseModel):
    """The CQRS read-model projection of a job (GET /v1/jobs/{id}/view)."""

    model_config = ConfigDict(from_attributes=True)

    job_id: uuid.UUID
    tenant_id: str
    status: JobStatus
    version: int
    event_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, entry: ReadModelEntry) -> "JobViewOut":
        return cls.model_validate(entry)


class JobOut(BaseModel):
    """Response body representing a job."""

    model_config = ConfigDict(from_attributes=True)  # allow building from an object

    id: uuid.UUID
    tenant_id: str
    status: JobStatus
    version: int
    idempotency_key: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, job: Job) -> "JobOut":
        return cls.model_validate(job)


# ---- Stage 6: admin bootstrap DTOs --------------------------------------


class TenantCreate(BaseModel):
    """Request body for POST /v1/admin/tenants."""

    name: str = Field(..., min_length=1, description="human-readable tenant name")
    monthly_quota: int | None = Field(
        default=None, ge=1, description="submissions/month; omit for the server default"
    )


class ApiKeyCreated(BaseModel):
    """The one-time reveal of a freshly minted API key.

    `api_key` is shown exactly ONCE — it isn't stored in plaintext and cannot be
    retrieved again. `key_prefix` is the safe, displayable identifier that lives
    in the DB.
    """

    tenant_id: str
    key_prefix: str
    api_key: str = Field(..., description="the full secret — store it now; shown only once")


class TenantCreated(BaseModel):
    """Response for POST /v1/admin/tenants — the tenant plus its first key."""

    tenant_id: str
    name: str
    monthly_quota: int
    key_prefix: str
    api_key: str = Field(..., description="the full secret — store it now; shown only once")


class VariantIn(BaseModel):
    """One arm in a POST /v1/experiments request."""

    name: str = Field(..., min_length=1)
    weight: int = Field(1, ge=1, description="relative traffic share")
    config: dict = Field(default_factory=dict, description="what this arm changes")


class ExperimentCreate(BaseModel):
    """Request body for POST /v1/experiments (admin)."""

    key: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9._-]+$")
    description: str = ""
    variants: list[VariantIn] = Field(..., min_length=1)


class ExperimentStatusUpdate(BaseModel):
    status: ExperimentStatus


class VariantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str
    weight: int
    config: dict


class ExperimentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    description: str
    status: ExperimentStatus
    variants: list[VariantOut]
    created_at: datetime

    @classmethod
    def from_entity(cls, exp) -> "ExperimentOut":
        return cls.model_validate(exp)


class AssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    experiment_key: str
    unit_id: str
    variant: str
    config: dict

    @classmethod
    def from_entity(cls, a) -> "AssignmentOut":
        return cls.model_validate(a)


class OnboardingRequest(BaseModel):
    """Request body for POST /v1/admin/onboarding (runs the onboarding saga)."""

    name: str = Field(..., min_length=1)
    monthly_quota: int | None = Field(default=None, ge=1)


class SagaStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    name: str
    status: str


class SagaInstanceOut(BaseModel):
    """A saga instance's state (returned by the onboarding + get-saga endpoints)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    status: str
    steps: list[SagaStepOut]
    context: dict
    error: str | None

    @classmethod
    def from_entity(cls, inst) -> "SagaInstanceOut":
        return cls.model_validate({
            "id": inst.id, "name": inst.name, "status": inst.status.value,
            "steps": [{"name": s.name, "status": s.status.value} for s in inst.steps],
            "context": inst.context, "error": inst.error,
        })


class WorkerStatusOut(BaseModel):
    """A worker's control-plane status (GET /v1/admin/worker/status), fetched
    from the worker over gRPC (Stage 7 part 2)."""

    model_config = ConfigDict(from_attributes=True)

    worker_id: str
    uptime_seconds: float
    in_flight: int
    processed_total: int
    healthy: bool

    @classmethod
    def from_entity(cls, status) -> "WorkerStatusOut":
        return cls.model_validate(status)
