"""Tests for sweep_paused_external (Phase 2 W2b2 §3.4)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.services.recovery import sweep_paused_external


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
async def test_sweep_recovers_paused_external_after_quiet_window(
    db_session: AsyncSession, env,
) -> None:
    """paused_external sub with last_paused_at=now-400s + active parent → recovered to pending."""
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="downloading",
    )
    db_session.add(task)
    await db_session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="m.bin",
        file_size=100, status="paused_external",
        last_paused_at=datetime.now(UTC) - timedelta(seconds=400),
        executor_id=None, executor_epoch=None,
    )
    db_session.add(sub)
    await db_session.flush()

    recovered = await sweep_paused_external(db_session)
    assert recovered == 1
    fresh = await db_session.get(FileSubTask, sub.id)
    assert fresh.status == "pending"


@pytest.mark.slow
async def test_sweep_skips_paused_external_under_cancelling_parent(
    db_session: AsyncSession, env,
) -> None:
    """paused_external sub whose parent is cancelling → NOT recovered."""
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="cancelling",
    )
    db_session.add(task)
    await db_session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="m.bin",
        file_size=100, status="paused_external",
        last_paused_at=datetime.now(UTC) - timedelta(seconds=400),
    )
    db_session.add(sub)
    await db_session.flush()

    recovered = await sweep_paused_external(db_session)
    assert recovered == 0
    fresh = await db_session.get(FileSubTask, sub.id)
    assert fresh.status == "paused_external"   # untouched
