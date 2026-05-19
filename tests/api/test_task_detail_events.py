"""Tests for GET /api/v1/tasks/{id}/events (UI-SP2, audit-derived)."""
from __future__ import annotations

import datetime as dt
import uuid

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


@pytest.fixture(autouse=True)
def _patch_hf(monkeypatch: pytest.MonkeyPatch):
    from dlw.services.hf_metadata import RepoFile

    async def fake(*args, **kwargs):
        return [
            RepoFile(path="config.json", size=4096, sha256=None),
            RepoFile(path="model.safetensors", size=64 * 1024, sha256="a" * 64),
        ]
    monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake)


@pytest.fixture
def auth() -> dict[str, str]:
    return principal_headers(secret=SECRET, role="tenant_admin")


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def _make_task(client, auth) -> str:
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/events", "revision": "3" * 40, "storage_id": 1,
    }, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.slow
async def test_events_unauthenticated_401(client: AsyncClient) -> None:
    r = await client.get(f"/api/v1/tasks/{uuid.uuid4()}/events")
    assert r.status_code == 401


@pytest.mark.slow
async def test_events_cross_tenant_404(client: AsyncClient, auth) -> None:
    tid = await _make_task(client, auth)
    other = principal_headers(secret=SECRET, role="tenant_admin",
                              user_id=9, tenant_id=2)
    r = await client.get(f"/api/v1/tasks/{tid}/events", headers=other)
    assert r.status_code == 404


@pytest.mark.slow
async def test_events_returns_audit_rows_and_paginates(
    client: AsyncClient, auth, engine,
) -> None:
    from dlw.db.models.audit import AuditLog
    tid = await _make_task(client, auth)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    base = dt.datetime(2026, 5, 20, 12, 0, 0, tzinfo=dt.UTC)
    async with factory() as s:
        for i in range(3):
            s.add(AuditLog(
                occurred_at=base + dt.timedelta(seconds=i),
                tenant_id=1, actor_user_id=1,
                action="task.note", resource_type="task",
                resource_id=tid, outcome="success",
                payload={"i": i}, self_hash="0" * 64))
        s.add(AuditLog(
            occurred_at=base + dt.timedelta(seconds=9),
            tenant_id=1, actor_user_id=1, action="task.denied",
            resource_type="task", resource_id=tid, outcome="denied",
            payload=None, self_hash="0" * 64))
        await s.commit()

    r = await client.get(f"/api/v1/tasks/{tid}/events?limit=2", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"]
    assert body["items"][0]["type"] == "task.denied"
    assert "denied" in body["items"][0]["message"]
    assert body["items"][0]["details"] == {}

    r2 = await client.get(
        f"/api/v1/tasks/{tid}/events?limit=2&cursor={body['next_cursor']}",
        headers=auth)
    assert r2.status_code == 200
    page2 = r2.json()["items"]
    assert len(page2) == 2
    assert page2[0]["ts"] != body["items"][0]["ts"]
