"""Phase 4 Part A: storage usage accounting + quota enforcement."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.storage_object import StorageObject
from dlw.services.quota import QuotaExceeded, aggregate_snapshots, check_quota_for_new_task


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1", quota_storage_gb=1))
        s.add(Tenant(id=2, slug="t2", display_name="T2", quota_storage_gb=1000))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


async def test_aggregate_computes_storage_gb_used(session):
    gib = 1024 ** 3
    session.add_all([
        StorageObject(tenant_id=1, storage_id=1, storage_key="k1",
                      sha256="a" * 64, size=gib, refcount=1),
        StorageObject(tenant_id=1, storage_id=1, storage_key="k2",
                      sha256="b" * 64, size=gib, refcount=1)])
    await session.commit()
    await aggregate_snapshots(session)
    await session.commit()
    from dlw.db.models.usage import QuotaSnapshot
    snap = await session.get(QuotaSnapshot, 1)
    assert snap.storage_gb_used == 2


async def test_quota_blocks_at_or_over_storage(session):
    with pytest.raises(QuotaExceeded) as ei:
        await check_quota_for_new_task(session, 1)
    assert ei.value.metric == "storage"


async def test_quota_passes_under_storage(session):
    await aggregate_snapshots(session)
    await session.commit()
    await check_quota_for_new_task(session, 2)   # must not raise
