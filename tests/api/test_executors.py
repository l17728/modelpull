"""Tests for executors API: join / heartbeat / poll with bearer auth."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base


_TOKEN = "test-bearer-token-12345"


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
async def test_executor_join_returns_201(client, auth) -> None:
    r = await client.post("/api/v1/executors/join", json={
        "id": "exec-test-1", "host_id": "host-test",
    }, headers=auth)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "exec-test-1"
    assert body["status"] == "joining"


@pytest.mark.slow
async def test_executor_heartbeat_transitions_to_healthy(client, auth) -> None:
    await client.post("/api/v1/executors/join", json={
        "id": "exec-hb-1", "host_id": "host-hb"
    }, headers=auth)
    r = await client.post("/api/v1/executors/exec-hb-1/heartbeat",
                          json={"health_score": 95}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "healthy"
    assert r.json()["health_score"] == 95


@pytest.mark.slow
async def test_poll_returns_assigned_false_when_no_work(client, auth) -> None:
    await client.post("/api/v1/executors/join", json={
        "id": "exec-poll-empty", "host_id": "host-pe"
    }, headers=auth)
    r = await client.post("/api/v1/executors/exec-poll-empty/poll", headers=auth)
    assert r.status_code == 200
    assert r.json()["assigned"] is False
    assert r.json()["subtask"] is None


@pytest.mark.slow
async def test_poll_returns_subtask_when_work_available(client, auth) -> None:
    await client.post("/api/v1/tasks", json={
        "repo_id": "o/poll", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    await client.post("/api/v1/executors/join", json={
        "id": "exec-poll-w", "host_id": "host-pw"
    }, headers=auth)
    r = await client.post("/api/v1/executors/exec-poll-w/poll", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assigned"] is True
    assert body["subtask"] is not None
    assert "id" in body["subtask"]
    assert body["assignment_token"] is not None


@pytest.mark.slow
async def test_unauthenticated_returns_401(client) -> None:
    r = await client.post("/api/v1/executors/join", json={
        "id": "x", "host_id": "y"
    })
    assert r.status_code == 401
