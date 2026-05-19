"""E2E-incremental: an upgrade with 1 changed file inherits the rest (SP3).

Planner-level end-to-end (no live mirrors): seed storage_objects as if
revision `old` completed; create the `new` task (upgrade_from_revision=old)
where all files but one keep their sha; assert ≥90% become inherit."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select
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


async def test_upgrade_inherits_at_least_90pct(f):
    N = 20
    async with f() as s:
        # revision `old` already produced N storage_objects (sha = f"{i}"*64)
        for i in range(N):
            s.add(StorageObject(tenant_id=1, storage_id=1,
                                storage_key=f"o/r/old/file{i}",
                                sha256=f"{i:02d}".ljust(64, "a"), size=100))
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="new", storage_id=1,
                         path_template="t", status="scheduling",
                         upgrade_from_revision="old")
        s.add(t)
        await s.flush()
        # new revision: file0 changed (new sha), file1..N-1 identical
        for i in range(N):
            sha = ("ff".ljust(64, "f") if i == 0
                   else f"{i:02d}".ljust(64, "a"))
            s.add(FileSubTask(task_id=t.id, tenant_id=1, filename=f"file{i}",
                              file_size=100, expected_sha256=sha,
                              status="pending"))
        await s.commit()
        await diff_and_dedup(s, t)
        await s.commit()
        inherited = await s.scalar(select(func.count()).select_from(
            FileSubTask).where(FileSubTask.task_id == t.id,
                               FileSubTask.status == "inherit"))
        pending = await s.scalar(select(func.count()).select_from(
            FileSubTask).where(FileSubTask.task_id == t.id,
                               FileSubTask.status == "pending"))
        assert inherited == N - 1 and pending == 1
        assert inherited / N >= 0.90       # roadmap §3.5 exit
