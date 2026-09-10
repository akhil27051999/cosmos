"""
Stage 7 (part 1) — OAuth2/OIDC JWT auth + scope-based authorization tests.

Two layers:
  * pure unit tests of the JWT verifier (`app/auth/jwt.py`) — signature, exp/nbf,
    iss/aud, alg confusion;
  * HTTP tests proving a JWT authenticates the same routes as an API key, and
    that scopes gate what a token may DO (authZ vs authN).
"""

from __future__ import annotations

import time

import pytest

from app.auth.jwt import JWTError, decode_and_verify, encode_hs256

pytestmark = pytest.mark.asyncio

SECRET = "test-jwt-secret"   # matches conftest's JWT_HS256_SECRET


def _token(env, **claims) -> str:
    base = {"tenant_id": env.tenant_id, "scope": "jobs:read jobs:write", "sub": "user-1"}
    base.update(claims)
    return encode_hs256(base, env.jwt_secret)


# ---- pure verifier ------------------------------------------------------

async def test_jwt_roundtrip_returns_claims():
    tok = encode_hs256({"sub": "abc", "tenant_id": "t1"}, SECRET)
    claims = decode_and_verify(tok, secret=SECRET)
    assert claims["sub"] == "abc" and claims["tenant_id"] == "t1"


async def test_jwt_bad_signature_rejected():
    tok = encode_hs256({"sub": "abc"}, SECRET)
    with pytest.raises(JWTError):
        decode_and_verify(tok, secret="wrong-secret")


async def test_jwt_expired_and_nbf_rejected():
    now = 1_000_000
    expired = encode_hs256({"exp": now - 100}, SECRET)
    with pytest.raises(JWTError):
        decode_and_verify(expired, secret=SECRET, now=now, leeway=10)

    future = encode_hs256({"nbf": now + 100}, SECRET)
    with pytest.raises(JWTError):
        decode_and_verify(future, secret=SECRET, now=now, leeway=10)


async def test_jwt_issuer_and_audience_checked():
    tok = encode_hs256({"iss": "https://idp.example", "aud": "cosmos"}, SECRET)
    # correct iss/aud passes; wrong ones raise
    assert decode_and_verify(tok, secret=SECRET, issuer="https://idp.example", audience="cosmos")
    with pytest.raises(JWTError):
        decode_and_verify(tok, secret=SECRET, issuer="https://evil")
    with pytest.raises(JWTError):
        decode_and_verify(tok, secret=SECRET, audience="other-service")


async def test_jwt_rejects_alg_none_confusion():
    # A token forged with alg=none must not be accepted.
    import base64, json
    def b(x): return base64.urlsafe_b64encode(x).rstrip(b"=").decode()
    header = b(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = b(json.dumps({"sub": "attacker"}).encode())
    forged = f"{header}.{payload}."
    with pytest.raises(JWTError):
        decode_and_verify(forged, secret=SECRET)


# ---- JWT over HTTP ------------------------------------------------------

async def test_jwt_authenticates_a_submit(env):
    h = {"Authorization": f"Bearer {_token(env)}"}
    r = await env.client.post("/v1/jobs", json={"payload": {"x": 1}}, headers=h)
    assert r.status_code == 202
    assert r.json()["tenant_id"] == env.tenant_id      # tenant came from the claim


async def test_jwt_is_scope_gated(env):
    # A read-only token can list but cannot submit or cancel.
    ro = {"Authorization": f"Bearer {_token(env, scope='jobs:read')}"}

    assert (await env.client.get("/v1/jobs", headers=ro)).status_code == 200
    r = await env.client.post("/v1/jobs", json={"payload": {"x": 1}}, headers=ro)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


async def test_jwt_for_unknown_tenant_is_403(env):
    h = {"Authorization": f"Bearer {_token(env, tenant_id='t_nonexistent')}"}
    r = await env.client.get("/v1/jobs", headers=h)
    assert r.status_code == 403


async def test_expired_jwt_over_http_is_401(env):
    h = {"Authorization": f"Bearer {_token(env, exp=int(time.time()) - 3600)}"}
    r = await env.client.get("/v1/jobs", headers=h)
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Bearer"


async def test_garbage_bearer_is_401(env):
    r = await env.client.get("/v1/jobs", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


async def test_api_key_still_works_and_has_full_scope(env):
    # Regression: the Stage-6 API key path is unchanged and full-access.
    r = await env.client.post("/v1/jobs", json={"payload": {"x": 1}})   # default = api key
    assert r.status_code == 202
