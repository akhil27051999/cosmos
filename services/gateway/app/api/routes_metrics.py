"""
GET /metrics — the Prometheus scrape endpoint (Stage 10).

Returns the whole registry in the text exposition format. **No auth**, by
convention: Prometheus scrapes it unauthenticated, and in production you restrict
it at the network layer (a ClusterIP-only port / NetworkPolicy / ServiceMonitor),
not with an API key. It carries no tenant data — only aggregate counters.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.observability.metrics import render_latest

router = APIRouter(tags=["observability"])

# The content type Prometheus expects for the v0.0.4 text format.
_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=render_latest(), media_type=_CONTENT_TYPE)
