"""Tests for tasks API: POST/GET list/GET by id with bearer auth."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base


_TOKEN = "test-bearer-token-12345"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """Create tables + seed default tenant/project/user/storage row."""
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Tenant(id=1, slug="default", display_name="Default"))
        await session.flush()
        session.add(Project(id=1, tenant_id=1, name="default"))
        session.add(User(id=1, tenant_id=1, oidc_subject="dev",
                         email="d@l", role="tenant_admin"))
        session.add(StorageBackend(id=1, tenant_id=1, name="default",
                                    backend_type="s3", config_encrypted=b""))
        await session.commit()
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
async def test_post_task_unauthenticated_returns_401(client: AsyncClient) -> None:
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/r", "revision": "0" * 40, "storage_id": 1,
    })
    assert r.status_code == 401


@pytest.mark.slow
async def test_post_task_returns_201_with_id(client: AsyncClient, auth) -> None:
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "deepseek-ai/DeepSeek-V3",
        "revision": "0123456789abcdef" * 2 + "01234567",
        "storage_id": 1,
        "priority": 5,
    }, headers=auth)
    assert r.status_code == 201, r.text
    body = r.json()
    assert "id" in body
    assert body["status"] == "pending"
    assert body["repo_id"] == "deepseek-ai/DeepSeek-V3"
    assert body["priority"] == 5


@pytest.mark.slow
async def test_get_tasks_list_returns_paginated(client: AsyncClient, auth) -> None:
    for i in range(2):
        await client.post("/api/v1/tasks", json={
            "repo_id": f"o/list-{i}",
            "revision": "1" * 40,
            "storage_id": 1,
        }, headers=auth)
    r = await client.get("/api/v1/tasks", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "total" in body
    assert body["total"] >= 2


@pytest.mark.slow
async def test_get_task_by_id_returns_detail(client: AsyncClient, auth) -> None:
    create = await client.post("/api/v1/tasks", json={
        "repo_id": "o/detail", "revision": "2" * 40, "storage_id": 1,
    }, headers=auth)
    task_id = create.json()["id"]
    r = await client.get(f"/api/v1/tasks/{task_id}", headers=auth)
    assert r.status_code == 200
    assert r.json()["id"] == task_id


@pytest.mark.slow
async def test_get_unknown_task_returns_404(client: AsyncClient, auth) -> None:
    import uuid
    r = await client.get(f"/api/v1/tasks/{uuid.uuid4()}", headers=auth)
    assert r.status_code == 404


@pytest.mark.slow
async def test_post_task_validation_error_returns_422(client: AsyncClient, auth) -> None:
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/r", "revision": "0" * 40,
    }, headers=auth)
    assert r.status_code == 422


@pytest.mark.slow
async def test_get_task_by_id_includes_subtasks_array(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """GET /tasks/{id} returns subtasks: [...] (Phase 1 W3 UI scaffold contract)."""
    create = await client.post("/api/v1/tasks", json={
        "repo_id": "o/with-subtasks",
        "revision": "3" * 40,
        "storage_id": 1,
    }, headers=auth)
    task_id = create.json()["id"]

    r = await client.get(f"/api/v1/tasks/{task_id}", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "subtasks" in body
    assert isinstance(body["subtasks"], list)
    assert len(body["subtasks"]) == 2
    filenames = {s["filename"] for s in body["subtasks"]}
    assert filenames == {"config.json", "model.safetensors"}
    # Each subtask carries the SubTaskRead shape
    for s in body["subtasks"]:
        assert {"id", "task_id", "filename", "status"} <= set(s.keys())


@pytest.mark.slow
async def test_get_tasks_list_omits_subtasks_field(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """List endpoint stays slim — TaskRead has no subtasks (avoids N+1)."""
    await client.post("/api/v1/tasks", json={
        "repo_id": "o/list-no-subtasks",
        "revision": "4" * 40,
        "storage_id": 1,
    }, headers=auth)
    r = await client.get("/api/v1/tasks", headers=auth)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) > 0
    for item in items:
        assert "subtasks" not in item
