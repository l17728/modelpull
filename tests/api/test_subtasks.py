"""Tests for subtasks API: POST /report including double-report idempotency."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base


_TOKEN = "test-bearer-token-12345"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
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
async def _cleanup_tasks(engine):
    """Truncate task + subtask tables between tests to avoid cross-test pollution."""
    yield
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE file_subtasks, download_tasks CASCADE"))


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


async def _setup_assigned_subtask(client, auth, repo_id="o/sub-test") -> str:
    """Helper: create task → join executor → poll → return subtask id."""
    await client.post("/api/v1/tasks", json={
        "repo_id": repo_id, "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    exec_id = f"ex-{repo_id.replace('/', '-')}"
    await client.post("/api/v1/executors/join", json={
        "id": exec_id, "host_id": "h"
    }, headers=auth)
    r = await client.post(f"/api/v1/executors/{exec_id}/poll", headers=auth)
    return r.json()["subtask"]["id"]


@pytest.mark.slow
async def test_report_succeeded_marks_subtask_done(client, auth) -> None:
    sub_id = await _setup_assigned_subtask(client, auth, "o/r1")
    r = await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "succeeded",
        "actual_sha256": "a" * 64,
        "bytes_downloaded": 1234,
    }, headers=auth)
    assert r.status_code == 200, r.text


@pytest.mark.slow
async def test_report_two_subtasks_succeed_then_task_succeeds(client, auth) -> None:
    create = await client.post("/api/v1/tasks", json={
        "repo_id": "o/full", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    task_id = create.json()["id"]
    await client.post("/api/v1/executors/join", json={
        "id": "ex-full", "host_id": "h"
    }, headers=auth)
    sub_ids = []
    for _ in range(2):
        r = await client.post("/api/v1/executors/ex-full/poll", headers=auth)
        sub_ids.append(r.json()["subtask"]["id"])
    for sid in sub_ids:
        await client.post(f"/api/v1/subtasks/{sid}/report", json={
            "status": "succeeded", "actual_sha256": "b" * 64, "bytes_downloaded": 100,
        }, headers=auth)
    r = await client.get(f"/api/v1/tasks/{task_id}", headers=auth)
    assert r.json()["status"] == "succeeded"
    assert r.json()["completed_at"] is not None


@pytest.mark.slow
async def test_report_one_failure_marks_task_failed(client, auth) -> None:
    create = await client.post("/api/v1/tasks", json={
        "repo_id": "o/fail", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    task_id = create.json()["id"]
    await client.post("/api/v1/executors/join", json={
        "id": "ex-fail", "host_id": "h"
    }, headers=auth)
    r = await client.post("/api/v1/executors/ex-fail/poll", headers=auth)
    sub_id = r.json()["subtask"]["id"]
    await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "failed", "error": "disk full",
    }, headers=auth)
    r = await client.get(f"/api/v1/tasks/{task_id}", headers=auth)
    assert r.json()["status"] == "failed"
    assert "disk full" in r.json()["error_message"]


@pytest.mark.slow
async def test_report_unknown_subtask_returns_404(client, auth) -> None:
    import uuid
    r = await client.post(f"/api/v1/subtasks/{uuid.uuid4()}/report", json={
        "status": "succeeded",
    }, headers=auth)
    assert r.status_code == 404


@pytest.mark.slow
async def test_report_unauthenticated_returns_401(client) -> None:
    import uuid
    r = await client.post(f"/api/v1/subtasks/{uuid.uuid4()}/report", json={
        "status": "succeeded",
    })
    assert r.status_code == 401


@pytest.mark.slow
async def test_double_report_returns_409(client, auth) -> None:
    """W2-G: idempotency / illegal-transition guard."""
    sub_id = await _setup_assigned_subtask(client, auth, "o/dup")
    r1 = await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "succeeded", "actual_sha256": "c" * 64, "bytes_downloaded": 100,
    }, headers=auth)
    assert r1.status_code == 200
    r2 = await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "succeeded", "actual_sha256": "c" * 64, "bytes_downloaded": 100,
    }, headers=auth)
    assert r2.status_code == 409, r2.text
