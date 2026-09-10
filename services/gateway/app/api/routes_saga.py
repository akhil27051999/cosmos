"""
Saga API (Stage 11) — run and inspect the tenant-onboarding saga (admin plane).

  POST /v1/admin/onboarding[?simulate_failure=true]   run the onboarding saga
  GET  /v1/admin/sagas/{saga_id}                       inspect a saga instance

On success the response's `context` carries the provisioned tenant id + one-time
API key; on a simulated failure you'll see status `compensated` and the steps
rolled back (the tenant/key/experiment are gone again).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import (
    get_experiment_store,
    get_identity_store,
    get_saga_store,
    require_admin,
)
from app.api.schemas import OnboardingRequest, SagaInstanceOut
from app.domain.exceptions import JobNotFound
from app.ports.experiment_store import ExperimentStore
from app.ports.identity_store import IdentityStore
from app.ports.saga_store import SagaStore
from app.saga.engine import SagaOrchestrator
from app.saga.onboarding import build_onboarding_saga

router = APIRouter(prefix="/v1/admin", tags=["saga"], dependencies=[Depends(require_admin)])


@router.post("/onboarding", response_model=SagaInstanceOut)
async def onboard(
    body: OnboardingRequest,
    simulate_failure: bool = Query(False, description="append a failing step to demo compensation"),
    identity: IdentityStore = Depends(get_identity_store),
    experiments: ExperimentStore = Depends(get_experiment_store),
    sagas: SagaStore = Depends(get_saga_store),
) -> SagaInstanceOut:
    definition = build_onboarding_saga(identity, experiments, simulate_failure=simulate_failure)
    orchestrator = SagaOrchestrator(sagas)
    instance = await orchestrator.start(
        definition, {"name": body.name, "monthly_quota": body.monthly_quota}
    )
    return SagaInstanceOut.from_entity(instance)


@router.get("/sagas/{saga_id}", response_model=SagaInstanceOut)
async def get_saga(
    saga_id: str,
    sagas: SagaStore = Depends(get_saga_store),
) -> SagaInstanceOut:
    inst = await sagas.get(saga_id)
    if inst is None:
        raise JobNotFound(f"saga '{saga_id}'")
    return SagaInstanceOut.from_entity(inst)
