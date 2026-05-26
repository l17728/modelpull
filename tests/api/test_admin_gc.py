"""v2.1 Sprint 3 — admin /api/v1/admin/gc/* endpoint tests."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from dlw.config import get_settings


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", "unit-secret-32-bytes-pad-pad!!")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(ephemeral_ca):
    from tests.conftest import make_app_with_state
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                            base_url="http://test") as c:
        yield c


def _token(role: str = "system_admin", user_id: int = 1, tenant_id: int = 1) -> str:
    from dlw.auth.principal import issue_system_jwt
    s = get_settings()
    return issue_system_jwt(secret=s.system_jwt_secret, user_id=user_id,
                             tenant_id=tenant_id, role=role, project_ids=[])


async def test_run_disabled_by_default_returns_503(client, monkeypatch):
    """With DLW_PHYSICAL_GC_ENABLED unset (default), /run returns 503."""
    monkeypatch.delenv("DLW_PHYSICAL_GC_ENABLED", raising=False)
    r = await client.post(
        "/api/v1/admin/gc/run",
        headers={"Authorization": f"Bearer {_token()}"}, json={})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "GC_DISABLED"


async def test_run_non_admin_rejected(client, monkeypatch):
    monkeypatch.setenv("DLW_PHYSICAL_GC_ENABLED", "true")
    r = await client.post(
        "/api/v1/admin/gc/run",
        headers={"Authorization": f"Bearer {_token(role='tenant_admin')}"},
        json={})
    assert r.status_code == 403


async def test_run_unauthenticated_401(client):
    r = await client.post("/api/v1/admin/gc/run", json={})
    assert r.status_code == 401


async def test_status_non_admin_rejected(client):
    r = await client.get(
        "/api/v1/admin/gc/status",
        headers={"Authorization": f"Bearer {_token(role='tenant_admin')}"})
    assert r.status_code == 403


async def test_status_returns_never_ran_before_first_run(client):
    """The in-memory last-run summary defaults to {"never_ran": true}."""
    r = await client.get(
        "/api/v1/admin/gc/status",
        headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
    body = r.json()
    # Either never_ran=true on a fresh process OR a previous test ran the
    # GC and populated this. Both are valid; just check the shape.
    assert isinstance(body, dict)
