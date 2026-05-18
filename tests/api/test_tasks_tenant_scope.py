"""tasks API is principal-scoped + tenant-filtered (Phase 3 SP1)."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.principal import issue_system_jwt
from dlw.config import get_settings
from dlw.db.base import Base

SECRET = "unit-secret"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add_all([
            Tenant(id=1, slug="t1", display_name="T1"),
            Tenant(id=2, slug="t2", display_name="T2"),
        ])
        await s.flush()
        s.add_all([
            Project(id=1, tenant_id=1, name="default"),
            Project(id=2, tenant_id=2, name="default"),
            User(id=1, tenant_id=1, oidc_subject="u1", email="u1@t1",
                 role="tenant_operator"),
            User(id=2, tenant_id=2, oidc_subject="u2", email="u2@t2",
                 role="tenant_operator"),
            StorageBackend(id=1, tenant_id=1, name="s1",
                           backend_type="s3", config_encrypted=b""),
            StorageBackend(id=2, tenant_id=2, name="s2",
                           backend_type="s3", config_encrypted=b""),
        ])
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
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


def _hdr(uid, tid):
    tok = issue_system_jwt(secret=SECRET, user_id=uid, tenant_id=tid,
                           role="tenant_operator", project_ids=[])
    return {"Authorization": f"Bearer {tok}"}


@pytest.mark.slow
async def test_create_stamps_principal_tenant(client):
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/r", "revision": "0" * 40, "storage_id": 1,
    }, headers=_hdr(1, 1))
    assert r.status_code == 201, r.text


@pytest.mark.slow
async def test_list_only_returns_own_tenant(client):
    await client.post("/api/v1/tasks", json={
        "repo_id": "o/a", "revision": "1" * 40, "storage_id": 1,
    }, headers=_hdr(1, 1))
    r2 = await client.get("/api/v1/tasks", headers=_hdr(2, 2))
    assert r2.status_code == 200
    assert all(it["repo_id"] != "o/a" for it in r2.json()["items"])


@pytest.mark.slow
async def test_cross_tenant_get_returns_404(client):
    c = await client.post("/api/v1/tasks", json={
        "repo_id": "o/secret", "revision": "2" * 40, "storage_id": 1,
    }, headers=_hdr(1, 1))
    tid = c.json()["id"]
    r = await client.get(f"/api/v1/tasks/{tid}", headers=_hdr(2, 2))
    assert r.status_code == 404


@pytest.mark.slow
async def test_cross_tenant_cancel_returns_404(client):
    c = await client.post("/api/v1/tasks", json={
        "repo_id": "o/c", "revision": "3" * 40, "storage_id": 1,
    }, headers=_hdr(1, 1))
    tid = c.json()["id"]
    r = await client.post(f"/api/v1/tasks/{tid}/cancel", headers=_hdr(2, 2))
    assert r.status_code == 404


@pytest.mark.slow
async def test_unauth_401(client):
    r = await client.get("/api/v1/tasks")
    assert r.status_code == 401
