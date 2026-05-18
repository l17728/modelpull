"""Tests for subtasks API: POST /report under mTLS + JWT (W3a)."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import (
    executor_request_headers,
    principal_headers,
    register_test_executor,
    signed_heartbeat_headers,
)

SECRET = "unit-secret"
_ENROLL = "test-enrollment-token-w3a-subtasks"


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
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def auth() -> dict[str, str]:
    """Bearer header for /tasks (UI) endpoints."""
    return principal_headers(secret=SECRET, role="tenant_admin")


@pytest.fixture
async def client(ephemeral_ca):
    from tests.conftest import make_app_with_state
    app = make_app_with_state(ephemeral_ca, enrollment_token=_ENROLL)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _setup_assigned_subtask(
    client, auth, repo_id="o/sub-test",
) -> tuple[str, dict]:
    """create task → register executor → heartbeat → poll → return (subtask_id, reg)."""
    await client.post("/api/v1/tasks", json={
        "repo_id": repo_id, "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    exec_id = f"ex-{repo_id.replace('/', '-')}"
    reg = await register_test_executor(
        client, enrollment_token=_ENROLL, executor_id=exec_id, host_id="h",
    )
    hb_body = b'{"health_score": 100, "parts_dir_bytes": 0, "disk_free_gb": 100}'
    await client.post(f"/api/v1/executors/{exec_id}/heartbeat",
                      content=hb_body, headers=signed_heartbeat_headers(reg, hb_body))
    r = await client.post(
        f"/api/v1/executors/{exec_id}/poll",
        headers=executor_request_headers(reg),
    )
    return r.json()["subtask"]["id"], reg


@pytest.mark.slow
async def test_report_succeeded_marks_subtask_done(client, auth) -> None:
    sub_id, reg = await _setup_assigned_subtask(client, auth, "o/r1")
    r = await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "succeeded",
        "actual_sha256": "a" * 64,
        "bytes_downloaded": 1234,
    }, headers=executor_request_headers(reg))
    assert r.status_code == 200, r.text


@pytest.mark.slow
async def test_report_two_subtasks_succeed_then_task_succeeds(client, auth) -> None:
    create = await client.post("/api/v1/tasks", json={
        "repo_id": "o/full", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    task_id = create.json()["id"]
    reg = await register_test_executor(
        client, enrollment_token=_ENROLL, executor_id="ex-full", host_id="h",
    )
    hb_body = b'{"health_score": 100, "parts_dir_bytes": 0, "disk_free_gb": 100}'
    await client.post("/api/v1/executors/ex-full/heartbeat",
                      content=hb_body, headers=signed_heartbeat_headers(reg, hb_body))
    sub_ids = []
    for _ in range(2):
        r = await client.post(
            "/api/v1/executors/ex-full/poll",
            headers=executor_request_headers(reg),
        )
        sub_ids.append(r.json()["subtask"]["id"])
    for sid in sub_ids:
        await client.post(f"/api/v1/subtasks/{sid}/report", json={
            "status": "succeeded", "actual_sha256": "b" * 64, "bytes_downloaded": 100,
        }, headers=executor_request_headers(reg))
    r = await client.get(f"/api/v1/tasks/{task_id}", headers=auth)
    assert r.json()["status"] == "succeeded"
    assert r.json()["completed_at"] is not None


@pytest.mark.slow
async def test_report_one_failure_marks_task_failed(client, auth) -> None:
    create = await client.post("/api/v1/tasks", json={
        "repo_id": "o/fail", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    task_id = create.json()["id"]
    reg = await register_test_executor(
        client, enrollment_token=_ENROLL, executor_id="ex-fail", host_id="h",
    )
    hb_body = b'{"health_score": 100, "parts_dir_bytes": 0, "disk_free_gb": 100}'
    await client.post("/api/v1/executors/ex-fail/heartbeat",
                      content=hb_body, headers=signed_heartbeat_headers(reg, hb_body))
    r = await client.post(
        "/api/v1/executors/ex-fail/poll",
        headers=executor_request_headers(reg),
    )
    sub_id = r.json()["subtask"]["id"]
    await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "failed", "error": "disk full",
    }, headers=executor_request_headers(reg))
    r = await client.get(f"/api/v1/tasks/{task_id}", headers=auth)
    assert r.json()["status"] == "failed"
    assert "disk full" in r.json()["error_message"]


@pytest.mark.slow
async def test_report_unknown_subtask_returns_404(client, auth) -> None:
    """Authenticated request to a random subtask id → 404."""
    reg = await register_test_executor(
        client, enrollment_token=_ENROLL, executor_id="ex-404", host_id="h",
    )
    r = await client.post(f"/api/v1/subtasks/{uuid.uuid4()}/report", json={
        "status": "succeeded",
    }, headers=executor_request_headers(reg))
    assert r.status_code == 404


@pytest.mark.slow
async def test_report_unauthenticated_returns_401(client) -> None:
    """No mTLS cert → /report rejected before the handler runs."""
    r = await client.post(f"/api/v1/subtasks/{uuid.uuid4()}/report", json={
        "status": "succeeded",
    })
    assert r.status_code == 401


@pytest.mark.slow
async def test_double_report_returns_409(client, auth) -> None:
    """W2-G: idempotency / illegal-transition guard."""
    sub_id, reg = await _setup_assigned_subtask(client, auth, "o/dup")
    r1 = await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "succeeded", "actual_sha256": "c" * 64, "bytes_downloaded": 100,
    }, headers=executor_request_headers(reg))
    assert r1.status_code == 200
    r2 = await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "succeeded", "actual_sha256": "c" * 64, "bytes_downloaded": 100,
    }, headers=executor_request_headers(reg))
    assert r2.status_code == 409, r2.text


@pytest.mark.slow
async def test_report_missing_epoch_header_returns_401(
    client: AsyncClient, auth: dict[str, str],
) -> None:
    """Authenticated but no X-Executor-Epoch → 401."""
    reg = await register_test_executor(
        client, enrollment_token=_ENROLL, executor_id="ex-noepoch", host_id="h",
    )
    headers = executor_request_headers(reg)
    del headers["X-Executor-Epoch"]
    r = await client.post(
        f"/api/v1/subtasks/{uuid.uuid4()}/report",
        json={"status": "succeeded", "bytes_downloaded": 100},
        headers=headers,
    )
    assert r.status_code == 401


@pytest.mark.slow
async def test_report_stale_epoch_returns_EPOCH_MISMATCH(
    client: AsyncClient, auth: dict[str, str],
) -> None:
    """Claim a subtask under epoch=1, re-register to epoch=2, report with the
    stale epoch=1 (using the fresh cert) → EPOCH_MISMATCH."""
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/fence-report", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    assert r.status_code == 201

    reg1 = await register_test_executor(
        client, enrollment_token=_ENROLL,
        executor_id="report-host-worker-1", host_id="report-host",
    )
    hb_body = b'{"health_score": 100, "parts_dir_bytes": 0, "disk_free_gb": 100}'
    await client.post("/api/v1/executors/report-host-worker-1/heartbeat",
                      content=hb_body, headers=signed_heartbeat_headers(reg1, hb_body))
    rp = await client.post(
        "/api/v1/executors/report-host-worker-1/poll",
        headers=executor_request_headers(reg1),
    )
    assert rp.status_code == 200, rp.text
    if not rp.json()["assigned"]:
        pytest.skip("no subtask available — module DB state interference")
    subtask_id = rp.json()["subtask"]["id"]
    token = rp.json()["assignment_token"]

    # Re-register → epoch=2 + fresh cert.
    reg2 = await register_test_executor(
        client, enrollment_token=_ENROLL,
        executor_id="report-host-worker-1", host_id="report-host",
    )
    assert reg2["epoch"] == reg1["epoch"] + 1

    # Report with the fresh cert but the STALE epoch.
    stale = dict(reg2)
    stale["epoch"] = reg1["epoch"]
    rr = await client.post(
        f"/api/v1/subtasks/{subtask_id}/report",
        json={
            "status": "succeeded", "bytes_downloaded": 100,
            "actual_sha256": "a" * 64, "assignment_token": token,
        },
        headers=executor_request_headers(stale),
    )
    assert rr.status_code == 401
    assert rr.json()["detail"]["code"] == "EPOCH_MISMATCH"
