"""v2.1 Sprint 10 — Reverse-WSS frame schema.

The frame is the wire format on the reverse WebSocket connection between
an enterprise-network executor (the WS client) and the controller (the
WS server). Executor INITIATES the connection — that's the "reverse" part
of the name; classic deploys had controller poll executor.

Each frame is a single JSON object on its own WebSocket text message.
Binary frames are reserved for Sprint 11 chunk-transfer payloads.

State machine (Sprint 10 — handshake + heartbeat only)
                                                                   .─ disconnect ─┐
                                                                  /               │
  [pre-open]  ──open──▶  [pre-hello]  ──hello/hello_ack──▶  [established]  ◀──────┘
                                                              │
                                                              │  heartbeat ─▶ heartbeat_ack
                                                              ▼
                                                          [established]

Sprint 11 will add: task_assign, task_assign_ack, progress_report, etc.
Sprint 12: credential_proxy_request, credential_proxy_response.
Sprint 13: console_input, console_output, command (whitelisted)."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

# Bump on any breaking schema change. Controller may refuse hello with
# mismatched major.
PROTOCOL_VERSION = "1.0"


class HelloFrame(BaseModel):
    """First frame sent by the executor right after open."""
    type: Literal["hello"] = "hello"
    executor_id: str
    protocol_version: str = PROTOCOL_VERSION
    # Free-form executor metadata; controller logs but doesn't act on it
    # in Sprint 10.
    executor_version: str | None = None


class HelloAckFrame(BaseModel):
    """Controller's response to hello — connection is now established."""
    type: Literal["hello_ack"] = "hello_ack"
    session_id: str
    server_protocol_version: str = PROTOCOL_VERSION
    # heartbeat_interval_seconds is the controller-side timeout; the
    # executor should send heartbeats faster than this.
    heartbeat_interval_seconds: float = 30.0


class HeartbeatFrame(BaseModel):
    """Sent periodically by the executor. Body intentionally empty —
    Sprint 11 may add load info; Sprint 12 may add credential nonce."""
    type: Literal["heartbeat"] = "heartbeat"


class HeartbeatAckFrame(BaseModel):
    """Server acknowledgment. Echoing reassures the executor the
    connection is alive even when no other traffic flows."""
    type: Literal["heartbeat_ack"] = "heartbeat_ack"


class ErrorFrame(BaseModel):
    """Either side may send an Error frame; receiving one means the
    sender intends to close the connection right after. code is a
    free-form short string (e.g. 'PROTOCOL_VERSION_MISMATCH')."""
    type: Literal["error"] = "error"
    code: str
    message: str = ""


# Sprint 11 — reverse RPC: controller pushes task assignments over the
# already-established WSS, executor acks.

class TaskAssignFrame(BaseModel):
    """Server → executor. assignment_id is a fresh UUID per push; the
    executor echoes it in TaskAssignAck so the controller can clear
    pending state. payload is the same JSON shape executors got from the
    legacy POST /poll AssignmentResponse — Sprint 11 wraps, doesn't
    redesign, so executors can switch transports without re-implementing
    task execution."""
    type: Literal["task_assign"] = "task_assign"
    assignment_id: str
    payload: dict


class TaskAssignAckFrame(BaseModel):
    """Executor → server. Means 'I received the assignment and have it
    queued'. NOT 'I have completed the task' — completion goes through
    the existing /subtasks/{id}/report REST path (or a future ReportFrame
    in Sprint 12)."""
    type: Literal["task_assign_ack"] = "task_assign_ack"
    assignment_id: str


# Discriminated union — pydantic v2 picks the right subtype by `type`.
AnyFrame = Annotated[
    Union[HelloFrame, HelloAckFrame, HeartbeatFrame, HeartbeatAckFrame,
          TaskAssignFrame, TaskAssignAckFrame, ErrorFrame],
    Field(discriminator="type"),
]
