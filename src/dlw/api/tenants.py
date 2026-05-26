"""Tenant admin endpoints — system_admin only.

PUT /api/v1/tenants/{id}/quota — set quota limits, audit-logged."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.principal import Principal, require_principal
from dlw.db.session import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tenants", tags=["tenants"])


async def _session() -> AsyncSession:
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as s:
        yield s


@router.put("/{tenant_id}/sla")
async def put_tenant_sla_tier(
    tenant_id: int,
    body: dict,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(_session),
) -> dict:
    """v2.1 SP1: set the tenant's sla_tier. system_admin only."""
    from dlw.services.sla_tier import (
        InvalidTier, TenantNotFound, TierPatch, set_tenant_sla_tier,
    )
    if principal.role != "system_admin":
        logger.warning(
            "tenants_api.role_denied user_id=%s role=%s tenant_id=%s",
            principal.user_id, principal.role, tenant_id)
        raise HTTPException(status_code=403, detail="system_admin role required")
    tier = body.get("sla_tier")
    if tier is None:
        raise HTTPException(status_code=422,
                            detail={"code": "MISSING_FIELD", "field": "sla_tier"})
    try:
        tenant = await set_tenant_sla_tier(
            session, tenant_id=tenant_id, actor_user_id=principal.user_id,
            patch=TierPatch(sla_tier=str(tier)))
    except InvalidTier as e:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_TIER", "message": str(e)}) from e
    except TenantNotFound as e:
        raise HTTPException(status_code=404, detail="tenant not found") from e
    await session.commit()
    return {"tenant_id": tenant.id, "sla_tier": tenant.sla_tier}


@router.put("/{tenant_id}/quota")
async def put_tenant_quota(
    tenant_id: int,
    body: dict,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(_session),
) -> dict:
    from dlw.services.tenant_quota import (
        InvalidQuota, TenantNotFound, TenantQuotaPatch, set_tenant_quota,
    )
    if principal.role != "system_admin":
        logger.warning(
            "tenants_api.role_denied user_id=%s role=%s tenant_id=%s",
            principal.user_id, principal.role, tenant_id)
        raise HTTPException(status_code=403, detail="system_admin role required")
    patch = TenantQuotaPatch(
        quota_bytes_month=body.get("quota_bytes_month"),
        quota_concurrent=body.get("quota_concurrent"),
        quota_storage_gb=body.get("quota_storage_gb"),
        quota_ai_tokens_month=body.get("quota_ai_tokens_month"))
    try:
        tenant = await set_tenant_quota(
            session, tenant_id=tenant_id,
            actor_user_id=principal.user_id, patch=patch)
    except TenantNotFound as e:
        raise HTTPException(status_code=404, detail="tenant not found") from e
    except InvalidQuota as e:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_QUOTA", "message": str(e)}) from e
    await session.commit()
    return {"tenant_id": tenant.id,
            "quota_bytes_month": tenant.quota_bytes_month,
            "quota_concurrent": tenant.quota_concurrent,
            "quota_storage_gb": tenant.quota_storage_gb,
            "quota_ai_tokens_month": tenant.quota_ai_tokens_month}
