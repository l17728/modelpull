"""complete_subtask records a storage_object; inherit doesn't double-count."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.storage_object import StorageObject
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.scheduler import complete_subtask

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


async def test_download_complete_records_object(f):
    async with f() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="abc", storage_id=1,
                         path_template="t", status="downloading")
        s.add(t)
        await s.flush()
        tok = uuid.uuid4()
        sub = FileSubTask(task_id=t.id, tenant_id=1, filename="m",
                          file_size=4, expected_sha256="c" * 64,
                          status="assigned", assignment_token=tok)
        s.add(sub)
        await s.flush()
        sid = sub.id
        await complete_subtask(s, sid, final_status="succeeded",
                               actual_sha256="c" * 64, bytes_downloaded=4,
                               error=None, assignment_token=tok,
                               s3_key="o/r/abc/m")
        await s.commit()
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert (obj.sha256, obj.storage_key, obj.refcount) == \
               ("c" * 64, "o/r/abc/m", 1)


async def test_inherit_complete_does_not_double_count(f):
    """An inherit sub is claimed (status -> assigned) before the executor
    calls complete_subtask; record_ref_only already added the ref at
    diff_and_dedup time, so the success-path record_object must no-op."""
    from dlw.services.storage_objects import record_ref_only
    async with f() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="new", storage_id=1,
                         path_template="t", status="downloading")
        s.add(t)
        await s.flush()
        tok = uuid.uuid4()
        sub = FileSubTask(task_id=t.id, tenant_id=1, filename="w",
                          file_size=10, expected_sha256="a" * 64,
                          status="assigned", inherit_from_key="old/k",
                          assignment_token=tok)
        s.add(sub)
        await s.flush()
        sid = sub.id
        await record_ref_only(s, tenant_id=1, storage_id=1,
                              storage_key="old/k", sha256="a" * 64,
                              size=10, subtask_id=sid)   # diff_and_dedup did this
        await s.commit()
        await complete_subtask(s, sid, final_status="succeeded",
                               actual_sha256="a" * 64, bytes_downloaded=10,
                               error=None, assignment_token=tok,
                               s3_key="o/r/new/w")
        await s.commit()
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert obj.refcount == 1          # NOT 2 — ref already existed


async def test_inherit_copy_failure_dereferences_and_repends(f):
    """banner 7f: a failed inherit copy must undo the diff-time refcount++
    and re-queue the file as pending (no permanent refcount leak)."""
    from dlw.db.models.storage_object import SubtaskObjectRef
    from dlw.services.storage_objects import record_ref_only
    async with f() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="new", storage_id=1,
                         path_template="t", status="downloading")
        s.add(t)
        await s.flush()
        tok = uuid.uuid4()
        sub = FileSubTask(task_id=t.id, tenant_id=1, filename="w",
                          file_size=10, expected_sha256="a" * 64,
                          status="assigned", inherit_from_key="old/k",
                          assignment_token=tok)
        s.add(sub)
        await s.flush()
        sid = sub.id
        await record_ref_only(s, tenant_id=1, storage_id=1,
                              storage_key="old/k", sha256="a" * 64,
                              size=10, subtask_id=sid)   # diff_and_dedup did this
        await s.commit()
        sub_done, _ = await complete_subtask(
            s, sid, final_status="failed", actual_sha256=None,
            bytes_downloaded=0, error="copy_object failed",
            assignment_token=tok)
        await s.commit()
        assert sub_done.status == "pending"          # re-queued
        assert sub_done.inherit_from_key is None
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert obj.refcount == 0                     # diff-time bump undone
        refs = (await s.execute(select(SubtaskObjectRef))).scalars().all()
        assert refs == []
