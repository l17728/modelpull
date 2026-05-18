"""auth router tests in dev mode (Phase 3 SP1)."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.tenant import Project, Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="default", display_name="Default"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="default"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch):
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", "unit-secret")
    monkeypatch.setenv(
        "DLW_AUTH_TENANT_RULES_JSON",
        '[{"match":"email_domain","value":"*","tenant_slug":"default",'
        '"role":"tenant_operator"}]')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(ephemeral_ca):
    from tests.conftest import make_app_with_state
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


@pytest.mark.slow
async def test_login_dev_redirects_to_callback(client):
    r = await client.get("/api/v1/auth/login", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/api/v1/auth/callback" in r.headers["location"]


@pytest.mark.slow
async def test_callback_dev_issues_system_jwt(client):
    login = await client.get("/api/v1/auth/login", follow_redirects=False)
    loc = login.headers["location"]
    r = await client.get(loc, follow_redirects=False)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == 1
    assert body["role"] == "tenant_operator"
    assert body["system_jwt"]
    me = await client.get("/api/v1/auth/me",
                          headers={"Authorization": f"Bearer {body['system_jwt']}"})
    assert me.status_code == 200
    assert me.json()["tenant_id"] == 1


@pytest.mark.slow
async def test_callback_unresolved_tenant_403(client, monkeypatch):
    monkeypatch.setenv("DLW_AUTH_TENANT_RULES_JSON", "[]")
    get_settings.cache_clear()
    login = await client.get("/api/v1/auth/login", follow_redirects=False)
    r = await client.get(login.headers["location"], follow_redirects=False)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "TENANT_UNRESOLVED"
