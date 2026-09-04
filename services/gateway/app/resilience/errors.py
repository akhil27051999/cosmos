"""
Resilience errors + the "is this worth retrying?" classifier.

Two custom signals:
  * CircuitOpenError — the breaker is open; fail fast, don't even try.
  * RateLimited — we're shedding load (too many requests); tell the caller to back off.

`is_retryable(exc)` decides which failures are TRANSIENT (worth a retry) vs
PERMANENT (a bad request will fail again no matter how often you retry). We
classify generically — by exception type, name, or HTTP status — so it works
across provider SDKs (Anthropic, Groq, …) without importing any of them.
"""

from __future__ import annotations


class CircuitOpenError(Exception):
    """Raised when a call is blocked because the circuit breaker is OPEN."""


class RateLimited(Exception):
    """Raised at the edge when we shed a request to protect the system."""

    def __init__(self, retry_after: int = 1) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


# substrings that mark a transient, retry-worthy failure in a class name
_TRANSIENT_NAME_HINTS = (
    "timeout", "connection", "ratelimit", "overloaded",
    "serviceunavailable", "internalserver", "apiconnection", "apitimeout",
)
_TRANSIENT_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


def is_retryable(exc: Exception) -> bool:
    # Timeouts and dropped connections are the classic transient failures.
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    # Never retry a circuit-open (that's a fail-fast signal, not a transient error).
    if isinstance(exc, CircuitOpenError):
        return False
    name = type(exc).__name__.lower()
    if any(hint in name for hint in _TRANSIENT_NAME_HINTS):
        return True
    # Many SDK errors carry an HTTP status code.
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    return status in _TRANSIENT_STATUSES
