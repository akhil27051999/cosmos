"""
Stage 6 — identity & multi-tenancy tests.

Covers the four pillars: API-key crypto, authentication (401), tenant isolation
(one tenant can't see another's data), per-tenant quota (429), and the admin
bootstrap that mints the first credentials.
"""

from __future__ import annotations

import pytest

from app.auth.keys import generate_api_key, parse_api_key, verify_secret
from app.domain.identity import KeyStatus, Tenant, TenantStatus

pytestmark = pytest.mark.asyncio


# ---- API-key crypto (pure, no app) --------------------------------------

async def test_api_key_roundtrip_and_verify():
    plaintext, record = generate_api_key("t_abc")
    assert plaintext.startswith("csk_")
    assert record.secret_hash and record.secret_hash not in plaintext  # only the hash is kept

    key_id, secret = parse_api_key(plaintext)
    assert key_id == record.id
    assert verify_secret(secret, record) is True
    assert verify_secret("wrong-secret", record) is False


async def test_parse_rejects_malformed_keys():
    assert parse_api_key("") is None
    assert parse_api_key("nope") is None
    assert parse_api_key("bearer_x_y") is None          # wrong scheme
    assert parse_api_key("csk_only") is None            # missing secret
    # a secret containing underscores survives (split on first two "_" only)
    assert parse_api_key("csk_id_a_b_c") == ("id", "a_b_c")


# ---- authentication -----------------------------------------------------

async def test_missing_key_is_401(env):
    r = await env.client.post("/v1/jobs", json={"payload": {"x": 1}},
                              headers={"Authorization": ""})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"
    assert r.headers.get("WWW-Authenticate") == "Bearer"


async def test_invalid_key_is_401(env):
    r = await env.client.get("/v1/jobs",
                             headers={"Authorization": "Bearer csk_deadbeef01_nope"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


async def test_x_api_key_header_also_works(env):
    # The alternate header form authenticates the same way.
    r = await env.client.get("/v1/jobs", headers={"Authorization": "", "X-API-Key": env.api_key})
    assert r.status_code == 200


# ---- tenant isolation ---------------------------------------------------

async def _add_tenant(env, tenant: Tenant) -> str:
    """Seed a tenant + key in the in-memory store; return the plaintext key."""
    await env.identity.add_tenant(tenant)
    plaintext, record = generate_api_key(tenant.id)
    await env.identity.add_api_key(record)
    return plaintext


async def test_one_tenant_cannot_see_anothers_job(env):
    # Tenant A (the default seeded tenant) submits a job.
    job = (await env.client.post("/v1/jobs", json={"payload": {"secret": 1}})).json()

    # Tenant B authenticates with its own key.
    b_key = await _add_tenant(env, Tenant(id="t_other", name="Other"))
    b = {"Authorization": f"Bearer {b_key}"}

    # B fetching A's job id gets a 404 — never a 403, never the data. The id's
    # very existence is not leaked across the tenant boundary.
    r = await env.client.get(f"/v1/jobs/{job['id']}", headers=b)
    assert r.status_code == 404

    # B's list and cancel are equally blind to A's job.
    assert (await env.client.get("/v1/jobs", headers=b)).json() == []
    assert (await env.client.post(f"/v1/jobs/{job['id']}/cancel", headers=b)).status_code == 404

    # A still sees exactly its own one job.
    assert len((await env.client.get("/v1/jobs")).json()) == 1


# ---- quota --------------------------------------------------------------

async def test_quota_exceeded_returns_429(env):
    key = await _add_tenant(env, Tenant(id="t_small", name="Small", monthly_quota=1))
    h = {"Authorization": f"Bearer {key}"}

    r1 = await env.client.post("/v1/jobs", json={"payload": {"n": 1}}, headers=h)
    r2 = await env.client.post("/v1/jobs", json={"payload": {"n": 2}}, headers=h)

    assert r1.status_code == 202                     # first submit fits the quota
    assert r2.status_code == 429                      # second is over the monthly quota
    assert r2.json()["error"]["code"] == "quota_exceeded"
    assert r2.headers.get("Retry-After")             # tells the client when to retry


async def test_idempotent_replay_does_not_consume_quota(env):
    key = await _add_tenant(env, Tenant(id="t_q1", name="Q1", monthly_quota=1))
    h = {"Authorization": f"Bearer {key}", "Idempotency-Key": "same"}

    r1 = await env.client.post("/v1/jobs", json={"payload": {"n": 1}}, headers=h)
    r2 = await env.client.post("/v1/jobs", json={"payload": {"n": 1}}, headers=h)
    assert r1.status_code == 202
    assert r2.status_code == 200                      # replay, not new consumption
    assert r1.json()["id"] == r2.json()["id"]


async def test_suspended_tenant_is_403(env):
    key = await _add_tenant(
        env, Tenant(id="t_susp", name="Suspended", status=TenantStatus.SUSPENDED)
    )
    r = await env.client.get("/v1/jobs", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


async def test_revoked_key_is_401(env):
    # Seed a tenant, then revoke its key directly.
    tenant = Tenant(id="t_rev", name="Rev")
    await env.identity.add_tenant(tenant)
    plaintext, record = generate_api_key(tenant.id)
    record.status = KeyStatus.REVOKED
    await env.identity.add_api_key(record)

    r = await env.client.get("/v1/jobs", headers={"Authorization": f"Bearer {plaintext}"})
    assert r.status_code == 401


# ---- admin bootstrap ----------------------------------------------------

async def test_admin_creates_tenant_and_key_that_works(env):
    admin = {"X-Admin-Token": env.admin_token}

    created = await env.client.post(
        "/v1/admin/tenants", json={"name": "Acme", "monthly_quota": 5}, headers=admin
    )
    assert created.status_code == 201
    body = created.json()
    assert body["tenant_id"].startswith("t_")
    assert body["monthly_quota"] == 5
    new_key = body["api_key"]

    # The freshly minted key authenticates and its jobs are its own tenant's.
    r = await env.client.post(
        "/v1/jobs", json={"payload": {"hello": 1}},
        headers={"Authorization": f"Bearer {new_key}"},
    )
    assert r.status_code == 202
    assert r.json()["tenant_id"] == body["tenant_id"]


async def test_admin_requires_the_admin_token(env):
    # No token at all -> 401.
    r = await env.client.post("/v1/admin/tenants", json={"name": "X"})
    assert r.status_code == 401
    # Wrong token -> 401.
    r = await env.client.post(
        "/v1/admin/tenants", json={"name": "X"}, headers={"X-Admin-Token": "nope"}
    )
    assert r.status_code == 401
