from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def fixtures(db_session: AsyncSession):
    """Reusable: tenant + project + user + storage_backend."""
    tenant = Tenant(slug=f"t-{uuid.uuid4().hex[:6]}", display_name="T")
    db_session.add(tenant)
    await db_session.flush()
    project = Project(tenant_id=tenant.id, name="research")
    db_session.add(project)
    user = User(
        tenant_id=tenant.id,
        oidc_subject=f"oidc-{uuid.uuid4()}",
        email="x@y.com",
        role="tenant_admin",
    )
    db_session.add(user)
    sb = StorageBackend(
        tenant_id=tenant.id, name="s3-prod", backend_type="s3", config_encrypted=b""
    )
    db_session.add(sb)
    await db_session.flush()
    return tenant, project, user, sb


@pytest.mark.slow
async def test_create_task(db_session: AsyncSession, fixtures) -> None:
    tenant, project, user, sb = fixtures
    task = DownloadTask(
        tenant_id=tenant.id,
        project_id=project.id,
        owner_user_id=user.id,
        repo_id="deepseek-ai/DeepSeek-V3",
        revision="abc123def4567890abc123def4567890abc12345",
        storage_id=sb.id,
        path_template="{tenant}/{repo_id}/{revision}",
        priority=2,
        status="pending",
    )
    db_session.add(task)
    await db_session.commit()
    assert task.id is not None
    assert task.is_simulation is False
    assert task.created_at is not None


@pytest.mark.slow
async def test_subtask_belongs_to_task(db_session: AsyncSession, fixtures) -> None:
    tenant, project, user, sb = fixtures
    task = DownloadTask(
        tenant_id=tenant.id,
        project_id=project.id,
        owner_user_id=user.id,
        repo_id="org/repo",
        revision="0" * 40,
        storage_id=sb.id,
        path_template="{tenant}/{repo_id}",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    subtask = FileSubTask(
        task_id=task.id,
        tenant_id=tenant.id,
        filename="model.safetensors",
        file_size=1_000_000,
        expected_sha256="abcd" * 16,
        status="pending",
    )
    db_session.add(subtask)
    await db_session.commit()
    assert subtask.id is not None
    assert subtask.chunks_completed == 0


@pytest.mark.slow
async def test_subtask_unique_filename_per_task(db_session: AsyncSession, fixtures) -> None:
    tenant, project, user, sb = fixtures
    task = DownloadTask(
        tenant_id=tenant.id, project_id=project.id, owner_user_id=user.id,
        repo_id="o/r", revision="0" * 40, storage_id=sb.id,
        path_template="{tenant}", status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    db_session.add(FileSubTask(
        task_id=task.id, tenant_id=tenant.id, filename="dup.bin", status="pending",
    ))
    await db_session.commit()
    db_session.add(FileSubTask(
        task_id=task.id, tenant_id=tenant.id, filename="dup.bin", status="pending",
    ))
    with pytest.raises(IntegrityError):
        await db_session.commit()
