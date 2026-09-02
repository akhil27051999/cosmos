"""
Correlation IDs.

Every request gets a unique id. If the caller supplies one (header
`X-Request-ID`), we honour it; otherwise we mint a UUID. We stash it in a
`ContextVar` so any code deep in the call stack (logs, errors) can read it
without it being threaded through every function, and we echo it back on the
response so a client — or you, debugging — can correlate a request across logs
and, later, across services and traces (this is the seed of distributed
tracing in Stage 12).
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

HEADER_NAME = "X-Request-ID"

# A ContextVar is like a thread-local but async-aware: each request gets its own
# value even though many requests share the event loop.
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def get_correlation_id() -> str:
    return _correlation_id.get()


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        cid = request.headers.get(HEADER_NAME) or str(uuid.uuid4())
        token = _correlation_id.set(cid)          # bind for this request only
        try:
            response = await call_next(request)
        finally:
            _correlation_id.reset(token)          # always unbind, even on error
        response.headers[HEADER_NAME] = cid       # let the caller see it
        return response
