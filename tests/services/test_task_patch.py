"""Service-level tests for task_patch."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.services.task_patch import (
    InvalidPatch, TaskNotFound, TaskPatch, TaskTerminal, patch_task,
)

TASK_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.task import DownloadTask
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="d"))
        s.add(User(id=1, tenant_id=1, oidc_subject="u1",
                   role="tenant_admin"))
        s.add(StorageBackend(id=1, tenant_id=1, name="s1",
                              backend_type="s3", config_encrypted=b""))
        await s.flush()
        s.add(DownloadTask(
            id=TASK_ID, tenant_id=1, project_id=1, owner_user_id=1,
            storage_id=1, repo_id="org/m", revision="0" * 40,
            path_template="hf/{model}/{revision}/{file}",
            status="running", priority=1, source_strategy="auto_balance",
            source_blacklist=[]))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


async def test_patch_priority(session):
    task = await patch_task(
        session, task_id=TASK_ID, tenant_id=1, patch=TaskPatch(priority=5))
    assert task.priority == 5
    await session.rollback()


async def test_patch_strategy(session):
    task = await patch_task(
        session, task_id=TASK_ID, tenant_id=1,
        patch=TaskPatch(source_strategy="fastest_only"))
    assert task.source_strategy == "fastest_only"
    await session.rollback()


async def test_patch_strategy_pin_is_valid(session):
    task = await patch_task(
        session, task_id=TASK_ID, tenant_id=1,
        patch=TaskPatch(source_strategy="pin_huggingface"))
    assert task.source_strategy == "pin_huggingface"
    await session.rollback()


async def test_patch_strategy_invalid_raises(session):
    with pytest.raises(InvalidPatch):
        await patch_task(
            session, task_id=TASK_ID, tenant_id=1,
            patch=TaskPatch(source_strategy="evil"))


async def test_patch_blacklist(session):
    task = await patch_task(
        session, task_id=TASK_ID, tenant_id=1,
        patch=TaskPatch(source_blacklist=["huggingface", "modelscope"]))
    assert task.source_blacklist == ["huggingface", "modelscope"]
    await session.rollback()


async def test_patch_priority_out_of_range_raises(session):
    with pytest.raises(InvalidPatch):
        await patch_task(
            session, task_id=TASK_ID, tenant_id=1,
            patch=TaskPatch(priority=99))


async def test_patch_cross_tenant_not_found(session):
    with pytest.raises(TaskNotFound):
        await patch_task(
            session, task_id=TASK_ID, tenant_id=999,
            patch=TaskPatch(priority=2))


async def test_patch_terminal_task_rejected(session):
    """Mark task succeeded then try to patch → TaskTerminal."""
    from dlw.db.models.task import DownloadTask
    t = await session.get(DownloadTask, TASK_ID)
    t.status = "succeeded"
    await session.commit()
    try:
        with pytest.raises(TaskTerminal):
            await patch_task(
                session, task_id=TASK_ID, tenant_id=1,
                patch=TaskPatch(priority=2))
    finally:
        # restore for other tests
        t2 = await session.get(DownloadTask, TASK_ID)
        t2.status = "running"
        await session.commit()
