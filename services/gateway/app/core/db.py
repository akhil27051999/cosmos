"""
Database plumbing: the async SQLAlchemy engine, session factory, and base class.

This is infrastructure, not business logic. It lives in `core` because several
adapters and the app entrypoint need it. We use SQLAlchemy 2.0 in async mode
with the asyncpg driver, so nothing in the request path blocks the event loop.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Parent class for all ORM models (see adapters/postgres/models.py)."""


# LAZY initialization: we build the engine only on first use, not at import
# time. Why it matters — creating the engine eagerly makes *importing this
# module* pull in the database driver (asyncpg). That would force tests, which
# swap in the in-memory adapter and never touch Postgres, to install a driver
# they don't use. Deferring it keeps imports cheap and tests hermetic. Both
# getters are cached, so there's still exactly one engine/sessionmaker per
# process — the singleton, just created on demand.


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    # `pool_pre_ping` quietly checks a pooled connection is alive before handing
    # it out, avoiding "server closed the connection" errors after idle periods.
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,   # log SQL when true — handy while learning
        pool_pre_ping=True,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    # `expire_on_commit=False` keeps attributes readable after commit (otherwise
    # SQLAlchemy expires them and would re-query on access, awkward in async).
    return async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)


async def create_all() -> None:
    """Create tables from the ORM metadata.

    A Stage-0 shortcut so a fresh clone is runnable immediately. Real schema
    management comes later with Alembic migrations (a versioned, reviewable
    history of schema changes) — noted so you don't mistake this for production
    practice.
    """
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
