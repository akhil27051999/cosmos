"""
Admin bootstrap API (Stage 6) — create tenants and mint API keys.

This is how the very first credentials come into existence: there's a
chicken-and-egg problem (you need a key to call the platform, but no key exists
yet), so these endpoints are guarded by a separate shared secret — the
`ADMIN_TOKEN` — sent as `X-Admin-Token`, NOT by an API key.

  POST /v1/admin/tenants               create a tenant + its first API key
  POST /v1/admin/tenants/{id}/keys     mint an additional key for a tenant

The freshly minted key's plaintext is returned ONCE and never again (we store
only its hash). In a real product this surface would live behind an internal
network / stronger auth; the shared-token guard keeps the learning build honest
about "the admin plane is separate from the tenant plane".
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, status

from app.api.deps import get_identity_store, get_worker_control, require_admin
from app.api.schemas import ApiKeyCreated, TenantCreate, TenantCreated, WorkerStatusOut
from app.auth.keys import generate_api_key
from app.core.config import get_settings
from app.domain.exceptions import JobNotFound, ServiceUnavailable
from app.domain.identity import Tenant
from app.ports.identity_store import IdentityStore
from app.ports.worker_control import WorkerControlClient

# `dependencies=[require_admin]` gates EVERY route on this router — none of them
# is reachable without a valid admin token.
router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post("/tenants", response_model=TenantCreated, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: TenantCreate,
    identity: IdentityStore = Depends(get_identity_store),
) -> TenantCreated:
    settings = get_settings()
    tenant = Tenant(
        id=f"t_{secrets.token_hex(6)}",
        name=body.name,
        monthly_quota=body.monthly_quota or settings.default_monthly_quota,
    )
    await identity.add_tenant(tenant)

    plaintext, key = generate_api_key(tenant.id)
    await identity.add_api_key(key)

    return TenantCreated(
        tenant_id=tenant.id, name=tenant.name, monthly_quota=tenant.monthly_quota,
        key_prefix=key.prefix, api_key=plaintext,
    )


@router.post(
    "/tenants/{tenant_id}/keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_key(
    tenant_id: str,
    identity: IdentityStore = Depends(get_identity_store),
) -> ApiKeyCreated:
    # Only mint keys for a tenant that actually exists.
    if await identity.get_tenant(tenant_id) is None:
        raise JobNotFound(f"tenant '{tenant_id}'")
    plaintext, key = generate_api_key(tenant_id)
    await identity.add_api_key(key)
    return ApiKeyCreated(tenant_id=tenant_id, key_prefix=key.prefix, api_key=plaintext)


@router.get("/worker/status", response_model=WorkerStatusOut)
async def worker_status(
    client: WorkerControlClient = Depends(get_worker_control),
) -> WorkerStatusOut:
    # Operational, cross-tenant info -> lives on the admin plane. The gateway
    # calls the worker synchronously over gRPC (the control plane). If the worker
    # is unreachable, surface a clean 503 rather than a 500.
    try:
        status = await client.get_status()
    except Exception as exc:  # noqa: BLE001 - any transport failure => unavailable
        raise ServiceUnavailable(f"worker control plane unreachable: {exc}") from exc
    return WorkerStatusOut.from_entity(status)
