"""FastAPI dep that 503s executor-loop calls while the controller is recovering
after a failover (Phase 2 W3c, INVARIANT 33)."""
from __future__ import annotations

from fastapi import HTTPException, Request


async def require_not_recovering(request: Request) -> None:
    """Returns normally if the controller is active (or state is unset, which
    defaults to active for tests bypassing the lifespan). Raises 503 with
    detail.code='CONTROLLER_RECOVERING' if state == 'recovering'."""
    state = getattr(request.app.state, "controller_state", "active")
    if state == "recovering":
        raise HTTPException(
            status_code=503,
            detail={"code": "CONTROLLER_RECOVERING",
                    "message": "controller recovering after failover, retry shortly"},
        )
