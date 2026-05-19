"""diff_and_dedup: existing object → inherit; else pending (SP3)."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.storage_object import StorageObject
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.incremental import diff_and_dedup

pytestmark = pytest.mark.slow


@pytest.fixture
async def f(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    fac = async_sessionmaker(engine, expire_on_commit=False)
    async with fac() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="e",
                        role="tenant_operator"),
                   StorageBackend(id=1, tenant_id=1, name="s",
                                  backend_type="s3", config_encrypted=b"")])
        await s.commit()
    yield fac
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


async def _mk_task(s, **kw):
    t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                     repo_id="o/r", revision="new", storage_id=1,
                     path_template="t", status="scheduling", **kw)
    s.add(t)
    await s.flush()
    return t


async def test_existing_object_becomes_inherit(f):
    async with f() as s:
        s.add(StorageObject(tenant_id=1, storage_id=1, storage_key="old/key",
                            sha256="a" * 64, size=10))
        t = await _mk_task(s)
        s.add(FileSubTask(task_id=t.id, tenant_id=1, filename="w.bin",
                          file_size=10, expected_sha256="a" * 64,
                          status="pending"))
        await s.commit()
        await diff_and_dedup(s, t)
        await s.commit()
        sub = (await s.execute(select(FileSubTask))).scalar_one()
        assert sub.status == "inherit"
        assert sub.inherit_from_key == "old/key"
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert obj.refcount == 2


async def test_no_object_stays_pending(f):
    async with f() as s:
        t = await _mk_task(s)
        s.add(FileSubTask(task_id=t.id, tenant_id=1, filename="w.bin",
                          file_size=10, expected_sha256="z" * 64,
                          status="pending"))
        await s.commit()
        await diff_and_dedup(s, t)
        await s.commit()
        sub = (await s.execute(select(FileSubTask))).scalar_one()
        assert sub.status == "pending" and sub.inherit_from_key is None


async def test_null_sha_never_inherited(f):
    async with f() as s:
        s.add(StorageObject(tenant_id=1, storage_id=1, storage_key="k",
                            sha256="a" * 64, size=10))
        t = await _mk_task(s)
        s.add(FileSubTask(task_id=t.id, tenant_id=1, filename="c.json",
                          file_size=4, expected_sha256=None,
                          status="pending"))
        await s.commit()
        await diff_and_dedup(s, t)
        await s.commit()
        sub = (await s.execute(select(FileSubTask))).scalar_one()
        assert sub.status == "pending"


async def test_cross_task_dedup(f):
    """Unified dedup: an object from an unrelated prior task is reused."""
    async with f() as s:
        s.add(StorageObject(tenant_id=1, storage_id=1, storage_key="prior/k",
                            sha256="d" * 64, size=99))
        t = await _mk_task(s, upgrade_from_revision=None)
        s.add(FileSubTask(task_id=t.id, tenant_id=1, filename="m.safetensors",
                          file_size=99, expected_sha256="d" * 64,
                          status="pending"))
        await s.commit()
        await diff_and_dedup(s, t)
        await s.commit()
        sub = (await s.execute(select(FileSubTask))).scalar_one()
        assert sub.status == "inherit" and sub.inherit_from_key == "prior/k"
