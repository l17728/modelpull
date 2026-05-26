"""v2.1 Sprint 11 — Reverse-WSS dispatcher tests.

Stubs the registry's websocket with a captured-frames double so we can
inspect what would have been sent over the wire."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from dlw.schemas.reverse_ws import TaskAssignFrame
from dlw.services.reverse_dispatcher import ReverseDispatcher, get_dispatcher
from dlw.services.reverse_ws_registry import (
    ReverseWSRegistry,
    get_registry,
)


@dataclass
class _CapturedFrame:
    raw: str


@dataclass
class _StubWS:
    """Minimal WebSocket stand-in: records every send_text + can be
    configured to raise on send (simulating network drop)."""
    sent: list[_CapturedFrame] = field(default_factory=list)
    fail_n_sends: int = 0

    async def send_text(self, raw: str) -> None:
        if self.fail_n_sends > 0:
            self.fail_n_sends -= 1
            raise ConnectionError("stub: simulated send failure")
        self.sent.append(_CapturedFrame(raw=raw))

    async def close(self, code: int) -> None:  # registry.register calls this
        pass


def _decoded_assigns(ws: _StubWS) -> list[TaskAssignFrame]:
    out: list[TaskAssignFrame] = []
    for f in ws.sent:
        obj = json.loads(f.raw)
        if obj.get("type") == "task_assign":
            out.append(TaskAssignFrame.model_validate(obj))
    return out


@pytest.fixture(autouse=True)
def _reset_singletons():
    """The dispatcher AND registry are module-level singletons; reset
    both between tests so state doesn't leak."""
    get_dispatcher()._reset_for_tests()
    get_registry()._reset_for_tests()
    yield
    get_dispatcher()._reset_for_tests()
    get_registry()._reset_for_tests()


# ---------------------------------------------------------------------------
# dispatch: live session → send + pending recorded

async def test_dispatch_to_live_session_sends_and_records():
    ws = _StubWS()
    await get_registry().register(
        executor_id="ex-1", websocket=ws, protocol_version="1.0")

    d = get_dispatcher()
    result = await d.dispatch(
        executor_id="ex-1", payload={"subtask_id": "abc"})

    assert result.sent_over_wire is True
    assert result.queued is True
    frames = _decoded_assigns(ws)
    assert len(frames) == 1
    assert frames[0].payload == {"subtask_id": "abc"}
    pending = await d.pending_for("ex-1")
    assert len(pending) == 1
    assert pending[0].assignment_id == result.assignment_id
    assert pending[0].send_attempts == 1


async def test_dispatch_with_no_live_session_only_queues():
    """Executor is offline — assignment is recorded but not sent."""
    d = get_dispatcher()
    result = await d.dispatch(
        executor_id="ex-offline", payload={"x": 1})
    assert result.sent_over_wire is False
    assert result.queued is True
    pending = await d.pending_for("ex-offline")
    assert len(pending) == 1
    assert pending[0].send_attempts == 0  # never attempted


# ---------------------------------------------------------------------------
# handle_ack clears pending

async def test_handle_ack_removes_pending():
    ws = _StubWS()
    await get_registry().register(
        executor_id="ex-2", websocket=ws, protocol_version="1.0")
    d = get_dispatcher()
    res = await d.dispatch(executor_id="ex-2", payload={"x": 2})
    assert len(await d.pending_for("ex-2")) == 1

    removed = await d.handle_ack(
        executor_id="ex-2", assignment_id=res.assignment_id)
    assert removed is True
    assert await d.pending_for("ex-2") == []


async def test_handle_ack_unknown_assignment_no_op():
    """Late or duplicated ack for an unknown id is silently ignored."""
    d = get_dispatcher()
    removed = await d.handle_ack(
        executor_id="ex-x", assignment_id="never-existed")
    assert removed is False


# ---------------------------------------------------------------------------
# on_session_established resends pending — the core reconnect guarantee

async def test_reconnect_resends_pending():
    """Executor goes offline with one pending; reconnects; dispatcher
    pushes the assignment over the new socket."""
    d = get_dispatcher()
    # Phase 1: executor offline → queue 2 assignments
    r1 = await d.dispatch(executor_id="ex-r", payload={"a": 1})
    r2 = await d.dispatch(executor_id="ex-r", payload={"a": 2})
    assert (r1.sent_over_wire, r2.sent_over_wire) == (False, False)

    # Phase 2: executor connects
    ws = _StubWS()
    await get_registry().register(
        executor_id="ex-r", websocket=ws, protocol_version="1.0")

    sent = await d.on_session_established(executor_id="ex-r")
    assert sent == 2
    frames = _decoded_assigns(ws)
    assert len(frames) == 2
    ids_on_wire = {f.assignment_id for f in frames}
    assert ids_on_wire == {r1.assignment_id, r2.assignment_id}


async def test_send_failure_keeps_pending():
    """If send_text raises, the assignment stays in pending so next
    reconnect retries — at-least-once delivery."""
    ws = _StubWS(fail_n_sends=99)  # every send blows up
    await get_registry().register(
        executor_id="ex-fail", websocket=ws, protocol_version="1.0")
    d = get_dispatcher()
    result = await d.dispatch(
        executor_id="ex-fail", payload={"k": "v"})
    assert result.sent_over_wire is False
    assert result.queued is True
    pending = await d.pending_for("ex-fail")
    assert len(pending) == 1  # still there for retry


# ---------------------------------------------------------------------------
# Multi-executor isolation

async def test_pending_isolated_per_executor():
    """A pending assignment for executor A is invisible to executor B
    and ack from B doesn't touch A's pending."""
    d = get_dispatcher()
    a = await d.dispatch(executor_id="ex-A", payload={"x": 1})
    b = await d.dispatch(executor_id="ex-B", payload={"x": 2})
    assert len(await d.pending_for("ex-A")) == 1
    assert len(await d.pending_for("ex-B")) == 1

    # B acks its own assignment — A is untouched
    await d.handle_ack(executor_id="ex-B", assignment_id=b.assignment_id)
    assert len(await d.pending_for("ex-A")) == 1
    assert len(await d.pending_for("ex-B")) == 0

    # B reconnects — only A still has stale pending
    ws_a = _StubWS()
    await get_registry().register(
        executor_id="ex-A", websocket=ws_a, protocol_version="1.0")
    sent = await d.on_session_established(executor_id="ex-A")
    assert sent == 1
    frames = _decoded_assigns(ws_a)
    assert len(frames) == 1
    assert frames[0].assignment_id == a.assignment_id


# ---------------------------------------------------------------------------
# cancel removes from pending

async def test_cancel_removes_pending():
    d = get_dispatcher()
    res = await d.dispatch(
        executor_id="ex-c", payload={"x": 1})
    assert len(await d.pending_for("ex-c")) == 1
    cancelled = await d.cancel(
        executor_id="ex-c", assignment_id=res.assignment_id)
    assert cancelled is True
    assert await d.pending_for("ex-c") == []


async def test_cancel_unknown_is_noop():
    d = get_dispatcher()
    cancelled = await d.cancel(
        executor_id="ex-z", assignment_id="never-existed")
    assert cancelled is False


async def test_total_pending_counts_all_executors():
    d = get_dispatcher()
    await d.dispatch(executor_id="ex-A", payload={})
    await d.dispatch(executor_id="ex-A", payload={})
    await d.dispatch(executor_id="ex-B", payload={})
    assert await d.total_pending() == 3


# ---------------------------------------------------------------------------
# on_session_established with empty queue is a no-op

async def test_on_session_established_with_no_pending_is_noop():
    ws = _StubWS()
    await get_registry().register(
        executor_id="ex-clean", websocket=ws, protocol_version="1.0")
    d = get_dispatcher()
    sent = await d.on_session_established(executor_id="ex-clean")
    assert sent == 0
    assert _decoded_assigns(ws) == []
