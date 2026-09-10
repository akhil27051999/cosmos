"""
Stage 9 — A/B experimentation tests.

Two layers: the pure deterministic bucketing (stable + proportional + independent
across experiments), and the HTTP flow (admin creates/activates, a tenant gets a
sticky assignment that records an exposure, admin reads results).
"""

from __future__ import annotations

import pytest

from app.domain.experiment import Experiment, ExperimentStatus, Variant
from app.experiments.bucketing import assign, bucket_of

pytestmark = pytest.mark.asyncio


# ---- pure bucketing -----------------------------------------------------

async def test_assignment_is_deterministic_and_sticky():
    exp = Experiment(
        key="agent-model", status=ExperimentStatus.RUNNING,
        variants=[Variant("control", 1), Variant("variant-a", 1)],
    )
    first = assign(exp, "tenant-42").name
    # same inputs -> same variant, every time
    for _ in range(10):
        assert assign(exp, "tenant-42").name == first


async def test_not_running_gives_control():
    exp = Experiment(
        key="e", status=ExperimentStatus.PAUSED,
        variants=[Variant("control", 1), Variant("variant-a", 1)],
    )
    assert all(assign(exp, f"t{i}").name == "control" for i in range(20))


async def test_weights_are_roughly_proportional():
    exp = Experiment(
        key="split", status=ExperimentStatus.RUNNING,
        variants=[Variant("control", 9), Variant("variant-a", 1)],  # 90/10
    )
    a = sum(assign(exp, f"tenant-{i}").name == "variant-a" for i in range(2000))
    # ~10% into variant-a; wide tolerance so it's not flaky
    assert 120 < a < 280


async def test_experiments_bucket_independently():
    # The same unit isn't correlated across different experiment keys.
    variants = [Variant("control", 1), Variant("variant-a", 1)]
    e1 = Experiment(key="exp-one", status=ExperimentStatus.RUNNING, variants=variants)
    e2 = Experiment(key="exp-two", status=ExperimentStatus.RUNNING, variants=variants)
    diff = sum(assign(e1, f"t{i}").name != assign(e2, f"t{i}").name for i in range(200))
    assert diff > 40   # a meaningful fraction differ (not identical mappings)


async def test_bucket_of_in_range():
    assert 0 <= bucket_of("k", "u", 100) < 100


# ---- HTTP flow ----------------------------------------------------------

def _admin(env):
    return {"X-Admin-Token": env.admin_token}


async def test_create_activate_assign_and_results(env):
    # admin creates a running-later experiment
    body = {
        "key": "agent-model",
        "description": "compare models",
        "variants": [
            {"name": "control", "weight": 1, "config": {"model": "opus"}},
            {"name": "variant-a", "weight": 1, "config": {"model": "sonnet"}},
        ],
    }
    r = await env.client.post("/v1/experiments", json=body, headers=_admin(env))
    assert r.status_code == 201
    assert r.json()["status"] == "draft"

    # while draft -> assignment is control, no exposure recorded
    a0 = await env.client.get("/v1/experiments/agent-model/assignment")
    assert a0.status_code == 200
    assert a0.json()["variant"] == "control"

    # activate it
    r = await env.client.post(
        "/v1/experiments/agent-model/status", json={"status": "running"}, headers=_admin(env)
    )
    assert r.json()["status"] == "running"

    # tenant assignment is now bucketed + sticky + carries the variant config
    a1 = (await env.client.get("/v1/experiments/agent-model/assignment")).json()
    a2 = (await env.client.get("/v1/experiments/agent-model/assignment")).json()
    assert a1["variant"] == a2["variant"]                 # sticky
    assert a1["unit_id"] == env.tenant_id                 # unit is the tenant
    assert "model" in a1["config"]

    # results show exactly one unique exposed unit (this tenant), counted once
    res = (await env.client.get("/v1/experiments/agent-model/results", headers=_admin(env))).json()
    assert res["total"] == 1
    assert res["counts"][a1["variant"]] == 1


async def test_assignment_requires_auth(env):
    await env.client.post(
        "/v1/experiments",
        json={"key": "e", "variants": [{"name": "control"}]},
        headers=_admin(env),
    )
    r = await env.client.get("/v1/experiments/e/assignment", headers={"Authorization": ""})
    assert r.status_code == 401


async def test_admin_routes_need_admin_token(env):
    r = await env.client.get("/v1/experiments")          # no admin token
    assert r.status_code == 401


async def test_unknown_experiment_is_404(env):
    r = await env.client.get("/v1/experiments/nope/assignment")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "experiment_not_found"
