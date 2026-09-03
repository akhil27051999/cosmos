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
    get_stream_bus,
)
from app.api.schemas import JobCreate, JobDocumentOut, JobEventOut, JobOut
from app.application.job_service import JobService
from app.domain.exceptions import JobNotFound
from app.ports.cache import JobCache
from app.ports.document_store import JobDocumentStore
from app.ports.event_store import EventStore
from app.ports.job_repository import JobRepository
from app.ports.stream_bus import StreamBus

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


@router.post("", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def submit_job(
    body: JobCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    service: JobService = Depends(get_job_service),
) -> JobOut:
    # The service creates + enqueues the job (or replays an existing one for a
    # repeated idempotency key). All the lifecycle logic lives there, not here.
    job, created = await service.submit(body.payload, idempotency_key)
    if not created:
        # idempotent replay of an already-accepted job
        response.status_code = status.HTTP_200_OK
    return JobOut.from_entity(job)


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: uuid.UUID,
    repo: JobRepository = Depends(get_job_repository),
    cache: JobCache = Depends(get_job_cache),
) -> JobOut:
    # CACHE-ASIDE: try the fast copy first; on a miss, read Postgres and
    # populate the cache so the next poll is cheap. Invalidation happens on
    # writes (see JobService), so the cache never lies for long.
    cached = await cache.get(job_id)
    if cached is not None:
        return JobOut.from_entity(cached)

    job = await repo.get(job_id)
    if job is None:
        raise JobNotFound(str(job_id))
    await cache.set(job)
    return JobOut.from_entity(job)


@router.get("/{job_id}/document", response_model=JobDocumentOut)
async def get_job_document(
    job_id: uuid.UUID,
    repo: JobRepository = Depends(get_job_repository),
    docs: JobDocumentStore = Depends(get_document_store),
) -> JobDocumentOut:
    # The big document (payload + result) lives in MongoDB, separate from the
    # metadata in Postgres. 404 if there's no such job.
    if await repo.get(job_id) is None:
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
) -> list[JobOut]:
    jobs = await repo.list(limit=limit, offset=offset)
    return [JobOut.from_entity(j) for j in jobs]


@router.get("/{job_id}/events", response_model=list[JobEventOut])
async def get_job_events(
    job_id: uuid.UUID,
    repo: JobRepository = Depends(get_job_repository),
    events: EventStore = Depends(get_event_store),
) -> list[JobEventOut]:
    # 404 if there's no such job at all (rather than returning an empty log).
    if await repo.get(job_id) is None:
        raise JobNotFound(str(job_id))
    log = await events.list_for_job(job_id)
    return [JobEventOut.from_entity(e) for e in log]


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(
    job_id: uuid.UUID,
    service: JobService = Depends(get_job_service),
) -> JobOut:
    # cancel raises JobNotFound (-> 404) or InvalidTransition (-> 409 if the job
    # is already terminal). Cancelling a PENDING or RUNNING job is allowed.
    job = await service.cancel(job_id)
    return JobOut.from_entity(job)


@router.get("/{job_id}/stream")
async def stream_job(
    job_id: uuid.UUID,
    repo: JobRepository = Depends(get_job_repository),
    bus: StreamBus = Depends(get_stream_bus),
) -> StreamingResponse:
    # Server-Sent Events: the worker publishes each agent step to the bus; we
    # forward them to the client as `data: {json}\n\n` frames until an `end`
    # event. (Live-only: steps emitted before the client connects aren't
    # replayed — a production version would replay the Mongo trajectory first.)
    if await repo.get(job_id) is None:
        raise JobNotFound(str(job_id))

    async def event_stream():
        async for event in bus.subscribe(job_id):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
