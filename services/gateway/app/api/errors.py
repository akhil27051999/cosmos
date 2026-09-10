"""
The single place where errors become HTTP responses.

Two goals:
  1. A CONSISTENT error envelope for every failure, so clients parse one shape:
        {"error": {"code": "...", "message": "...", "correlation_id": "..."}}
  2. This is the ONLY module that maps domain exceptions to HTTP status codes.
     The domain raises `InvalidTransition`; *here* we decide that means 409.
     That keeps the domain ignorant of HTTP, on purpose.

`install_error_handlers(app)` wires these onto the FastAPI app at startup.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.correlation import get_correlation_id
from app.domain.exceptions import (
    DomainError,
    ExperimentNotFound,
    Forbidden,
    InvalidTransition,
    JobNotFound,
    QuotaExceeded,
    ServiceUnavailable,
    Unauthenticated,
    VersionConflict,
)
from app.resilience.errors import RateLimited


def _envelope(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "correlation_id": get_correlation_id(),
            }
        },
    )


# Which domain error maps to which HTTP status + machine-readable code.
_DOMAIN_MAP: dict[type[DomainError], tuple[int, str]] = {
    JobNotFound: (404, "job_not_found"),
    ExperimentNotFound: (404, "experiment_not_found"),
    InvalidTransition: (409, "invalid_transition"),
    VersionConflict: (409, "version_conflict"),
    # Stage 6 — identity / multi-tenancy.
    Unauthenticated: (401, "unauthenticated"),
    Forbidden: (403, "forbidden"),
    QuotaExceeded: (429, "quota_exceeded"),
    ServiceUnavailable: (503, "service_unavailable"),
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _handle_domain(_: Request, exc: DomainError) -> JSONResponse:
        # Look up the specific subclass; fall back to 400 for any new domain
        # error we haven't explicitly mapped yet.
        status_code, code = _DOMAIN_MAP.get(type(exc), (400, "domain_error"))
        resp = _envelope(status_code, code, str(exc))
        # RFC 7235: a 401 SHOULD say how to authenticate. We use a bearer API key.
        if status_code == 401:
            resp.headers["WWW-Authenticate"] = "Bearer"
        # A quota 429 gets a Retry-After like the load-shed 429 does.
        if isinstance(exc, QuotaExceeded):
            resp.headers["Retry-After"] = "3600"
        return resp

    @app.exception_handler(RateLimited)
    async def _handle_rate_limited(_: Request, exc: RateLimited) -> JSONResponse:
        # Load shed: 429 Too Many Requests + a Retry-After hint so well-behaved
        # clients back off instead of retrying immediately.
        resp = _envelope(429, "rate_limited", "too many requests — try again shortly")
        resp.headers["Retry-After"] = str(exc.retry_after)
        return resp

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI raises this when the request body/query fails Pydantic
        # validation. We reshape its detail into our envelope.
        return _envelope(422, "validation_error", _summarise(exc))

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Last resort: never leak a stack trace or internal detail to the client.
        # (In real ops you'd also log `exc` with the correlation id here.)
        return _envelope(500, "internal_error", "an unexpected error occurred")


def _summarise(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid request"
