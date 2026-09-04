"""
Retry with exponential backoff + FULL JITTER.

The idea: a transient failure often clears if you wait and try again. But naive
retries make things worse two ways, which this addresses:
  * **Backoff** — wait longer after each failure (base·2^(n-1)) instead of
    hammering a struggling dependency.
  * **Jitter** — randomize the wait so many clients retrying at once don't
    synchronize into repeated stampedes ("thundering herd"). We use *full
    jitter*: sleep a random amount in [0, backoff].

`run_with_retry` only retries failures `is_retryable` approves — a permanent
error (e.g. a 400) fails immediately, no pointless retries.

`sleep` and `rand` are injectable so tests run instantly and deterministically.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from app.resilience.errors import is_retryable as default_is_retryable

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3          # total tries, including the first
    base_delay: float = 0.5        # seconds; the first backoff step
    max_delay: float = 10.0        # cap on any single backoff


def backoff_delay(policy: RetryPolicy, attempt: int, rand: Callable[[], float]) -> float:
    """Full-jitter delay for a given attempt number (1-based)."""
    capped = min(policy.max_delay, policy.base_delay * (2 ** (attempt - 1)))
    return rand() * capped         # uniform in [0, capped]


async def run_with_retry(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    is_retryable: Callable[[Exception], bool] = default_is_retryable,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> T:
    last: Exception | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 - re-raised below unless retryable
            last = exc
            # Stop if we're out of attempts, or the error isn't transient.
            if attempt >= policy.max_attempts or not is_retryable(exc):
                raise
            await sleep(backoff_delay(policy, attempt, rand))
    assert last is not None
    raise last
