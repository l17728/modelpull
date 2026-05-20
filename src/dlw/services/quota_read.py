"""Shared quota-snapshot read used by /quota/current (one-shot) and
/quota/current/stream (SSE) — UI-SP5e."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.tenant import Tenant
from dlw.db.models.usage import QuotaSnapshot


async def get_quota_snapshot(
    session: AsyncSession, tenant_id: int,
) -> dict | None:
    """Read tenant + quota_snapshots row → flat dict; None when tenant gone."""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return None
    snap = await session.get(QuotaSnapshot, tenant_id)
    return {
        "tenant_id": tenant.id,
        "bytes_used_month": snap.bytes_used_month if snap else 0,
        "bytes_quota_month": tenant.quota_bytes_month,
        "storage_gb_used": snap.storage_gb_used if snap else 0,
        "storage_gb_quota": tenant.quota_storage_gb,
        "concurrent_tasks": snap.concurrent_tasks if snap else 0,
        "concurrent_quota": tenant.quota_concurrent,
    }
