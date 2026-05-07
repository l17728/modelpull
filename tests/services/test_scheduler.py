"""Tests for scheduler.claim_one_subtask — atomic FOR UPDATE SKIP LOCKED."""
from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

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


@pytest_asyncio.fixture(scope="module", autouse=True)
async def env(engine: AsyncEngine):
    """Seed minimum data ONCE per module — committed rows would PK-conflict
    if this fixture were function-scoped (W2-C from review)."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="d", display_name="D"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="d"))
        s.add(User(id=1, tenant_id=1, oidc_subject="d",
                    email="d@l", role="tenant_admin"))
        s.add(StorageBackend(id=1, tenant_id=1, name="d",
                              backend_type="s3", config_encrypted=b""))
        s.add(Executor(id="exec-A", host_id="ha",
                        cert_fingerprint="fp", status="healthy"))
        await s.commit()


async def _make_pending_task(session: AsyncSession, n_subtasks: int) -> DownloadTask:
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="0" * 40,
        storage_id=1, path_template="x", status="pending",
    )
    session.add(task)
    await session.flush()
    for i in range(n_subtasks):
        session.add(FileSubTask(
            task_id=task.id, tenant_id=1,
            filename=f"file-{i}.bin", status="pending",
        ))
    await session.commit()
    return task


@pytest.mark.slow
async def test_claim_returns_subtask_when_pending_exists(
    db_session: AsyncSession, engine
) -> None:
    await _make_pending_task(db_session, n_subtasks=1)
    sub, token = await claim_one_subtask(db_session, executor_id="exec-A")
    await db_session.commit()
    assert sub is not None
    assert token is not None
    assert sub.status == "assigned"
    assert sub.executor_id == "exec-A"
    assert sub.assignment_token == token


@pytest.mark.slow
async def test_claim_returns_none_when_no_pending(db_session: AsyncSession) -> None:
    # First clear any leftover pending subtasks from prior tests
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with factory() as cleanup:
        from sqlalchemy import update
        await cleanup.execute(
            update(FileSubTask).where(FileSubTask.status == "pending").values(status="assigned")
        )
        await cleanup.commit()
    sub, token = await claim_one_subtask(db_session, executor_id="exec-A")
    await db_session.commit()
    assert sub is None
    assert token is None


@pytest.mark.slow
async def test_two_concurrent_claims_get_different_subtasks(
    db_session: AsyncSession, engine
) -> None:
    """Concurrency: 2 sessions polling at once must get DIFFERENT subtasks."""
    await _make_pending_task(db_session, n_subtasks=2)

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def claim_in_own_session() -> uuid.UUID | None:
        async with factory() as s:
            sub, _ = await claim_one_subtask(s, executor_id="exec-A")
            await s.commit()
            return sub.id if sub else None

    id1, id2 = await asyncio.gather(claim_in_own_session(), claim_in_own_session())
    assert id1 is not None and id2 is not None
    assert id1 != id2


@pytest.mark.slow
async def test_one_subtask_two_claimants_only_one_wins(
    db_session: AsyncSession, engine
) -> None:
    """Critical: with EXACTLY 1 pending subtask and 2 concurrent claimants,
    exactly one must succeed (W2-D). Without SKIP LOCKED, both would block
    on FOR UPDATE and one would eventually claim the row regardless."""
    await _make_pending_task(db_session, n_subtasks=1)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def claim_in_own_session() -> uuid.UUID | None:
        async with factory() as s:
            sub, _ = await claim_one_subtask(s, executor_id="exec-A")
            await s.commit()
            return sub.id if sub else None

    r1, r2 = await asyncio.gather(claim_in_own_session(), claim_in_own_session())
    succeeded = [r for r in (r1, r2) if r is not None]
    assert len(succeeded) == 1, f"Expected exactly 1 winner, got {succeeded}"


@pytest.mark.slow
async def test_third_claim_returns_none_when_all_assigned(
    db_session: AsyncSession, engine
) -> None:
    # Clear leftover pending subtasks
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as cleanup:
        from sqlalchemy import update
        await cleanup.execute(
            update(FileSubTask).where(FileSubTask.status == "pending").values(status="assigned")
        )
        await cleanup.commit()
    await _make_pending_task(db_session, n_subtasks=2)
    async with factory() as s:
        sub1, _ = await claim_one_subtask(s, executor_id="exec-A")
        await s.commit()
    async with factory() as s:
        sub2, _ = await claim_one_subtask(s, executor_id="exec-A")
        await s.commit()
    assert sub1 is not None and sub2 is not None
    async with factory() as s:
        sub3, _ = await claim_one_subtask(s, executor_id="exec-A")
        await s.commit()
    assert sub3 is None
