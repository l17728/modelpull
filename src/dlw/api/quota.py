"""GET /api/v1/quota/current (Phase 3 SP1; security §7.5, no forecast)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.db.session import get_engine
from dlw.services.quota_read import get_quota_snapshot

router = APIRouter(prefix="/api/v1/quota", tags=["quota"])


async def _session():
    f = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with f() as s:
        yield s


@router.get("/current")
async def current(
    principal: Principal = Depends(require_perm("/api/v1/quota*", "GET")),
    session: AsyncSession = Depends(_session),
) -> dict:
    snap = await get_quota_snapshot(session, principal.tenant_id)
    if snap is None:
        raise HTTPException(404, detail="tenant not found")
    return snap
