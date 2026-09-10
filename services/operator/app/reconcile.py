"""
The Cosmos operator's RECONCILE logic — pure, framework-free, unit-testable.

An operator watches a Custom Resource (here `CosmosTenant`) and drives the real
world toward the resource's declared spec. This module is that logic with NO kopf
and NO kubernetes-client imports, so it runs in plain `pytest`: given a desired
`TenantSpec` and the CR's current status, decide what to do and return a result.
The kopf handlers (handlers.py) are thin wrappers that do the actual k8s I/O
around this function.

The golden rule of operators is **idempotency / level-triggered reconciliation**:
the handler may fire many times for the same resource (creates, resumes after an
operator restart, periodic re-syncs). Reconciling must therefore be safe to run
repeatedly — we must NOT mint a fresh tenant on every reconcile. So we key on the
CR's status: once `tenantId` is recorded, provisioning is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class TenantSpec:
    """The desired state, read from the CosmosTenant's `spec`."""

    display_name: str
    monthly_quota: int | None = None


@dataclass
class ReconcileResult:
    """What the handler should apply after reconciling."""

    status_patch: dict          # merged into the CR's `status`
    secret_data: dict | None    # non-None => create/update the credentials Secret
    message: str
    created: bool               # True only when a new tenant was provisioned


class AdminClient(Protocol):
    """The gateway's admin API, as the operator needs it (a port)."""

    async def create_tenant(self, name: str, monthly_quota: int | None) -> dict:
        """Provision a tenant; return {tenant_id, api_key, key_prefix, monthly_quota}."""
        ...


async def reconcile(
    spec: TenantSpec,
    current_status: dict,
    admin: AdminClient,
    secret_name: str,
) -> ReconcileResult:
    """Decide what to do for one CosmosTenant, idempotently.

    * Already provisioned (status has `tenantId`) -> no-op (do NOT create again).
    * Otherwise -> call the admin API, and return the credentials to store in a
      Secret plus the status to record on the CR.
    """
    existing = current_status.get("tenantId")
    if existing:
        # Level-triggered: nothing to do. Reassert Ready so a resume/re-sync
        # leaves the status correct, but never re-mint (that would leak tenants).
        return ReconcileResult(
            status_patch={"phase": "Ready", "tenantId": existing},
            secret_data=None,
            message=f"tenant {existing} already provisioned",
            created=False,
        )

    result = await admin.create_tenant(spec.display_name, spec.monthly_quota)
    tenant_id = result["tenant_id"]
    status_patch = {
        "phase": "Ready",
        "tenantId": tenant_id,
        "keyPrefix": result.get("key_prefix", ""),
        "monthlyQuota": result.get("monthly_quota"),
        "secretName": secret_name,
    }
    # The plaintext API key is shown by the gateway exactly once — capture it into
    # a Secret now (the only place it can be persisted).
    secret_data = {
        "tenant_id": tenant_id,
        "api_key": result["api_key"],
        "key_prefix": result.get("key_prefix", ""),
    }
    return ReconcileResult(
        status_patch=status_patch,
        secret_data=secret_data,
        message=f"provisioned tenant {tenant_id}",
        created=True,
    )
