"""Tests for sweep_paused_disk_full (Phase 2 W2b1 §3.7)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.services.recovery import sweep_paused_disk_full


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def env(db_session: AsyncSession):
    """Seed minimum FK rows. Mirror of test_scheduler_host_affinity.env."""
    db_session.add(Tenant(id=1, slug="d", display_name="D"))
    await db_session.flush()
    db_session.add(Project(id=1, tenant_id=1, name="d"))
    db_session.add(User(id=1, tenant_id=1, oidc_subject="d",
                        email="d@l", role="tenant_admin"))
    db_session.add(StorageBackend(id=1, tenant_id=1, name="d",
                                  backend_type="s3", config_encrypted=b""))
    await db_session.flush()


@pytest.mark.slow
async def test_sweep_recovers_paused_disk_full_when_disk_free_increases(
    db_session: AsyncSession, env,
) -> None:
    """Seed paused_disk_full subtask; bump disk_free_gb; run sweep; assert pending."""
    ex = Executor(
        id="ex-recovered", host_id="host-r", cert_fingerprint="x",
        status="healthy", epoch=1,
        disk_free_gb=0, parts_dir_bytes=0,
    )
    db_session.add(ex)
    await db_session.flush()

    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="pending",
    )
    db_session.add(task)
    await db_session.flush()

    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="big.bin",
        file_size=100 * 1024 * 1024, status="paused_disk_full",
        executor_id="ex-recovered", executor_epoch=1,
    )
    db_session.add(sub)
    await db_session.flush()

    # Initial sweep — disk still tight, no recovery.
    recovered = await sweep_paused_disk_full(db_session)
    assert recovered == 0
    fresh = await db_session.get(FileSubTask, sub.id)
    assert fresh.status == "paused_disk_full"

    # Bump disk_free_gb; sweep again.
    ex.disk_free_gb = 10
    await db_session.flush()
    recovered = await sweep_paused_disk_full(db_session)
    assert recovered == 1
    fresh = await db_session.get(FileSubTask, sub.id)
    assert fresh.status == "pending"
    assert fresh.executor_id is None
    assert fresh.executor_epoch is None
