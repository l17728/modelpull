"""E2E happy path: a mock executor completes a full task via HTTP only.

This test does NOT import dlw services or models. It drives the controller
exclusively through its public HTTP API, exactly like a real executor would.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import principal_headers

SECRET = "unit-secret"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """Create tables + minimal seed (tenant + project + user + storage)."""
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


_ENROLL = "e2e-enrollment-token-happy-path"


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.slow
async def test_full_task_lifecycle_via_http(ephemeral_ca) -> None:
    from tests.conftest import (
        executor_request_headers,
        make_app_with_state,
        register_test_executor,
        signed_heartbeat_headers,
    )
    app = make_app_with_state(ephemeral_ca, enrollment_token=_ENROLL)
    auth = principal_headers(secret=SECRET, role="tenant_admin")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # 1. Create task
        r = await c.post("/api/v1/tasks", json={
            "repo_id": "deepseek-ai/DeepSeek-V3",
            "revision": "abc123def4567890" * 2 + "abc12345",
            "storage_id": 1,
            "priority": 3,
        }, headers=auth)
        assert r.status_code == 201, r.text
        task_id = r.json()["id"]
        assert r.json()["status"] == "pending"

        # 2. Register a worker executor via mTLS enrollment (P2-W3a).
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="e2e-worker-1", host_id="e2e-host",
        )

        # 3. Heartbeat (executor reports liveness) — mTLS + JWT + HMAC.
        hb_body = b'{"health_score": 100, "parts_dir_bytes": 0, "disk_free_gb": 100}'
        r = await c.post("/api/v1/executors/e2e-worker-1/heartbeat",
                         content=hb_body,
                         headers=signed_heartbeat_headers(reg, hb_body))
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

        # 4. Poll twice (2 mock subtasks per task)
        sub_ids: list[str] = []
        tokens: list[str] = []
        for _ in range(2):
            r = await c.post("/api/v1/executors/e2e-worker-1/poll",
                             headers=executor_request_headers(reg))
            assert r.status_code == 200, r.text
            assert r.json()["assigned"] is True
            sub_ids.append(r.json()["subtask"]["id"])
            tokens.append(r.json()["assignment_token"])
        assert len(set(sub_ids)) == 2

        # 5. Third poll → no work
        r = await c.post("/api/v1/executors/e2e-worker-1/poll",
                         headers=executor_request_headers(reg))
        assert r.json()["assigned"] is False

        # 6. Report success for both subtasks (with token + epoch verification)
        for sid, tok in zip(sub_ids, tokens, strict=True):
            r = await c.post(f"/api/v1/subtasks/{sid}/report", json={
                "status": "succeeded",
                "assignment_token": tok,
                "actual_sha256": "f" * 64,
                "bytes_downloaded": 100_000_000,
            }, headers=executor_request_headers(reg))
            assert r.status_code == 200, r.text

        # 7. Task should now be succeeded
        r = await c.get(f"/api/v1/tasks/{task_id}", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "succeeded", body
        assert body["completed_at"] is not None
        assert body["error_message"] is None
