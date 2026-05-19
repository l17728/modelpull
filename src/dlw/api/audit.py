"""GET /api/v1/audit/log — tenant-scoped audit search (UI-SP3)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.db.session import get_engine
from dlw.schemas.audit import AuditSearchResponse
from dlw.services.audit_query import search_audit_log

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


async def _session():
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as session:
        yield session


@router.get("/log")
async def get_audit_log(
    actor_user_id: int | None = Query(default=None),
    action: str | None = Query(default=None, description="prefix match"),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_perm("/api/v1/audit*", "GET")),
    session: AsyncSession = Depends(_session),
) -> AuditSearchResponse:
    items, next_cursor = await search_audit_log(
        session, principal.tenant_id,
        actor_user_id=actor_user_id, action_prefix=action,
        from_=from_, to=to, cursor=cursor, limit=limit)
    return AuditSearchResponse(items=items, next_cursor=next_cursor)
