"""Local username/password authentication endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.principal import Principal, issue_system_jwt, require_principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.session import get_engine
from dlw.services import local_auth as svc

router = APIRouter(prefix="/api/v1/auth/local", tags=["local-auth"])


async def _session() -> AsyncSession:
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as s:
        yield s


class LoginReq(BaseModel):
    username: str
    password: str


class CreateUserReq(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8)
    tenant_id: int
    role: str
    project_ids: list[int] = Field(default_factory=list)


class ChangePasswordReq(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8)


class ResetPasswordReq(BaseModel):
    new_password: str = Field(min_length=8)


@router.post("/login")
async def login(req: LoginReq, session: AsyncSession = Depends(_session)) -> dict:
    cred = await svc.authenticate(session, req.username, req.password)
    if cred is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_CREDENTIALS",
                    "message": "invalid username or password"})
    settings = get_settings()
    token = issue_system_jwt(
        secret=settings.system_jwt_secret,
        user_id=cred.user_id,
        tenant_id=cred.tenant_id,
        role=cred.role,
        project_ids=cred.project_ids or [],
        ttl_seconds=3600,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": 3600,
        "user_id": cred.user_id,
        "tenant_id": cred.tenant_id,
        "role": cred.role,
        "must_change_password": cred.must_change_password,
    }


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    req: CreateUserReq,
    principal: Principal = Depends(require_perm("/api/v1/auth/local/users", "POST")),
    session: AsyncSession = Depends(_session),
) -> dict:
    cred = await svc.create_user(
        session, req.username, req.password,
        req.tenant_id, req.role,
        req.project_ids or None,
    )
    await session.commit()
    return {
        "user_id": cred.user_id,
        "username": cred.username,
        "tenant_id": cred.tenant_id,
        "role": cred.role,
    }


@router.get("/users")
async def list_users(
    principal: Principal = Depends(require_perm("/api/v1/auth/local/users", "GET")),
    session: AsyncSession = Depends(_session),
) -> list[dict]:
    users = await svc.list_users(session)
    return [
        {
            "user_id": u.user_id,
            "username": u.username,
            "tenant_id": u.tenant_id,
            "role": u.role,
            "must_change_password": u.must_change_password,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


@router.post("/password")
async def change_password(
    req: ChangePasswordReq,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(_session),
) -> dict:
    await svc.change_password(
        session, principal.user_id, req.old_password, req.new_password)
    await session.commit()
    return {"ok": True}


@router.post("/users/{user_id}/reset")
async def reset_password(
    user_id: int,
    req: ResetPasswordReq,
    principal: Principal = Depends(
        require_perm("/api/v1/auth/local/users/{user_id}/reset", "POST")),
    session: AsyncSession = Depends(_session),
) -> dict:
    await svc.reset_password(session, user_id, req.new_password)
    await session.commit()
    return {"ok": True}
