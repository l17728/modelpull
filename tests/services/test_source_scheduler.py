"""plan_task_sources: resolve→assign→persist + HF-authority gate (SP2)."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.source import SubtaskChunk
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.source_scheduler import plan_task_sources
from dlw.sources.base import SourceFile, SourceManifest

pytestmark = pytest.mark.slow


class _FakeDriver:
    def __init__(self, sid, files, sha):
        self.id = sid
        self.provides_sha256 = sha
        self._files = files

    async def resolve(self, repo_id, revision):
        return SourceManifest(self.id, repo_id, revision, self._files,
                              has_lfs_sha256=any(
                                  f.sha256 for f in self._files))


class _FakeReg:
    def __init__(self, drivers):
        self._d = drivers

    def enabled_ids(self):
        return list(self._d)

    def get(self, sid):
        return self._d.get(sid)


class _IdResolver:
    def resolve(self, source_id, hf_repo_id):
        return hf_repo_id


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


def _files():
    return [SourceFile("model.safetensors", 200 * 1024 * 1024, "a" * 64,
                       "ref"),
            SourceFile("config.json", 10, None, "ref2")]


async def test_plan_assigns_and_persists(factory):
    async with factory() as s:
        task = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                            repo_id="o/r", revision="abc", storage_id=1,
                            path_template="t", status="scheduling")
        s.add(task)
        await s.flush()
        for f in _files():
            s.add(FileSubTask(task_id=task.id, tenant_id=1, filename=f.filename,
                              file_size=f.size, expected_sha256=f.sha256,
                              status="pending"))
        await s.commit()
        reg = _FakeReg({"huggingface": _FakeDriver("huggingface", _files(),
                                                   True),
                        "modelscope": _FakeDriver("modelscope", _files(),
                                                  False)})
        await plan_task_sources(
            s, task, registry=reg, resolver=_IdResolver(),
            speeds={"huggingface": 50.0, "modelscope": 900.0},
            chunk_min_mb=100)
        await s.commit()
        subs = (await s.execute(select(FileSubTask).where(
            FileSubTask.task_id == task.id))).scalars().all()
        assert all(x.source_id in {"huggingface", "modelscope"} for x in subs)
        big = next(x for x in subs if x.filename == "model.safetensors")
        assert big.is_chunked is True
        chunks = (await s.execute(select(SubtaskChunk).where(
            SubtaskChunk.subtask_id == big.id))).scalars().all()
        assert len(chunks) >= 2


async def test_hf_absent_pauses_when_not_trusted(factory):
    async with factory() as s:
        task = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                            repo_id="o/r", revision="abc", storage_id=1,
                            path_template="t", status="scheduling",
                            trust_non_hf_sha256=False)
        s.add(task)
        await s.flush()
        s.add(FileSubTask(task_id=task.id, tenant_id=1, filename="c.json",
                          file_size=10, expected_sha256=None,
                          status="pending"))
        await s.commit()
        reg = _FakeReg({"modelscope": _FakeDriver("modelscope", _files(),
                                                  False)})
        await plan_task_sources(s, task, registry=reg, resolver=_IdResolver(),
                                speeds={"modelscope": 900.0}, chunk_min_mb=100)
        await s.commit()
        assert task.status == "paused_external"
        assert task.error_message == "no_sha256_authority"


async def test_no_sha_file_pinned_to_huggingface(factory):
    """INVARIANT 12 (spec ruling 6a): a file with expected_sha256=None must
    stay on huggingface even when a faster non-HF source covers it."""
    async with factory() as s:
        task = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                            repo_id="o/r", revision="abc", storage_id=1,
                            path_template="t", status="scheduling")
        s.add(task)
        await s.flush()
        s.add(FileSubTask(task_id=task.id, tenant_id=1,
                          filename="config.json", file_size=10,
                          expected_sha256=None, status="pending"))
        await s.commit()
        reg = _FakeReg({"huggingface": _FakeDriver("huggingface", _files(),
                                                   True),
                        "modelscope": _FakeDriver("modelscope", _files(),
                                                  False)})
        await plan_task_sources(s, task, registry=reg, resolver=_IdResolver(),
                                speeds={"huggingface": 1.0,
                                        "modelscope": 9000.0},
                                chunk_min_mb=100)
        await s.commit()
        sub = (await s.execute(select(FileSubTask).where(
            FileSubTask.task_id == task.id))).scalar_one()
        assert sub.source_id == "huggingface" and sub.is_chunked is False


async def test_pin_modelscope_unreachable_pauses(factory):
    async with factory() as s:
        task = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                            repo_id="o/r", revision="abc", storage_id=1,
                            path_template="t", status="scheduling",
                            source_strategy="pin_modelscope")
        s.add(task)
        await s.flush()
        s.add(FileSubTask(task_id=task.id, tenant_id=1, filename="m",
                          file_size=10, expected_sha256="a" * 64,
                          status="pending"))
        await s.commit()
        reg = _FakeReg({"huggingface": _FakeDriver("huggingface", _files(),
                                                   True)})
        await plan_task_sources(s, task, registry=reg, resolver=_IdResolver(),
                                speeds={"huggingface": 50.0}, chunk_min_mb=100)
        await s.commit()
        assert task.status == "paused_external"
        assert task.error_message == "pinned_source_unavailable"


async def test_run_scheduling_tick_inherits_before_planning(factory):
    """diff_and_dedup runs before plan_task_sources: a subtask whose sha has
    an existing storage_object is `inherit` (not source-planned)."""
    from dlw.config import get_settings
    from dlw.db.models.storage_object import StorageObject
    from dlw.services.source_scheduler import run_scheduling_tick
    async with factory() as s:
        s.add(StorageObject(tenant_id=1, storage_id=1, storage_key="old/k",
                            sha256="a" * 64, size=10))
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="new", storage_id=1,
                         path_template="t", status="pending")
        s.add(t)
        await s.flush()
        s.add(FileSubTask(task_id=t.id, tenant_id=1, filename="w.bin",
                          file_size=10, expected_sha256="a" * 64,
                          status="pending"))
        await s.commit()
        reg = _FakeReg({"huggingface": _FakeDriver("huggingface", [], True)})
        await run_scheduling_tick(s, reg, _IdResolver(), get_settings())
        await s.commit()
        sub = (await s.execute(select(FileSubTask))).scalar_one()
        assert sub.status == "inherit"
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert obj.refcount == 2
        t2 = (await s.execute(select(DownloadTask))).scalar_one()
        # BLOCKER fix (banner 7a): a fully-inherited task must NOT be paused
        # by plan_task_sources' pinned/no_sha/no_speed gates.
        assert t2.status != "paused_external"
        assert t2.status == "downloading"
