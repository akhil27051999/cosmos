"""
HTTP client to the gateway's admin API — the real `AdminClient` the operator uses.

Kept thin and behind the `AdminClient` port (see reconcile.py) so tests inject a
fake and never touch the network. `httpx` is imported lazily so importing this
module (or the pure reconcile logic) doesn't require it.
"""

from __future__ import annotations


class HttpAdminClient:
    def __init__(self, base_url: str, admin_token: str, timeout: float = 10.0) -> None:
        # base_url e.g. "http://cosmos-gateway:8000"
        self._base_url = base_url.rstrip("/")
        self._admin_token = admin_token
        self._timeout = timeout

    async def create_tenant(self, name: str, monthly_quota: int | None) -> dict:
        import httpx  # lazy

        payload: dict = {"name": name}
        if monthly_quota is not None:
            payload["monthly_quota"] = monthly_quota

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base_url}/v1/admin/tenants",
                headers={"X-Admin-Token": self._admin_token},
                json=payload,
            )
        resp.raise_for_status()   # non-2xx -> raise -> handler retries (TemporaryError)
        return resp.json()
