"""
Unit tests for the operator's pure reconcile logic — no kopf, no cluster.

These prove the two properties that matter for a correct operator:
  * it provisions a tenant (and captures the one-time API key into a Secret) when
    the CR has no status yet;
  * it is IDEMPOTENT — a second reconcile (create/resume/update re-fire) does NOT
    mint another tenant.
"""

from __future__ import annotations

import pytest

from app.reconcile import TenantSpec, reconcile

pytestmark = pytest.mark.asyncio


class FakeAdmin:
    """Records calls and returns a canned tenant, like the gateway admin API."""

    def __init__(self):
        self.calls = []

    async def create_tenant(self, name, monthly_quota):
        self.calls.append((name, monthly_quota))
        return {
            "tenant_id": f"t_{len(self.calls):04d}",
            "api_key": "csk_abc_secretsecret",
            "key_prefix": "csk_abc",
            "monthly_quota": monthly_quota or 1000,
        }


async def test_provisions_when_no_status():
    admin = FakeAdmin()
    spec = TenantSpec(display_name="Acme", monthly_quota=250)

    result = await reconcile(spec, {}, admin, secret_name="acme-credentials")

    assert result.created is True
    assert admin.calls == [("Acme", 250)]                 # called the admin API once
    assert result.status_patch["phase"] == "Ready"
    assert result.status_patch["tenantId"] == "t_0001"
    assert result.status_patch["secretName"] == "acme-credentials"
    # the one-time API key is captured for the Secret
    assert result.secret_data["api_key"] == "csk_abc_secretsecret"
    assert result.secret_data["tenant_id"] == "t_0001"


async def test_idempotent_when_already_provisioned():
    admin = FakeAdmin()
    spec = TenantSpec(display_name="Acme", monthly_quota=250)
    status = {"tenantId": "t_9999", "phase": "Ready"}

    result = await reconcile(spec, status, admin, secret_name="acme-credentials")

    assert result.created is False
    assert admin.calls == []                              # did NOT mint another tenant
    assert result.secret_data is None                    # no new Secret to write
    assert result.status_patch["tenantId"] == "t_9999"


async def test_quota_omitted_passes_none():
    admin = FakeAdmin()
    spec = TenantSpec(display_name="NoQuota")

    await reconcile(spec, {}, admin, secret_name="nq-credentials")

    assert admin.calls == [("NoQuota", None)]            # server default applies
