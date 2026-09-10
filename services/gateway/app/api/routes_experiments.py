"""
Experimentation API (Stage 9) — A/B tests & feature flags.

Two audiences on one router:
  * **admin plane** (X-Admin-Token) manages experiment definitions + reads results;
  * **tenant plane** (API key / JWT with jobs:read) resolves its own assignment.

  POST /v1/experiments                 create an experiment            (admin)
  GET  /v1/experiments                 list experiments                (admin)
  POST /v1/experiments/{key}/status    activate/pause/complete         (admin)
  GET  /v1/experiments/{key}/results   per-variant exposure counts     (admin)
  GET  /v1/experiments/{key}/assignment  this tenant's variant (+expose) (tenant)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.deps import get_experiment_service, require_admin, require_scope
from app.api.schemas import (
    AssignmentOut,
    ExperimentCreate,
    ExperimentOut,
    ExperimentStatusUpdate,
)
from app.application.experiment_service import ExperimentService
from app.auth.scopes import JOBS_READ
from app.domain.experiment import Experiment, Variant
from app.domain.identity import Principal

router = APIRouter(prefix="/v1/experiments", tags=["experiments"])


# ---- admin plane --------------------------------------------------------

@router.post(
    "", response_model=ExperimentOut, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_experiment(
    body: ExperimentCreate,
    service: ExperimentService = Depends(get_experiment_service),
) -> ExperimentOut:
    exp = Experiment(
        key=body.key,
        description=body.description,
        variants=[Variant(name=v.name, weight=v.weight, config=v.config) for v in body.variants],
    )
    return ExperimentOut.from_entity(await service.create(exp))


@router.get("", response_model=list[ExperimentOut], dependencies=[Depends(require_admin)])
async def list_experiments(
    service: ExperimentService = Depends(get_experiment_service),
) -> list[ExperimentOut]:
    return [ExperimentOut.from_entity(e) for e in await service.list()]


@router.post(
    "/{key}/status", response_model=ExperimentOut, dependencies=[Depends(require_admin)]
)
async def set_status(
    key: str,
    body: ExperimentStatusUpdate,
    service: ExperimentService = Depends(get_experiment_service),
) -> ExperimentOut:
    return ExperimentOut.from_entity(await service.set_status(key, body.status))


@router.get("/{key}/results", dependencies=[Depends(require_admin)])
async def results(
    key: str,
    service: ExperimentService = Depends(get_experiment_service),
) -> dict:
    return await service.results(key)


# ---- tenant plane -------------------------------------------------------

@router.get("/{key}/assignment", response_model=AssignmentOut)
async def assignment(
    key: str,
    service: ExperimentService = Depends(get_experiment_service),
    principal: Principal = Depends(require_scope(JOBS_READ)),
) -> AssignmentOut:
    # The unit is the tenant, so a tenant gets a stable variant and every call
    # records (idempotently) that it was exposed.
    a = await service.assignment(key, principal.tenant_id, record=True)
    return AssignmentOut.from_entity(a)
