"""
RED-metrics + access-log middleware (Stage 10).

RED = **R**ate, **E**rrors, **D**uration — the three signals you want for every
request-driven service. This records all three per route:
  * Rate/Errors: `http_requests_total{method,path,status}` (errors = 5xx/4xx share)
  * Duration:    `http_request_duration_seconds` histogram (p50/p95/p99)
  * plus `http_requests_in_flight` (concurrency) and a structured access log line.

Written as a **pure ASGI middleware**, not Starlette's `BaseHTTPMiddleware`, on
purpose: BaseHTTPMiddleware buffers the response body, which would break the SSE
streaming endpoint. Pure ASGI only inspects the `http.response.start` message for
the status code and never touches the body, so streaming stays intact.

The `path` label is the matched **route template** (`/v1/jobs/{job_id}`), not the
raw URL — otherwise every distinct id would be its own label value and blow up
cardinality.
"""

from __future__ import annotations

import time

from app.observability.logging import get_logger
from app.observability.metrics import (
    http_request_duration_seconds,
    http_requests_in_flight,
    http_requests_total,
)

_log = get_logger("access")


def _route_template(scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    # No route matched (404) — bucket under a constant so random URLs don't
    # explode the label space.
    return "__unmatched__"


class MetricsMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        method = scope["method"]
        start = time.perf_counter()
        http_requests_in_flight.inc(method=method)
        status_holder = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - start
            http_requests_in_flight.dec(method=method)
            path = _route_template(scope)
            status = str(status_holder["code"])
            http_requests_total.inc(method=method, path=path, status=status)
            http_request_duration_seconds.observe(duration, method=method, path=path)
            _log.info(
                "request",
                extra={
                    "http_method": method,
                    "http_path": path,
                    "http_status": status_holder["code"],
                    "duration_ms": round(duration * 1000, 2),
                },
            )
