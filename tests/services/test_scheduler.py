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
from dlw.services.scheduler import claim_one_subtask, complete_subtask


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
    sub, token = await claim_one_subtask(db_session, executor_id="exec-A", executor_epoch=1)
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
    sub, token = await claim_one_subtask(db_session, executor_id="exec-A", executor_epoch=1)
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
            sub, _ = await claim_one_subtask(s, executor_id="exec-A", executor_epoch=1)
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
            sub, _ = await claim_one_subtask(s, executor_id="exec-A", executor_epoch=1)
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
        sub1, _ = await claim_one_subtask(s, executor_id="exec-A", executor_epoch=1)
        await s.commit()
    async with factory() as s:
        sub2, _ = await claim_one_subtask(s, executor_id="exec-A", executor_epoch=1)
        await s.commit()
    assert sub1 is not None and sub2 is not None
    async with factory() as s:
        sub3, _ = await claim_one_subtask(s, executor_id="exec-A", executor_epoch=1)
        await s.commit()
    assert sub3 is None


async def _make_pending_subtask_with_expected_sha(
    session: AsyncSession, expected_sha: str | None,
) -> uuid.UUID:
    """Helper: create a tiny task with one subtask carrying expected_sha256."""
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="a" * 40, storage_id=1,
        path_template="t/{tenant}", priority=1, status="pending",
    )
    session.add(task); await session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1,
        filename="config.json", file_size=4096,
        expected_sha256=expected_sha, status="pending",
    )
    session.add(sub); await session.flush()
    return sub.id


@pytest.mark.slow
async def test_complete_subtask_marks_failed_on_sha_mismatch(
    db_session: AsyncSession, env,
) -> None:
    """W4: controller-side sha256 verification flips succeeded → failed on mismatch."""
    sub_id = await _make_pending_subtask_with_expected_sha(db_session, "a" * 64)
    # Pretend executor claimed it
    sub = await db_session.get(FileSubTask, sub_id)
    sub.status = "assigned"
    sub.assignment_token = uuid.uuid4()
    await db_session.flush()

    sub_returned, parent = await complete_subtask(
        db_session, sub_id,
        final_status="succeeded",
        actual_sha256="b" * 64,           # mismatch!
        bytes_downloaded=4096,
        error=None,
        assignment_token=sub.assignment_token,
    )
    assert sub_returned.status == "failed"
    assert sub_returned.last_error is not None
    assert "sha256" in sub_returned.last_error.lower()


@pytest.mark.slow
async def test_complete_subtask_succeeds_when_sha_matches(
    db_session: AsyncSession, env,
) -> None:
    sub_id = await _make_pending_subtask_with_expected_sha(db_session, "c" * 64)
    sub = await db_session.get(FileSubTask, sub_id)
    sub.status = "assigned"
    sub.assignment_token = uuid.uuid4()
    await db_session.flush()

    sub_returned, _ = await complete_subtask(
        db_session, sub_id,
        final_status="succeeded",
        actual_sha256="c" * 64,
        bytes_downloaded=4096,
        error=None,
        assignment_token=sub.assignment_token,
    )
    assert sub_returned.status == "succeeded"


@pytest.mark.slow
async def test_complete_subtask_succeeds_when_expected_sha_is_null(
    db_session: AsyncSession, env,
) -> None:
    """Non-LFS files have expected_sha256=None — verification skipped."""
    sub_id = await _make_pending_subtask_with_expected_sha(db_session, None)
    sub = await db_session.get(FileSubTask, sub_id)
    sub.status = "assigned"
    sub.assignment_token = uuid.uuid4()
    await db_session.flush()

    sub_returned, _ = await complete_subtask(
        db_session, sub_id,
        final_status="succeeded",
        actual_sha256="d" * 64,           # would mismatch if expected was set
        bytes_downloaded=4096,
        error=None,
        assignment_token=sub.assignment_token,
    )
    assert sub_returned.status == "succeeded"


@pytest.mark.slow
async def test_complete_subtask_persists_s3_key(
    db_session: AsyncSession, env,
) -> None:
    sub_id = await _make_pending_subtask_with_expected_sha(db_session, None)
    sub = await db_session.get(FileSubTask, sub_id)
    sub.status = "assigned"
    sub.assignment_token = uuid.uuid4()
    await db_session.flush()

    sub_returned, _ = await complete_subtask(
        db_session, sub_id,
        final_status="succeeded",
        actual_sha256="e" * 64,
        bytes_downloaded=1024,
        error=None,
        assignment_token=sub.assignment_token,
        s3_key="phase1/o/r/abc123/config.json",
    )
    assert sub_returned.s3_key == "phase1/o/r/abc123/config.json"


async def _make_pending_subtask(session: AsyncSession) -> uuid.UUID:
    """Helper: create a task with one pending subtask; returns the subtask id."""
    return await _make_pending_subtask_with_expected_sha(session, None)


@pytest.mark.slow
async def test_claim_writes_executor_epoch(db_session: AsyncSession, env) -> None:
    """P2-W1: claim_one_subtask must persist the executor_epoch passed in."""
    sub_id = await _make_pending_subtask(db_session)
    sub, token = await claim_one_subtask(db_session, "host-x-worker-1", executor_epoch=5)
    assert sub is not None
    assert sub.executor_epoch == 5


@pytest.mark.slow
async def test_claim_writes_assigned_at(db_session: AsyncSession, env) -> None:
    """P2-W1: claim_one_subtask must set assigned_at to a recent timestamp."""
    from datetime import UTC, datetime, timedelta

    before = datetime.now(UTC) - timedelta(seconds=1)
    sub_id = await _make_pending_subtask(db_session)
    sub, token = await claim_one_subtask(db_session, "host-x-worker-1", executor_epoch=1)
    after = datetime.now(UTC) + timedelta(seconds=1)
    assert sub is not None
    assert sub.assigned_at is not None
    assert before <= sub.assigned_at <= after
