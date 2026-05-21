"""Write tool registry — tenant isolation + execute (UI-SP4b)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.ai.write_tools import WRITE_TOOLS
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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1",
                     quota_bytes_month=10**12, quota_concurrent=5,
                     quota_storage_gb=100))
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


async def test_cancel_cross_tenant_is_error(session):
    out = await WRITE_TOOLS["dlw_cancel_task"].run(
        session, _principal(1), task_id=str(TASK_T2))
    assert out == {"error": "task not found"}
    # tenant-2 task untouched
    from dlw.db.models.task import DownloadTask
    t2 = await session.get(DownloadTask, TASK_T2)
    assert t2.status == "running"


async def test_cancel_owned_flips_to_cancelling(session):
    out = await WRITE_TOOLS["dlw_cancel_task"].run(
        session, _principal(1), task_id=str(TASK_T1))
    assert out["status"] == "cancelling"
    await session.commit()
    from dlw.db.models.task import DownloadTask
    t1 = await session.get(DownloadTask, TASK_T1)
    assert t1.status == "cancelling"


async def test_create_happy_with_mocked_hf(session, monkeypatch):
    from dlw.services.hf_metadata import RepoFile

    async def fake(*a, **k):
        return [RepoFile(path="model.safetensors", size=1024, sha256="a" * 64)]

    monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake)
    out = await WRITE_TOOLS["dlw_create_task"].run(
        session, _principal(1), repo_id="org/new", revision="b" * 40,
        storage_id=1)
    assert "task_id" in out
    assert out["repo_id"] == "org/new"
    await session.commit()


async def test_create_quota_exceeded(session, monkeypatch):
    from dlw.services.quota import QuotaExceeded

    async def boom(*a, **k):
        raise QuotaExceeded("concurrent")

    monkeypatch.setattr("dlw.ai.write_tools.check_quota_for_new_task", boom)
    out = await WRITE_TOOLS["dlw_create_task"].run(
        session, _principal(1), repo_id="org/x", revision="c" * 40,
        storage_id=1)
    assert out["error"] == "quota_exceeded"
