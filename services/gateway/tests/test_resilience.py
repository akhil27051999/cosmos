"""
Unit tests for the resilience primitives — deterministic (injected clocks/sleep),
no real waiting, no infrastructure.
"""

from __future__ import annotations

import pytest

from app.resilience.circuit_breaker import CircuitBreaker, State
from app.resilience.errors import CircuitOpenError, is_retryable
from app.resilience.rate_limiter import TokenBucket
from app.resilience.retry import RetryPolicy, run_with_retry


# ---- circuit breaker (sync) -------------------------------------------------

def test_circuit_breaker_opens_then_half_opens_then_closes():
    t = {"v": 0.0}
    cb = CircuitBreaker(failure_threshold=3, reset_timeout=10.0, now=lambda: t["v"])

    cb.before_call()                       # CLOSED: allowed
    for _ in range(3):
        cb.record_failure()
    assert cb.state is State.OPEN

    with pytest.raises(CircuitOpenError):  # OPEN: fast-fail during cooldown
        cb.before_call()

    t["v"] = 11.0                          # cooldown elapsed
    cb.before_call()                       # -> HALF_OPEN (one probe allowed)
    assert cb.state is State.HALF_OPEN
    cb.record_success()                    # probe succeeded
    assert cb.state is State.CLOSED


def test_failed_probe_reopens_breaker():
    t = {"v": 0.0}
    cb = CircuitBreaker(failure_threshold=1, reset_timeout=5.0, now=lambda: t["v"])
    cb.record_failure()                    # threshold 1 -> OPEN
    assert cb.state is State.OPEN
    t["v"] = 6.0
    cb.before_call()                       # HALF_OPEN
    cb.record_failure()                    # probe failed -> OPEN again
    assert cb.state is State.OPEN


# ---- retry (async) ----------------------------------------------------------

async def _noop_sleep(_d):
    return None


async def test_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("blip")     # transient -> retried
        return "ok"

    delays: list[float] = []

    async def rec_sleep(d):
        delays.append(d)

    out = await run_with_retry(
        fn, RetryPolicy(max_attempts=5, base_delay=1.0), sleep=rec_sleep, rand=lambda: 1.0
    )
    assert out == "ok"
    assert calls["n"] == 3
    assert delays == [1.0, 2.0]            # exponential backoff (full jitter, rand=1.0)


async def test_retry_gives_up_after_max_attempts():
    async def fn():
        raise TimeoutError("always")

    with pytest.raises(TimeoutError):
        await run_with_retry(
            fn, RetryPolicy(max_attempts=3), sleep=_noop_sleep, rand=lambda: 0.0
        )


async def test_retry_does_not_retry_permanent_error():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise ValueError("bad request")    # not transient -> no retry

    with pytest.raises(ValueError):
        await run_with_retry(
            fn, RetryPolicy(max_attempts=5), sleep=_noop_sleep, rand=lambda: 0.0
        )
    assert calls["n"] == 1


# ---- classifier + token bucket ---------------------------------------------

def test_is_retryable_classification():
    class RateLimitError(Exception):
        pass

    boom = Exception()
    boom.status_code = 503                 # type: ignore[attr-defined]

    assert is_retryable(TimeoutError())
    assert is_retryable(ConnectionError())
    assert is_retryable(RateLimitError())  # matched by name hint
    assert is_retryable(boom)              # matched by status code
    assert not is_retryable(ValueError("nope"))
    assert not is_retryable(CircuitOpenError())


async def test_token_bucket_burst_then_deny_then_refill():
    t = {"v": 0.0}
    b = TokenBucket(capacity=2, refill_per_sec=1.0, now=lambda: t["v"])
    assert await b.try_acquire() is True   # 2 -> 1
    assert await b.try_acquire() is True   # 1 -> 0
    assert await b.try_acquire() is False  # empty -> denied
    t["v"] = 1.0                           # +1s -> +1 token
    assert await b.try_acquire() is True
