"""Non-HF completion verified vs HF expected_sha256 → blacklist on mismatch."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.source import SourceBlacklist
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.scheduler import complete_subtask

pytestmark = pytest.mark.slow


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


async def test_non_hf_sha_mismatch_blacklists(factory):
    async with factory() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                          repo_id="o/r", revision="abc", storage_id=1,
                          path_template="t", status="downloading")
        s.add(t)
        await s.flush()
        tok = uuid.uuid4()
        sub = FileSubTask(task_id=t.id, tenant_id=1, filename="m",
                          file_size=4, expected_sha256="c" * 64,
                          status="assigned", assignment_token=tok,
                          source_id="modelscope")
        s.add(sub)
        await s.flush()
        sid = sub.id
        done, _ = await complete_subtask(
            s, sid, final_status="succeeded", actual_sha256="d" * 64,
            bytes_downloaded=4, error=None, assignment_token=tok)
        await s.commit()
        assert done.status == "failed"
        bl = (await s.execute(select(SourceBlacklist).where(
            SourceBlacklist.source_id == "modelscope"))).scalars().all()
        assert len(bl) == 1 and bl[0].filename == "m"


async def test_hf_source_mismatch_not_blacklisted(factory):
    """A mismatch on the huggingface source itself must NOT blacklist HF."""
    async with factory() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                          repo_id="o/r", revision="abc", storage_id=1,
                          path_template="t", status="downloading")
        s.add(t)
        await s.flush()
        tok = uuid.uuid4()
        sub = FileSubTask(task_id=t.id, tenant_id=1, filename="m",
                          file_size=4, expected_sha256="c" * 64,
                          status="assigned", assignment_token=tok,
                          source_id="huggingface")
        s.add(sub)
        await s.flush()
        sid = sub.id
        await complete_subtask(
            s, sid, final_status="succeeded", actual_sha256="d" * 64,
            bytes_downloaded=4, error=None, assignment_token=tok)
        await s.commit()
        bl = (await s.execute(select(SourceBlacklist))).scalars().all()
        assert bl == []
