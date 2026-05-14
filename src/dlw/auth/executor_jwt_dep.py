"""Executor JWT dependency (Phase 2 W3a §3.4)."""
from __future__ import annotations

import jwt as _pyjwt
from fastapi import Depends, Header, HTTPException, Request

from dlw.auth.executor_mtls import require_executor_mtls
from dlw.auth.jwt_signing import verify
from dlw.db.models.executor import Executor


async def require_executor_jwt(
    request: Request,
    authorization: str | None = Header(default=None),
    ex: Executor = Depends(require_executor_mtls),
) -> Executor:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, detail="missing executor JWT")
    token = authorization.split(" ", 1)[1]
    try:
        claims = verify(request.app.state.jwt_keypair, token)
    except _pyjwt.PyJWTError as e:
        raise HTTPException(401, detail=f"invalid JWT: {e}") from e
    if claims["sub"] != ex.id:
        raise HTTPException(401, detail="JWT sub mismatch")
    return ex
