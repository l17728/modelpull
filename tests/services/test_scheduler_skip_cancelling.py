"""Tests for claim_one_subtask parent-active EXISTS clause (Phase 2 W2b2 §3.3)."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.services.scheduler import claim_one_subtask


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def env(db_session: AsyncSession):
    db_session.add(Tenant(id=1, slug="d", display_name="D"))
    await db_session.flush()
    db_session.add(Project(id=1, tenant_id=1, name="d"))
    db_session.add(User(id=1, tenant_id=1, oidc_subject="d",
                        email="d@l", role="tenant_admin"))
    db_session.add(StorageBackend(id=1, tenant_id=1, name="d",
                                  backend_type="s3", config_encrypted=b""))
    await db_session.flush()


@pytest.mark.slow
async def test_claim_skips_subtask_under_cancelling_parent(
    db_session: AsyncSession, env,
) -> None:
    """Pending subtask whose parent is cancelling → claim returns None."""
    db_session.add(Executor(
        id="ex-1", host_id="host-1", cert_fingerprint="x",
        status="healthy", epoch=1, disk_free_gb=100, parts_dir_bytes=0,
    ))
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="cancelling",
    )
    db_session.add(task)
    await db_session.flush()
    db_session.add(FileSubTask(
        task_id=task.id, tenant_id=1, filename="m.bin",
        file_size=100, status="pending",
    ))
    await db_session.flush()

    sub, token = await claim_one_subtask(db_session, "ex-1", 1)
    assert sub is None and token is None
