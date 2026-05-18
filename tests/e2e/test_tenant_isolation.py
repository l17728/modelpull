"""E2E-MT-*: cross-tenant isolation + quota isolation + service token."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import (
    make_app_with_state,
    principal_headers,
    service_headers,
)

SECRET = "unit-secret"
SVC = "svc-tok"
pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    from dlw.db.models.usage import QuotaSnapshot
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add_all([
            Tenant(id=1, slug="a", display_name="A", quota_concurrent=1),
            Tenant(id=2, slug="b", display_name="B", quota_concurrent=50),
        ])
        await s.flush()
        s.add_all([
            Project(id=1, tenant_id=1, name="d"),
            Project(id=2, tenant_id=2, name="d"),
            User(id=1, tenant_id=1, oidc_subject="a1", email="a@a",
                 role="tenant_operator"),
            User(id=2, tenant_id=2, oidc_subject="b1", email="b@b",
                 role="tenant_operator"),
            StorageBackend(id=1, tenant_id=1, name="s1",
                           backend_type="s3", config_encrypted=b""),
            StorageBackend(id=2, tenant_id=2, name="s2",
                           backend_type="s3", config_encrypted=b""),
            QuotaSnapshot(tenant_id=1), QuotaSnapshot(tenant_id=2),
        ])
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_SYSTEM_ADMIN_TOKEN", SVC)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


def _a():
    return principal_headers(secret=SECRET, user_id=1, tenant_id=1)


def _b():
    return principal_headers(secret=SECRET, user_id=2, tenant_id=2)


async def test_tenant_b_cannot_see_tenant_a_task(client):
    c = await client.post("/api/v1/tasks", json={
        "repo_id": "o/a-secret", "revision": "0" * 40, "storage_id": 1,
    }, headers=_a())
    assert c.status_code == 201, c.text
    tid = c.json()["id"]
    assert (await client.get(f"/api/v1/tasks/{tid}",
                             headers=_b())).status_code == 404
    lst = await client.get("/api/v1/tasks", headers=_b())
    assert all(i["repo_id"] != "o/a-secret" for i in lst.json()["items"])


async def test_tenant_a_quota_exhaustion_does_not_block_b(client, engine):
    # tenant A quota_concurrent=1 — first task ok, second 429
    r1 = await client.post("/api/v1/tasks", json={
        "repo_id": "o/a1", "revision": "1" * 40, "storage_id": 1,
    }, headers=_a())
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/tasks", json={
        "repo_id": "o/a2", "revision": "2" * 40, "storage_id": 1,
    }, headers=_a())
    assert r2.status_code == 429
    assert r2.json()["detail"]["code"] == "QUOTA_EXCEEDED"
    # spec §7 / doc 04 §9.2: the 429 must be audited as quota.exceeded
    from sqlalchemy import select

    from dlw.db.models.audit import AuditLog
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        rows = (await s.execute(
            select(AuditLog).where(AuditLog.action == "quota.exceeded",
                                   AuditLog.tenant_id == 1))).scalars().all()
    assert len(rows) >= 1
    # tenant B (quota 50) unaffected
    rb = await client.post("/api/v1/tasks", json={
        "repo_id": "o/b1", "revision": "3" * 40, "storage_id": 2,
    }, headers=_b())
    assert rb.status_code == 201, rb.text


async def test_service_token_acts_as_tenant_1(client):
    r = await client.get("/api/v1/auth/me", headers=service_headers(SVC))
    assert r.status_code == 200
    assert r.json()["tenant_id"] == 1 and r.json()["is_service"] is True
