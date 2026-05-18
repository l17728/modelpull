"""E2E-002: auto_balance planning + HF-authority pause (Phase 3 SP2).

End-to-end at the planner+DB level (no live mirrors): a task with HF + a
faster ModelScope-style fake source gets files assigned to the faster
source, and an HF-absent task without trust pauses (INVARIANT 13)."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.source_scheduler import plan_task_sources
from dlw.sources.base import SourceFile, SourceManifest

pytestmark = pytest.mark.slow


class _Drv:
    def __init__(self, sid, files):
        self.id = sid
        self.provides_sha256 = sid in ("huggingface", "hf_mirror")
        self._f = files

    async def resolve(self, repo, rev):
        return SourceManifest(self.id, repo, rev, self._f,
                              has_lfs_sha256=any(f.sha256 for f in self._f))


class _Reg:
    def __init__(self, d):
        self._d = d

    def enabled_ids(self):
        return list(self._d)

    def get(self, s):
        return self._d.get(s)


class _Id:
    def resolve(self, sid, repo):
        return repo


@pytest.fixture
async def factory(engine):
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with f() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="e",
                        role="tenant_operator"),
                   StorageBackend(id=1, tenant_id=1, name="s",
                                  backend_type="s3", config_encrypted=b"")])
        await s.commit()
    yield f
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


async def test_auto_balance_prefers_fast_source(factory):
    files = [SourceFile("a.safetensors", 50, "a" * 64, "r"),
             SourceFile("b.safetensors", 50, "b" * 64, "r")]
    reg = _Reg({"huggingface": _Drv("huggingface", files),
                "modelscope": _Drv("modelscope", files)})
    async with factory() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="abc", storage_id=1,
                         path_template="t", status="scheduling")
        s.add(t)
        await s.flush()
        for f in files:
            s.add(FileSubTask(task_id=t.id, tenant_id=1, filename=f.filename,
                              file_size=f.size, expected_sha256=f.sha256,
                              status="pending"))
        await s.commit()
        await plan_task_sources(s, t, registry=reg, resolver=_Id(),
                                speeds={"huggingface": 50.0,
                                        "modelscope": 5000.0},
                                chunk_min_mb=100)
        await s.commit()
        subs = (await s.execute(select(FileSubTask).where(
            FileSubTask.task_id == t.id))).scalars().all()
        assert all(x.source_id == "modelscope" for x in subs)  # HF too slow


async def test_hf_unavailable_pauses(factory):
    files = [SourceFile("a", 10, None, "r")]
    reg = _Reg({"modelscope": _Drv("modelscope", files)})
    async with factory() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="abc", storage_id=1,
                         path_template="t", status="scheduling",
                         trust_non_hf_sha256=False)
        s.add(t)
        await s.flush()
        s.add(FileSubTask(task_id=t.id, tenant_id=1, filename="a",
                          file_size=10, status="pending"))
        await s.commit()
        await plan_task_sources(s, t, registry=reg, resolver=_Id(),
                                speeds={"modelscope": 900.0}, chunk_min_mb=100)
        await s.commit()
        assert t.status == "paused_external"
        assert t.error_message == "no_sha256_authority"
