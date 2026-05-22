"""Phase 4: physical reclamation of fully-dereferenced keys (mock S3)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.storage_object import StorageObject, StoragePhysicalKey
from dlw.services.storage_objects import reclaim_physical_orphans


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1"))
        await s.flush()
        s.add(StorageBackend(id=1, tenant_id=1, name="bkt", backend_type="s3",
                             config_encrypted=b"{}"))
        s.add(StorageBackend(id=2, tenant_id=1, name="loc", backend_type="local",
                             config_encrypted=b"{}"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


class _FakeS3:
    def __init__(self):
        self.deleted = []
    def delete_object(self, Bucket, Key):  # noqa: N803
        self.deleted.append((Bucket, Key))


def _old(days=2):
    return datetime.now(UTC) - timedelta(days=days)


async def test_dereferenced_s3_keys_deleted_when_enabled(session):
    fake = _FakeS3()
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="a"*64,
                                   storage_key="repo/rev1/f", size=10,
                                   created_at=_old()))
    session.add(StorageObject(tenant_id=1, storage_id=1, storage_key="repo/rev1/g",
                              sha256="b"*64, size=10, refcount=1))
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="b"*64,
                                   storage_key="repo/rev1/g", size=10,
                                   created_at=_old()))
    await session.commit()
    audited = []
    res = await reclaim_physical_orphans(
        session, grace_seconds=3600, delete_enabled=True,
        make_client=lambda sid: (fake, "bkt", "s3"),
        audit=lambda **k: audited.append(k))
    await session.commit()
    assert ("bkt", "repo/rev1/f") in fake.deleted
    assert all(k != "repo/rev1/g" for _, k in fake.deleted)
    remaining = (await session.execute(select(StoragePhysicalKey.storage_key))).scalars().all()
    assert "repo/rev1/f" not in remaining and "repo/rev1/g" in remaining
    assert res["deleted"] == 1 and audited
    # audit carries the row id + key (id used as resource_id; key in payload)
    assert "id" in audited[0] and audited[0]["storage_key"] == "repo/rev1/f"


async def test_disabled_is_dry_run(session):
    fake = _FakeS3()
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="d"*64,
                                   storage_key="repo/rev1/h", size=10,
                                   created_at=_old()))
    await session.commit()
    res = await reclaim_physical_orphans(
        session, grace_seconds=3600, delete_enabled=False,
        make_client=lambda sid: (fake, "bkt", "s3"), audit=lambda **k: None)
    await session.commit()
    assert fake.deleted == []
    assert res["candidates"] >= 1 and res["deleted"] == 0
    rows = (await session.execute(select(StoragePhysicalKey.storage_key)
            .where(StoragePhysicalKey.storage_key == "repo/rev1/h"))).scalars().all()
    assert rows == ["repo/rev1/h"]


async def test_non_s3_backend_skipped(session):
    fake = _FakeS3()
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=2, sha256="e"*64,
                                   storage_key="loc/rev1/i", size=10,
                                   created_at=_old()))
    await session.commit()
    res = await reclaim_physical_orphans(
        session, grace_seconds=3600, delete_enabled=True,
        make_client=lambda sid: (fake, "loc", "local"), audit=lambda **k: None)
    await session.commit()
    assert fake.deleted == []
    rows = (await session.execute(select(StoragePhysicalKey.storage_key)
            .where(StoragePhysicalKey.storage_key == "loc/rev1/i"))).scalars().all()
    assert rows == ["loc/rev1/i"]


async def test_grace_respected(session):
    fake = _FakeS3()
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="f0"*32,
                                   storage_key="repo/rev1/fresh", size=10,
                                   created_at=datetime.now(UTC)))
    await session.commit()
    await reclaim_physical_orphans(
        session, grace_seconds=3600, delete_enabled=True,
        make_client=lambda sid: (fake, "bkt", "s3"), audit=lambda **k: None)
    assert all(k != "repo/rev1/fresh" for _, k in fake.deleted)


async def test_per_tick_cap(session):
    fake = _FakeS3()
    for n in range(5):
        session.add(StoragePhysicalKey(tenant_id=1, storage_id=1,
                                       sha256=f"{n:064d}",
                                       storage_key=f"repo/cap/{n}", size=1,
                                       created_at=_old()))
    await session.commit()
    res = await reclaim_physical_orphans(
        session, grace_seconds=3600, delete_enabled=True,
        make_client=lambda sid: (fake, "bkt", "s3"), audit=lambda **k: None,
        max_objects_per_tick=2)
    assert res["candidates"] == 2   # capped


async def test_priority_tenant_reclaimed_first_under_cap(session):
    from sqlalchemy import select

    from dlw.db.models.storage_object import StoragePhysicalKey
    from dlw.db.models.tenant import Tenant
    fake = _FakeS3()
    session.add(Tenant(id=42, slug="t42p", display_name="T42P"))
    await session.flush()
    session.add(StoragePhysicalKey(tenant_id=42, storage_id=1, sha256="p" * 64,
                                   storage_key="repo/prio/new", size=1,
                                   created_at=_old(1)))    # newer, priority
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="q" * 64,
                                   storage_key="repo/other/old", size=1,
                                   created_at=_old(5)))    # older, non-priority
    await session.commit()
    res = await reclaim_physical_orphans(
        session, grace_seconds=3600, delete_enabled=True,
        make_client=lambda sid: (fake, "bkt", "s3"), audit=lambda **k: None,
        max_objects_per_tick=1, priority_tenant_ids=frozenset({42}))
    await session.commit()
    assert ("bkt", "repo/prio/new") in fake.deleted
    assert res["deleted"] == 1
    remaining = (await session.execute(
        select(StoragePhysicalKey.storage_key))).scalars().all()
    assert "repo/prio/new" not in remaining and "repo/other/old" in remaining
