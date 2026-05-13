"""Tests for claim_one_subtask disk pre-flight (Phase 2 W2b1 §3.6)."""
from __future__ import annotations

import asyncio
import uuid

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
    """Seed minimum FK rows (tenant/project/user/storage). Mirror of the
    inline `env` fixture in tests/services/test_scheduler_host_affinity.py."""
    db_session.add(Tenant(id=1, slug="d", display_name="D"))
    await db_session.flush()
    db_session.add(Project(id=1, tenant_id=1, name="d"))
    db_session.add(User(id=1, tenant_id=1, oidc_subject="d",
                        email="d@l", role="tenant_admin"))
    db_session.add(StorageBackend(id=1, tenant_id=1, name="d",
                                  backend_type="s3", config_encrypted=b""))
    await db_session.flush()


async def _seed_pending_with_size(
    session: AsyncSession, file_size: int, filename: str = "model.bin",
) -> tuple[DownloadTask, FileSubTask]:
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="pending",
    )
    session.add(task)
    await session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename=filename,
        file_size=file_size, status="pending",
    )
    session.add(sub)
    await session.flush()
    return task, sub


@pytest.mark.slow
async def test_claim_skips_subtask_too_big_for_executor(
    db_session: AsyncSession, env,
) -> None:
    """Executor has 1 GB free; subtask is 5 GB → no claim."""
    db_session.add(Executor(
        id="ex-tiny", host_id="host-1", cert_fingerprint="x",
        status="healthy", epoch=1,
        disk_free_gb=1, parts_dir_bytes=0,
    ))
    await _seed_pending_with_size(db_session, file_size=5 * 1024**3)

    sub, token = await claim_one_subtask(db_session, "ex-tiny", 1)
    assert sub is None and token is None


@pytest.mark.slow
async def test_claim_picks_next_candidate_if_first_too_big(
    db_session: AsyncSession, env,
) -> None:
    """Executor has 1 GB free. Two pending: 5 GB (first/older), 100 MiB (second/newer).
    Claim must return the 100 MiB one."""
    db_session.add(Executor(
        id="ex-half-gb", host_id="host-2", cert_fingerprint="x",
        status="healthy", epoch=1,
        disk_free_gb=1, parts_dir_bytes=0,
    ))
    await db_session.flush()

    # Big subtask is created first → older created_at → first in scheduler order.
    big_task, big_sub = await _seed_pending_with_size(
        db_session, file_size=5 * 1024**3, filename="big.bin"
    )
    # 10 ms sleep ensures the second row's created_at is strictly later.
    await asyncio.sleep(0.01)
    small_task, small_sub = await _seed_pending_with_size(
        db_session, file_size=100 * 1024**2, filename="small.bin"
    )

    sub, token = await claim_one_subtask(db_session, "ex-half-gb", 1)
    assert sub is not None
    assert sub.filename == "small.bin"
    assert token is not None
