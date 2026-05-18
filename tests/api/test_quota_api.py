"""GET /api/v1/quota/current (Phase 3 SP1)."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import make_app_with_state, principal_headers

SECRET = "unit-secret"
pytestmark = pytest.mark.slow


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.tenant import Project, Tenant, User
    from dlw.db.models.usage import QuotaSnapshot
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t", display_name="T",
                     quota_bytes_month=1000, quota_concurrent=5,
                     quota_storage_gb=10))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="u@t",
                        role="tenant_viewer"),
                   QuotaSnapshot(tenant_id=1, bytes_used_month=42)])
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def test_quota_current_shape(client):
    r = await client.get("/api/v1/quota/current",
                         headers=principal_headers(secret=SECRET,
                                                   role="tenant_viewer"))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["tenant_id"] == 1
    assert b["bytes_used_month"] == 42
    assert b["bytes_quota_month"] == 1000
    assert b["concurrent_quota"] == 5


async def test_quota_current_unauth_401(client):
    assert (await client.get("/api/v1/quota/current")).status_code == 401
