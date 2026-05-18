"""Request principal decoded from the system-JWT (Phase 3 SP1).

The system-JWT is issued by /auth/callback after OIDC. It is HS256-signed
with settings.system_jwt_secret (shared across active/standby — both must
verify the same user tokens, so it's a config secret, not a per-instance
bootstrapped keypair like the executor EdDSA key)."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

import jwt as _pyjwt
from fastapi import Header, HTTPException, Request, status

SYSTEM_JWT_ALG = "HS256"
SYSTEM_JWT_ISS = "dlw-controller"
SYSTEM_JWT_TTL_SECONDS = 3600


@dataclass(frozen=True)
class Principal:
    user_id: int
    tenant_id: int
    role: str
    project_ids: tuple[int, ...]
    is_service: bool = False


def issue_system_jwt(
    *,
    secret: str,
    user_id: int,
    tenant_id: int,
    role: str,
    project_ids: list[int],
    ttl_seconds: int = SYSTEM_JWT_TTL_SECONDS,
) -> str:
    now = int(time.time())
    return _pyjwt.encode(
        {
            "iss": SYSTEM_JWT_ISS,
            "sub": str(user_id),
            "tid": tenant_id,
            "role": role,
            "pids": project_ids,
            "iat": now,
            "exp": now + ttl_seconds,
        },
        secret,
        algorithm=SYSTEM_JWT_ALG,
    )


def _ct_eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


async def require_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    settings = request.app.state.settings
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()

    svc = settings.system_admin_token
    if svc and _ct_eq(token, svc):
        return Principal(
            user_id=0, tenant_id=1, role="system_admin",
            project_ids=(), is_service=True,
        )
    try:
        claims = _pyjwt.decode(
            token,
            settings.system_jwt_secret,
            algorithms=[SYSTEM_JWT_ALG],
            issuer=SYSTEM_JWT_ISS,
            options={"require": ["sub", "tid", "role", "exp", "iss", "iat"]},
        )
    except _pyjwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    return Principal(
        user_id=int(claims["sub"]),
        tenant_id=int(claims["tid"]),
        role=str(claims["role"]),
        project_ids=tuple(int(x) for x in claims.get("pids", [])),
    )
