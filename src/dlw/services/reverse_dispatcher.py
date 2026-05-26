"""v2.1 Sprint 11 — Reverse-WSS task dispatcher.

Sits ABOVE the reverse_ws_registry and provides the controller-side API
for "assign this task to executor X". If X is currently connected we send
a TaskAssignFrame immediately and record it as pending; if X is offline
we record pending and the next handshake will resend it.

Why a separate module from the registry
---------------------------------------
The registry is a low-level map of connections. The dispatcher tracks
business state — which assignments still need an ack. Splitting keeps
the registry stable for Sprint 12 (credential proxy) and Sprint 13 (live
console), which will reuse it without needing dispatcher semantics.

Pending state is in-memory, per controller. If the controller restarts,
pending assignments are lost — but the underlying download_tasks rows
remain queued in PG, so the next scheduler tick re-fires the dispatch.
That's why the dispatcher takes assignments AFTER the DB row is written,
not before.

Acknowledgement model
---------------------
TaskAssignAck means "I received the frame". It is NOT a completion
signal — that still flows via the existing /subtasks/{id}/report REST
path. Sprint 12 may layer a ReportFrame on top of WSS; until then this
is purely a transport-layer reliability mechanism."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from dlw.schemas.reverse_ws import (
    WHITELIST_COMMANDS,
    CommandFrame,
    TaskAssignFrame,
)
from dlw.services.reverse_ws_registry import get_registry

logger = logging.getLogger(__name__)


class CommandUnknownExecutor(LookupError):
    """No live session for the requested executor_id (Sprint 13)."""


class CommandNotWhitelisted(ValueError):
    """Command outside the WHITELIST_COMMANDS tuple (Sprint 13)."""


@dataclass
class PendingAssignment:
    """One assignment the dispatcher is waiting on an ack for."""
    assignment_id: str
    executor_id: str
    payload: dict
    queued_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_sent_at: datetime | None = None
    send_attempts: int = 0


@dataclass
class DispatchResult:
    assignment_id: str
    sent_over_wire: bool
    queued: bool


class ReverseDispatcher:
    """Per-controller singleton. Methods are async because we touch the
    registry's socket — a slow send call shouldn't hold the lock."""

    def __init__(self) -> None:
        # executor_id → {assignment_id → PendingAssignment}
        self._pending: dict[str, dict[str, PendingAssignment]] = {}
        self._lock = asyncio.Lock()

    async def _record(
        self, *, executor_id: str, assignment: PendingAssignment,
        bump_send: bool,
    ) -> None:
        async with self._lock:
            ex_map = self._pending.setdefault(executor_id, {})
            ex_map[assignment.assignment_id] = assignment
            if bump_send:
                assignment.send_attempts += 1
                assignment.last_sent_at = datetime.now(UTC)

    async def _try_send(self, websocket: Any, frame: TaskAssignFrame) -> bool:
        try:
            await websocket.send_text(frame.model_dump_json())
            return True
        except Exception:  # noqa: BLE001
            logger.warning(
                "reverse_dispatcher: send_text failed for assignment %s",
                frame.assignment_id, exc_info=True)
            return False

    async def dispatch(
        self, *, executor_id: str, payload: dict,
        assignment_id: str | None = None,
    ) -> DispatchResult:
        """Queue an assignment for one executor. If a live session exists,
        send it now; otherwise just queue. Either way the caller gets
        back a DispatchResult and the assignment is tracked until acked
        (or explicitly cancelled)."""
        aid = assignment_id or str(uuid.uuid4())
        assignment = PendingAssignment(
            assignment_id=aid, executor_id=executor_id, payload=payload)

        session = await get_registry().get(executor_id)
        if session is None:
            await self._record(executor_id=executor_id,
                                assignment=assignment, bump_send=False)
            logger.info(
                "reverse_dispatcher: queued assignment %s for offline executor %s",
                aid, executor_id)
            return DispatchResult(
                assignment_id=aid, sent_over_wire=False, queued=True)

        frame = TaskAssignFrame(assignment_id=aid, payload=payload)
        ok = await self._try_send(session.websocket, frame)
        await self._record(executor_id=executor_id,
                            assignment=assignment, bump_send=ok)
        logger.info(
            "reverse_dispatcher: dispatched assignment %s to %s (sent=%s)",
            aid, executor_id, ok)
        return DispatchResult(
            assignment_id=aid, sent_over_wire=ok, queued=True)

    async def handle_ack(
        self, *, executor_id: str, assignment_id: str,
    ) -> bool:
        """Remove an acked assignment from the pending map. Returns True
        iff something was actually removed."""
        async with self._lock:
            ex_map = self._pending.get(executor_id)
            if ex_map is None or assignment_id not in ex_map:
                return False
            del ex_map[assignment_id]
            if not ex_map:
                del self._pending[executor_id]
        logger.info(
            "reverse_dispatcher: acked assignment %s from %s",
            assignment_id, executor_id)
        return True

    async def on_session_established(self, *, executor_id: str) -> int:
        """Called by the WS endpoint right after a successful handshake.
        Resends every pending assignment for this executor over the new
        socket. Returns count attempted."""
        async with self._lock:
            pending_snapshot = list(
                self._pending.get(executor_id, {}).values())
        if not pending_snapshot:
            return 0
        session = await get_registry().get(executor_id)
        if session is None:
            # Race: connection closed between handshake and this call.
            # Next handshake will retry.
            return 0
        attempted = 0
        for p in pending_snapshot:
            frame = TaskAssignFrame(
                assignment_id=p.assignment_id, payload=p.payload)
            ok = await self._try_send(session.websocket, frame)
            await self._record(
                executor_id=executor_id, assignment=p, bump_send=ok)
            if ok:
                attempted += 1
        logger.info(
            "reverse_dispatcher: resent %d/%d pending assignments to %s",
            attempted, len(pending_snapshot), executor_id)
        return attempted

    async def cancel(self, *, executor_id: str, assignment_id: str) -> bool:
        """Forget about a pending assignment (e.g. the parent task was
        cancelled). Idempotent."""
        async with self._lock:
            ex_map = self._pending.get(executor_id)
            if ex_map is None or assignment_id not in ex_map:
                return False
            del ex_map[assignment_id]
            if not ex_map:
                del self._pending[executor_id]
        return True

    async def pending_for(self, executor_id: str) -> list[PendingAssignment]:
        """Snapshot of pending assignments for one executor."""
        async with self._lock:
            return list(self._pending.get(executor_id, {}).values())

    async def total_pending(self) -> int:
        async with self._lock:
            return sum(len(m) for m in self._pending.values())

    # ----- Sprint 13 — Live Console command channel ----------------------
    # Commands are fire-and-forget at the dispatcher level: we don't track
    # pending state because the admin caller waits synchronously for the
    # response over the same WS (Sprint 14 may add a result-correlation
    # map). Whitelist is enforced HERE so a future caller can't bypass it
    # by constructing a CommandFrame manually.

    async def send_command(
        self, *, executor_id: str, command: str, command_id: str | None = None,
    ) -> str:
        """Send a whitelisted command to a connected executor. Returns
        the command_id so the caller can correlate with CommandResultFrame
        (when result correlation lands in Sprint 14)."""
        if command not in WHITELIST_COMMANDS:
            raise CommandNotWhitelisted(
                f"command {command!r} not in whitelist {WHITELIST_COMMANDS}")
        session = await get_registry().get(executor_id)
        if session is None:
            raise CommandUnknownExecutor(executor_id)
        cid = command_id or str(uuid.uuid4())
        frame = CommandFrame(command_id=cid, command=command)
        try:
            await session.websocket.send_text(frame.model_dump_json())
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "reverse_dispatcher: send_command(%s, %s) failed: %s",
                executor_id, command, e)
            raise
        logger.info(
            "reverse_dispatcher: sent command %r (id=%s) to %s",
            command, cid, executor_id)
        return cid

    def _reset_for_tests(self) -> None:
        self._pending.clear()


# Module-level singleton — the WS endpoint + future scheduler call sites
# all share the same dispatcher state.
_DISPATCHER = ReverseDispatcher()


def get_dispatcher() -> ReverseDispatcher:
    return _DISPATCHER
