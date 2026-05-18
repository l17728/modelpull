"""complete_subtask emits a bytes_month usage record (Phase 3 SP1)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.scheduler import complete_subtask

pytestmark = pytest.mark.slow


@pytest.fixture
async def seeded(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([
            Project(id=1, tenant_id=1, name="d"),
            User(id=1, tenant_id=1, oidc_subject="u", email="u@t",
                 role="tenant_operator"),
            StorageBackend(id=1, tenant_id=1, name="s",
                           backend_type="s3", config_encrypted=b""),
        ])
        await s.commit()
    yield factory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def test_complete_subtask_records_usage(seeded, monkeypatch):
    calls = []

    async def spy(session, **kw):
        calls.append(kw)
    monkeypatch.setattr("dlw.services.scheduler.record_usage", spy)

    async with seeded() as s:
        task = DownloadTask(
            tenant_id=1, project_id=1, owner_user_id=1, repo_id="o/r",
            revision="a" * 40, storage_id=1, path_template="t/{tenant}",
            priority=1, status="pending")
        s.add(task)
        await s.flush()
        token = uuid.uuid4()
        sub = FileSubTask(task_id=task.id, tenant_id=1,
                          filename="config.json", file_size=4096,
                          expected_sha256=None, status="assigned",
                          assignment_token=token)
        s.add(sub)
        await s.flush()
        sub_id = sub.id

        sub_done, _ = await complete_subtask(
            s, sub_id, final_status="succeeded", actual_sha256=None,
            bytes_downloaded=4096, error=None, assignment_token=token)
        await s.commit()

    assert sub_done.status == "succeeded"
    assert any(c.get("metric") == "bytes_month" and c.get("value") == 4096
               for c in calls), calls
