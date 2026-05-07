"""Bearer token authentication.

Single shared secret pulled from DLW_BEARER_TOKEN env var.
Multi-user OIDC PKCE comes in Phase 3 (see docs/v2.0/04-security-and-tenancy.md).
"""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from dlw.config import get_settings


async def require_bearer(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: 401 unless Authorization: Bearer <correct-token> present."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="malformed authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    expected = get_settings().bearer_token
    if not secrets.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
