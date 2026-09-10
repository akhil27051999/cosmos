"""
GET /v1/admin/slo (Stage 12) — the SLO / error-budget report.

Computes availability + latency SLIs from the live metrics and compares them to
the configured targets, returning how much error budget remains. This is the
operational view an SRE checks before a release ("is there budget to spend?").
Admin plane — it's an aggregate operational summary, no tenant data.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import require_admin
from app.core.config import get_settings
from app.observability.slo import compute_slo

router = APIRouter(prefix="/v1/admin", tags=["slo"], dependencies=[Depends(require_admin)])


@router.get("/slo")
async def slo() -> dict:
    s = get_settings()
    return compute_slo(
        availability_target=s.slo_availability_target,
        latency_threshold_seconds=s.slo_latency_threshold_seconds,
        latency_target=s.slo_latency_target,
    )
