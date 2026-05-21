"""Read-only tool registry — tenant isolation (UI-SP4a)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.ai.tools import READONLY_TOOLS
from dlw.auth.principal import Principal
from dlw.db.base import Base

TASK_T1 = uuid.UUID("11111111-1111-1111-1111-111111111111")
TASK_T2 = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _principal(tenant_id: int, user_id: int = 1) -> Principal:
    return Principal(user_id=user_id, tenant_id=tenant_id,
                     role="tenant_admin", project_ids=())


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.task import DownloadTask
    from dlw.db.models.tenant import Project, Tenant, User
    from dlw.db.models.usage import QuotaSnapshot
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1",
                     quota_bytes_month=1000, quota_concurrent=5,
                     quota_storage_gb=10))
        s.add(Tenant(id=2, slug="t2", display_name="T2"))
        await s.flush()
        s.add_all([
            Project(id=1, tenant_id=1, name="d1"),
            Project(id=2, tenant_id=2, name="d2"),
            User(id=1, tenant_id=1, oidc_subject="u1", email="u1@t",
                 role="tenant_admin"),
            User(id=2, tenant_id=2, oidc_subject="u2", email="u2@t",
                 role="tenant_admin"),
            StorageBackend(id=1, tenant_id=1, name="s1",
                           backend_type="s3", config_encrypted=b""),
            StorageBackend(id=2, tenant_id=2, name="s2",
                           backend_type="s3", config_encrypted=b""),
            QuotaSnapshot(tenant_id=1, bytes_used_month=42),
        ])
        await s.flush()
        s.add_all([
            DownloadTask(id=TASK_T1, tenant_id=1, project_id=1,
                         owner_user_id=1, storage_id=1, repo_id="org/m1",
                         revision="0" * 40,
                         path_template="hf/{model}/{revision}/{file}",
                         status="running"),
            DownloadTask(id=TASK_T2, tenant_id=2, project_id=2,
                         owner_user_id=2, storage_id=2, repo_id="org/m2",
                         revision="1" * 40,
                         path_template="hf/{model}/{revision}/{file}",
                         status="running"),
        ])
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


async def test_list_tasks_tenant_isolation(session):
    out = await READONLY_TOOLS["dlw_list_tasks"].run(session, _principal(1))
    ids = {it["id"] for it in out["items"]}
    assert str(TASK_T1) in ids
    assert str(TASK_T2) not in ids
    out2 = await READONLY_TOOLS["dlw_list_tasks"].run(session, _principal(2))
    ids2 = {it["id"] for it in out2["items"]}
    assert str(TASK_T2) in ids2
    assert str(TASK_T1) not in ids2


async def test_get_task_cross_tenant_is_error(session):
    out = await READONLY_TOOLS["dlw_get_task"].run(
        session, _principal(1), task_id=str(TASK_T2))
    assert out.get("error") == "task not found"
    ok = await READONLY_TOOLS["dlw_get_task"].run(
        session, _principal(1), task_id=str(TASK_T1))
    assert ok["id"] == str(TASK_T1)


async def test_quota_current_shape(session):
    out = await READONLY_TOOLS["dlw_quota_current"].run(session, _principal(1))
    assert set(out.keys()) == {
        "tenant_id", "bytes_used_month", "bytes_quota_month",
        "storage_gb_used", "storage_gb_quota",
        "concurrent_tasks", "concurrent_quota"}
    assert out["tenant_id"] == 1
    assert out["bytes_used_month"] == 42
