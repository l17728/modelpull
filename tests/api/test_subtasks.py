"""Tests for subtasks API: POST /report including double-report idempotency."""
from __future__ import annotations

import uuid

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


@pytest.fixture
async def joined_executor(client: AsyncClient, auth: dict[str, str]) -> tuple[str, int]:
    """POST /join and return (executor_id, epoch). Used by tests that need fence headers."""
    r = await client.post("/api/v1/executors/join", json={
        "id": "sub-fence-worker-1", "host_id": "sub-fence-host",
    }, headers=auth)
    assert r.status_code == 201
    body = r.json()
    return body["id"], body["epoch"]


async def _setup_assigned_subtask(client, auth, repo_id="o/sub-test") -> tuple[str, str, int]:
    """Helper: create task → join executor → heartbeat → poll → return (subtask_id, exec_id, epoch).

    W2a: claim_one_subtask requires status='healthy'|'degraded'. A heartbeat
    after join transitions the executor from 'joining' to 'healthy'.
    """
    await client.post("/api/v1/tasks", json={
        "repo_id": repo_id, "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    exec_id = f"ex-{repo_id.replace('/', '-')}"
    rj = await client.post("/api/v1/executors/join", json={
        "id": exec_id, "host_id": "h"
    }, headers=auth)
    epoch = rj.json()["epoch"]
    await client.post(f"/api/v1/executors/{exec_id}/heartbeat",
                      json={"health_score": 100, "parts_dir_bytes": 0},
                      headers={**auth, "X-Executor-Epoch": str(epoch)})
    r = await client.post(
        f"/api/v1/executors/{exec_id}/poll",
        headers={**auth, "X-Executor-Epoch": str(epoch)},
    )
    return r.json()["subtask"]["id"], exec_id, epoch


@pytest.mark.slow
async def test_report_succeeded_marks_subtask_done(client, auth) -> None:
    sub_id, exec_id, epoch = await _setup_assigned_subtask(client, auth, "o/r1")
    r = await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "succeeded",
        "actual_sha256": "a" * 64,
        "bytes_downloaded": 1234,
    }, headers={**auth, "X-Executor-Epoch": str(epoch)})
    assert r.status_code == 200, r.text


@pytest.mark.slow
async def test_report_two_subtasks_succeed_then_task_succeeds(client, auth) -> None:
    create = await client.post("/api/v1/tasks", json={
        "repo_id": "o/full", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    task_id = create.json()["id"]
    rj = await client.post("/api/v1/executors/join", json={
        "id": "ex-full", "host_id": "h"
    }, headers=auth)
    epoch = rj.json()["epoch"]
    # W2a: heartbeat transitions joining → healthy before poll can claim work.
    await client.post("/api/v1/executors/ex-full/heartbeat",
                      json={"health_score": 100, "parts_dir_bytes": 0},
                      headers={**auth, "X-Executor-Epoch": str(epoch)})
    sub_ids = []
    for _ in range(2):
        r = await client.post(
            "/api/v1/executors/ex-full/poll",
            headers={**auth, "X-Executor-Epoch": str(epoch)},
        )
        sub_ids.append(r.json()["subtask"]["id"])
    for sid in sub_ids:
        await client.post(f"/api/v1/subtasks/{sid}/report", json={
            "status": "succeeded", "actual_sha256": "b" * 64, "bytes_downloaded": 100,
        }, headers={**auth, "X-Executor-Epoch": str(epoch)})
    r = await client.get(f"/api/v1/tasks/{task_id}", headers=auth)
    assert r.json()["status"] == "succeeded"
    assert r.json()["completed_at"] is not None


@pytest.mark.slow
async def test_report_one_failure_marks_task_failed(client, auth) -> None:
    create = await client.post("/api/v1/tasks", json={
        "repo_id": "o/fail", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    task_id = create.json()["id"]
    rj = await client.post("/api/v1/executors/join", json={
        "id": "ex-fail", "host_id": "h"
    }, headers=auth)
    epoch = rj.json()["epoch"]
    # W2a: heartbeat transitions joining → healthy before poll can claim work.
    await client.post("/api/v1/executors/ex-fail/heartbeat",
                      json={"health_score": 100, "parts_dir_bytes": 0},
                      headers={**auth, "X-Executor-Epoch": str(epoch)})
    r = await client.post(
        "/api/v1/executors/ex-fail/poll",
        headers={**auth, "X-Executor-Epoch": str(epoch)},
    )
    sub_id = r.json()["subtask"]["id"]
    await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "failed", "error": "disk full",
    }, headers={**auth, "X-Executor-Epoch": str(epoch)})
    r = await client.get(f"/api/v1/tasks/{task_id}", headers=auth)
    assert r.json()["status"] == "failed"
    assert "disk full" in r.json()["error_message"]


@pytest.mark.slow
async def test_report_unknown_subtask_returns_404(client, auth) -> None:
    r = await client.post(f"/api/v1/subtasks/{uuid.uuid4()}/report", json={
        "status": "succeeded",
    }, headers={**auth, "X-Executor-Epoch": "1"})
    assert r.status_code == 404


@pytest.mark.slow
async def test_report_unauthenticated_returns_401(client) -> None:
    r = await client.post(f"/api/v1/subtasks/{uuid.uuid4()}/report", json={
        "status": "succeeded",
    })
    assert r.status_code == 401


@pytest.mark.slow
async def test_double_report_returns_409(client, auth) -> None:
    """W2-G: idempotency / illegal-transition guard."""
    sub_id, exec_id, epoch = await _setup_assigned_subtask(client, auth, "o/dup")
    r1 = await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "succeeded", "actual_sha256": "c" * 64, "bytes_downloaded": 100,
    }, headers={**auth, "X-Executor-Epoch": str(epoch)})
    assert r1.status_code == 200
    r2 = await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "succeeded", "actual_sha256": "c" * 64, "bytes_downloaded": 100,
    }, headers={**auth, "X-Executor-Epoch": str(epoch)})
    assert r2.status_code == 409, r2.text


@pytest.mark.slow
async def test_report_missing_epoch_header_returns_401(
    client: AsyncClient, auth: dict[str, str],
) -> None:
    r = await client.post(
        f"/api/v1/subtasks/{uuid.uuid4()}/report",
        json={"status": "succeeded", "bytes_downloaded": 100},
        headers=auth,
    )
    assert r.status_code == 401


@pytest.mark.slow
async def test_report_stale_epoch_returns_EPOCH_MISMATCH(
    client: AsyncClient, auth: dict[str, str],
) -> None:
    """Create task + join executor + claim subtask + report with stale epoch."""
    # Setup: create a task to generate subtasks
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/fence-report", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    task_id = r.json()["id"]

    # Join executor — get epoch=1
    rj = await client.post("/api/v1/executors/join", json={
        "id": "report-host-worker-1", "host_id": "report-host",
    }, headers=auth)
    epoch = rj.json()["epoch"]

    # W2a: heartbeat transitions joining → healthy before poll can claim work.
    await client.post("/api/v1/executors/report-host-worker-1/heartbeat",
                      json={"health_score": 100, "parts_dir_bytes": 0},
                      headers={**auth, "X-Executor-Epoch": str(epoch)})

    # Claim subtask via poll
    rp = await client.post(
        "/api/v1/executors/report-host-worker-1/poll",
        headers={**auth, "X-Executor-Epoch": str(epoch)},
    )
    assert rp.status_code == 200, rp.text
    if not rp.json()["assigned"]:
        pytest.skip("no subtask available — module DB state interference")
    subtask_id = rp.json()["subtask"]["id"]
    token = rp.json()["assignment_token"]

    # Bump epoch (re-join → epoch=2)
    rj2 = await client.post("/api/v1/executors/join", json={
        "id": "report-host-worker-1", "host_id": "report-host",
    }, headers=auth)
    assert rj2.json()["epoch"] == epoch + 1

    # Report with STALE epoch (the one we claimed under)
    rr = await client.post(
        f"/api/v1/subtasks/{subtask_id}/report",
        json={
            "status": "succeeded", "bytes_downloaded": 100,
            "actual_sha256": "a" * 64, "assignment_token": token,
        },
        headers={**auth, "X-Executor-Epoch": str(epoch)},  # stale!
    )
    assert rr.status_code == 401
    assert rr.json()["detail"]["code"] == "EPOCH_MISMATCH"
