"""FU9 — adopt_orphan_local_keys (heartbeat-driven executor_id backfill)."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.storage_object import StoragePhysicalKey
from dlw.services.storage_objects import adopt_orphan_local_keys


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="proj1"))
        s.add(User(id=1, tenant_id=1, oidc_subject="u1", email="u1@t",
                   role="tenant_operator"))
        s.add(StorageBackend(id=1, tenant_id=1, name="bkt",
                             backend_type="s3", config_encrypted=b"{}"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


@pytest.mark.asyncio
async def test_adopt_null_key_on_accessible_storage(session):
    """NULL-executor_id key on accessible storage_id → adopted, returns 1."""
    key = StoragePhysicalKey(
        tenant_id=1, storage_id=1, sha256="a" * 64,
        storage_key="fu9/a.bin", size=100, executor_id=None)
    session.add(key)
    await session.flush()

    n = await adopt_orphan_local_keys(
        session, "exec-1",
        accessible_storage_ids=frozenset([1]),
        limit=10, tenant_id=1)

    assert n == 1
    await session.refresh(key)
    assert key.executor_id == "exec-1"
    await session.rollback()


@pytest.mark.asyncio
async def test_adopt_skips_already_owned(session):
    """Key already has executor_id → unchanged, returns 0."""
    key = StoragePhysicalKey(
        tenant_id=1, storage_id=1, sha256="b" * 64,
        storage_key="fu9/b.bin", size=100, executor_id="old-exec")
    session.add(key)
    await session.flush()

    n = await adopt_orphan_local_keys(
        session, "exec-1",
        accessible_storage_ids=frozenset([1]),
        limit=10, tenant_id=1)

    assert n == 0
    await session.refresh(key)
    assert key.executor_id == "old-exec"
    await session.rollback()


@pytest.mark.asyncio
async def test_adopt_skips_other_tenant(session):
    """NULL key on storage_id=1 tenant=1; adoption called with tenant=2 → not adopted."""
    key = StoragePhysicalKey(
        tenant_id=1, storage_id=1, sha256="c" * 64,
        storage_key="fu9/c.bin", size=100, executor_id=None)
    session.add(key)
    await session.flush()

    # Executor of tenant=2 should NOT adopt tenant=1 key
    n = await adopt_orphan_local_keys(
        session, "exec-1",
        accessible_storage_ids=frozenset([1]),
        limit=10, tenant_id=2)

    assert n == 0
    await session.refresh(key)
    assert key.executor_id is None
    await session.rollback()


@pytest.mark.asyncio
async def test_adopt_empty_acc_ids(session):
    """Empty accessible_storage_ids → returns 0 immediately, no DB access."""
    key = StoragePhysicalKey(
        tenant_id=1, storage_id=1, sha256="d" * 64,
        storage_key="fu9/d.bin", size=100, executor_id=None)
    session.add(key)
    await session.flush()

    n = await adopt_orphan_local_keys(
        session, "exec-1",
        accessible_storage_ids=frozenset(),
        limit=10, tenant_id=1)

    assert n == 0
    await session.refresh(key)
    assert key.executor_id is None
    await session.rollback()


@pytest.mark.asyncio
async def test_adopt_limit(session):
    """limit=2 with 5 NULL keys → exactly 2 adopted, 3 remain NULL."""
    # Use distinct sha256/keys to avoid collisions with earlier tests
    keys = [
        StoragePhysicalKey(
            tenant_id=1, storage_id=1,
            sha256=f"{'e' * 63}{i}", storage_key=f"fu9/lim{i}.bin",
            size=100, executor_id=None)
        for i in range(5)
    ]
    session.add_all(keys)
    await session.flush()

    n = await adopt_orphan_local_keys(
        session, "exec-1",
        accessible_storage_ids=frozenset([1]),
        limit=2, tenant_id=1)

    assert n == 2
    remaining_null = (await session.execute(
        select(StoragePhysicalKey)
        .where(StoragePhysicalKey.executor_id.is_(None),
               StoragePhysicalKey.storage_key.like("fu9/lim%.bin"))
    )).scalars().all()
    assert len(remaining_null) == 3
    await session.rollback()
