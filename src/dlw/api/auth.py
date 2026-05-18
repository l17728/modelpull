"""OIDC login / callback / me (Phase 3 SP1)."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.oidc import (
    OidcClaims,
    exchange_code_dev,
    exchange_code_real,
    parse_tenant_rules,
    resolve_tenant,
)
from dlw.auth.principal import Principal, issue_system_jwt, require_principal
from dlw.config import get_settings
from dlw.db.models.tenant import Tenant, User
from dlw.db.session import get_engine
from dlw.services.audit import write_audit

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_STATE_COOKIE = "dlw_oidc_state"


async def _session() -> AsyncSession:  # pragma: no cover - trivial
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as s:
        yield s


@router.get("/login")
async def login() -> Response:
    settings = get_settings()
    state = secrets.token_urlsafe(24)
    if settings.auth_dev_mode:
        loc = f"/api/v1/auth/callback?code=dev:dev@local&state={state}"
    else:
        import httpx
        from authlib.integrations.httpx_client import AsyncOAuth2Client
        async with httpx.AsyncClient(timeout=10) as http:
            meta = (await http.get(
                f"{settings.oidc_issuer.rstrip('/')}"
                "/.well-known/openid-configuration")).json()
        oauth = AsyncOAuth2Client(
            settings.oidc_client_id, settings.oidc_client_secret,
            redirect_uri=settings.oidc_redirect_url,
            scope="openid email profile groups")
        loc, _ = oauth.create_authorization_url(
            meta["authorization_endpoint"], state=state)
    resp = RedirectResponse(loc, status_code=307)
    resp.set_cookie(_STATE_COOKIE, state, httponly=True,
                    samesite="strict", secure=not settings.auth_dev_mode)
    return resp


@router.get("/callback")
async def callback(
    request: Request, code: str, state: str,
    session: AsyncSession = Depends(_session),
) -> Response:
    settings = get_settings()
    if request.cookies.get(_STATE_COOKIE) != state:
        raise HTTPException(400, detail="invalid state")

    try:
        if settings.auth_dev_mode:
            claims: OidcClaims = exchange_code_dev(code)
        else:
            claims = await exchange_code_real(
                code=code, state=state, issuer=settings.oidc_issuer,
                client_id=settings.oidc_client_id,
                client_secret=settings.oidc_client_secret,
                redirect_url=settings.oidc_redirect_url)
    except Exception as e:
        raise HTTPException(503, detail=f"oidc upstream error: {e}") from e

    # Always re-validate tenant rules on every login so that rule changes
    # take effect immediately (e.g. an operator removes a domain mapping).
    rules = parse_tenant_rules(settings.auth_tenant_rules_json)
    resolved = resolve_tenant(claims, rules)
    if resolved is None:
        await write_audit(session, action="login", resource_type="user",
                          resource_id=claims.sub, outcome="denied",
                          tenant_id=None, actor_user_id=None)
        await session.commit()
        raise HTTPException(
            403, detail={"code": "TENANT_UNRESOLVED",
                         "message": "no tenant rule matched"})
    slug, role = resolved

    user = (await session.execute(
        select(User).where(User.oidc_subject == claims.sub)
    )).scalar_one_or_none()

    if user is None:
        tenant = (await session.execute(
            select(Tenant).where(Tenant.slug == slug)
        )).scalar_one_or_none()
        if tenant is None:
            raise HTTPException(
                403, detail={"code": "TENANT_UNRESOLVED",
                             "message": f"tenant '{slug}' not provisioned"})
        user = User(tenant_id=tenant.id, oidc_subject=claims.sub,
                    email=claims.email, role=role)
        session.add(user)
        await session.flush()
    else:
        user.email = claims.email or user.email

    token = issue_system_jwt(
        secret=settings.system_jwt_secret, user_id=user.id,
        tenant_id=user.tenant_id, role=user.role, project_ids=[])
    await write_audit(session, action="login", resource_type="user",
                      resource_id=str(user.id), outcome="success",
                      tenant_id=user.tenant_id, actor_user_id=user.id)
    await session.commit()
    return JSONResponse({
        "system_jwt": token, "expires_in": 3600,
        "tenant_id": user.tenant_id, "role": user.role,
    })


@router.get("/me")
async def me(principal: Principal = Depends(require_principal)) -> dict:
    return {
        "user_id": principal.user_id, "tenant_id": principal.tenant_id,
        "role": principal.role, "project_ids": list(principal.project_ids),
        "is_service": principal.is_service,
    }
