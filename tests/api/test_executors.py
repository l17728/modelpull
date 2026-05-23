"""Tests for executors API: register / heartbeat / poll under mTLS + JWT + HMAC (W3a)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

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


# ── FU3: heartbeat confirm + dispatch tests ──────────────────────────────────

@pytest.mark.slow
async def test_heartbeat_confirm_deletes_scoped_rows(
    client: AsyncClient, engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reclaimed_key_ids=[K, K2]: K (owned by ex) deleted; K2 (other executor) survives."""
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.storage_object import StoragePhysicalKey

    # Register an executor
    reg = await register_test_executor(
        client, enrollment_token=_ENROLL,
        executor_id="ex-confirm-1", host_id="host-confirm",
    )
    ex_id = reg["executor_id"]

    # Seed rows via the test DB
    factory = async_sessionmaker(engine, expire_on_commit=False)
    old_time = datetime.now(UTC) - timedelta(hours=2)
    async with factory() as s:
        # StorageBackend id=1 already seeded by _bootstrap
        k_row = StoragePhysicalKey(
            tenant_id=1, storage_id=1,
            sha256="a" * 64, storage_key="models/confirm/file1.bin",
            size=100, executor_id=ex_id, created_at=old_time,
            last_referenced_at=old_time,
        )
        k2_row = StoragePhysicalKey(
            tenant_id=1, storage_id=1,
            sha256="b" * 64, storage_key="models/confirm/file2.bin",
            size=200, executor_id="other-executor-999", created_at=old_time,
            last_referenced_at=old_time,
        )
        s.add(k_row)
        s.add(k2_row)
        await s.commit()
        k_id = k_row.id
        k2_id = k2_row.id

    # Heartbeat confirming both IDs
    hb_body = json.dumps({
        "health_score": 100, "parts_dir_bytes": 0,
        "reclaimed_key_ids": [k_id, k2_id],
    }).encode()
    r = await client.post(
        f"/api/v1/executors/{ex_id}/heartbeat",
        content=hb_body,
        headers=signed_heartbeat_headers(reg, hb_body),
    )
    assert r.status_code == 200, r.text

    # Verify: K deleted (scoped to ex_id), K2 survives (other executor)
    from sqlalchemy import select
    async with factory() as s:
        k_exists = await s.scalar(
            select(StoragePhysicalKey.id).where(StoragePhysicalKey.id == k_id))
        k2_exists = await s.scalar(
            select(StoragePhysicalKey.id).where(StoragePhysicalKey.id == k2_id))
    assert k_exists is None, "K should have been deleted (owned by ex)"
    assert k2_exists is not None, "K2 should survive (owned by other executor)"

    # Cleanup K2
    async with factory() as s:
        row = await s.get(StoragePhysicalKey, k2_id)
        if row:
            await s.delete(row)
            await s.commit()


@pytest.mark.slow
async def test_heartbeat_dispatch_enabled_returns_reclaim_item(
    client: AsyncClient, engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With gc_delete_physical_bytes=true + local orphan → response reclaim has item."""
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.storage_object import StoragePhysicalKey

    # Register executor
    reg = await register_test_executor(
        client, enrollment_token=_ENROLL,
        executor_id="ex-dispatch-1", host_id="host-dispatch",
    )
    ex_id = reg["executor_id"]

    # Seed a local StorageBackend with base_path in config.
    # Use explicit id=9001 to avoid sequence collision with seeded id=1 backend.
    base_path = "/tmp/local-store"
    cfg_bytes = json.dumps({
        "bucket": "local-bucket", "base_path": base_path,
        "backend_type": "local",
    }).encode()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    local_storage_id = 9001
    async with factory() as s:
        local_backend = StorageBackend(
            id=local_storage_id, tenant_id=1, name=f"local-dispatch-{ex_id}",
            backend_type="local", config_encrypted=cfg_bytes,
        )
        s.add(local_backend)
        # Seed an old local orphan (no matching storage_object row → orphan)
        old_time = datetime.now(UTC) - timedelta(hours=2)
        phys = StoragePhysicalKey(
            tenant_id=1, storage_id=local_storage_id,
            sha256="c" * 64, storage_key="models/dispatch/fileX.bin",
            size=512, executor_id=ex_id, created_at=old_time,
            last_referenced_at=old_time,
        )
        s.add(phys)
        await s.flush()
        phys_id = phys.id
        await s.commit()

    # Enable GC dispatch via env var (mirroring _set_env / test_ai_chat pattern).
    # Set both grace_seconds and archive_after_days to 0 so the cutoff is "now"
    # and our 2-hour-old row is past grace.
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_GC_DELETE_PHYSICAL_BYTES", "true")
    monkeypatch.setenv("DLW_GC_GRACE_SECONDS", "0")
    monkeypatch.setenv("DLW_GC_ARCHIVE_AFTER_DAYS", "0")
    get_settings.cache_clear()

    hb_body = b'{"health_score": 100, "parts_dir_bytes": 0}'
    r = await client.post(
        f"/api/v1/executors/{ex_id}/heartbeat",
        content=hb_body,
        headers=signed_heartbeat_headers(reg, hb_body),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "reclaim" in body
    assert len(body["reclaim"]) == 1
    item = body["reclaim"][0]
    assert item["id"] == phys_id
    assert item["base_path"] == base_path
    assert item["storage_key"] == "models/dispatch/fileX.bin"

    # Cleanup
    get_settings.cache_clear()
    async with factory() as s:
        row = await s.get(StoragePhysicalKey, phys_id)
        if row:
            await s.delete(row)
            await s.commit()


@pytest.mark.slow
async def test_heartbeat_dispatch_disabled_returns_empty_reclaim(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With gc_delete_physical_bytes=false (default) → reclaim == []."""
    reg = await register_test_executor(
        client, enrollment_token=_ENROLL,
        executor_id="ex-nodispatch-1", host_id="host-nodispatch",
    )
    ex_id = reg["executor_id"]

    # Ensure GC is disabled (default)
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_GC_DELETE_PHYSICAL_BYTES", "false")
    get_settings.cache_clear()

    hb_body = b'{"health_score": 100, "parts_dir_bytes": 0}'
    r = await client.post(
        f"/api/v1/executors/{ex_id}/heartbeat",
        content=hb_body,
        headers=signed_heartbeat_headers(reg, hb_body),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("reclaim") == []

    get_settings.cache_clear()


# ── FU4: accessible_base_paths stored on capabilities ─────────────────────────

@pytest.mark.slow
async def test_heartbeat_stores_accessible_base_paths(
    client: AsyncClient, engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heartbeat with accessible_base_paths=["/srv/dlw"] stores it on
    ex.capabilities["base_paths"]; a subsequent heartbeat omitting the field
    leaves capabilities unchanged (None = no update)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from dlw.db.models.executor import Executor

    reg = await register_test_executor(
        client, enrollment_token=_ENROLL,
        executor_id="ex-basepaths-1", host_id="host-basepaths",
    )
    ex_id = reg["executor_id"]

    # Heartbeat WITH accessible_base_paths
    hb_body = json.dumps({
        "health_score": 100, "parts_dir_bytes": 0,
        "accessible_base_paths": ["/srv/dlw"],
    }).encode()
    r = await client.post(
        f"/api/v1/executors/{ex_id}/heartbeat",
        content=hb_body,
        headers=signed_heartbeat_headers(reg, hb_body),
    )
    assert r.status_code == 200, r.text

    # Verify stored in capabilities
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        ex = await s.get(Executor, ex_id)
        assert ex is not None
        assert ex.capabilities.get("base_paths") == ["/srv/dlw"]

    # Heartbeat WITHOUT accessible_base_paths → capabilities unchanged
    hb_body2 = json.dumps({
        "health_score": 95, "parts_dir_bytes": 0,
    }).encode()
    r2 = await client.post(
        f"/api/v1/executors/{ex_id}/heartbeat",
        content=hb_body2,
        headers=signed_heartbeat_headers(reg, hb_body2),
    )
    assert r2.status_code == 200, r2.text

    async with factory() as s:
        ex = await s.get(Executor, ex_id)
        assert ex is not None
        # Still the same base_paths from the first heartbeat
        assert ex.capabilities.get("base_paths") == ["/srv/dlw"]


# ── FU9: heartbeat adopts NULL-executor_id keys on accessible backends ────────

@pytest.mark.slow
async def test_heartbeat_adopts_orphan_local_keys(
    client: AsyncClient, engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heartbeat with accessible_base_paths → NULL-executor_id keys on that
    backend get executor_id backfilled (FU9 adoption)."""
    from sqlalchemy import select

    from dlw.db.models.executor import Executor
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.storage_object import StoragePhysicalKey

    reg = await register_test_executor(
        client, enrollment_token=_ENROLL,
        executor_id="ex-adopt-1", host_id="host-adopt",
    )
    ex_id = reg["executor_id"]

    base_path = "/tmp/fu9-adopt-store"
    cfg_bytes = json.dumps({
        "bucket": "local-bucket", "base_path": base_path,
        "backend_type": "local",
    }).encode()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    local_storage_id = 9002  # avoids collision with 9001 used by dispatch test
    async with factory() as s:
        # FU9 guard requires executor.tenant_id is not None — set it on the row.
        ex_row = await s.get(Executor, ex_id)
        ex_row.tenant_id = 1
        s.add(StorageBackend(
            id=local_storage_id, tenant_id=1, name=f"local-adopt-{ex_id}",
            backend_type="local", config_encrypted=cfg_bytes,
        ))
        # Seed a NULL-executor_id key on this local backend
        s.add(StoragePhysicalKey(
            tenant_id=1, storage_id=local_storage_id,
            sha256="fa" * 32, storage_key="models/adopt/file.bin",
            size=256, executor_id=None,
        ))
        await s.commit()

    hb_body = json.dumps({
        "health_score": 100, "parts_dir_bytes": 0,
        "accessible_base_paths": [base_path],
    }).encode()
    r = await client.post(
        f"/api/v1/executors/{ex_id}/heartbeat",
        content=hb_body,
        headers=signed_heartbeat_headers(reg, hb_body),
    )
    assert r.status_code == 200, r.text

    # Verify the NULL key now has executor_id = ex_id
    async with factory() as s:
        row = (await s.execute(
            select(StoragePhysicalKey)
            .where(StoragePhysicalKey.storage_key == "models/adopt/file.bin")
        )).scalar_one()
        assert row.executor_id == ex_id

    # Cleanup
    async with factory() as s:
        row = (await s.execute(
            select(StoragePhysicalKey)
            .where(StoragePhysicalKey.storage_key == "models/adopt/file.bin")
        )).scalar_one_or_none()
        if row:
            await s.delete(row)
        backend = await s.get(StorageBackend, local_storage_id)
        if backend:
            await s.delete(backend)
        await s.commit()
