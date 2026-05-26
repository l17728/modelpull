"""PUT /api/v1/tenants/{id}/quota REST endpoint tests."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=55, slug="t55", display_name="T55",
                     quota_bytes_month=100, quota_concurrent=5,
                     quota_storage_gb=10, quota_ai_tokens_month=500))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


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


def _token(role: str = "system_admin", user_id: int = 1, tenant_id: int = 55) -> str:
    from dlw.auth.principal import issue_system_jwt
    s = get_settings()
    return issue_system_jwt(secret=s.system_jwt_secret, user_id=user_id,
                             tenant_id=tenant_id, role=role, project_ids=[])


async def test_put_quota_system_admin_succeeds(client):
    r = await client.put("/api/v1/tenants/55/quota",
                          json={"quota_concurrent": 25,
                                "quota_storage_gb": 200},
                          headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
    body = r.json()
    assert body["quota_concurrent"] == 25
    assert body["quota_storage_gb"] == 200


async def test_put_quota_non_admin_rejected(client):
    r = await client.put("/api/v1/tenants/55/quota",
                          json={"quota_concurrent": 1},
                          headers={"Authorization": f"Bearer {_token(role='tenant_admin')}"})
    assert r.status_code == 403


async def test_put_quota_unauthenticated_returns_401(client):
    r = await client.put("/api/v1/tenants/55/quota", json={"quota_concurrent": 1})
    assert r.status_code == 401


async def test_put_quota_nonexistent_tenant_404(client):
    r = await client.put("/api/v1/tenants/9999/quota",
                          json={"quota_concurrent": 1},
                          headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 404


async def test_put_quota_negative_value_422(client):
    r = await client.put("/api/v1/tenants/55/quota",
                          json={"quota_concurrent": -1},
                          headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_QUOTA"


# ---------------------------------------------------------------------------
# v2.1 SP1 — PUT /tenants/{id}/sla
# ---------------------------------------------------------------------------

async def test_put_sla_tier_system_admin_succeeds(client):
    r = await client.put("/api/v1/tenants/55/sla",
                          json={"sla_tier": "critical"},
                          headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
    assert r.json()["sla_tier"] == "critical"


async def test_put_sla_tier_non_admin_rejected(client):
    r = await client.put("/api/v1/tenants/55/sla",
                          json={"sla_tier": "bulk"},
                          headers={"Authorization": f"Bearer {_token(role='tenant_admin')}"})
    assert r.status_code == 403


async def test_put_sla_tier_invalid_value_422(client):
    r = await client.put("/api/v1/tenants/55/sla",
                          json={"sla_tier": "platinum"},
                          headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_TIER"


async def test_put_sla_tier_missing_field_422(client):
    r = await client.put("/api/v1/tenants/55/sla",
                          json={},
                          headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "MISSING_FIELD"


async def test_put_sla_tier_nonexistent_tenant_404(client):
    r = await client.put("/api/v1/tenants/99999/sla",
                          json={"sla_tier": "standard"},
                          headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 404
