"""
ResilientLLMClient — resilience as a DECORATOR over the LLMClient port.

This is the Stage-4 centerpiece and a clean demonstration of ports-and-adapters:
it *is* an `LLMClient` (same `create_turn`), and it *wraps* one or two real
`LLMClient`s, adding — in order — timeout, retry, and a circuit breaker, then
falling back to a second provider if the first is exhausted or tripped. The
agent loop calls it exactly like any other LLM client and is completely unaware.

Per call to the primary provider:
    circuit breaker (fail fast if OPEN)
      └─ retry with backoff+jitter
           └─ timeout around the actual model call
If the primary ultimately fails (or its breaker is open), we try the FALLBACK
provider (its own independent breaker). Each provider has its own breaker so one
being down doesn't blind us to the other's health.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.ports.llm import LLMClient, LLMTurn
from app.resilience.circuit_breaker import CircuitBreaker
from app.resilience.errors import is_retryable
from app.resilience.retry import RetryPolicy, run_with_retry


@dataclass(frozen=True)
class BreakerConfig:
    failure_threshold: int = 5
    reset_timeout: float = 30.0


class ResilientLLMClient:
    def __init__(
        self,
        primary: LLMClient,
        fallback: LLMClient | None = None,
        *,
        retry: RetryPolicy | None = None,
        timeout_s: float = 60.0,
        breaker: BreakerConfig | None = None,
        # injectables for deterministic tests:
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._retry = retry or RetryPolicy()
        self._timeout = timeout_s
        self._sleep = sleep
        self._rand = rand
        breaker = breaker or BreakerConfig()
        # one breaker per provider — independent health tracking
        self._cb_primary = CircuitBreaker(breaker.failure_threshold, breaker.reset_timeout, now)
        self._cb_fallback = CircuitBreaker(breaker.failure_threshold, breaker.reset_timeout, now)

    async def create_turn(self, **kwargs) -> LLMTurn:
        try:
            return await self._guarded_call(self._primary, self._cb_primary, kwargs)
        except Exception:
            # Primary is exhausted (retries used up) or its breaker is OPEN.
            if self._fallback is not None:
                return await self._guarded_call(self._fallback, self._cb_fallback, kwargs)
            raise

    async def _guarded_call(self, client: LLMClient, cb: CircuitBreaker, kwargs: dict) -> LLMTurn:
        cb.before_call()  # raises CircuitOpenError immediately if the breaker is OPEN

        async def attempt() -> LLMTurn:
            # Cap each individual model call; a hung provider shouldn't hang us.
            return await asyncio.wait_for(client.create_turn(**kwargs), self._timeout)

        try:
            turn = await run_with_retry(
                attempt, self._retry, is_retryable, sleep=self._sleep, rand=self._rand
            )
        except Exception:
            cb.record_failure()   # feed the breaker so it can trip
            raise
        cb.record_success()
        return turn
