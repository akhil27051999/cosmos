"""
Postgres IdentityStore — reads/writes tenants and API keys.

Like the CQRS read model, this uses its own short sessions (a sessionmaker),
because authentication runs as a dependency *before* the request's write-side
session exists, and admin bootstrap is a self-contained write.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.postgres.identity_models import ApiKeyModel, TenantModel
from app.domain.identity import ApiKey, KeyStatus, Tenant, TenantStatus


def _tenant_to_entity(row: TenantModel) -> Tenant:
    return Tenant(
        id=row.id, name=row.name, status=TenantStatus(row.status),
        monthly_quota=row.monthly_quota, created_at=row.created_at,
    )


def _key_to_entity(row: ApiKeyModel) -> ApiKey:
    return ApiKey(
        id=row.id, tenant_id=row.tenant_id, secret_hash=row.secret_hash,
        prefix=row.prefix, status=KeyStatus(row.status), created_at=row.created_at,
    )


class PostgresIdentityStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def add_tenant(self, tenant: Tenant) -> None:
        async with self._sm() as session:
            session.add(TenantModel(
                id=tenant.id, name=tenant.name, status=tenant.status.value,
                monthly_quota=tenant.monthly_quota, created_at=tenant.created_at,
            ))
            await session.commit()

    async def get_tenant(self, tenant_id: str) -> Tenant | None:
        async with self._sm() as session:
            row = await session.get(TenantModel, tenant_id)
            return _tenant_to_entity(row) if row else None

    async def add_api_key(self, key: ApiKey) -> None:
        async with self._sm() as session:
            session.add(ApiKeyModel(
                id=key.id, tenant_id=key.tenant_id, secret_hash=key.secret_hash,
                prefix=key.prefix, status=key.status.value, created_at=key.created_at,
            ))
            await session.commit()

    async def get_api_key(self, key_id: str) -> ApiKey | None:
        async with self._sm() as session:
            row = await session.get(ApiKeyModel, key_id)
            return _key_to_entity(row) if row else None

    async def delete_tenant(self, tenant_id: str) -> None:
        async with self._sm() as session:
            row = await session.get(TenantModel, tenant_id)
            if row is not None:
                await session.delete(row)
                await session.commit()

    async def delete_api_key(self, key_id: str) -> None:
        async with self._sm() as session:
            row = await session.get(ApiKeyModel, key_id)
            if row is not None:
                await session.delete(row)
                await session.commit()
