"""storage_objects: upsert/ref/refcount + inherit idempotency (SP3)."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.storage_object import StorageObject, SubtaskObjectRef
from dlw.services.storage_objects import record_object, record_ref_only

pytestmark = pytest.mark.slow


@pytest.fixture
async def seeded(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.task import DownloadTask, FileSubTask
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="e",
                        role="tenant_operator"),
                   StorageBackend(id=1, tenant_id=1, name="s",
                                  backend_type="s3", config_encrypted=b"")])
        await s.flush()
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="abc", storage_id=1,
                         path_template="t", status="downloading")
        s.add(t)
        await s.flush()
        subs = []
        for i in range(3):
            sub = FileSubTask(task_id=t.id, tenant_id=1, filename=f"f{i}",
                              file_size=10, status="pending")
            s.add(sub)
            await s.flush()
            subs.append(sub.id)
        await s.commit()
    yield f, subs
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


async def test_record_object_inserts_then_refcounts(seeded):
    f, subs = seeded
    async with f() as s:
        await record_object(s, tenant_id=1, storage_id=1, storage_key="k",
                            sha256="a" * 64, size=10, subtask_id=subs[0])
        await s.commit()
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert obj.refcount == 1
    async with f() as s:
        await record_object(s, tenant_id=1, storage_id=1, storage_key="k2",
                            sha256="a" * 64, size=10, subtask_id=subs[1])
        await s.commit()
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert obj.refcount == 2
        refs = (await s.execute(select(SubtaskObjectRef))).scalars().all()
        assert len(refs) == 2


async def test_record_object_idempotent_when_ref_exists(seeded):
    """Inherit path: record_ref_only already added the ref+refcount; a later
    complete_subtask record_object MUST NOT double-count."""
    f, subs = seeded
    async with f() as s:
        obj_id = await record_ref_only(s, tenant_id=1, storage_id=1,
                                       storage_key="k", sha256="a" * 64,
                                       size=10, subtask_id=subs[0])
        await s.commit()
        o = await s.get(StorageObject, obj_id)
        assert o.refcount == 1
    async with f() as s:
        await record_object(s, tenant_id=1, storage_id=1, storage_key="k",
                            sha256="a" * 64, size=10, subtask_id=subs[0])
        await s.commit()
        o = (await s.execute(select(StorageObject))).scalar_one()
        assert o.refcount == 1            # NOT 2 — ref already existed
        refs = (await s.execute(select(SubtaskObjectRef))).scalars().all()
        assert len(refs) == 1
