"""Tests for require_not_recovering dep — 503 barrier on executor-loop endpoints
during controller recovery (Phase 2 W3c, INVARIANT 33)."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base


_ENROLL = "test-enroll-barrier"


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
def _set_env(monkeypatch: pytest.MonkeyPatch):
    from dlw.config import get_settings
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.slow
async def test_heartbeat_503_when_recovering(ephemeral_ca) -> None:
    from tests.conftest import (
        make_app_with_state, register_test_executor, signed_heartbeat_headers,
    )
    app = make_app_with_state(
        ephemeral_ca, enrollment_token=_ENROLL, controller_state="recovering",
    )
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="bar-worker-1", host_id="bar-host",
        )
        hb_body = b'{"health_score": 100, "parts_dir_bytes": 0, "disk_free_gb": 100}'
        r = await c.post("/api/v1/executors/bar-worker-1/heartbeat",
                         content=hb_body,
                         headers=signed_heartbeat_headers(reg, hb_body))
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "CONTROLLER_RECOVERING"


@pytest.mark.slow
async def test_poll_and_report_also_503_when_recovering(ephemeral_ca) -> None:
    from tests.conftest import (
        executor_request_headers, make_app_with_state, register_test_executor,
    )
    app = make_app_with_state(
        ephemeral_ca, enrollment_token=_ENROLL, controller_state="recovering",
    )
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="bar-worker-2", host_id="bar-host",
        )
        # /poll → 503
        r = await c.post("/api/v1/executors/bar-worker-2/poll",
                         headers=executor_request_headers(reg))
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "CONTROLLER_RECOVERING"
        # /report → 503
        r = await c.post(f"/api/v1/subtasks/{uuid.uuid4()}/report", json={
            "status": "succeeded", "actual_sha256": "f" * 64,
            "bytes_downloaded": 0,
        }, headers=executor_request_headers(reg))
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "CONTROLLER_RECOVERING"


@pytest.mark.slow
async def test_heartbeat_passes_when_active(ephemeral_ca) -> None:
    """Sanity: with controller_state='active', the barrier does not fire."""
    from tests.conftest import (
        make_app_with_state, register_test_executor, signed_heartbeat_headers,
    )
    app = make_app_with_state(
        ephemeral_ca, enrollment_token=_ENROLL, controller_state="active",
    )
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="bar-worker-3", host_id="bar-host",
        )
        hb_body = b'{"health_score": 100, "parts_dir_bytes": 0, "disk_free_gb": 100}'
        r = await c.post("/api/v1/executors/bar-worker-3/heartbeat",
                         content=hb_body,
                         headers=signed_heartbeat_headers(reg, hb_body))
    assert r.status_code == 200
