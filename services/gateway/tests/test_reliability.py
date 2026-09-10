"""
Stage 12 — capstone reliability tests: SLO/error-budget math, graceful-shutdown
draining readiness, and chaos fault injection (which burns the budget).
"""

from __future__ import annotations

import pytest

from app.core import lifecycle
from app.observability import chaos
from app.observability.metrics import http_requests_total
from app.observability.slo import compute_slo

pytestmark = pytest.mark.asyncio


# ---- SLO computation ----------------------------------------------------

async def test_slo_endpoint_shape(env):
    r = await env.client.get("/v1/admin/slo", headers={"X-Admin-Token": env.admin_token})
    assert r.status_code == 200
    body = r.json()
    for section in ("availability", "latency"):
        assert set(body[section]) >= {"target", "actual", "error_budget_remaining"}


async def test_slo_requires_admin(env):
    assert (await env.client.get("/v1/admin/slo")).status_code == 401


async def test_availability_and_budget_react_to_5xx():
    before = compute_slo(0.99, 0.5, 0.95)["availability"]
    for _ in range(200):
        http_requests_total.inc(method="GET", path="/slo-test", status="500")
    after = compute_slo(0.99, 0.5, 0.95)["availability"]
    # more 5xx -> lower availability and less error budget remaining
    assert after["actual"] < before["actual"]
    assert after["error_budget_remaining"] <= before["error_budget_remaining"]


async def test_budget_remaining_math():
    # 99% target => 1% allowed. If actual availability is 99.5%, half the budget
    # (0.5%) is spent -> ~0.5 remaining.
    from app.observability.slo import _budget_remaining
    assert _budget_remaining(0.995, 0.99) == pytest.approx(0.5, abs=0.01)
    assert _budget_remaining(1.0, 0.99) == 1.0        # untouched
    assert _budget_remaining(0.98, 0.99) == 0.0        # blown past (clamped)


# ---- graceful shutdown / draining ---------------------------------------

async def test_readyz_reports_draining(env):
    try:
        lifecycle.begin_draining()
        r = await env.client.get("/readyz")
        assert r.status_code == 503
        assert r.json()["status"] == "draining"     # not-ready WITHOUT touching the DB
    finally:
        lifecycle.reset_draining()

    # liveness stays OK while draining — we're alive, just not taking new traffic
    assert (await env.client.get("/healthz")).status_code == 200


# ---- chaos injection ----------------------------------------------------

async def test_chaos_injects_faults_and_burns_budget(env):
    def total_503() -> float:
        # Chaos short-circuits BEFORE routing, so the metrics path label is
        # "__unmatched__"; sum 503s across all series rather than a fixed path.
        return sum(v for lbl, v in http_requests_total.collect() if lbl.get("status") == "503")

    try:
        chaos.configure(enabled=True, fault_rate=1.0)      # fail every request
        before_503 = total_503()
        r = await env.client.get("/healthz")
        assert r.status_code == 503
        assert r.headers.get("x-chaos-injected") == "1"
        # the injected fault was recorded by the metrics middleware (counts toward SLO)
        assert total_503() == before_503 + 1
    finally:
        chaos.configure(False)

    # disabled again -> normal behavior returns
    assert (await env.client.get("/healthz")).status_code == 200


async def test_chaos_off_by_default(env):
    # The fixture resets chaos; a normal request is unaffected.
    assert (await env.client.get("/healthz")).status_code == 200
