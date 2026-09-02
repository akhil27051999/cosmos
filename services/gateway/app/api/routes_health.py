"""
Health endpoints — small but not optional.

Two DIFFERENT questions, two endpoints (this distinction matters in Kubernetes):
  * /healthz  (liveness)  — "is the process alive?" If this fails, restart me.
  * /readyz   (readiness) — "can I serve traffic right now?" It checks the DB;
                            if the DB is down, take me OUT of the load balancer
                            but DON'T restart me (a restart won't fix the DB).

Confusing the two causes restart loops, so we separate them from day one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def liveness() -> dict:
    # No dependencies: if the event loop can answer, the process is alive.
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(response: Response, session: AsyncSession = Depends(get_session)) -> dict:
    try:
        await session.execute(text("SELECT 1"))  # cheapest possible DB probe
        return {"status": "ready"}
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "reason": "database_unreachable"}
