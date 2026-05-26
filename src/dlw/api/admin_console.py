"""v2.1 Sprint 13 — Live Console admin REST endpoints.

system_admin only — these endpoints can drain executors and trigger
restarts. The actual command-result correlation (waiting for the
CommandResultFrame to come back over the reverse-WSS) lands in
Sprint 14 / a follow-on UI. Sprint 13 ships fire-and-forget semantics:
the caller gets back the command_id and can poll a future
GET /admin/executors/{id}/command/{cid} endpoint when correlation lands.

Listing live sessions (GET /admin/reverse-ws/sessions) is read-only and
useful both for operators and for the Live Console UI's executor picker."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dlw.auth.principal import Principal, require_principal
from dlw.schemas.reverse_ws import WHITELIST_COMMANDS
from dlw.services.reverse_dispatcher import (
    CommandNotWhitelisted,
    CommandUnknownExecutor,
    get_dispatcher,
)
from dlw.services.reverse_ws_registry import get_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin-console"])


class CommandRequest(BaseModel):
    command: str


class CommandResponse(BaseModel):
    command_id: str
    executor_id: str
    command: str


class SessionOut(BaseModel):
    session_id: str
    executor_id: str
    opened_at: str
    last_heartbeat_at: str
    protocol_version: str


class SessionsResponse(BaseModel):
    items: list[SessionOut]


@router.post("/executors/{executor_id}/command",
              response_model=CommandResponse)
async def post_executor_command(
    executor_id: str,
    body: CommandRequest,
    principal: Principal = Depends(require_principal),
) -> CommandResponse:
    """v2.1 Sprint 13 — send a whitelisted command (status / drain /
    restart) to a connected executor. system_admin only.

    Returns 404 if no live session for the executor; 422 if command is
    not in the whitelist."""
    if principal.role != "system_admin":
        logger.warning(
            "admin_console.command role_denied user_id=%s role=%s "
            "executor=%s command=%s",
            principal.user_id, principal.role, executor_id, body.command)
        raise HTTPException(status_code=403,
                            detail="system_admin role required")
    try:
        cid = await get_dispatcher().send_command(
            executor_id=executor_id, command=body.command)
    except CommandNotWhitelisted as e:
        raise HTTPException(
            status_code=422,
            detail={"code": "NOT_WHITELISTED", "allowed": list(WHITELIST_COMMANDS),
                    "message": str(e)}) from e
    except CommandUnknownExecutor as e:
        raise HTTPException(
            status_code=404,
            detail={"code": "EXECUTOR_OFFLINE",
                    "message": f"no live reverse-WSS session for {executor_id}"}) from e
    return CommandResponse(
        command_id=cid, executor_id=executor_id, command=body.command)


@router.get("/reverse-ws/sessions", response_model=SessionsResponse)
async def get_reverse_ws_sessions(
    principal: Principal = Depends(require_principal),
) -> SessionsResponse:
    """List every live reverse-WSS executor session. system_admin only.

    The UI's Live Console uses this to populate the executor picker;
    operators use it to verify an executor reconnected after a network
    outage."""
    if principal.role != "system_admin":
        logger.warning(
            "admin_console.sessions role_denied user_id=%s role=%s",
            principal.user_id, principal.role)
        raise HTTPException(status_code=403,
                            detail="system_admin role required")
    sessions = await get_registry().list_sessions()
    return SessionsResponse(items=[
        SessionOut(
            session_id=s.session_id,
            executor_id=s.executor_id,
            opened_at=s.opened_at.isoformat(),
            last_heartbeat_at=s.last_heartbeat_at.isoformat(),
            protocol_version=s.protocol_version)
        for s in sessions
    ])
