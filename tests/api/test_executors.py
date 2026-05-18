"""Tests for executors API: register / heartbeat / poll under mTLS + JWT + HMAC (W3a)."""
from __future__ import annotations

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
_ENROLL = "test-enrollment-token-w3a-executors"


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
    """JWT secret for /tasks (UI) endpoints + trusted-proxy bypass for mTLS."""
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def auth() -> dict[str, str]:
    """Bearer header for /tasks (UI) endpoints — executor endpoints use mTLS+JWT."""
    return principal_headers(secret=SECRET, role="tenant_admin")


@pytest.fixture
async def client(ephemeral_ca):
    """App with W3a auth state injected onto app.state from the session CA."""
    from tests.conftest import make_app_with_state
    app = make_app_with_state(ephemeral_ca, enrollment_token=_ENROLL)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def registered_executor(client: AsyncClient) -> dict:
    """Register a fence-test executor via /register; return the reg dict."""
    return await register_test_executor(
        client, enrollment_token=_ENROLL,
        executor_id="fence-host-worker-1", host_id="fence-host",
    )


async def _register_and_heartbeat(client, executor_id: str, host_id: str) -> dict:
    """Register an executor + send one heartbeat (joining → healthy) so /poll
    can claim work. Returns the reg dict."""
    reg = await register_test_executor(
        client, enrollment_token=_ENROLL,
        executor_id=executor_id, host_id=host_id,
    )
    hb_body = b'{"health_score": 100, "parts_dir_bytes": 0, "disk_free_gb": 100}'
    r = await client.post(
        f"/api/v1/executors/{executor_id}/heartbeat",
        content=hb_body,
        headers=signed_heartbeat_headers(reg, hb_body),
    )
    assert r.status_code == 200, r.text
    return reg


@pytest.mark.slow
async def test_executor_register_returns_201(client) -> None:
    reg = await register_test_executor(
        client, enrollment_token=_ENROLL,
        executor_id="exec-test-1", host_id="host-test",
    )
    assert reg["executor_id"] == "exec-test-1"
    assert reg["epoch"] == 1
    assert reg["cert_pem"].startswith("-----BEGIN CERTIFICATE-----")
    assert reg["jwt"]
    assert len(reg["hmac_seed"]) == 32


@pytest.mark.slow
async def test_executor_heartbeat_transitions_to_healthy(client) -> None:
    reg = await register_test_executor(
        client, enrollment_token=_ENROLL,
        executor_id="exec-hb-1", host_id="host-hb",
    )
    hb_body = b'{"health_score": 95}'
    r = await client.post(
        "/api/v1/executors/exec-hb-1/heartbeat",
        content=hb_body,
        headers=signed_heartbeat_headers(reg, hb_body),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "healthy"
    assert r.json()["health_score"] == 95


@pytest.mark.slow
async def test_poll_returns_assigned_false_when_no_work(client) -> None:
    reg = await _register_and_heartbeat(client, "exec-poll-empty", "host-pe")
    r = await client.post("/api/v1/executors/exec-poll-empty/poll",
                          headers=executor_request_headers(reg))
    assert r.status_code == 200, r.text
    assert r.json()["assigned"] is False
    assert r.json()["subtask"] is None


@pytest.mark.slow
async def test_poll_returns_subtask_when_work_available(client, auth) -> None:
    await client.post("/api/v1/tasks", json={
        "repo_id": "o/poll", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    reg = await _register_and_heartbeat(client, "exec-poll-w", "host-pw")
    r = await client.post("/api/v1/executors/exec-poll-w/poll",
                          headers=executor_request_headers(reg))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assigned"] is True
    assert body["subtask"] is not None
    assert "id" in body["subtask"]
    assert body["assignment_token"] is not None


@pytest.mark.slow
async def test_unauthenticated_returns_401(client) -> None:
    """No mTLS cert header → /heartbeat is rejected before the handler runs."""
    r = await client.post(
        "/api/v1/executors/whoever/heartbeat",
        content=b'{"health_score": 100}',
    )
    assert r.status_code == 401


@pytest.mark.slow
async def test_register_rejects_bad_enrollment_token(client) -> None:
    """A wrong enrollment token on /register is rejected."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.x509.oid import NameOID
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")]))
        .sign(key, None))
    csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    r = await client.post("/api/v1/executors/register", json={
        "host_id": "y", "executor_id_proposal": "x",
        "capabilities": {}, "client_csr_pem": csr_pem,
    }, headers={"X-Enrollment-Token": "wrong"})
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
    drain = await _register_and_heartbeat(client, "host-x-drain", "host-x")
    for _ in range(20):  # safety upper bound
        dr = await client.post("/api/v1/executors/host-x-drain/poll",
                               headers=executor_request_headers(drain))
        if not dr.json().get("assigned"):
            break

    # Create a task → 1 subtask
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/storage-test",
        "revision": "9" * 40,
        "storage_id": 1,
    }, headers=auth)
    assert r.status_code == 201

    worker = await _register_and_heartbeat(client, "host-x-worker-1", "host-x")
    pr = await client.post("/api/v1/executors/host-x-worker-1/poll",
                           headers=executor_request_headers(worker))
    assert pr.status_code == 200
    body = pr.json()
    assert body["assigned"] is True
    assert body["repo_id"] == "o/storage-test"
    assert body["revision"] == "9" * 40
    assert "storage_config" in body
    assert body["storage_config"]["bucket"]   # default falls back to storage.name


@pytest.mark.slow
async def test_heartbeat_missing_epoch_header_returns_401(
    client: AsyncClient, registered_executor: dict,
) -> None:
    reg = registered_executor
    hb_body = b'{"health_score": 100, "parts_dir_bytes": 0}'
    headers = signed_heartbeat_headers(reg, hb_body)
    del headers["X-Executor-Epoch"]
    r = await client.post(
        f"/api/v1/executors/{reg['executor_id']}/heartbeat",
        content=hb_body, headers=headers,
    )
    assert r.status_code == 401


@pytest.mark.slow
async def test_heartbeat_wrong_epoch_returns_EPOCH_MISMATCH(
    client: AsyncClient, registered_executor: dict,
) -> None:
    reg = registered_executor
    hb_body = b'{"health_score": 100, "parts_dir_bytes": 0}'
    headers = signed_heartbeat_headers(reg, hb_body)
    headers["X-Executor-Epoch"] = str(reg["epoch"] + 1)
    r = await client.post(
        f"/api/v1/executors/{reg['executor_id']}/heartbeat",
        content=hb_body, headers=headers,
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "EPOCH_MISMATCH"


@pytest.mark.slow
async def test_heartbeat_correct_epoch_passes(
    client: AsyncClient, registered_executor: dict,
) -> None:
    reg = registered_executor
    hb_body = b'{"health_score": 100, "parts_dir_bytes": 0}'
    r = await client.post(
        f"/api/v1/executors/{reg['executor_id']}/heartbeat",
        content=hb_body, headers=signed_heartbeat_headers(reg, hb_body),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


@pytest.mark.slow
async def test_poll_after_reregister_uses_new_epoch(client: AsyncClient) -> None:
    """Re-register the same executor_id: poll with the old epoch fails, new passes.

    W3a: re-register bumps epoch AND issues a fresh cert. The old cert's
    fingerprint is overwritten on the executor row, so polling with the new
    cert + old epoch exercises the W1 fence under the new auth chain."""
    reg1 = await register_test_executor(
        client, enrollment_token=_ENROLL,
        executor_id="rejoin-host-worker-1", host_id="rejoin-host",
    )
    reg2 = await register_test_executor(
        client, enrollment_token=_ENROLL,
        executor_id="rejoin-host-worker-1", host_id="rejoin-host",
    )
    assert reg2["epoch"] == reg1["epoch"] + 1

    # New cert + OLD epoch → EPOCH_MISMATCH.
    stale = dict(reg2)
    stale["epoch"] = reg1["epoch"]
    r3 = await client.post(
        "/api/v1/executors/rejoin-host-worker-1/poll",
        headers=executor_request_headers(stale),
    )
    assert r3.status_code == 401

    # New cert + new epoch → accepted.
    r4 = await client.post(
        "/api/v1/executors/rejoin-host-worker-1/poll",
        headers=executor_request_headers(reg2),
    )
    assert r4.status_code == 200
