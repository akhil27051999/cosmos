"""In-memory IdentityStore — for tests and local wiring. Two dicts, no infra."""

from __future__ import annotations

import copy

from app.domain.identity import ApiKey, Tenant


class InMemoryIdentityStore:
    def __init__(self) -> None:
        self._tenants: dict[str, Tenant] = {}
        self._keys: dict[str, ApiKey] = {}     # key_id -> ApiKey

    async def add_tenant(self, tenant: Tenant) -> None:
        self._tenants[tenant.id] = copy.deepcopy(tenant)

    async def get_tenant(self, tenant_id: str) -> Tenant | None:
        found = self._tenants.get(tenant_id)
        return copy.deepcopy(found) if found else None

    async def add_api_key(self, key: ApiKey) -> None:
        self._keys[key.id] = copy.deepcopy(key)

    async def get_api_key(self, key_id: str) -> ApiKey | None:
        found = self._keys.get(key_id)
        return copy.deepcopy(found) if found else None

    async def delete_tenant(self, tenant_id: str) -> None:
        self._tenants.pop(tenant_id, None)   # idempotent

    async def delete_api_key(self, key_id: str) -> None:
        self._keys.pop(key_id, None)         # idempotent
