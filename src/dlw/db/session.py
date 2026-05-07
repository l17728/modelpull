"""Async engine + session factory; depend on app config."""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from dlw.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Process-wide singleton engine.

    Cached so concurrent /health/ready probes share one connection pool —
    creating an engine per request and disposing it in finally races with
    in-flight asyncpg connections (ConnectionDoesNotExistError under k8s
    1s probe interval). Lifespan disposes once at shutdown.
    """
    settings = get_settings()
    return create_async_engine(
        settings.db_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


async def reset_engine() -> None:
    """For tests: dispose cached engine + clear cache so next call rebuilds.

    Must be called after monkeypatching DLW_DB_* env so the new settings
    are picked up.
    """
    cached = get_engine.cache_info()
    if cached.currsize > 0:
        await get_engine().dispose()
    get_engine.cache_clear()


def get_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
