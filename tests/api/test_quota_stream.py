"""Tests for GET /api/v1/quota/current/stream (UI-SP5e SSE)."""
from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import make_app_with_state, principal_headers

SECRET = "unit-secret"
TICK = "0.1"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    from dlw.db.models.usage import QuotaSnapshot
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1",
                     quota_bytes_month=1000, quota_concurrent=5,
                     quota_storage_gb=10))
        s.add(Tenant(id=2, slug="t2", display_name="T2",
                     quota_bytes_month=2000, quota_concurrent=7,
                     quota_storage_gb=20))
        await s.flush()
        s.add_all([
            Project(id=1, tenant_id=1, name="d1"),
            Project(id=2, tenant_id=2, name="d2"),
            User(id=1, tenant_id=1, oidc_subject="u1",
                 email="u1@t", role="tenant_admin"),
            User(id=2, tenant_id=2, oidc_subject="u2",
                 email="u2@t", role="tenant_admin"),
            StorageBackend(id=1, tenant_id=1, name="s1",
                           backend_type="s3", config_encrypted=b""),
            StorageBackend(id=2, tenant_id=2, name="s2",
                           backend_type="s3", config_encrypted=b""),
            QuotaSnapshot(tenant_id=1, bytes_used_month=111,
                          storage_gb_used=1, concurrent_tasks=1),
            QuotaSnapshot(tenant_id=2, bytes_used_month=222,
                          storage_gb_used=2, concurrent_tasks=2),
        ])
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_QUOTA_STREAM_INTERVAL_SECONDS", TICK)
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
                           base_url="http://test", timeout=10.0) as c:
        yield c


async def _collect(client, url, headers, *, count, timeout=4.0):
    received: list[str] = []
    async with asyncio.timeout(timeout):
        async with client.stream("GET", url, headers=headers) as resp:
            assert resp.status_code == 200, await resp.aread()
            ctype = resp.headers.get("content-type", "")
            assert "text/event-stream" in ctype, ctype
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    received.append(line[len("data: "):])
                    if len(received) >= count:
                        return received
    return received


@pytest.mark.slow
async def test_quota_stream_unauthenticated_401(client: AsyncClient) -> None:
    async with client.stream("GET", "/api/v1/quota/current/stream") as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_quota_stream_tenant_isolation(
    client: AsyncClient, auth,
) -> None:
    received = await _collect(
        client, "/api/v1/quota/current/stream?max_ticks=1", auth,
        count=1, timeout=3.0)
    body = json.loads(received[0])
    assert body["tenant_id"] == 1
    assert body["bytes_used_month"] == 111
    assert body["bytes_quota_month"] == 1000
    assert body["storage_gb_used"] == 1
    assert body["storage_gb_quota"] == 10
    assert body["concurrent_tasks"] == 1
    assert body["concurrent_quota"] == 5


@pytest.mark.slow
async def test_quota_stream_multi_snapshot(
    client: AsyncClient, auth,
) -> None:
    received = await _collect(
        client, "/api/v1/quota/current/stream?max_ticks=2", auth,
        count=2, timeout=3.0)
    assert len(received) >= 2
    expected_keys = {
        "tenant_id", "bytes_used_month", "bytes_quota_month",
        "storage_gb_used", "storage_gb_quota",
        "concurrent_tasks", "concurrent_quota",
        "sla_tier",                      # v2.1 SP1 — added to snapshot
    }
    for raw in received[:2]:
        body = json.loads(raw)
        assert set(body.keys()) == expected_keys
