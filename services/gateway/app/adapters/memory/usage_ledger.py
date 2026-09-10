"""In-memory UsageLedger — a dict keyed by (tenant_id, period)."""

from __future__ import annotations


class InMemoryUsageLedger:
    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = {}

    async def get_usage(self, tenant_id: str, period: str) -> int:
        return self._counts.get((tenant_id, period), 0)

    async def record(self, tenant_id: str, period: str, amount: int = 1) -> int:
        key = (tenant_id, period)
        self._counts[key] = self._counts.get(key, 0) + amount
        return self._counts[key]
