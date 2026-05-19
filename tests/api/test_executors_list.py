"""Tests for GET /api/v1/executors (UI-SP3)."""
from __future__ import annotations

import datetime as dt

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import make_app_with_state, principal_headers

SECRET = "unit-secret"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Tenant(id=1, slug="default", display_name="Default"))
        session.add(Tenant(id=2, slug="other", display_name="Other"))
        await session.flush()
        session.add(Project(id=1, tenant_id=1, name="default"))
        session.add(User(id=1, tenant_id=1, oidc_subject="dev",
                         email="d@l", role="tenant_admin"))
        session.add(StorageBackend(id=1, tenant_id=1, name="default",
                                   backend_type="s3", config_encrypted=b""))
        await session.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def auth() -> dict[str, str]:
    return principal_headers(secret=SECRET, role="tenant_admin")


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def _seed_executors(engine):
    from sqlalchemy import text
    from dlw.db.models.executor import Executor
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(text("DELETE FROM executors"))
        s.add(Executor(id="t1-w1", host_id="host-a", cert_fingerprint="fp1",
                       status="healthy", epoch=1, health_score=95,
                       tenant_id=1, nic_speed_gbps=10,
                       disk_free_gb=500, disk_total_gb=1000,
                       last_heartbeat_at=dt.datetime(
                           2026, 5, 20, 12, 0, tzinfo=dt.UTC)))
        s.add(Executor(id="t2-w1", host_id="host-b", cert_fingerprint="fp2",
                       status="degraded", epoch=2, health_score=60,
                       tenant_id=2))
        s.add(Executor(id="shared-1", host_id="host-c", cert_fingerprint="fp3",
                       status="healthy", epoch=1, health_score=100,
                       tenant_id=None))
        await s.commit()


@pytest.mark.slow
async def test_executors_unauthenticated_401(client: AsyncClient) -> None:
    r = await client.get("/api/v1/executors")
    assert r.status_code == 401


@pytest.mark.slow
async def test_executors_tenant_filter(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed_executors(engine)
    r = await client.get("/api/v1/executors", headers=auth)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    ids = {it["id"] for it in items}
    assert "t1-w1" in ids
    assert "shared-1" in ids
    assert "t2-w1" not in ids


@pytest.mark.slow
async def test_executors_status_filter(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed_executors(engine)
    r = await client.get("/api/v1/executors?status=healthy", headers=auth)
    items = r.json()["items"]
    assert all(it["status"] == "healthy" for it in items)
    assert {it["id"] for it in items} >= {"t1-w1", "shared-1"}


@pytest.mark.slow
async def test_executors_system_admin_sees_all(
    client: AsyncClient, engine,
) -> None:
    await _seed_executors(engine)
    admin = principal_headers(secret=SECRET, role="system_admin",
                              user_id=0, tenant_id=1)
    r = await client.get("/api/v1/executors", headers=admin)
    items = r.json()["items"]
    ids = {it["id"] for it in items}
    assert {"t1-w1", "t2-w1", "shared-1"} <= ids
