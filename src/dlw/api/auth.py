"""OIDC login / callback / me (Phase 3 SP1)."""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
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
from dlw.db.models.device_auth import DeviceAuthSession
from dlw.db.models.tenant import Tenant, User
from dlw.db.session import get_engine
from dlw.services.audit import write_audit

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_STATE_COOKIE = "dlw_oidc_state"

# ---------------------------------------------------------------------------
# RFC 8628 helpers
# ---------------------------------------------------------------------------

_USER_CODE_ALPHA = "BCDFGHJKLMNPQRSTVWXZ23456789"


def _hash_code(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _gen_user_code() -> str:
    chars = "".join(secrets.choice(_USER_CODE_ALPHA) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"


def _norm_user_code(s: str) -> str:
    """Canonicalize a user-supplied code → XXXX-XXXX (uppercase, alnum only)."""
    cleaned = "".join(c for c in s.upper() if c.isalnum())
    if len(cleaned) >= 8:
        return f"{cleaned[:4]}-{cleaned[4:8]}"
    return cleaned


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class DeviceTokenReq(BaseModel):
    device_code: str


class DeviceApproveReq(BaseModel):
    user_code: str
    action: str = "approve"


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


# ---------------------------------------------------------------------------
# RFC 8628 device-code endpoints (FU6)
# ---------------------------------------------------------------------------


@router.post("/device")
async def device_authorize(
    session: AsyncSession = Depends(_session),
) -> JSONResponse:
    """Start a device-code flow (no auth required)."""
    settings = get_settings()
    now = datetime.now(UTC)
    device_code = secrets.token_urlsafe(32)
    code_hash = _hash_code(device_code)
    expires_at = now + timedelta(seconds=settings.device_code_ttl_seconds)

    # Retry up to 5× on the unlikely user_code collision.
    for _ in range(5):
        uc = _gen_user_code()
        existing = (await session.execute(
            select(DeviceAuthSession).where(
                DeviceAuthSession.user_code == uc,
                DeviceAuthSession.status == "pending",
            )
        )).scalar_one_or_none()
        if existing is None:
            break

    row = DeviceAuthSession(
        device_code_hash=code_hash,
        user_code=uc,
        status="pending",
        expires_at=expires_at,
        interval_seconds=settings.device_poll_interval_seconds,
    )
    session.add(row)
    try:
        await write_audit(
            session, action="auth.device.authorize",
            resource_type="device_auth", resource_id=uc,
            outcome="success", tenant_id=None, actor_user_id=None,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    vuri = settings.device_verification_uri
    return JSONResponse({
        "device_code": device_code,
        "user_code": uc,
        "verification_uri": vuri,
        "verification_uri_complete": f"{vuri}?user_code={uc}",
        "expires_in": settings.device_code_ttl_seconds,
        "interval": settings.device_poll_interval_seconds,
    })


@router.post("/device/approve")
async def device_approve(
    body: DeviceApproveReq,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(_session),
) -> dict:
    """Approve or deny a pending device-code session (requires auth; rejects service tokens)."""
    if principal.is_service:
        raise HTTPException(403, detail={"code": "SERVICE_CANNOT_APPROVE"})

    now = datetime.now(UTC)
    norm_code = _norm_user_code(body.user_code)

    row = (await session.execute(
        select(DeviceAuthSession)
        .where(
            DeviceAuthSession.user_code == norm_code,
            DeviceAuthSession.status == "pending",
            DeviceAuthSession.expires_at > now,
        )
        .with_for_update()
    )).scalar_one_or_none()

    if row is None:
        raise HTTPException(404, detail={"code": "DEVICE_CODE_INVALID"})

    if body.action == "deny":
        row.status = "denied"
        audit_action = "auth.device.deny"
    else:
        row.status = "approved"
        row.user_id = principal.user_id
        row.tenant_id = principal.tenant_id
        row.role = principal.role
        row.project_ids = list(principal.project_ids)
        row.approved_at = now
        audit_action = "auth.device.approve"

    await write_audit(
        session, action=audit_action,
        resource_type="device_auth", resource_id=row.user_code,
        outcome="success", tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
    )
    await session.commit()
    return {"status": row.status}


@router.post("/device/token")
async def device_token(
    body: DeviceTokenReq,
    session: AsyncSession = Depends(_session),
) -> JSONResponse:
    """Poll for a token (no auth required). Returns RFC 8628 error codes as HTTP 400."""
    settings = get_settings()
    now = datetime.now(UTC)

    def _rfc_error(code: str) -> JSONResponse:
        return JSONResponse({"error": code}, status_code=400)

    row = (await session.execute(
        select(DeviceAuthSession)
        .where(DeviceAuthSession.device_code_hash == _hash_code(body.device_code))
        .with_for_update()
    )).scalar_one_or_none()

    if row is None:
        return _rfc_error("invalid_grant")

    # Rate-limit: slow_down only while pending (RFC 8628 §3.5)
    if row.status == "pending" and row.last_polled_at is not None:
        elapsed = (now - row.last_polled_at).total_seconds()
        if elapsed < row.interval_seconds:
            row.last_polled_at = now
            await session.commit()
            return _rfc_error("slow_down")

    row.last_polled_at = now

    # Check expiry (only for non-terminal statuses)
    if row.status not in ("approved", "consumed") and now > row.expires_at:
        await session.commit()
        return _rfc_error("expired_token")

    if row.status == "pending":
        await session.commit()
        return _rfc_error("authorization_pending")

    if row.status == "denied":
        await session.commit()
        return _rfc_error("access_denied")

    if row.status == "consumed":
        await session.commit()
        return _rfc_error("expired_token")

    # status == "approved" → mint JWT and mark consumed
    access_token = issue_system_jwt(
        secret=settings.system_jwt_secret,
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        role=row.role,
        project_ids=row.project_ids or [],
    )
    row.status = "consumed"
    await write_audit(
        session, action="auth.device.token",
        resource_type="device_auth", resource_id=row.user_code,
        outcome="success", tenant_id=row.tenant_id,
        actor_user_id=row.user_id,
    )
    await session.commit()
    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "tenant_id": row.tenant_id,
        "role": row.role,
    })
