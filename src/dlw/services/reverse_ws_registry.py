"""v2.1 Sprint 10 — Reverse-WSS connection registry.

Tracks live (executor_id → session) so:
  - Sprint 11 task-assign can route to a specific executor
  - Operators can list connected executors via REST (future Sprint 13)
  - Reconnects fence out stale sessions (last-write-wins by session_id)

Lives in-memory per controller instance. Standby controller has its own
empty registry until promotion; that's fine — Sprint 10 only needs
heartbeat. Cross-instance state (e.g. "which controller holds the
session") would belong in a follow-on if multi-active becomes a thing."""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ReverseWSSession:
    """One live executor connection. websocket is an opaque Any so the
    registry doesn't depend on starlette types."""
    session_id: str
    executor_id: str
    websocket: Any
    opened_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_heartbeat_at: datetime = field(
        default_factory=lambda: datetime.now(UTC))
    protocol_version: str = ""


class ReverseWSRegistry:
    """In-memory map of live sessions. Methods are async because real
    code calls websocket.send_text(...) under the same lock."""

    def __init__(self) -> None:
        self._by_executor: dict[str, ReverseWSSession] = {}
        self._lock = asyncio.Lock()

    async def register(
        self, *, executor_id: str, websocket: Any,
        protocol_version: str,
    ) -> ReverseWSSession:
        """Add a new session. If executor_id already has one, the old
        one is closed (reconnect wins). Returns the new session."""
        session_id = str(uuid.uuid4())
        new_session = ReverseWSSession(
            session_id=session_id, executor_id=executor_id,
            websocket=websocket, protocol_version=protocol_version)
        async with self._lock:
            old = self._by_executor.get(executor_id)
            self._by_executor[executor_id] = new_session
        if old is not None:
            # Close old socket OUTSIDE the lock — close may block
            try:
                await old.websocket.close(code=1012)  # service restart
            except Exception:  # noqa: BLE001
                logger.debug(
                    "reverse_ws: close-old-session for %s raised, ignoring",
                    executor_id)
        logger.info(
            "reverse_ws: registered session %s for executor %s",
            session_id, executor_id)
        return new_session

    async def unregister(self, *, executor_id: str, session_id: str) -> bool:
        """Remove only if the registered session is the SAME one. A
        late-closing old socket whose session was already replaced
        must not evict the new one (race during reconnect)."""
        async with self._lock:
            cur = self._by_executor.get(executor_id)
            if cur is not None and cur.session_id == session_id:
                del self._by_executor[executor_id]
                logger.info(
                    "reverse_ws: unregistered session %s for executor %s",
                    session_id, executor_id)
                return True
        return False

    async def get(self, executor_id: str) -> ReverseWSSession | None:
        async with self._lock:
            return self._by_executor.get(executor_id)

    async def touch_heartbeat(
        self, *, executor_id: str, session_id: str,
    ) -> bool:
        async with self._lock:
            cur = self._by_executor.get(executor_id)
            if cur is None or cur.session_id != session_id:
                return False
            cur.last_heartbeat_at = datetime.now(UTC)
            return True

    async def list_sessions(self) -> list[ReverseWSSession]:
        """Snapshot of live sessions — safe to read outside the lock
        once returned (registry state may change after the snapshot)."""
        async with self._lock:
            return list(self._by_executor.values())

    def _reset_for_tests(self) -> None:
        """Drop all sessions WITHOUT closing sockets — tests own that."""
        self._by_executor.clear()


# Module-level singleton. Both the WS endpoint and a future REST
# `GET /api/v1/admin/reverse-ws/sessions` read the same registry.
_REGISTRY = ReverseWSRegistry()


def get_registry() -> ReverseWSRegistry:
    return _REGISTRY
