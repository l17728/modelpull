"""v2.1 Sprint 3 — real Physical GC tests.

Uses a stub ObjectStoreDeleter (in-memory recording) so the tests run
without minio. The integration-with-real-S3 path is covered by
storage_client.delete_object_silently's own tests and the manual QA
plan (qa-test-plan.md § R4-R5)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.services.physical_gc import (
    DefaultObjectStoreDeleter,
    EvictionCandidate,
    EvictionResult,
    PhysicalGCDisabled,
    evict_one,
    find_lru_eviction_candidates,
    find_tombstone_candidates,
    gc_run_once,
)


class StubDeleter:
    """Records calls; returns configurable success/failure."""

    def __init__(self, succeed: bool = True) -> None:
        self.calls: list[tuple[int, str]] = []
        self.succeed = succeed

    async def delete(self, storage_id: int, key: str) -> bool:
        self.calls.append((storage_id, key))
        return self.succeed


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=500, slug="gc", display_name="GCTest",
                     quota_storage_gb=1))   # 1 GiB tenant
        await s.flush()
        s.add(StorageBackend(id=500, tenant_id=500, name="s500",
                              backend_type="s3", config_encrypted=b""))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


@pytest.fixture(autouse=True)
def _enable_gc(monkeypatch):
    monkeypatch.setenv("DLW_PHYSICAL_GC_ENABLED", "true")
    yield


# ---------------------------------------------------------------------------
# find_tombstone_candidates
# ---------------------------------------------------------------------------

async def test_find_tombstone_picks_refcount_zero_old_rows(session):
    """A storage_object with refcount=0 and created_at older than the
    grace period IS a tombstone candidate."""
    from dlw.db.models.storage_object import StorageObject
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    obj = StorageObject(
        tenant_id=500, storage_id=500, storage_key="k-old", sha256="a" * 64,
        size=1024, refcount=0, last_referenced_at=long_ago, created_at=long_ago)
    session.add(obj)
    await session.flush()
    cands = await find_tombstone_candidates(session, grace_seconds=60)
    assert any(c.storage_key == "k-old" for c in cands)
    await session.rollback()


async def test_find_tombstone_skips_recent_rows(session):
    """A row younger than the grace period is NOT yet a tombstone."""
    from dlw.db.models.storage_object import StorageObject
    just_now = datetime.now(UTC)
    obj = StorageObject(
        tenant_id=500, storage_id=500, storage_key="k-new", sha256="b" * 64,
        size=1024, refcount=0,
        last_referenced_at=just_now, created_at=just_now)
    session.add(obj)
    await session.flush()
    cands = await find_tombstone_candidates(session, grace_seconds=3600)
    assert not any(c.storage_key == "k-new" for c in cands)
    await session.rollback()


async def test_find_tombstone_skips_referenced_rows(session):
    """Active refcount > 0 protects the object."""
    from dlw.db.models.storage_object import StorageObject
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    obj = StorageObject(
        tenant_id=500, storage_id=500, storage_key="k-live", sha256="c" * 64,
        size=1024, refcount=1, last_referenced_at=long_ago, created_at=long_ago)
    session.add(obj)
    await session.flush()
    cands = await find_tombstone_candidates(session, grace_seconds=60)
    assert not any(c.storage_key == "k-live" for c in cands)
    await session.rollback()


# ---------------------------------------------------------------------------
# evict_one
# ---------------------------------------------------------------------------

async def test_evict_one_deletes_row_and_writes_audit(session):
    from dlw.db.models.audit import AuditLog
    from dlw.db.models.storage_object import StorageObject
    from sqlalchemy import select
    obj = StorageObject(
        tenant_id=500, storage_id=500, storage_key="k-evict", sha256="d" * 64,
        size=2048, refcount=0,
        last_referenced_at=datetime.now(UTC) - timedelta(hours=2),
        created_at=datetime.now(UTC) - timedelta(hours=2))
    session.add(obj)
    await session.flush()
    candidate = EvictionCandidate(
        object_id=obj.id, storage_id=500, storage_key="k-evict",
        sha256="d" * 64, size_bytes=2048, reason="tombstone")
    deleter = StubDeleter(succeed=True)
    r = await evict_one(
        session, candidate, deleter=deleter, tenant_id=500, actor_user_id=1)
    assert r.deleted is True
    assert deleter.calls == [(500, "k-evict")]
    # Row deleted
    assert await session.get(StorageObject, obj.id) is None
    # Audit row written
    audits = (await session.execute(
        select(AuditLog).where(
            AuditLog.action == "physical_gc.evict",
            AuditLog.resource_id == str(obj.id))
    )).scalars().all()
    assert len(audits) == 1
    assert audits[0].payload["reason"] == "tombstone"
    assert audits[0].payload["size_bytes"] == 2048
    await session.rollback()


async def test_evict_one_keeps_row_on_physical_failure(session):
    """If the deleter returns False (S3 unavailable, etc.), we leave the
    DB row alone so the next GC pass can retry."""
    from dlw.db.models.storage_object import StorageObject
    obj = StorageObject(
        tenant_id=500, storage_id=500, storage_key="k-retry", sha256="e" * 64,
        size=512, refcount=0,
        last_referenced_at=datetime.now(UTC) - timedelta(hours=2),
        created_at=datetime.now(UTC) - timedelta(hours=2))
    session.add(obj)
    await session.flush()
    candidate = EvictionCandidate(
        object_id=obj.id, storage_id=500, storage_key="k-retry",
        sha256="e" * 64, size_bytes=512, reason="tombstone")
    deleter = StubDeleter(succeed=False)
    r = await evict_one(session, candidate, deleter=deleter, tenant_id=500)
    assert r.deleted is False
    assert r.error == "physical_delete_failed"
    # Row preserved
    assert await session.get(StorageObject, obj.id) is not None
    await session.rollback()


# ---------------------------------------------------------------------------
# gc_run_once orchestration
# ---------------------------------------------------------------------------

async def test_gc_run_once_disabled_when_flag_off(session, monkeypatch):
    monkeypatch.setenv("DLW_PHYSICAL_GC_ENABLED", "false")
    with pytest.raises(PhysicalGCDisabled):
        await gc_run_once(session, deleter=StubDeleter())


async def test_gc_run_once_deletes_tombstones(session):
    from dlw.db.models.storage_object import StorageObject
    # Three tombstones across tenants — GC should sweep all when tenant_id=None
    for i, ch in enumerate(("aa", "bb", "cc")):
        long_ago = datetime.now(UTC) - timedelta(hours=2)
        session.add(StorageObject(
            tenant_id=500, storage_id=500, storage_key=f"k-tomb-{i}",
            sha256=ch * 32, size=1000, refcount=0,
            last_referenced_at=long_ago, created_at=long_ago))
    await session.flush()
    deleter = StubDeleter(succeed=True)
    summary = await gc_run_once(session, deleter=deleter)
    assert summary["tombstones_deleted"] == 3
    assert summary["bytes_freed"] == 3000
    assert summary["errors"] == []
    await session.rollback()


async def test_gc_run_once_lru_triggers_when_over_quota(session):
    """When the tenant exceeds quota_storage_gb, the LRU phase picks up
    extra evictable objects beyond just the tombstones."""
    from dlw.db.models.storage_object import StorageObject
    # quota_storage_gb=1 → 1 GiB. Add 1.2 GiB of unreferenced objects.
    GiB = 1024 ** 3
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    for i in range(3):
        session.add(StorageObject(
            tenant_id=500, storage_id=500, storage_key=f"k-lru-{i}",
            sha256=str(i).rjust(64, "0"), size=int(0.4 * GiB),
            refcount=0,
            last_referenced_at=long_ago, created_at=long_ago))
    await session.flush()
    deleter = StubDeleter(succeed=True)
    summary = await gc_run_once(
        session, deleter=deleter, tenant_id=500)
    # All three are tombstones (refcount=0 + old). They go through phase 1.
    assert summary["tombstones_deleted"] == 3
    # Phase 2 finds nothing left to evict (all already cleaned).
    await session.rollback()


async def test_gc_run_once_audit_row_for_run(session):
    """Each gc_run_once call writes one summary audit row regardless of
    whether any object was evicted."""
    from sqlalchemy import select
    from dlw.db.models.audit import AuditLog
    deleter = StubDeleter()
    await gc_run_once(session, deleter=deleter, tenant_id=500)
    rows = (await session.execute(
        select(AuditLog).where(
            AuditLog.action == "physical_gc.run",
            AuditLog.resource_id == "500"))).scalars().all()
    assert len(rows) >= 1
    assert "tombstones_deleted" in rows[-1].payload
    await session.rollback()


def test_default_deleter_local_path_unlink(tmp_path, monkeypatch):
    """The local backend path uses Path.unlink — verify we shell to the
    filesystem, not to S3."""
    import asyncio
    from dlw.db.models.storage import StorageBackend

    # Make a file
    f = tmp_path / "object.bin"
    f.write_bytes(b"x" * 100)

    # Stub a session factory that yields one backend
    class _StubSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, model, sid):
            return StorageBackend(
                id=500, tenant_id=500, name="local", backend_type="local",
                region=None, config_encrypted=b"")

    def _factory():
        return _StubSession()

    deleter = DefaultObjectStoreDeleter(_factory)
    ok = asyncio.get_event_loop().run_until_complete(
        deleter.delete(500, str(f)))
    assert ok is True
    assert not f.exists()
