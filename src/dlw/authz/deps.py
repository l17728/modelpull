"""require_perm FastAPI dependency factory (Phase 3 SP1, tenant-scoped)."""
from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.principal import Principal, require_principal
from dlw.db.session import get_engine
from dlw.services.audit import write_audit


async def _session() -> AsyncSession:  # pragma: no cover - trivial
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as s:
        yield s


def require_perm(obj: str, act: str) -> Callable:
    """Dependency: casbin-enforce (role:<principal.role>, tenant, obj, act,
    rtenant). SP1 is tenant-scoped: rtenant == principal.tenant_id for
    collection/create routes, and object routes already re-assert ownership
    via tenant_filtered (cross-tenant id -> 404). system_admin / service
    principals short-circuit allow. Deny -> 403 RBAC_DENIED + audit."""

    async def _dep(
        request: Request,
        principal: Principal = Depends(require_principal),
        session: AsyncSession = Depends(_session),
    ) -> Principal:
        if principal.is_service or principal.role == "system_admin":
            return principal
        enforcer = request.app.state.casbin
        rtenant = principal.tenant_id
        if not enforcer.enforce(
            f"role:{principal.role}", principal.tenant_id, obj, act, rtenant
        ):
            await write_audit(
                session, action="permission_denied", resource_type="route",
                resource_id=f"{act} {obj}", outcome="denied",
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id or None)
            await session.commit()
            raise HTTPException(403, detail={"code": "RBAC_DENIED"})
        return principal

    return _dep
