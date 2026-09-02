"""
The Jobs HTTP API.

This is the thin "adapter" that turns HTTP requests into domain/port calls and
domain objects back into HTTP responses. Notice how little logic lives here:
the rules are in the domain, persistence is in the repository. A route's job is
orchestration, not business logic.

Endpoints:
  POST /v1/jobs                      create a job (idempotent via a header)
  GET  /v1/jobs/{id}                 fetch one
  GET  /v1/jobs                      list a page
  POST /v1/jobs/{id}/transition      change status (optimistic concurrency)
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Query, Response, status

from app.api.deps import get_job_repository
from app.api.schemas import JobCreate, JobOut, JobTransition
from app.domain.exceptions import JobNotFound, VersionConflict
from app.domain.job import Job
from app.ports.job_repository import JobRepository

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreate,
    response: Response,
    # An idempotency key lets a client retry a submission safely: the same key
    # always yields the same job instead of creating duplicates. It's optional
    # and travels in a header, not the body (it's about the request, not the job).
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    repo: JobRepository = Depends(get_job_repository),
) -> JobOut:
    # 1) If we've seen this key before, replay the original result (200, not 201).
    if idempotency_key:
        existing = await repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return JobOut.from_entity(existing)

    # 2) Otherwise create a fresh job (status defaults to PENDING).
    job = Job(payload=body.payload, idempotency_key=idempotency_key)
    try:
        await repo.add(job)
    except VersionConflict:
        # A concurrent request with the SAME key won the race between our
        # lookup above and this insert. The DB unique constraint stopped the
        # duplicate; fetch and return the winner so both callers see one job.
        if idempotency_key:
            existing = await repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                response.status_code = status.HTTP_200_OK
                return JobOut.from_entity(existing)
        raise

    return JobOut.from_entity(job)


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: uuid.UUID,
    repo: JobRepository = Depends(get_job_repository),
) -> JobOut:
    job = await repo.get(job_id)
    if job is None:
        # Raise the *domain* error; api/errors.py maps it to 404. The route
        # doesn't mention status codes — that mapping lives in one place.
        raise JobNotFound(str(job_id))
    return JobOut.from_entity(job)


@router.get("", response_model=list[JobOut])
async def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: JobRepository = Depends(get_job_repository),
) -> list[JobOut]:
    jobs = await repo.list(limit=limit, offset=offset)
    return [JobOut.from_entity(j) for j in jobs]


@router.post("/{job_id}/transition", response_model=JobOut)
async def transition_job(
    job_id: uuid.UUID,
    body: JobTransition,
    repo: JobRepository = Depends(get_job_repository),
) -> JobOut:
    job = await repo.get(job_id)
    if job is None:
        raise JobNotFound(str(job_id))

    # Fail fast if the client is working from a stale version. (The repository
    # enforces this again at write time to close the read-then-write gap — this
    # early check just gives a clearer, cheaper error in the common case.)
    if body.expected_version != job.version:
        raise VersionConflict(
            f"expected version {body.expected_version}, but current is {job.version}"
        )

    # Apply the business rule. Illegal moves raise InvalidTransition (-> 409).
    job.transition_to(body.to)

    # Persist with the version guard. `expected_version` is the pre-transition
    # version; the domain already bumped `job.version` for the write.
    updated = await repo.update(job, expected_version=body.expected_version)
    return JobOut.from_entity(updated)
