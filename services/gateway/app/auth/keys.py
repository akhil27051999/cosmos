"""
API-key minting and verification — the crypto-hygiene of Stage 6.

An API key here looks like:   csk_<key_id>_<secret>
                              └──┘ └────┘ └──────┘
                              scheme  public   secret half
                                      lookup    (never stored in the clear)

Why this shape:
  * The `key_id` is a NON-secret handle. We index API-key rows by it, so
    authentication is a single indexed lookup — we never scan every key.
  * The `secret` is high-entropy random. We store only `sha256(secret)`; on each
    request we hash the presented secret and compare in constant time. A DB leak
    exposes hashes, not usable keys.

Why sha256 and not bcrypt/argon2: those slow KDFs exist to make *low-entropy*
human passwords expensive to brute-force. An API key's secret is 24+ random bytes
— brute force is already infeasible — so a fast hash is the right, standard
choice for tokens. (Passwords would be a different story.)

This module is pure/stdlib (hashlib, hmac, secrets) so it needs no dependency and
is trivially unit-testable.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from app.domain.identity import ApiKey

_SCHEME = "csk"  # "cosmos secret key"


def _hash_secret(secret: str) -> str:
    """One-way hash of the secret half. Same input -> same hash (for lookup)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_api_key(tenant_id: str) -> tuple[str, ApiKey]:
    """Mint a new key for a tenant.

    Returns `(plaintext, record)`:
      * `plaintext` — the full `csk_<id>_<secret>` string, shown to the user ONCE
        and never persisted.
      * `record` — the `ApiKey` to store, holding only the hash + public parts.
    """
    key_id = secrets.token_hex(6)          # 12 hex chars, the public lookup handle
    secret = secrets.token_urlsafe(24)     # ~32 chars of entropy, the secret half
    plaintext = f"{_SCHEME}_{key_id}_{secret}"
    record = ApiKey(
        id=key_id,
        tenant_id=tenant_id,
        secret_hash=_hash_secret(secret),
        prefix=f"{_SCHEME}_{key_id}",       # safe to show; identifies the key
    )
    return plaintext, record


def parse_api_key(raw: str) -> tuple[str, str] | None:
    """Split a presented key into `(key_id, secret)`, or None if malformed.

    We split on the first two underscores only (`maxsplit=2`) because the secret
    itself may contain '_' or '-' (url-safe base64). So "csk_ID_a_b" parses to
    key_id="ID", secret="a_b".
    """
    if not raw:
        return None
    parts = raw.split("_", 2)
    if len(parts) != 3 or parts[0] != _SCHEME or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def verify_secret(secret: str, record: ApiKey) -> bool:
    """Constant-time check that `secret` matches the stored hash.

    `hmac.compare_digest` avoids a timing side-channel that a naive `==` on the
    hashes could leak. (Belt-and-braces: the hashes are equal-length hex anyway.)
    """
    return hmac.compare_digest(_hash_secret(secret), record.secret_hash)
