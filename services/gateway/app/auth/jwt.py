"""
A minimal, dependency-free JWT verifier (HS256) — the OAuth2/OIDC token path.

A JWT is three base64url segments joined by dots:  header.payload.signature
  * header  — {"alg": "HS256", "typ": "JWT"}
  * payload — the CLAIMS (iss, aud, exp, sub, plus our tenant_id + scope)
  * signature — HMAC-SHA256(header.payload, secret)

This module implements the *validation* that matters for security:
  1. the signature is authentic (HMAC, constant-time compare) — proves the token
     wasn't forged or tampered with;
  2. it isn't expired (`exp`) or used too early (`nbf`), within a clock-skew leeway;
  3. it was minted by the expected issuer (`iss`) for us (`aud`) — so a token for
     a different service can't be replayed against this one.

Why HS256 and pure stdlib: HS256 is symmetric HMAC — no crypto library needed, so
tests stay hermetic and you can see exactly what "verify a JWT" means. A real
OIDC deployment uses **RS256** (asymmetric): the provider signs with a private
key and we verify with its public key fetched from a **JWKS** endpoint. That's a
later increment — it slots in here behind the same `decode_and_verify` shape,
just swapping the signature check for RSA-verify against the JWKS key by `kid`.
We NEVER hand-roll RSA; that path will use a vetted library (PyJWT/cryptography).

Hand-rolling HS256 is fine (it's a well-understood HMAC compare); hand-rolling
asymmetric crypto is not — hence the split.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class JWTError(Exception):
    """Any validation failure. The web layer maps this to 401 (never leaks why)."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    # JWT strips base64 padding; add it back before decoding.
    pad = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + pad)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise JWTError("malformed base64url segment") from exc


def _decode_json(segment: str):
    """base64url-decode a segment and parse it as JSON, else raise JWTError."""
    try:
        return json.loads(_b64url_decode(segment))
    except (ValueError, UnicodeDecodeError) as exc:
        raise JWTError("segment is not valid JSON") from exc


def _sign(signing_input: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()


def encode_hs256(claims: dict, secret: str) -> str:
    """Mint an HS256 JWT. Used by tests (and demonstrates the symmetry with verify)."""
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig_b64 = _b64url_encode(_sign(signing_input, secret))
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def decode_and_verify(
    token: str,
    *,
    secret: str,
    issuer: str | None = None,
    audience: str | None = None,
    leeway: int = 60,
    now: float | None = None,
) -> dict:
    """Verify signature + standard claims and return the payload, or raise JWTError.

    `leeway` (seconds) absorbs small clock differences between the signer and us,
    so a token that just expired (or is about to become valid) isn't rejected on a
    few seconds of skew.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise JWTError("token must have three segments")
    header_b64, payload_b64, sig_b64 = parts

    header = _decode_json(header_b64)
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        # Reject anything else — critically, refuse "none" and silently-different
        # algs (the classic JWT alg-confusion attack).
        raise JWTError(f"unsupported alg: {header.get('alg')!r}")

    # 1) signature — recompute and constant-time compare.
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = _sign(signing_input, secret)
    if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
        raise JWTError("bad signature")

    claims = _decode_json(payload_b64)
    if not isinstance(claims, dict):
        raise JWTError("payload is not a JSON object")
    now = time.time() if now is None else now

    # 2) time-based claims.
    if "exp" in claims and now > float(claims["exp"]) + leeway:
        raise JWTError("token expired")
    if "nbf" in claims and now < float(claims["nbf"]) - leeway:
        raise JWTError("token not yet valid")

    # 3) issuer / audience — only checked when we expect a specific value.
    if issuer is not None and claims.get("iss") != issuer:
        raise JWTError("unexpected issuer")
    if audience is not None:
        aud = claims.get("aud")
        allowed = aud if isinstance(aud, list) else [aud]
        if audience not in allowed:
            raise JWTError("unexpected audience")

    return claims
