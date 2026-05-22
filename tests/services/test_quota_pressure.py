"""FU2 quota-pressure detection."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.services.storage_objects import pressured_tenant_ids


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.tenant import Tenant
    from dlw.db.models.usage import QuotaSnapshot
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1", quota_storage_gb=10))
        s.add(Tenant(id=2, slug="t2", display_name="T2", quota_storage_gb=10))
        s.add(Tenant(id=3, slug="t3", display_name="T3", quota_storage_gb=0))
        await s.flush()
        s.add(QuotaSnapshot(tenant_id=1, storage_gb_used=10))   # 100% -> pressured
        s.add(QuotaSnapshot(tenant_id=2, storage_gb_used=5))    # 50%  -> not
        s.add(QuotaSnapshot(tenant_id=3, storage_gb_used=999))  # quota 0 -> not
        await s.commit()
    yield
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


async def test_pressured_set(session):
    assert await pressured_tenant_ids(session, threshold=0.9) == frozenset({1})
