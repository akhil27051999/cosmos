"""
The UsageLedger PORT — per-tenant consumption, for quota enforcement.

The ledger answers two questions:
  * "how much has tenant T used in period P?"  (to enforce a quota)
  * "record that tenant T just used N more."    (after accepting work)

`period` is a coarse bucket string — we use the calendar month "YYYY-MM" — so a
quota is "N submissions per month". Keeping the period a plain string (rather than
baking month-logic into the port) means a later stage can switch to daily buckets
or sliding windows without changing the interface.

This is deliberately separate from the Stage-4 rate limiter: the rate limiter
smooths *bursts* (requests/sec, protects the system); the ledger enforces a
*volume* entitlement over time (requests/month, the billing/fairness lever).
"""

from __future__ import annotations

from typing import Protocol


class UsageLedger(Protocol):
    async def get_usage(self, tenant_id: str, period: str) -> int:
        """How many units the tenant has consumed in `period` (0 if none)."""
        ...

    async def record(self, tenant_id: str, period: str, amount: int = 1) -> int:
        """Add `amount` to the tenant's usage for `period`; return the new total.

        Returning the running total lets a caller record-and-check atomically in
        adapters that support it (the Postgres one does the increment in SQL).
        """
        ...
