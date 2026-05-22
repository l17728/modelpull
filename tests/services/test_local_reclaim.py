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


# ── FU4: NFS-shared widening tests ───────────────────────────────────────────

async def test_resolve_accessible_storage_ids(session):
    """resolve_accessible_storage_ids returns the set of storage_ids for local
    backends whose decrypted base_path is in the given set."""
    from dlw.services.storage_objects import resolve_accessible_storage_ids
    acc = await resolve_accessible_storage_ids(session, frozenset({"/srv/dlw"}))
    assert acc == {1}


async def test_resolve_accessible_storage_ids_empty_input(session):
    """Empty base_paths → empty result without hitting the DB."""
    from dlw.services.storage_objects import resolve_accessible_storage_ids
    acc = await resolve_accessible_storage_ids(session, frozenset())
    assert acc == set()


async def test_dispatch_sibling_with_accessible_ids(session):
    """A sibling executor (not the writer) can dispatch a key whose storage_id
    is in its accessible_storage_ids set."""
    from dlw.services.storage_objects import resolve_accessible_storage_ids

    orphan = StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="aa"*32,
        storage_key="repo/sibling/f", size=42, created_at=_old(),
        executor_id="ex-writer")
    session.add(orphan)
    await session.commit()

    acc = await resolve_accessible_storage_ids(session, frozenset({"/srv/dlw"}))
    items = await dispatch_local_reclaim(
        session, "ex-sibling", grace_seconds=3600, limit=10,
        accessible_storage_ids=acc)
    keys = {it.storage_key for it in items}
    assert "repo/sibling/f" in keys


async def test_dispatch_sibling_without_accessible_ids_excluded(session):
    """Without accessible_storage_ids, a sibling cannot see the writer's key."""
    orphan = StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="bb"*32,
        storage_key="repo/sib-noac/f", size=42, created_at=_old(),
        executor_id="ex-writer2")
    session.add(orphan)
    await session.commit()

    items = await dispatch_local_reclaim(
        session, "ex-sibling2", grace_seconds=3600, limit=10,
        accessible_storage_ids=frozenset())
    keys = {it.storage_key for it in items}
    assert "repo/sib-noac/f" not in keys


async def test_dispatch_writer_gets_own_key(session):
    """The writer gets its own key regardless of accessible_storage_ids."""
    orphan = StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="cc"*32,
        storage_key="repo/writer-own/f", size=99, created_at=_old(),
        executor_id="ex-writer3")
    session.add(orphan)
    await session.commit()

    items = await dispatch_local_reclaim(
        session, "ex-writer3", grace_seconds=3600, limit=10,
        accessible_storage_ids=frozenset())
    keys = {it.storage_key for it in items}
    assert "repo/writer-own/f" in keys


async def test_confirm_widening_sibling_authorized(session):
    """Critical: confirm_local_reclaim with accessible_storage_ids allows a sibling
    executor to confirm (delete) a key written by a different executor."""
    from dlw.services.storage_objects import resolve_accessible_storage_ids

    orphan = StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="dd"*32,
        storage_key="repo/confirm-sib/f", size=77, created_at=_old(),
        executor_id="ex-writer4")
    session.add(orphan)
    await session.commit()
    key_id = orphan.id

    async def _a(**k): pass

    acc = await resolve_accessible_storage_ids(session, frozenset({"/srv/dlw"}))
    n = await confirm_local_reclaim(
        session, "ex-sibling4", [key_id], audit=_a,
        accessible_storage_ids=acc)
    await session.commit()
    assert n == 1

    # Row should be deleted
    row = await session.get(StoragePhysicalKey, key_id)
    assert row is None


async def test_confirm_widening_sibling_unauthorized_without_acc(session):
    """Without accessible_storage_ids, sibling confirm returns 0 (no match)."""
    orphan = StoragePhysicalKey(tenant_id=1, storage_id=1, sha256="ee"*32,
        storage_key="repo/confirm-sib-no/f", size=55, created_at=_old(),
        executor_id="ex-writer5")
    session.add(orphan)
    await session.commit()
    key_id = orphan.id

    async def _a(**k): pass

    n = await confirm_local_reclaim(
        session, "ex-sibling5", [key_id], audit=_a,
        accessible_storage_ids=frozenset())
    await session.commit()
    assert n == 0

    # Row should still exist
    row = await session.get(StoragePhysicalKey, key_id)
    assert row is not None


async def test_confirm_tenant_scope(session):
    """confirm_local_reclaim with tenant_id=1 refuses to delete a key with tenant_id=2."""
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Tenant
    from dlw.services.storage_objects import resolve_accessible_storage_ids

    # We need tenant 2 and a local backend for tenant 2
    async with session.begin_nested():
        t2_exists = await session.scalar(
            select(Tenant.id).where(Tenant.id == 2))
        if t2_exists is None:
            session.add(Tenant(id=2, slug="t2", display_name="T2"))
            await session.flush()
        b99_exists = await session.scalar(
            select(StorageBackend.id).where(StorageBackend.id == 99))
        if b99_exists is None:
            session.add(StorageBackend(id=99, tenant_id=2, name="loc2",
                backend_type="local",
                config_encrypted=b'{"base_path": "/srv/dlw"}'))
            await session.flush()

    orphan = StoragePhysicalKey(tenant_id=2, storage_id=99, sha256="ff"*32,
        storage_key="repo/tenant-scope/f", size=33, created_at=_old(),
        executor_id="ex-writer6")
    session.add(orphan)
    await session.commit()
    key_id = orphan.id

    async def _a(**k): pass

    acc = await resolve_accessible_storage_ids(session, frozenset({"/srv/dlw"}))
    # Both storage_id=1 and 99 have base_path="/srv/dlw"
    assert 1 in acc

    n = await confirm_local_reclaim(
        session, "ex-sibling6", [key_id], audit=_a,
        accessible_storage_ids=acc, tenant_id=1)
    await session.commit()
    assert n == 0  # tenant_id=1 cannot touch tenant_id=2 key

    # Row should still exist
    row = await session.get(StoragePhysicalKey, key_id)
    assert row is not None
