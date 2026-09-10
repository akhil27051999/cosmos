"""
The IdentityStore PORT — where tenants and API keys live.

Authentication needs two lookups: fetch an API key by its public `key_id`, and
fetch a tenant by id (to check it's active and read its quota). Admin bootstrap
needs to create both. That's the whole interface — kept tiny on purpose.

Like every port, it's a `Protocol`: the in-memory adapter (tests) and the
Postgres adapter (prod) both satisfy it without inheritance.
"""

from __future__ import annotations

from typing import Protocol

from app.domain.identity import ApiKey, Tenant


class IdentityStore(Protocol):
    async def add_tenant(self, tenant: Tenant) -> None:
        """Persist a new tenant."""
        ...

    async def get_tenant(self, tenant_id: str) -> Tenant | None:
        """Fetch a tenant (to check status + read its quota), or None."""
        ...

    async def add_api_key(self, key: ApiKey) -> None:
        """Persist a new API key (only the hash + public parts)."""
        ...

    async def get_api_key(self, key_id: str) -> ApiKey | None:
        """Fetch an API key by its public key-id — the auth hot path, one lookup."""
        ...

    async def delete_tenant(self, tenant_id: str) -> None:
        """Remove a tenant (used to compensate a failed onboarding saga). Idempotent."""
        ...

    async def delete_api_key(self, key_id: str) -> None:
        """Remove an API key (saga compensation / deprovision). Idempotent."""
        ...
