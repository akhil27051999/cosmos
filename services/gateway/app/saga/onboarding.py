"""
The tenant-onboarding saga (Stage 11) — a real, multi-subsystem workflow.

Onboarding a tenant touches three subsystems that don't share a transaction:
  1. create the tenant          (identity)   comp: delete the tenant
  2. mint its first API key     (identity)   comp: delete the key
  3. seed a default experiment  (experiments) comp: delete the experiment

If any step fails, the earlier ones are compensated in reverse — so onboarding is
all-or-nothing without a distributed transaction. This is exactly the kind of
cross-service consistency the saga pattern exists for (and the deprovision paths
double as the tenant teardown Stage 8's operator noted was missing).

`build_onboarding_saga` returns a `SagaDefinition`; the orchestrator runs it. The
`simulate_failure` flag appends an always-failing step so you can watch the whole
thing unwind end-to-end.
"""

from __future__ import annotations

import secrets

from app.auth.keys import generate_api_key
from app.domain.experiment import Experiment, Variant
from app.domain.identity import Tenant
from app.ports.experiment_store import ExperimentStore
from app.ports.identity_store import IdentityStore
from app.saga.engine import SagaDefinition, Step

DEFAULT_QUOTA = 1000


def build_onboarding_saga(
    identity: IdentityStore,
    experiments: ExperimentStore,
    *,
    simulate_failure: bool = False,
) -> SagaDefinition:

    async def create_tenant(ctx: dict) -> dict:
        tenant = Tenant(
            id=f"t_{secrets.token_hex(6)}",
            name=ctx["name"],
            monthly_quota=ctx.get("monthly_quota") or DEFAULT_QUOTA,
        )
        await identity.add_tenant(tenant)
        return {"tenant_id": tenant.id, "monthly_quota": tenant.monthly_quota}

    async def undo_create_tenant(ctx: dict) -> None:
        await identity.delete_tenant(ctx["tenant_id"])

    async def issue_key(ctx: dict) -> dict:
        plaintext, key = generate_api_key(ctx["tenant_id"])
        await identity.add_api_key(key)
        return {"key_id": key.id, "key_prefix": key.prefix, "api_key": plaintext}

    async def undo_issue_key(ctx: dict) -> None:
        await identity.delete_api_key(ctx["key_id"])

    async def seed_experiment(ctx: dict) -> dict:
        key = f"welcome-{ctx['tenant_id']}"
        await experiments.add(Experiment(
            key=key, description="onboarding feature flag",
            variants=[Variant("control", 1)],
        ))
        return {"experiment_key": key}

    async def undo_seed_experiment(ctx: dict) -> None:
        await experiments.delete(ctx["experiment_key"])

    steps = [
        Step("create-tenant", create_tenant, undo_create_tenant),
        Step("issue-key", issue_key, undo_issue_key),
        Step("seed-experiment", seed_experiment, undo_seed_experiment),
    ]

    if simulate_failure:
        async def _boom(ctx: dict) -> None:
            raise RuntimeError("simulated failure to demonstrate compensation")

        steps.append(Step("simulate-failure", _boom, None))

    return SagaDefinition(name="tenant-onboarding", steps=steps)
