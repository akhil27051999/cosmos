"""
The Jobs HTTP API (Stage 1).

The big change from Stage 0: creating a job no longer does any work inline. It
ACCEPTS the job (persist + enqueue) and returns **202 Accepted + id** in
milliseconds; a worker does the work later. The client tracks progress by
polling the job or reading its event log.

Endpoints:
  POST /v1/jobs                  submit a job  -> 202 (200 on idempotent replay)
  GET  /v1/jobs/{id}             fetch one
  GET  /v1/jobs                  list a page
  GET  /v1/jobs/{id}/events      the append-only event log for a job
  POST /v1/jobs/{id}/cancel      request cancellation
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Header, Query, Response, status
from fastapi.responses import StreamingResponse

from app.api.deps import (
    get_document_store,
    get_event_store,
    get_job_cache,
    get_job_repository,
    get_job_service,
    get_principal,
    get_stream_bus,
    get_submit_rate_limiter,
)
from app.api.schemas import JobCreate, JobDocumentOut, JobEventOut, JobOut
from app.application.job_service import JobService
from app.domain.exceptions import JobNotFound
from app.domain.identity import Principal
from app.ports.cache import JobCache
from app.ports.document_store import JobDocumentStore
from app.ports.event_store import EventStore
from app.ports.job_repository import JobRepository
from app.ports.stream_bus import StreamBus
from app.resilience.errors import RateLimited
from app.resilience.rate_limiter import RateLimiter

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


@router.post("", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def submit_job(
    body: JobCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    service: JobService = Depends(get_job_service),
    limiter: RateLimiter = Depends(get_submit_rate_limiter),
    principal: Principal = Depends(get_principal),
) -> JobOut:
    # LOAD SHEDDING (admission control): if we're over the allowed submission
    # rate, reject fast with 429 instead of accepting work we can't keep up with.
    # Shedding early protects the whole system from collapsing under overload.
    if not await limiter.try_acquire():
        raise RateLimited(retry_after=1)

    # The service creates + enqueues the job for THIS tenant (or replays an
    # existing one for a repeated idempotency key). It also enforces the tenant's
    # quota. All the lifecycle logic lives there, not here.
    job, created = await service.submit(body.payload, idempotency_key, principal.tenant_id)
    if not created:
        # idempotent replay of an already-accepted job
        response.status_code = status.HTTP_200_OK
    return JobOut.from_entity(job)


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: uuid.UUID,
    repo: JobRepository = Depends(get_job_repository),
    cache: JobCache = Depends(get_job_cache),
    principal: Principal = Depends(get_principal),
) -> JobOut:
    # CACHE-ASIDE: try the fast copy first; on a miss, read Postgres and
    # populate the cache so the next poll is cheap. Invalidation happens on
    # writes (see JobService), so the cache never lies for long.
    # Tenant isolation: verify the cached copy belongs to the caller before
    # serving it, and scope the DB read — another tenant's id is a 404.
    cached = await cache.get(job_id)
    if cached is not None and cached.tenant_id == principal.tenant_id:
        return JobOut.from_entity(cached)

    job = await repo.get(job_id, principal.tenant_id)
    if job is None:
        raise JobNotFound(str(job_id))
    await cache.set(job)
    return JobOut.from_entity(job)


@router.get("/{job_id}/document", response_model=JobDocumentOut)
async def get_job_document(
    job_id: uuid.UUID,
    repo: JobRepository = Depends(get_job_repository),
    docs: JobDocumentStore = Depends(get_document_store),
    principal: Principal = Depends(get_principal),
) -> JobDocumentOut:
    # The big document (payload + result) lives in MongoDB, separate from the
    # metadata in Postgres. 404 if there's no such job for this tenant.
    if await repo.get(job_id, principal.tenant_id) is None:
        raise JobNotFound(str(job_id))
    doc = await docs.get(job_id)
    if doc is None:
        raise JobNotFound(str(job_id))
    return JobDocumentOut.from_entity(doc)


@router.get("", response_model=list[JobOut])
async def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: JobRepository = Depends(get_job_repository),
    principal: Principal = Depends(get_principal),
) -> list[JobOut]:
    # Only ever this tenant's jobs — the list is scoped, never global.
    jobs = await repo.list(limit=limit, offset=offset, tenant_id=principal.tenant_id)
    return [JobOut.from_entity(j) for j in jobs]


@router.get("/{job_id}/events", response_model=list[JobEventOut])
async def get_job_events(
    job_id: uuid.UUID,
    repo: JobRepository = Depends(get_job_repository),
    events: EventStore = Depends(get_event_store),
    principal: Principal = Depends(get_principal),
) -> list[JobEventOut]:
    # 404 if there's no such job for this tenant (rather than an empty log).
    if await repo.get(job_id, principal.tenant_id) is None:
        raise JobNotFound(str(job_id))
    log = await events.list_for_job(job_id)
    return [JobEventOut.from_entity(e) for e in log]


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(
    job_id: uuid.UUID,
    service: JobService = Depends(get_job_service),
    principal: Principal = Depends(get_principal),
) -> JobOut:
    # cancel raises JobNotFound (-> 404, incl. another tenant's job) or
    # InvalidTransition (-> 409 if already terminal). Scoped to the caller.
    job = await service.cancel(job_id, principal.tenant_id)
    return JobOut.from_entity(job)


@router.get("/{job_id}/stream")
async def stream_job(
    job_id: uuid.UUID,
    repo: JobRepository = Depends(get_job_repository),
    bus: StreamBus = Depends(get_stream_bus),
    principal: Principal = Depends(get_principal),
) -> StreamingResponse:
    # Server-Sent Events: the worker publishes each agent step to the bus; we
    # forward them to the client as `data: {json}\n\n` frames until an `end`
    # event. (Live-only: steps emitted before the client connects aren't
    # replayed — a production version would replay the Mongo trajectory first.)
    if await repo.get(job_id, principal.tenant_id) is None:
        raise JobNotFound(str(job_id))

    async def event_stream():
        async for event in bus.subscribe(job_id):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
