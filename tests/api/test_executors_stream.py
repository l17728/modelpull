"""Tests for GET /api/v1/executors/stream (UI-SP5b SSE)."""
from __future__ import annotations

import asyncio
import datetime as dt
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
def _set_env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_EXECUTORS_STREAM_INTERVAL_SECONDS", TICK)
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


async def _seed(engine):
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
async def test_executors_stream_unauthenticated_401(
    client: AsyncClient,
) -> None:
    async with client.stream(
        "GET", "/api/v1/executors/stream",
    ) as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_executors_stream_tenant_filter(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed(engine)
    received = await _collect(
        client, "/api/v1/executors/stream?max_ticks=1", auth,
        count=1, timeout=3.0)
    body = json.loads(received[0])
    ids = {it["id"] for it in body["items"]}
    assert "t1-w1" in ids
    assert "shared-1" in ids
    assert "t2-w1" not in ids


@pytest.mark.slow
async def test_executors_stream_multi_snapshot(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed(engine)
    received = await _collect(
        client, "/api/v1/executors/stream?max_ticks=2", auth,
        count=2, timeout=3.0)
    assert len(received) >= 2
    for raw in received[:2]:
        body = json.loads(raw)
        assert "items" in body
        assert isinstance(body["items"], list)


@pytest.mark.slow
async def test_executors_stream_status_filter(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed(engine)
    received = await _collect(
        client, "/api/v1/executors/stream?status=healthy&max_ticks=1",
        auth, count=1, timeout=3.0)
    body = json.loads(received[0])
    assert all(it["status"] == "healthy" for it in body["items"])
    assert {it["id"] for it in body["items"]} >= {"t1-w1", "shared-1"}
