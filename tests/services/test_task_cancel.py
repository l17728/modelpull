"""Tests for cancel_task (Phase 2 W2b2 §3.1)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.services.task_service import cancel_task


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def env(db_session: AsyncSession):
    """Seed minimum FK rows."""
    db_session.add(Tenant(id=1, slug="d", display_name="D"))
    await db_session.flush()
    db_session.add(Project(id=1, tenant_id=1, name="d"))
    db_session.add(User(id=1, tenant_id=1, oidc_subject="d",
                        email="d@l", role="tenant_admin"))
    db_session.add(StorageBackend(id=1, tenant_id=1, name="d",
                                  backend_type="s3", config_encrypted=b""))
    await db_session.flush()


async def _seed_task(session: AsyncSession, status: str = "pending") -> DownloadTask:
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status=status,
    )
    session.add(task)
    await session.flush()
    return task


@pytest.mark.slow
async def test_cancel_task_flips_status_and_cancelled_at(
    db_session: AsyncSession, env,
) -> None:
    task = await _seed_task(db_session, status="pending")

    cancelled = await cancel_task(db_session, task.id)
    await db_session.flush()

    assert cancelled.status == "cancelling"
    assert cancelled.cancelled_at is not None


@pytest.mark.slow
async def test_cancel_task_force_terminates_paused_subtasks(
    db_session: AsyncSession, env,
) -> None:
    task = await _seed_task(db_session, status="downloading")
    db_session.add(FileSubTask(
        task_id=task.id, tenant_id=1, filename="a.bin",
        file_size=100, status="paused_disk_full",
    ))
    db_session.add(FileSubTask(
        task_id=task.id, tenant_id=1, filename="b.bin",
        file_size=200, status="paused_external",
    ))
    db_session.add(FileSubTask(
        task_id=task.id, tenant_id=1, filename="c.bin",
        file_size=300, status="assigned",
    ))
    await db_session.flush()

    await cancel_task(db_session, task.id)
    await db_session.flush()

    from sqlalchemy import select
    rows = (await db_session.execute(
        select(FileSubTask).where(FileSubTask.task_id == task.id)
        .order_by(FileSubTask.filename)
    )).scalars().all()
    statuses = {r.filename: r.status for r in rows}
    assert statuses == {
        "a.bin": "cancelled",   # was paused_disk_full
        "b.bin": "cancelled",   # was paused_external
        "c.bin": "assigned",    # in-flight; untouched
    }


@pytest.mark.slow
async def test_cancel_task_idempotent_when_already_cancelling(
    db_session: AsyncSession, env,
) -> None:
    task = await _seed_task(db_session, status="pending")
    await cancel_task(db_session, task.id)
    # Second call is idempotent (no error, returns same row).
    again = await cancel_task(db_session, task.id)
    assert again.status == "cancelling"


@pytest.mark.slow
async def test_cancel_task_raises_on_terminal_state(
    db_session: AsyncSession, env,
) -> None:
    for terminal in ("succeeded", "failed", "cancelled"):
        task = await _seed_task(db_session, status=terminal)
        with pytest.raises(ValueError):
            await cancel_task(db_session, task.id)
