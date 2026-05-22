"""Phase 4: physical-key ledger records every written key (download + inherit)."""
from __future__ import annotations

import uuid as _uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.storage_object import StorageObject, StoragePhysicalKey
from dlw.services.storage_objects import record_physical_key


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


async def test_record_is_idempotent(session):
    for _ in range(2):
        await record_physical_key(session, tenant_id=1, storage_id=1,
                                  sha256="a" * 64, storage_key="repo/rev1/f",
                                  size=10)
        await session.commit()
    rows = (await session.execute(select(StoragePhysicalKey).where(
        StoragePhysicalKey.storage_key == "repo/rev1/f"))).scalars().all()
    assert len(rows) == 1


async def test_two_revisions_same_sha_two_keys(session):
    await record_physical_key(session, tenant_id=1, storage_id=1,
                              sha256="c" * 64, storage_key="repo/rev1/g", size=5)
    await record_physical_key(session, tenant_id=1, storage_id=1,
                              sha256="c" * 64, storage_key="repo/rev2/g", size=5)
    await session.commit()
    rows = (await session.execute(select(StoragePhysicalKey).where(
        StoragePhysicalKey.sha256 == "c" * 64))).scalars().all()
    assert {r.storage_key for r in rows} == {"repo/rev1/g", "repo/rev2/g"}


async def test_record_physical_key_does_not_bump_refcount(session):
    # Insert a StorageObject directly (no FileSubTask FK needed) so we can
    # assert that record_physical_key leaves refcount untouched.
    session.add(StorageObject(tenant_id=1, storage_id=1,
                              storage_key="repo/rev1/z", sha256="f" * 64,
                              size=7, refcount=1))
    await session.commit()
    before = await session.scalar(select(StorageObject.refcount).where(
        StorageObject.sha256 == "f" * 64))
    await record_physical_key(session, tenant_id=1, storage_id=1,
                              sha256="f" * 64, storage_key="repo/rev2/z", size=7)
    await session.commit()
    after = await session.scalar(select(StorageObject.refcount).where(
        StorageObject.sha256 == "f" * 64))
    assert before == after == 1


async def test_record_physical_key_stores_executor_id(session):
    await record_physical_key(session, tenant_id=1, storage_id=1, sha256="e" * 64,
                              storage_key="repo/exec/k", size=3, executor_id="ex-9")
    await session.commit()
    row = (await session.execute(select(StoragePhysicalKey).where(
        StoragePhysicalKey.storage_key == "repo/exec/k"))).scalar_one()
    assert row.executor_id == "ex-9"
