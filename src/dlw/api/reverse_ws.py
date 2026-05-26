"""v2.1 Sprint 10 — Reverse-WSS controller endpoint.

WebSocket entry point at ``GET /api/v1/exec/reverse-ws`` upgraded to WS.
Sprint 10 ships ONLY:
  - handshake (hello → hello_ack)
  - heartbeat (heartbeat → heartbeat_ack)
  - clean disconnect + registry cleanup

Auth model
----------
mTLS is expected to be terminated at the reverse proxy (nginx/envoy/cloud
LB) the same way the rest of the executor surface authenticates. The WS
endpoint additionally requires a Bearer JWT passed as a query string
``?token=...`` parameter — WebSocket clients can't reliably set
Authorization headers across all environments.

Sprint 11 will add the multiplexed task-assign frames; this file's
``handle_session()`` is the structured place that work hooks in."""
from __future__ import annotations

import asyncio
import json
import logging

import jwt as _pyjwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.jwt_signing import verify as verify_jwt
from dlw.db.models.executor import Executor
from dlw.db.session import get_engine
from dlw.schemas.reverse_ws import (
    PROTOCOL_VERSION,
    ErrorFrame,
    HeartbeatAckFrame,
    HeartbeatFrame,
    HelloAckFrame,
    HelloFrame,
)
from dlw.services.reverse_ws_registry import get_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/exec", tags=["reverse-ws"])


async def _authenticate_executor(
    token: str, jwt_keypair, expected_executor_id: str,
) -> Executor | None:
    """Validate the JWT and resolve the Executor row. Returns None on
    any auth failure — caller decides whether to send an Error frame
    or just close."""
    try:
        claims = verify_jwt(jwt_keypair, token)
    except _pyjwt.PyJWTError:
        return None
    if claims.get("sub") != expected_executor_id:
        return None
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as session:
        return await session.get(Executor, expected_executor_id)


async def _send_frame(ws: WebSocket, frame) -> None:
    await ws.send_text(frame.model_dump_json())


@router.websocket("/reverse-ws")
async def reverse_ws_endpoint(
    websocket: WebSocket,
    token: str = Query(...),
    executor_id: str = Query(...),
) -> None:
    """v2.1 Sprint 10 reverse-WSS endpoint. Accepts the upgrade, runs
    handshake, then a heartbeat loop until the peer disconnects."""
    await websocket.accept()

    # Auth gate. We accepted the upgrade first so we can send an Error
    # frame on failure; clients can't read response bodies on a 4xx WS
    # rejection, but they CAN read a text frame before close.
    jwt_keypair = websocket.app.state.jwt_keypair
    executor = await _authenticate_executor(
        token, jwt_keypair, executor_id)
    if executor is None:
        await _send_frame(websocket, ErrorFrame(
            code="UNAUTHORIZED",
            message="JWT validation failed or executor not found"))
        return  # FastAPI closes the WS when the handler returns

    # Expect Hello as the very first message
    try:
        first_raw = await asyncio.wait_for(websocket.receive_text(),
                                            timeout=10.0)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        return  # FastAPI closes the WS when the handler returns

    try:
        first_obj = json.loads(first_raw)
        hello = HelloFrame.model_validate(first_obj)
    except (json.JSONDecodeError, ValidationError):
        await _send_frame(websocket, ErrorFrame(
            code="BAD_HELLO", message="first frame must be a valid Hello"))
        return  # FastAPI closes the WS when the handler returns

    if hello.executor_id != executor_id:
        # Mismatch between query-string ID and hello body — refuse
        await _send_frame(websocket, ErrorFrame(
            code="EXECUTOR_ID_MISMATCH",
            message="hello.executor_id does not match query string"))
        await websocket.close(code=1008)
        return

    if not hello.protocol_version.startswith(PROTOCOL_VERSION.split(".")[0] + "."):
        await _send_frame(websocket, ErrorFrame(
            code="PROTOCOL_VERSION_MISMATCH",
            message=f"server requires {PROTOCOL_VERSION}, "
                    f"client offered {hello.protocol_version}"))
        return  # FastAPI closes the WS when the handler returns

    # Register the session — this evicts any old socket for the same id
    registry = get_registry()
    session = await registry.register(
        executor_id=executor_id, websocket=websocket,
        protocol_version=hello.protocol_version)

    await _send_frame(websocket, HelloAckFrame(
        session_id=session.session_id))

    # Main loop: heartbeat-only in Sprint 10
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(
                    "reverse_ws: bad json from %s, ignoring", executor_id)
                continue
            ftype = obj.get("type")
            if ftype == HeartbeatFrame.model_fields["type"].default:
                await registry.touch_heartbeat(
                    executor_id=executor_id,
                    session_id=session.session_id)
                await _send_frame(websocket, HeartbeatAckFrame())
            elif ftype == ErrorFrame.model_fields["type"].default:
                # Client signaled it's leaving; honor by ending the loop
                logger.info(
                    "reverse_ws: %s sent Error %r, closing",
                    executor_id, obj.get("code"))
                break
            else:
                # Sprint 11+ frames will land here — log and ignore for now
                logger.debug(
                    "reverse_ws: ignoring frame type=%r in Sprint 10", ftype)

    except WebSocketDisconnect:
        logger.info("reverse_ws: %s disconnected", executor_id)
    finally:
        await registry.unregister(
            executor_id=executor_id, session_id=session.session_id)
