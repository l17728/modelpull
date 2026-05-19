"""Tests for GET /api/v1/audit/log (UI-SP3, audit-derived)."""
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


async def _seed(engine, *, tenant_id: int, n: int, action: str = "task.note",
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


@pytest.mark.slow
async def test_audit_unauthenticated_401(client: AsyncClient) -> None:
    r = await client.get("/api/v1/audit/log")
    assert r.status_code == 401


@pytest.mark.slow
async def test_audit_tenant_isolation(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed(engine, tenant_id=2, n=2,
                base=dt.datetime(2026, 5, 20, 9, 0, tzinfo=dt.UTC))
    await _seed(engine, tenant_id=1, n=2,
                base=dt.datetime(2026, 5, 20, 10, 0, tzinfo=dt.UTC))
    r = await client.get("/api/v1/audit/log", headers=auth)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 2
    assert all(item["tenant_id"] == 1 for item in items)


@pytest.mark.slow
async def test_audit_happy_filters_and_pagination(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed(engine, tenant_id=1, n=3, action="task.created",
                base=dt.datetime(2026, 5, 20, 11, 0, tzinfo=dt.UTC))
    await _seed(engine, tenant_id=1, n=2, action="task.cancelled",
                base=dt.datetime(2026, 5, 20, 11, 30, tzinfo=dt.UTC))
    r = await client.get("/api/v1/audit/log?limit=3", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    items = body["items"]
    assert len(items) == 3
    assert items[0]["action"] == "task.cancelled"
    assert "next_cursor" in body
    assert body["next_cursor"]
    r2 = await client.get("/api/v1/audit/log?action=task.created&limit=10",
                          headers=auth)
    assert r2.status_code == 200
    items2 = r2.json()["items"]
    assert len(items2) >= 3
    assert all(it["action"].startswith("task.created") for it in items2)
    r3 = await client.get(
        f"/api/v1/audit/log?limit=3&cursor={body['next_cursor']}",
        headers=auth)
    assert r3.status_code == 200
    page2 = r3.json()["items"]
    assert len(page2) >= 1
    assert page2[0]["id"] != items[0]["id"]


@pytest.mark.slow
async def test_audit_actor_and_time_range_filters(
    client: AsyncClient, auth, engine,
) -> None:
    from dlw.db.models.audit import AuditLog
    factory = async_sessionmaker(engine, expire_on_commit=False)
    base = dt.datetime(2026, 5, 20, 13, 0, 0, tzinfo=dt.UTC)
    async with factory() as s:
        s.add(AuditLog(
            occurred_at=base, tenant_id=1, actor_user_id=42,
            action="user.login", resource_type="user", resource_id="42",
            outcome="success", payload={}, self_hash="0" * 64))
        s.add(AuditLog(
            occurred_at=base + dt.timedelta(hours=1), tenant_id=1,
            actor_user_id=99, action="user.login", resource_type="user",
            resource_id="99", outcome="success", payload={},
            self_hash="0" * 64))
        await s.commit()
    r = await client.get(
        "/api/v1/audit/log?actor_user_id=42&limit=10", headers=auth)
    items = r.json()["items"]
    assert all(it["actor_user_id"] == 42 for it in items)
    assert any(it["action"] == "user.login" for it in items)
    # ISO format with +00:00 gets URL-mangled (+ → space); use Z suffix.
    from_iso = (base + dt.timedelta(minutes=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    r2 = await client.get(
        f"/api/v1/audit/log?from={from_iso}&limit=10", headers=auth)
    items2 = r2.json()["items"]
    assert all(it["actor_user_id"] != 42 or
               dt.datetime.fromisoformat(it["occurred_at"]) >=
               base + dt.timedelta(minutes=30) for it in items2)
