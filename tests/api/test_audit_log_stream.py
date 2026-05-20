"""Tests for GET /api/v1/audit/log/stream (UI-SP5d SSE)."""
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
    monkeypatch.setenv("DLW_AUDIT_STREAM_INTERVAL_SECONDS", TICK)
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


async def _seed_audit(engine, *, tenant_id: int, n: int,
                       action: str = "task.note",
                       base: dt.datetime | None = None):
    from dlw.db.models.audit import AuditLog
    base = base or dt.datetime(2026, 5, 20, 12, 0, 0, tzinfo=dt.UTC)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        for i in range(n):
            s.add(AuditLog(
                occurred_at=base + dt.timedelta(seconds=i),
                tenant_id=tenant_id, actor_user_id=1, action=action,
                resource_type="task", resource_id=f"r{i}",
                outcome="success", payload={"i": i}, self_hash="0" * 64))
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
async def test_audit_stream_unauthenticated_401(client: AsyncClient) -> None:
    async with client.stream("GET", "/api/v1/audit/log/stream") as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_audit_stream_tenant_isolation(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed_audit(engine, tenant_id=2, n=2,
                       base=dt.datetime(2026, 5, 20, 9, 0, tzinfo=dt.UTC))
    await _seed_audit(engine, tenant_id=1, n=3,
                       base=dt.datetime(2026, 5, 20, 10, 0, tzinfo=dt.UTC))
    received = await _collect(
        client, "/api/v1/audit/log/stream?max_ticks=1", auth,
        count=1, timeout=3.0)
    body = json.loads(received[0])
    assert all(it["tenant_id"] == 1 for it in body["items"])
    assert len(body["items"]) == 3


@pytest.mark.slow
async def test_audit_stream_multi_snapshot(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed_audit(engine, tenant_id=1, n=2,
                       base=dt.datetime(2026, 5, 20, 11, 0, tzinfo=dt.UTC))
    received = await _collect(
        client, "/api/v1/audit/log/stream?max_ticks=2", auth,
        count=2, timeout=3.0)
    assert len(received) >= 2
    for raw in received[:2]:
        body = json.loads(raw)
        assert "items" in body
        assert "next_cursor" in body
        assert isinstance(body["items"], list)


@pytest.mark.slow
async def test_audit_stream_action_filter(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed_audit(engine, tenant_id=1, n=2, action="task.created",
                       base=dt.datetime(2026, 5, 20, 13, 0, tzinfo=dt.UTC))
    await _seed_audit(engine, tenant_id=1, n=2, action="task.cancelled",
                       base=dt.datetime(2026, 5, 20, 13, 30, tzinfo=dt.UTC))
    received = await _collect(
        client,
        "/api/v1/audit/log/stream?action=task.created&max_ticks=1",
        auth, count=1, timeout=3.0)
    body = json.loads(received[0])
    assert body["items"], "expected at least one matching entry"
    assert all(it["action"].startswith("task.created")
               for it in body["items"])
