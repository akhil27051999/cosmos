"""
Circuit breaker — stop hammering a dependency that's already down.

Retrying is good for a blip, but if a provider is genuinely down, every request
retrying just piles on load and makes every caller wait for the full timeout.
A breaker "trips" after too many failures and then FAILS FAST for a cooldown,
giving the dependency room to recover and freeing callers instantly.

Three states (like a household breaker):
  * CLOSED    — normal; calls flow. Count consecutive failures.
  * OPEN      — tripped; calls are rejected immediately (CircuitOpenError) until
                `reset_timeout` elapses.
  * HALF_OPEN — after the cooldown, let ONE probe through. Success → CLOSED;
                failure → OPEN again.

The clock (`now`) is injectable so tests can advance time without sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum

from app.resilience.errors import CircuitOpenError


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._now = now
        self._state = State.CLOSED
        self._failures = 0
        self._opened_at = 0.0

    @property
    def state(self) -> State:
        return self._state

    def before_call(self) -> None:
        """Call this before attempting the protected operation.

        Raises CircuitOpenError if the breaker is OPEN and still cooling down.
        If the cooldown has elapsed, it moves to HALF_OPEN and allows one probe.
        """
        if self._state is State.OPEN:
            if self._now() - self._opened_at >= self._reset_timeout:
                self._state = State.HALF_OPEN      # allow a single trial call
            else:
                raise CircuitOpenError()

    def record_success(self) -> None:
        # Any success clears the count and (re)closes the circuit.
        self._failures = 0
        self._state = State.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        # A failed probe in HALF_OPEN, or too many failures while CLOSED, trips it.
        if self._state is State.HALF_OPEN or self._failures >= self._threshold:
            self._state = State.OPEN
            self._opened_at = self._now()
