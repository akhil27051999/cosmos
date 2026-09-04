"""
Rate limiting via a TOKEN BUCKET.

Mental model: a bucket holds up to `capacity` tokens and refills at
`refill_per_sec`. Each request takes one token; if the bucket is empty the
request is denied. This gives you a steady average rate (the refill) while still
allowing short bursts (the capacity) — which is how real traffic behaves.

We use it two ways:
  * at the gateway, to **shed load** — deny excess submissions with 429 so a
    flood can't overwhelm the system (admission control).
  * (same primitive) to throttle calls to a provider so we stay under its limits.

This in-memory bucket is for a single process and for tests (injectable clock).
The Redis-backed bucket (adapters/redis/rate_limiter.py) shares one limit across
many gateway/worker replicas — that's the *distributed* version.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol


class RateLimiter(Protocol):
    async def try_acquire(self, tokens: int = 1) -> bool:
        """Take `tokens` if available; return True if allowed, False if denied."""
        ...


class TokenBucket:
    def __init__(
        self,
        capacity: float,
        refill_per_sec: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._refill = refill_per_sec
        self._now = now
        self._tokens = capacity          # start full (allow an initial burst)
        self._last = now()

    async def try_acquire(self, tokens: int = 1) -> bool:
        t = self._now()
        elapsed = max(0.0, t - self._last)
        self._last = t
        # Refill based on elapsed time, capped at capacity.
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill)
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False
