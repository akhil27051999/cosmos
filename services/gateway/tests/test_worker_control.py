"""
Stage 7 part 2 — gRPC control-plane tests (via the in-memory fake).

The gateway's `/v1/admin/worker/status` route depends on the `WorkerControlClient`
port, so we exercise it with the in-memory fake — no gRPC needed, exactly like
the Redpanda/Mongo adapters. We also assert the worker's own metrics->status
snapshot logic, and that importing the app never pulls in grpc.
"""

from __future__ import annotations

import sys

import pytest

from app.api.deps import get_worker_control
from app.domain.worker_status import WorkerStatus
from app.worker import WorkerMetrics

pytestmark = pytest.mark.asyncio


async def test_worker_metrics_snapshot():
    m = WorkerMetrics("w-1")
    m.in_flight = 2
    m.processed_total = 7
    st = m.snapshot()
    assert st.worker_id == "w-1"
    assert st.in_flight == 2 and st.processed_total == 7
    assert st.healthy is True
    assert st.uptime_seconds >= 0


async def test_worker_status_route_requires_admin(env):
    # The whole admin router is gated by the admin token.
    r = await env.client.get("/v1/admin/worker/status")
    assert r.status_code == 401


async def test_worker_status_route_returns_status(env):
    env.worker_control.status = WorkerStatus(
        worker_id="worker-42", uptime_seconds=12.5,
        in_flight=1, processed_total=99, healthy=True,
    )
    r = await env.client.get(
        "/v1/admin/worker/status", headers={"X-Admin-Token": env.admin_token}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["worker_id"] == "worker-42"
    assert body["processed_total"] == 99
    assert body["in_flight"] == 1
    assert body["healthy"] is True


async def test_worker_status_503_when_worker_unreachable(env):
    # Simulate the gRPC call failing -> the route maps it to a clean 503.
    class _Down:
        async def get_status(self):
            raise ConnectionError("worker down")

    env.app.dependency_overrides[get_worker_control] = lambda: _Down()

    r = await env.client.get(
        "/v1/admin/worker/status", headers={"X-Admin-Token": env.admin_token}
    )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "service_unavailable"


async def test_importing_app_does_not_import_grpc():
    # The gRPC runtime must stay off the import path (lazy) so pytest needs no grpc.
    import app.main  # noqa: F401 - already imported via conftest, this is explicit
    assert "grpc" not in sys.modules
