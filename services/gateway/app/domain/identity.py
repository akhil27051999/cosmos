"""
The identity domain — tenants, API keys, and the authenticated principal.

Stage 6 turns the platform from single-user into **multi-tenant**: every request
now belongs to a *tenant* (a customer/org), proven by an *API key*, and every
piece of data is scoped to that tenant. This file is the pure-domain core of that
story — no FastAPI, no SQLAlchemy, no hashing library. It only describes WHAT a
tenant / key / principal *is* and the rules about their state.

Three concepts:
  * `Tenant`   — the account that owns jobs; has a status and a monthly quota.
  * `ApiKey`   — a credential belonging to a tenant. We store only a *hash* of
                 the secret (see app/auth/keys.py), never the secret itself.
  * `Principal`— the result of authenticating one request: "this call is tenant
                 X, via key Y". The routes carry it and scope everything to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TenantStatus(str, Enum):
    ACTIVE = "active"        # may submit and read
    SUSPENDED = "suspended"  # blocked (unpaid, abuse, ...) -> 403 on use


class KeyStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"      # key disabled -> 401 on use


@dataclass
class Tenant:
    """A customer/organisation. The unit of isolation and of billing/quotas."""

    id: str
    name: str
    status: TenantStatus = TenantStatus.ACTIVE
    # Simple monthly submission quota — the coarse "how much may you use" lever.
    # Enforced per calendar month against the usage ledger (see UsageLedger).
    monthly_quota: int = 1000
    created_at: datetime = field(default_factory=_now)

    @property
    def is_active(self) -> bool:
        return self.status is TenantStatus.ACTIVE


@dataclass
class ApiKey:
    """A credential that authenticates as its tenant.

    SECURITY: `secret_hash` is a one-way hash of the key's secret half — the
    plaintext key is shown to the user exactly ONCE at creation and never stored.
    A database leak therefore exposes no usable credentials. `prefix` is the
    non-secret, displayable part (e.g. "csk_ab12cd34") so a key can be identified
    in a UI/audit log without revealing anything sensitive.
    """

    id: str              # the public key-id used to look the row up (the prefix's tail)
    tenant_id: str
    secret_hash: str     # sha256 of the secret half — never the secret itself
    prefix: str          # displayable, non-secret label e.g. "csk_ab12cd34"
    status: KeyStatus = KeyStatus.ACTIVE
    created_at: datetime = field(default_factory=_now)

    @property
    def is_active(self) -> bool:
        return self.status is KeyStatus.ACTIVE


@dataclass(frozen=True)
class Principal:
    """The authenticated caller for one request.

    Immutable on purpose: once we've decided who you are, nothing downstream
    should be able to change it. Routes read `principal.tenant_id` to scope every
    query and stamp every write (tenant isolation), and `principal.scopes` to
    decide what the caller is allowed to DO (authorization, Stage 7).

    `credential` records HOW the caller authenticated ("api_key" or "jwt") — handy
    for audit logs and for reasoning about the two supported schemes.
    """

    tenant_id: str
    key_id: str
    scopes: frozenset[str] = frozenset()
    credential: str = "api_key"

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes
