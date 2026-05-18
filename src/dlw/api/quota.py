"""GET /api/v1/quota/current (Phase 3 SP1; security §7.5, no forecast)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.db.models.tenant import Tenant
from dlw.db.models.usage import QuotaSnapshot
from dlw.db.session import get_engine

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
    tenant = await session.get(Tenant, principal.tenant_id)
    if tenant is None:
        raise HTTPException(404, detail="tenant not found")
    snap = await session.get(QuotaSnapshot, principal.tenant_id)
    return {
        "tenant_id": tenant.id,
        "bytes_used_month": snap.bytes_used_month if snap else 0,
        "bytes_quota_month": tenant.quota_bytes_month,
        "storage_gb_used": snap.storage_gb_used if snap else 0,
        "storage_gb_quota": tenant.quota_storage_gb,
        "concurrent_tasks": snap.concurrent_tasks if snap else 0,
        "concurrent_quota": tenant.quota_concurrent,
    }
