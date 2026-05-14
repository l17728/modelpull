"""Tests for complete_subtask cancel-aware tail + paused branches (Phase 2 W2b2 §3.3)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.services.scheduler import complete_subtask


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
async def test_succeeded_under_cancelling_keeps_file_and_transitions_task(
    db_session: AsyncSession, env,
) -> None:
    """Task in cancelling + last sub completes succeeded → task transitions
    cancelled (not succeeded); sub stays succeeded (file preserved)."""
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
    token = uuid.uuid4()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="m.bin",
        file_size=100, status="assigned",
        executor_id="ex-1", executor_epoch=1,
        assignment_token=token,
    )
    db_session.add(sub)
    await db_session.flush()

    updated_sub, updated_task = await complete_subtask(
        db_session, sub.id,
        final_status="succeeded",
        actual_sha256=None,
        bytes_downloaded=100,
        error=None,
        assignment_token=token,
        executor_epoch=1,
    )
    await db_session.flush()

    assert updated_sub.status == "succeeded"
    assert updated_task.status == "cancelled"
    assert updated_task.completed_at is not None


@pytest.mark.slow
async def test_paused_external_short_circuits(
    db_session: AsyncSession, env,
) -> None:
    """complete_subtask(final_status='paused_external') with non-cancelling
    parent → sub becomes paused_external, last_paused_at written, no
    retry_count bump, no task status change."""
    db_session.add(Executor(
        id="ex-pe", host_id="host-pe", cert_fingerprint="x",
        status="healthy", epoch=1, disk_free_gb=100, parts_dir_bytes=0,
    ))
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="downloading",
    )
    db_session.add(task)
    await db_session.flush()
    token = uuid.uuid4()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="m.bin",
        file_size=100, status="assigned",
        executor_id="ex-pe", executor_epoch=1,
        assignment_token=token,
        retry_count=2,
    )
    db_session.add(sub)
    await db_session.flush()

    updated_sub, updated_task = await complete_subtask(
        db_session, sub.id,
        final_status="paused_external",
        actual_sha256=None,
        bytes_downloaded=0,
        error="HTTP 429",
        assignment_token=token,
        executor_epoch=1,
    )
    await db_session.flush()

    assert updated_sub.status == "paused_external"
    assert updated_sub.last_paused_at is not None
    assert updated_sub.executor_id is None
    assert updated_sub.last_error == "HTTP 429"
    assert updated_sub.retry_count == 2          # unchanged
    assert updated_task.status == "downloading"  # unchanged


@pytest.mark.slow
async def test_paused_external_under_cancelling_force_terminates_to_cancelled(
    db_session: AsyncSession, env,
) -> None:
    """paused_external arriving after /cancel → sub becomes cancelled
    (not paused_external); sibling-terminal tail transitions task to cancelled."""
    db_session.add(Executor(
        id="ex-pe2", host_id="host-pe2", cert_fingerprint="x",
        status="healthy", epoch=1, disk_free_gb=100, parts_dir_bytes=0,
    ))
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="cancelling",
    )
    db_session.add(task)
    await db_session.flush()
    token = uuid.uuid4()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="m.bin",
        file_size=100, status="assigned",
        executor_id="ex-pe2", executor_epoch=1,
        assignment_token=token,
    )
    db_session.add(sub)
    await db_session.flush()

    updated_sub, updated_task = await complete_subtask(
        db_session, sub.id,
        final_status="paused_external",
        actual_sha256=None,
        bytes_downloaded=0,
        error="HTTP 503",
        assignment_token=token,
        executor_epoch=1,
    )
    await db_session.flush()

    assert updated_sub.status == "cancelled"     # force-terminated, not paused
    assert updated_sub.last_paused_at is None    # not set on force-terminate path
    assert updated_task.status == "cancelled"
