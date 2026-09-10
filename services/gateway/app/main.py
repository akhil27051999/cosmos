"""
The application entrypoint / composition root.

This is where everything is wired together — the ONE place that knows about all
the pieces at once:
  * create the FastAPI app,
  * attach the correlation-id middleware,
  * install the error handlers (domain errors -> HTTP),
  * mount the routers,
  * on startup, create the DB tables (Stage-0 shortcut; Alembic later).

Run it with:  uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import (
    routes_admin,
    routes_experiments,
    routes_health,
    routes_jobs,
    routes_metrics,
    routes_read,
    routes_saga,
    routes_slo,
)
from app.api.errors import install_error_handlers
from app.core.config import get_settings
from app.core.correlation import CorrelationIdMiddleware
from app.core.db import create_all, get_engine
from app.core.lifecycle import begin_draining
from app.observability import chaos
from app.observability.chaos import ChaosMiddleware
from app.observability.logging import configure_logging
from app.observability.middleware import MetricsMiddleware

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Runs once at startup (before the first request) and once at shutdown.
    # We ensure the schema exists so a fresh clone is runnable immediately.
    await create_all()
    yield
    # Stage 12 — graceful shutdown: mark not-ready (so k8s stops routing here
    # while in-flight requests finish), then release the DB connection pool.
    begin_draining()
    try:
        await get_engine().dispose()
    except Exception:  # noqa: BLE001 - never fail shutdown on cleanup
        pass


def create_app() -> FastAPI:
    """Application factory.

    Building the app inside a function (rather than at import time) makes it
    easy for tests to construct their own instance with overridden dependencies.
    """
    # Structured logging first, so everything after logs in the chosen format.
    configure_logging(settings.log_level, settings.log_format)
    # Apply chaos config from env (off by default).
    chaos.configure(settings.chaos_enabled, settings.chaos_fault_rate, settings.chaos_latency_ms)

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    # Middleware order: the LAST added is OUTERMOST (runs first). Flow becomes
    # correlation -> metrics -> chaos -> app, so: (a) the correlation id is set
    # before the metrics access log, and (b) chaos-injected errors are recorded by
    # the metrics middleware (they burn the SLO budget — the point of the demo).
    app.add_middleware(ChaosMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    # Turn domain/validation errors into our consistent JSON envelope.
    install_error_handlers(app)

    # Mount the feature routers.
    app.include_router(routes_health.router)
    app.include_router(routes_metrics.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_read.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_experiments.router)
    app.include_router(routes_saga.router)
    app.include_router(routes_slo.router)

    return app


# The module-level `app` uvicorn imports (`app.main:app`).
app = create_app()
