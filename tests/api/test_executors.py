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


@pytest.fixture
async def joined_executor(client: AsyncClient, auth: dict[str, str]) -> tuple[str, int]:
    """POST /join and return (executor_id, epoch). Used by tests that need fence headers."""
    r = await client.post("/api/v1/executors/join", json={
        "id": "fence-host-worker-1", "host_id": "fence-host",
    }, headers=auth)
    assert r.status_code == 201
    body = r.json()
    return body["id"], body["epoch"]


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
    r_join = await client.post("/api/v1/executors/join", json={
        "id": "exec-hb-1", "host_id": "host-hb"
    }, headers=auth)
    epoch = r_join.json()["epoch"]
    r = await client.post("/api/v1/executors/exec-hb-1/heartbeat",
                          json={"health_score": 95},
                          headers={**auth, "X-Executor-Epoch": str(epoch)})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "healthy"
    assert r.json()["health_score"] == 95


@pytest.mark.slow
async def test_poll_returns_assigned_false_when_no_work(client, auth) -> None:
    r_join = await client.post("/api/v1/executors/join", json={
        "id": "exec-poll-empty", "host_id": "host-pe"
    }, headers=auth)
    epoch = r_join.json()["epoch"]
    r = await client.post("/api/v1/executors/exec-poll-empty/poll",
                          headers={**auth, "X-Executor-Epoch": str(epoch)})
    assert r.status_code == 200
    assert r.json()["assigned"] is False
    assert r.json()["subtask"] is None


@pytest.mark.slow
async def test_poll_returns_subtask_when_work_available(client, auth) -> None:
    await client.post("/api/v1/tasks", json={
        "repo_id": "o/poll", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    r_join = await client.post("/api/v1/executors/join", json={
        "id": "exec-poll-w", "host_id": "host-pw"
    }, headers=auth)
    epoch = r_join.json()["epoch"]
    r = await client.post("/api/v1/executors/exec-poll-w/poll",
                          headers={**auth, "X-Executor-Epoch": str(epoch)})
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


@pytest.mark.slow
async def test_poll_returns_assignment_with_repo_and_storage_config(
    client: AsyncClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W4: /poll response carries repo_id + revision + storage_config."""
    from dlw.services.hf_metadata import RepoFile

    async def fake_hf(*args, **kwargs):
        return [RepoFile(path="config.json", size=4096, sha256=None)]
    monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake_hf)

    # Drain any leftover pending subtasks from earlier tests so we can assert
    # the exact repo_id returned by this test's task.
    r_drain_join = await client.post("/api/v1/executors/join", json={
        "id": "host-x-drain", "host_id": "host-x",
    }, headers=auth)
    drain_epoch = r_drain_join.json()["epoch"]
    for _ in range(20):  # safety upper bound
        dr = await client.post("/api/v1/executors/host-x-drain/poll",
                               headers={**auth, "X-Executor-Epoch": str(drain_epoch)})
        if not dr.json().get("assigned"):
            break

    # Create a task → 1 subtask
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/storage-test",
        "revision": "9" * 40,
        "storage_id": 1,
    }, headers=auth)
    assert r.status_code == 201

    # Join an executor
    r_worker_join = await client.post("/api/v1/executors/join", json={
        "id": "host-x-worker-1", "host_id": "host-x",
    }, headers=auth)
    worker_epoch = r_worker_join.json()["epoch"]

    # Poll
    pr = await client.post("/api/v1/executors/host-x-worker-1/poll",
                           headers={**auth, "X-Executor-Epoch": str(worker_epoch)})
    assert pr.status_code == 200
    body = pr.json()
    assert body["assigned"] is True
    assert body["repo_id"] == "o/storage-test"
    assert body["revision"] == "9" * 40
    assert "storage_config" in body
    assert body["storage_config"]["bucket"]   # default falls back to storage.name


@pytest.mark.slow
async def test_heartbeat_missing_epoch_header_returns_401(
    client: AsyncClient, auth: dict[str, str], joined_executor,
) -> None:
    eid, _ = joined_executor
    r = await client.post(
        f"/api/v1/executors/{eid}/heartbeat",
        json={"health_score": 100, "parts_dir_bytes": 0},
        headers=auth,
    )
    assert r.status_code == 401


@pytest.mark.slow
async def test_heartbeat_wrong_epoch_returns_EPOCH_MISMATCH(
    client: AsyncClient, auth: dict[str, str], joined_executor,
) -> None:
    eid, epoch = joined_executor
    r = await client.post(
        f"/api/v1/executors/{eid}/heartbeat",
        json={"health_score": 100, "parts_dir_bytes": 0},
        headers={**auth, "X-Executor-Epoch": str(epoch + 1)},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "EPOCH_MISMATCH"


@pytest.mark.slow
async def test_heartbeat_correct_epoch_passes(
    client: AsyncClient, auth: dict[str, str], joined_executor,
) -> None:
    eid, epoch = joined_executor
    r = await client.post(
        f"/api/v1/executors/{eid}/heartbeat",
        json={"health_score": 100, "parts_dir_bytes": 0},
        headers={**auth, "X-Executor-Epoch": str(epoch)},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


@pytest.mark.slow
async def test_poll_after_rejoin_uses_new_epoch(
    client: AsyncClient, auth: dict[str, str],
) -> None:
    """Two consecutive joins for the same id: poll with old epoch fails."""
    r1 = await client.post("/api/v1/executors/join", json={
        "id": "rejoin-host-worker-1", "host_id": "rejoin-host",
    }, headers=auth)
    epoch_old = r1.json()["epoch"]

    r2 = await client.post("/api/v1/executors/join", json={
        "id": "rejoin-host-worker-1", "host_id": "rejoin-host",
    }, headers=auth)
    epoch_new = r2.json()["epoch"]
    assert epoch_new == epoch_old + 1

    # Old epoch rejected
    r3 = await client.post(
        "/api/v1/executors/rejoin-host-worker-1/poll",
        headers={**auth, "X-Executor-Epoch": str(epoch_old)},
    )
    assert r3.status_code == 401

    # New epoch accepted
    r4 = await client.post(
        "/api/v1/executors/rejoin-host-worker-1/poll",
        headers={**auth, "X-Executor-Epoch": str(epoch_new)},
    )
    assert r4.status_code == 200
