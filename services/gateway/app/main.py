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
    routes_read,
)
from app.api.errors import install_error_handlers
from app.core.config import get_settings
from app.core.correlation import CorrelationIdMiddleware
from app.core.db import create_all

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Runs once at startup (before the first request) and once at shutdown.
    # We ensure the schema exists so a fresh clone is runnable immediately.
    await create_all()
    yield
    # (nothing to tear down yet; engine disposal would go here)


def create_app() -> FastAPI:
    """Application factory.

    Building the app inside a function (rather than at import time) makes it
    easy for tests to construct their own instance with overridden dependencies.
    """
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    # Order matters: middleware wraps every request/response.
    app.add_middleware(CorrelationIdMiddleware)

    # Turn domain/validation errors into our consistent JSON envelope.
    install_error_handlers(app)

    # Mount the feature routers.
    app.include_router(routes_health.router)
    app.include_router(routes_jobs.router)
    app.include_router(routes_read.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_experiments.router)

    return app


# The module-level `app` uvicorn imports (`app.main:app`).
app = create_app()
