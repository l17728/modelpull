"""Tests for dlw.services.task_service.create_task."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.schemas.task import TaskCreate
from dlw.services.task_service import create_task


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def env(db_session: AsyncSession):
    """Tenant + project + user + storage fixtures (tenant_id=1 hardcoded for Week 2).

    Uses flush() not commit() so per-test rollback (db_session fixture) cleans up.
    """
    tenant = Tenant(id=1, slug="default", display_name="Default")
    db_session.add(tenant)
    await db_session.flush()
    project = Project(id=1, tenant_id=1, name="default")
    db_session.add(project)
    user = User(
        id=1, tenant_id=1, oidc_subject="dev-user", email="dev@local",
        role="tenant_admin",
    )
    db_session.add(user)
    sb = StorageBackend(
        id=1, tenant_id=1, name="default", backend_type="s3", config_encrypted=b""
    )
    db_session.add(sb)
    await db_session.flush()


@pytest.mark.slow
async def test_create_task_persists_2_subtasks(db_session: AsyncSession, env) -> None:
    body = TaskCreate(
        repo_id="deepseek-ai/DeepSeek-V3",
        revision="0123456789abcdef" * 2 + "01234567",
        storage_id=1,
    )
    task = await create_task(db_session, body, owner_user_id=1, tenant_id=1, project_id=1)
    assert task.id is not None
    assert task.status == "pending"

    subs = (await db_session.execute(
        select(FileSubTask).where(FileSubTask.task_id == task.id)
    )).scalars().all()
    assert len(subs) == 2
    filenames = sorted(s.filename for s in subs)
    assert filenames == ["config.json", "model.safetensors"]
    assert all(s.status == "pending" for s in subs)
    assert all(s.tenant_id == 1 for s in subs)


@pytest.mark.slow
async def test_create_task_status_pending(db_session: AsyncSession, env) -> None:
    body = TaskCreate(repo_id="o/r", revision="0" * 40, storage_id=1)
    task = await create_task(db_session, body, owner_user_id=1, tenant_id=1, project_id=1)
    assert task.status == "pending"
    assert task.is_simulation is False
