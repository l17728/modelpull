"""Tests for POST /api/v1/tasks/{task_id}/cancel (Phase 2 W2b2 §3.2)."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base


_TOKEN = "test-bearer-token-cancel"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """Create tables + seed default tenant/project/user/storage."""
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="d", display_name="D"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="d"))
        s.add(User(id=1, tenant_id=1, oidc_subject="d",
                   email="d@l", role="tenant_admin"))
        s.add(StorageBackend(id=1, tenant_id=1, name="d",
                              backend_type="s3", config_encrypted=b""))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DLW_BEARER_TOKEN", _TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture
async def client():
    from dlw.main import create_app
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.slow
async def test_post_cancel_returns_202_and_cancelling(
    client: AsyncClient, auth: dict[str, str],
) -> None:
    """Create a task, cancel it, expect 202 + status=cancelling."""
    create_resp = await client.post(
        "/api/v1/tasks",
        json={
            "repo_id": "owner/repo",
            "revision": "b" * 40,
            "storage_id": 1,
            "path_template": "t",
            "priority": 1,
        },
        headers=auth,
    )
    assert create_resp.status_code == 201, create_resp.text
    task_id = create_resp.json()["id"]

    resp = await client.post(f"/api/v1/tasks/{task_id}/cancel", headers=auth)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "cancelling"


@pytest.mark.slow
async def test_post_cancel_returns_404_on_missing_task(
    client: AsyncClient, auth: dict[str, str],
) -> None:
    random_id = uuid.uuid4()
    resp = await client.post(f"/api/v1/tasks/{random_id}/cancel", headers=auth)
    assert resp.status_code == 404


@pytest.mark.slow
async def test_post_cancel_returns_409_on_terminal_task(
    client: AsyncClient, auth: dict[str, str], engine,
) -> None:
    """Create a task, force-mark it succeeded via DB, then cancel → 409."""
    from datetime import UTC, datetime
    from dlw.db.models.task import DownloadTask
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        task = DownloadTask(
            tenant_id=1, project_id=1, owner_user_id=1,
            repo_id="o/r-terminal", revision="b" * 40, storage_id=1,
            path_template="t", priority=1, status="succeeded",
            completed_at=datetime.now(UTC),
        )
        s.add(task)
        await s.commit()
        task_id = task.id

    resp = await client.post(f"/api/v1/tasks/{task_id}/cancel", headers=auth)
    assert resp.status_code == 409
