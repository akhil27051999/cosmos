"""
Chaos fault injection (Stage 12) — deliberately break things to prove resilience.

Chaos engineering validates that your resilience (retries, breakers, SLOs, alerts)
actually works by injecting controlled failure. This middleware can add latency
and/or return errors for a configured fraction of requests. It is **off by
default and must be explicitly enabled** — you never want random failures in prod
unless you asked for them (a game day).

State lives in a small mutable singleton (not read from cached settings) so an
operator — or a test — can toggle it at runtime. Faults are injected *inside* the
metrics middleware, so an injected error shows up in `http_requests_total` and
burns the SLO error budget — which is exactly the loop we want to demonstrate:
inject faults → availability drops → error budget falls (see /v1/admin/slo).
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass


@dataclass
class ChaosConfig:
    enabled: bool = False
    fault_rate: float = 0.0     # 0..1 — share of requests returned as errors
    latency_ms: int = 0         # extra latency added to every request when enabled


_config = ChaosConfig()


def configure(enabled: bool, fault_rate: float = 0.0, latency_ms: int = 0) -> None:
    _config.enabled = enabled
    _config.fault_rate = fault_rate
    _config.latency_ms = latency_ms


def current() -> ChaosConfig:
    return _config


async def _send_error(send) -> None:
    body = json.dumps(
        {"error": {"code": "chaos_injected", "message": "fault injected by chaos"}}
    ).encode()
    await send({
        "type": "http.response.start",
        "status": 503,
        "headers": [(b"content-type", b"application/json"), (b"x-chaos-injected", b"1")],
    })
    await send({"type": "http.response.body", "body": body})


class ChaosMiddleware:
    """Pure-ASGI so it can short-circuit with its own response without buffering."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and _config.enabled:
            if _config.latency_ms > 0:
                await asyncio.sleep(_config.latency_ms / 1000.0)
            if _config.fault_rate > 0 and random.random() < _config.fault_rate:
                await _send_error(send)          # never reaches the app
                return
        await self.app(scope, receive, send)
