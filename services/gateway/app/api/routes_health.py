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

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.core.lifecycle import is_draining

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def liveness() -> dict:
    # No dependencies: if the event loop can answer, the process is alive.
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(response: Response) -> dict:
    # Stage 12: during graceful shutdown we report not-ready FIRST (before any DB
    # work) so k8s pulls us from the Service while in-flight requests drain.
    if is_draining():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "draining"}
    try:
        # Own short session so a not-ready pod never holds request-path resources.
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))  # cheapest possible DB probe
        return {"status": "ready"}
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "reason": "database_unreachable"}
