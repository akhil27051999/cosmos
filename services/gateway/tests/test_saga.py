"""
Stage 11 — saga orchestrator tests.

Engine (pure, fake steps): forward success, compensate-in-reverse on failure, and
resume (DONE steps aren't re-run). Then the applied onboarding saga over the real
in-memory identity + experiment stores: happy path provisions everything; a
simulated failure unwinds it completely.
"""

from __future__ import annotations

import pytest

from app.adapters.memory.saga_store import InMemorySagaStore
from app.domain.saga import SagaInstance, SagaStatus, StepStatus
from app.saga.engine import SagaDefinition, SagaOrchestrator, Step

pytestmark = pytest.mark.asyncio


# ---- engine (fake steps) ------------------------------------------------

async def test_all_steps_succeed():
    log = []

    def step(n):
        async def act(ctx):
            log.append(f"do-{n}")
            return {n: True}
        return Step(n, act, None)

    defn = SagaDefinition("t", [step("a"), step("b"), step("c")])
    inst = await SagaOrchestrator(InMemorySagaStore()).start(defn, {})
    assert inst.status is SagaStatus.COMPLETED
    assert log == ["do-a", "do-b", "do-c"]
    assert all(s.status is StepStatus.DONE for s in inst.steps)


async def test_failure_compensates_completed_steps_in_reverse():
    log = []

    def ok(n):
        async def act(ctx):
            log.append(f"do-{n}")
        async def comp(ctx):
            log.append(f"undo-{n}")
        return Step(n, act, comp)

    async def boom(ctx):
        log.append("do-c(boom)")
        raise RuntimeError("nope")

    defn = SagaDefinition("t", [ok("a"), ok("b"), Step("c", boom, None)])
    inst = await SagaOrchestrator(InMemorySagaStore()).start(defn, {})

    assert inst.status is SagaStatus.COMPENSATED
    # forward a, b, c(fails); then compensate b, a in REVERSE
    assert log == ["do-a", "do-b", "do-c(boom)", "undo-b", "undo-a"]
    assert inst.steps[0].status is StepStatus.COMPENSATED
    assert inst.steps[1].status is StepStatus.COMPENSATED
    assert inst.steps[2].status is StepStatus.FAILED
    assert "step 'c' failed" in inst.error


async def test_resume_skips_already_done_steps():
    ran = []

    def step(n):
        async def act(ctx):
            ran.append(n)
        return Step(n, act, None)

    defn = SagaDefinition("t", [step("a"), step("b")])
    store = InMemorySagaStore()
    # An instance where 'a' is already DONE (as if we crashed after step a).
    inst = SagaInstance.new("t", ["a", "b"], {})
    inst.steps[0].status = StepStatus.DONE
    inst.current_index = 1

    resumed = await SagaOrchestrator(store).run(defn, inst)
    assert resumed.status is SagaStatus.COMPLETED
    assert ran == ["b"]                      # 'a' was NOT re-run


async def test_compensation_failure_marks_saga_failed():
    async def a_act(ctx): return None
    async def a_comp(ctx): raise RuntimeError("cannot undo a")
    async def b_boom(ctx): raise RuntimeError("b failed")

    defn = SagaDefinition("t", [Step("a", a_act, a_comp), Step("b", b_boom, None)])
    inst = await SagaOrchestrator(InMemorySagaStore()).start(defn, {})
    assert inst.status is SagaStatus.FAILED   # a compensation failed -> manual repair
    assert "compensation for 'a' failed" in inst.error


# ---- applied onboarding saga over HTTP ----------------------------------

def _admin(env):
    return {"X-Admin-Token": env.admin_token}


async def test_onboarding_happy_path_provisions_everything(env):
    r = await env.client.post(
        "/v1/admin/onboarding", json={"name": "Acme", "monthly_quota": 250},
        headers=_admin(env),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    ctx = body["context"]
    tid = ctx["tenant_id"]

    # tenant, key, and experiment all exist
    assert await env.identity.get_tenant(tid) is not None
    assert await env.identity.get_api_key(ctx["key_id"]) is not None
    assert await env.experiment_store.get(f"welcome-{tid}") is not None
    # the minted key actually authenticates
    who = await env.client.get("/v1/jobs", headers={"Authorization": f"Bearer {ctx['api_key']}"})
    assert who.status_code == 200


async def test_onboarding_failure_compensates_everything(env):
    r = await env.client.post(
        "/v1/admin/onboarding?simulate_failure=true", json={"name": "Doomed"},
        headers=_admin(env),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "compensated"
    ctx = body["context"]
    tid = ctx["tenant_id"]

    # everything created was rolled back
    assert await env.identity.get_tenant(tid) is None
    assert await env.identity.get_api_key(ctx["key_id"]) is None
    assert await env.experiment_store.get(f"welcome-{tid}") is None


async def test_get_saga_by_id_and_404(env):
    r = await env.client.post("/v1/admin/onboarding", json={"name": "Acme"}, headers=_admin(env))
    saga_id = r.json()["id"]

    got = await env.client.get(f"/v1/admin/sagas/{saga_id}", headers=_admin(env))
    assert got.status_code == 200
    assert got.json()["id"] == saga_id

    missing = await env.client.get("/v1/admin/sagas/deadbeef", headers=_admin(env))
    assert missing.status_code == 404


async def test_onboarding_requires_admin(env):
    r = await env.client.post("/v1/admin/onboarding", json={"name": "X"})
    assert r.status_code == 401
