"""
Read-side (CQRS) endpoints — served from the projected read model, never the
write tables. This is the "Q" in CQRS: queries hit a store shaped for reading.

  GET /v1/stats              job counts per status (instant; no GROUP BY on writes)
  GET /v1/jobs/{id}/view     the denormalized projection of one job

Mounted on its own `/v1` router so `/v1/jobs/{id}/view` doesn't collide with the
write router's `/v1/jobs/{id}`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from app.api.deps import get_read_model_store, require_scope
from app.api.schemas import JobViewOut
from app.auth.scopes import JOBS_READ
from app.domain.exceptions import JobNotFound
from app.domain.identity import Principal
from app.ports.read_model import ReadModelStore

router = APIRouter(prefix="/v1", tags=["read-model"])


@router.get("/stats")
async def stats(
    read_model: ReadModelStore = Depends(get_read_model_store),
    principal: Principal = Depends(require_scope(JOBS_READ)),
) -> dict[str, int]:
    # This tenant's counts only, e.g. {"pending": 3, "succeeded": 42}.
    return await read_model.stats(principal.tenant_id)


@router.get("/jobs/{job_id}/view", response_model=JobViewOut)
async def job_view(
    job_id: uuid.UUID,
    read_model: ReadModelStore = Depends(get_read_model_store),
    principal: Principal = Depends(require_scope(JOBS_READ)),
) -> JobViewOut:
    entry = await read_model.get(str(job_id))
    # 404 if not projected yet (eventual consistency) OR it's another tenant's
    # job — a cross-tenant view must never leak, same as a miss.
    if entry is None or entry.tenant_id != principal.tenant_id:
        raise JobNotFound(str(job_id))
    return JobViewOut.from_entity(entry)
