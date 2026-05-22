"""Phase 4: physical-key ledger records every written key (download + inherit)."""
from __future__ import annotations

import uuid as _uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.storage_object import StorageObject, StoragePhysicalKey
from dlw.services.storage_objects import record_physical_key, record_ref_only


def _old(seconds=7200):
    return datetime.now(UTC) - timedelta(seconds=seconds)


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


async def test_record_physical_key_bumps_last_referenced_on_conflict(session):
    """On conflict, last_referenced_at advances; executor_id and size unchanged."""
    old_ts = _old(7200)
    session.add(StoragePhysicalKey(
        tenant_id=1, storage_id=1, sha256="bb" * 32,
        storage_key="repo/upsert/bump", size=7,
        executor_id="ex1", last_referenced_at=old_ts))
    await session.commit()

    # Re-record the same (tenant, storage, storage_key) with different size/executor
    await record_physical_key(session, tenant_id=1, storage_id=1,
                              sha256="bb" * 32, storage_key="repo/upsert/bump",
                              size=99, executor_id="ex2")
    await session.commit()

    row = (await session.execute(select(StoragePhysicalKey).where(
        StoragePhysicalKey.storage_key == "repo/upsert/bump"))).scalar_one()
    assert row.last_referenced_at > old_ts, "last_referenced_at should have advanced"
    assert row.executor_id == "ex1", "executor_id must NOT be overwritten on conflict"
    assert row.size == 7, "size must NOT be overwritten on conflict"


async def test_record_ref_only_touches_physical_keys_for_sha(session):
    """record_ref_only bumps last_referenced_at on all physical keys for the sha."""
    import uuid

    from dlw.db.models.task import DownloadTask, FileSubTask

    # Seed FK chain: Tenant(1) already exists; need Project(1), User(1), StorageBackend(1)
    # all seeded in _bootstrap. Create DownloadTask → FileSubTask.
    old_ts = _old(9000)
    sha = "cc" * 32

    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="org/repo", revision="rev1", storage_id=1,
        path_template="{file}", status="downloading")
    session.add(task)
    await session.flush()

    subtask = FileSubTask(
        task_id=task.id, tenant_id=1, filename="file.bin",
        status="pending")
    session.add(subtask)
    await session.flush()

    # Seed the physical key with an old last_referenced_at
    session.add(StoragePhysicalKey(
        tenant_id=1, storage_id=1, sha256=sha,
        storage_key="repo/ref/touch", size=5,
        last_referenced_at=old_ts))
    # Seed the storage object (needed by record_ref_only's _upsert_object)
    session.add(StorageObject(
        tenant_id=1, storage_id=1, storage_key="repo/ref/touch",
        sha256=sha, size=5, refcount=1))
    await session.commit()

    await record_ref_only(session, tenant_id=1, storage_id=1,
                          storage_key="repo/ref/touch", sha256=sha,
                          size=5, subtask_id=subtask.id)
    await session.commit()

    row = (await session.execute(select(StoragePhysicalKey).where(
        StoragePhysicalKey.storage_key == "repo/ref/touch"))).scalar_one()
    assert row.last_referenced_at > old_ts, "record_ref_only must bump last_referenced_at"
