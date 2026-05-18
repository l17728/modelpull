"""Idempotent data-seed shared by the SP1 migration and tests."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.tenant import Tenant
from dlw.db.models.usage import QuotaSnapshot


async def seed(session: AsyncSession) -> None:
    # Tenant.quota_*/is_active use Python-side default= (NOT server_default),
    # so a Core pg_insert MUST supply them explicitly or the NOT NULL fails.
    await session.execute(pg_insert(Tenant).values(
        id=1, slug="default", display_name="Default Tenant",
        quota_bytes_month=0, quota_concurrent=10, quota_storage_gb=1024,
        is_active=True,
    ).on_conflict_do_nothing(index_elements=["id"]))
    tenant_ids = (await session.execute(select(Tenant.id))).scalars().all()
    for tid in tenant_ids:
        await session.execute(pg_insert(QuotaSnapshot).values(
            tenant_id=tid,
        ).on_conflict_do_nothing(index_elements=["tenant_id"]))
