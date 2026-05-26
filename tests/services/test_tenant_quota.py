"""Service-level tests for tenant_quota."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.services.tenant_quota import (
    InvalidQuota, TenantNotFound, TenantQuotaPatch, set_tenant_quota,
)


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=42, slug="t42", display_name="T42",
                     quota_bytes_month=1024, quota_concurrent=5,
                     quota_storage_gb=100, quota_ai_tokens_month=1000))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


async def test_set_bytes_quota_updates_field(session):
    t = await set_tenant_quota(
        session, tenant_id=42, actor_user_id=1,
        patch=TenantQuotaPatch(quota_bytes_month=2048))
    assert t.quota_bytes_month == 2048
    await session.rollback()


async def test_set_multiple_fields_at_once(session):
    t = await set_tenant_quota(
        session, tenant_id=42, actor_user_id=1,
        patch=TenantQuotaPatch(quota_concurrent=20, quota_storage_gb=500))
    assert t.quota_concurrent == 20
    assert t.quota_storage_gb == 500
    await session.rollback()


async def test_no_change_does_not_write_audit(session):
    """Passing the same values shouldn't emit an audit row."""
    from dlw.db.models.audit import AuditLog
    before = (await session.execute(
        select(AuditLog).where(AuditLog.action == "tenant.quota.update")
    )).scalars().all()
    # patch with the *existing* value
    await set_tenant_quota(
        session, tenant_id=42, actor_user_id=1,
        patch=TenantQuotaPatch(quota_bytes_month=1024))
    await session.commit()
    after = (await session.execute(
        select(AuditLog).where(AuditLog.action == "tenant.quota.update")
    )).scalars().all()
    assert len(after) == len(before)


async def test_audit_log_written_on_change(session):
    from dlw.db.models.audit import AuditLog
    await set_tenant_quota(
        session, tenant_id=42, actor_user_id=7,
        patch=TenantQuotaPatch(quota_ai_tokens_month=99999))
    await session.commit()
    row = (await session.execute(
        select(AuditLog).where(
            AuditLog.action == "tenant.quota.update",
            AuditLog.resource_id == "42").order_by(AuditLog.id.desc()).limit(1)
    )).scalar_one()
    assert row.actor_user_id == 7
    assert row.outcome == "success"
    assert "quota_ai_tokens_month" in (row.payload.get("changes") or {})


async def test_negative_quota_raises(session):
    with pytest.raises(InvalidQuota):
        await set_tenant_quota(
            session, tenant_id=42, actor_user_id=1,
            patch=TenantQuotaPatch(quota_concurrent=-1))


async def test_nonexistent_tenant_raises(session):
    with pytest.raises(TenantNotFound):
        await set_tenant_quota(
            session, tenant_id=9999, actor_user_id=1,
            patch=TenantQuotaPatch(quota_bytes_month=1))
