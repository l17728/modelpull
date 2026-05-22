"""FU3 local-fs reclaim dispatch/confirm."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.storage_object import StorageObject, StoragePhysicalKey
from dlw.services.storage_objects import confirm_local_reclaim, dispatch_local_reclaim


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1"))
        await s.flush()
        s.add(StorageBackend(id=1, tenant_id=1, name="loc", backend_type="local",
                             config_encrypted=b'{"base_path": "/srv/dlw"}'))
        s.add(StorageBackend(id=2, tenant_id=1, name="bkt", backend_type="s3",
                             config_encrypted=b"{}"))
        await s.commit()
    yield
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


def _old(days=2):
    return datetime.now(UTC) - timedelta(days=days)


async def test_dispatch_returns_only_this_executor_local_orphans(session):
    # ex-1 local orphan (storage 1) -> dispatched; s3 key, other-executor key,
    # non-orphan (has storage_objects row), fresh key -> excluded.
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="a"*64,
        storage_key="repo/r/f", size=1, created_at=_old(), executor_id="ex-1"))
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=2, sha256="b"*64,   # s3
        storage_key="repo/s3/f", size=1, created_at=_old(), executor_id="ex-1"))
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="c"*64,   # other exec
        storage_key="repo/other/f", size=1, created_at=_old(), executor_id="ex-2"))
    session.add(StorageObject(tenant_id=1, storage_id=1, storage_key="repo/r/g",
        sha256="d"*64, size=1, refcount=1))                                    # live sha
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="d"*64,   # non-orphan
        storage_key="repo/r/g", size=1, created_at=_old(), executor_id="ex-1"))
    session.add(StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="e"*64,   # fresh
        storage_key="repo/fresh/f", size=1, created_at=datetime.now(UTC), executor_id="ex-1"))
    await session.commit()
    items = await dispatch_local_reclaim(session, "ex-1", grace_seconds=3600, limit=10)
    keys = {it.storage_key for it in items}
    assert keys == {"repo/r/f"}
    assert items[0].base_path == "/srv/dlw" and isinstance(items[0].id, int)


async def test_confirm_deletes_only_scoped_rows(session):
    r = StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="f"*64,
        storage_key="repo/conf/f", size=1, created_at=_old(), executor_id="ex-1")
    other = StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="g"*64,
        storage_key="repo/conf/o", size=1, created_at=_old(), executor_id="ex-2")
    session.add_all([r, other]); await session.commit()
    audited = []
    async def _audit(**kw): audited.append(kw)
    n = await confirm_local_reclaim(session, "ex-1", [r.id, other.id], audit=_audit)
    await session.commit()
    assert n == 1 and audited and audited[0]["action"] == "storage.gc.physical.local"
    remaining = (await session.execute(select(StoragePhysicalKey.storage_key))).scalars().all()
    assert "repo/conf/f" not in remaining and "repo/conf/o" in remaining   # other-exec row survives
