"""Controller leader election via PG session advisory lock (Phase 2 W3c).

The instance holding pg_try_advisory_lock(<active_lock_id>) is the active
controller. PG releases the lock the instant its holding session ends, which
gives us automatic failover with no lease/expiry logic. Single-shared-PG
design — PG HA is a separate concern (Phase 4)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

ControllerState = Literal["standby", "recovering", "active"]


class LeaderElector:
    """Owns a dedicated PG connection that holds the advisory lock."""

    def __init__(self, db_url: str, lock_id: int) -> None:
        self._db_url = db_url
        self._lock_id = lock_id
        self._engine: AsyncEngine | None = None
        self._conn: AsyncConnection | None = None

    async def try_acquire(self) -> bool:
        """Try to acquire the lock. Returns True on success (and keeps the
        connection open holding it); False if another instance holds it."""
        if self._engine is None:
            # Dedicated engine. NullPool so SQLAlchemy never recycles the
            # connection out from under us (recycling would release the lock).
            # tcp_keepalives so a half-open TCP gets detected reasonably fast,
            # bounding the worst-case failover floor.
            self._engine = create_async_engine(
                self._db_url,
                poolclass=NullPool,
                connect_args={"server_settings": {
                    "tcp_keepalives_idle": "30",
                    "tcp_keepalives_interval": "10",
                }},
            )
        if self._conn is None:
            self._conn = await self._engine.connect()
        result = await self._conn.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": self._lock_id},
        )
        return bool(result.scalar())

    async def verify(self) -> bool:
        """Ping the lock-holding connection. Returns True if still alive
        (we still hold the lock); False if the connection died (lock lost)."""
        if self._conn is None:
            return False
        try:
            await self._conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.warning("leader connection lost: %s", e)
            await self._cleanup_connection()
            return False

    async def release(self) -> None:
        """Explicit release on graceful shutdown. Closing the connection
        also releases — this is just the polite version."""
        if self._conn is not None:
            try:
                await self._conn.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": self._lock_id},
                )
            except Exception as e:
                logger.warning("pg_advisory_unlock failed (releases on close anyway): %s", e)
        await self._cleanup_connection()
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def _cleanup_connection(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                pass
            self._conn = None
