"""Tests for claim_one_subtask host-affinity + status eligibility (W2a §3.3)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
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
    """Seed minimum FK rows (tenant/project/user/storage) for this module's tests."""
    db_session.add(Tenant(id=1, slug="d", display_name="D"))
    await db_session.flush()
    db_session.add(Project(id=1, tenant_id=1, name="d"))
    db_session.add(User(id=1, tenant_id=1, oidc_subject="d",
                        email="d@l", role="tenant_admin"))
    db_session.add(StorageBackend(id=1, tenant_id=1, name="d",
                                  backend_type="s3", config_encrypted=b""))
    await db_session.flush()


async def _seed_pending_subtask(session: AsyncSession) -> tuple[DownloadTask, FileSubTask]:
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="pending",
    )
    session.add(task)
    await session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="model.bin",
        file_size=100, status="pending",
    )
    session.add(sub)
    await session.flush()
    return task, sub


@pytest.mark.slow
async def test_claim_skips_when_self_status_faulty(db_session: AsyncSession, env) -> None:
    db_session.add(Executor(
        id="ex-faulty", host_id="host-1", cert_fingerprint="x",
        status="faulty", epoch=1,
    ))
    await _seed_pending_subtask(db_session)

    sub, token = await claim_one_subtask(db_session, "ex-faulty", 1)
    assert sub is None and token is None


@pytest.mark.slow
async def test_claim_skips_when_same_host_other_executor_holds_file(
    db_session: AsyncSession, env,
) -> None:
    """D-10 reverse: ex-A2 on host-1 cannot claim sibling subtask while ex-A1
    on host-1 already holds an 'assigned' subtask of the same (task_id, filename).
    Bypass UniqueConstraint(task_id, filename) inside this test transaction by
    dropping the constraint; rollback restores it."""
    await db_session.execute(text(
        "ALTER TABLE file_subtasks DROP CONSTRAINT uq_file_subtasks_task_id"
    ))

    db_session.add(Executor(
        id="ex-A1", host_id="host-1", cert_fingerprint="x",
        status="healthy", epoch=1,
        disk_free_gb=100, parts_dir_bytes=0,
    ))
    db_session.add(Executor(
        id="ex-A2", host_id="host-1", cert_fingerprint="x",
        status="healthy", epoch=1,
        disk_free_gb=100, parts_dir_bytes=0,
    ))
    task, _existing = await _seed_pending_subtask(db_session)
    # Mark the existing subtask as assigned to ex-A1.
    _existing.status = "assigned"
    _existing.executor_id = "ex-A1"
    _existing.executor_epoch = 1
    _existing.assignment_token = uuid.uuid4()
    # Insert a sibling pending subtask with the same task_id + filename (constraint dropped).
    sibling = FileSubTask(
        task_id=task.id, tenant_id=1, filename="model.bin",
        file_size=100, status="pending",
    )
    db_session.add(sibling)
    await db_session.flush()

    # ex-A2 on host-1 must not claim it.
    sub, token = await claim_one_subtask(db_session, "ex-A2", 1)
    assert sub is None and token is None

    # An executor on a different host can claim it.
    db_session.add(Executor(
        id="ex-B1", host_id="host-2", cert_fingerprint="x",
        status="healthy", epoch=1,
        disk_free_gb=100, parts_dir_bytes=0,
    ))
    await db_session.flush()
    sub, token = await claim_one_subtask(db_session, "ex-B1", 1)
    assert sub is not None
    assert sub.executor_id == "ex-B1"
