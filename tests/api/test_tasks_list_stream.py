"""Tests for GET /api/v1/tasks/stream (UI-SP5c SSE)."""
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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Tenant(id=1, slug="default", display_name="Default"))
        session.add(Tenant(id=2, slug="other", display_name="Other"))
        await session.flush()
        session.add(Project(id=1, tenant_id=1, name="default"))
        session.add(Project(id=2, tenant_id=2, name="other"))
        session.add(User(id=1, tenant_id=1, oidc_subject="dev",
                         email="d@l", role="tenant_admin"))
        # Pre-review BLOCKER 2 fix: tenant 2 needs its own User row so the
        # cross-tenant POST as user_id=2 doesn't FK-violate users.id.
        session.add(User(id=2, tenant_id=2, oidc_subject="dev2",
                         email="d2@l", role="tenant_admin"))
        session.add(StorageBackend(id=1, tenant_id=1, name="default",
                                   backend_type="s3", config_encrypted=b""))
        session.add(StorageBackend(id=2, tenant_id=2, name="other",
                                   backend_type="s3", config_encrypted=b""))
        await session.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_TASKS_LIST_STREAM_INTERVAL_SECONDS", TICK)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _patch_hf(monkeypatch: pytest.MonkeyPatch):
    from dlw.services.hf_metadata import RepoFile

    async def fake(*args, **kwargs):
        return [RepoFile(path="config.json", size=4096, sha256=None)]
    monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake)


@pytest.fixture
def auth() -> dict[str, str]:
    return principal_headers(secret=SECRET, role="tenant_admin")


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test", timeout=10.0) as c:
        yield c


async def _seed_tasks(client, *, tenant_admin_headers, n: int, prefix: str,
                      storage_id: int = 1):
    out: list[str] = []
    for i in range(n):
        r = await client.post("/api/v1/tasks", json={
            "repo_id": f"o/{prefix}-{i}", "revision": "0" * 40,
            "storage_id": storage_id,
        }, headers=tenant_admin_headers)
        assert r.status_code == 201, r.text
        out.append(r.json()["id"])
    return out


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
async def test_tasks_list_stream_unauthenticated_401(
    client: AsyncClient,
) -> None:
    async with client.stream("GET", "/api/v1/tasks/stream") as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_tasks_list_stream_tenant_isolation(
    client: AsyncClient, auth,
) -> None:
    t1_ids = await _seed_tasks(client, tenant_admin_headers=auth,
                                n=2, prefix="t1")
    other = principal_headers(secret=SECRET, role="tenant_admin",
                              user_id=2, tenant_id=2)
    # Pre-review IMPORTANT 1 fix: use tenant-2's storage so the seeded task
    # doesn't leak across tenants (current backend doesn't validate storage
    # ownership but the test should be semantically correct).
    await _seed_tasks(client, tenant_admin_headers=other,
                       n=1, prefix="t2", storage_id=2)
    received = await _collect(
        client, "/api/v1/tasks/stream?max_ticks=1", auth,
        count=1, timeout=3.0)
    body = json.loads(received[0])
    ids = {it["id"] for it in body["items"]}
    assert set(t1_ids) <= ids
    # No t2 task in the tenant-1 snapshot.
    assert body["total"] == len(t1_ids)


@pytest.mark.slow
async def test_tasks_list_stream_multi_snapshot(
    client: AsyncClient, auth,
) -> None:
    received = await _collect(
        client, "/api/v1/tasks/stream?max_ticks=2", auth,
        count=2, timeout=3.0)
    assert len(received) >= 2
    for raw in received[:2]:
        body = json.loads(raw)
        assert "items" in body and "total" in body
        assert isinstance(body["items"], list)
        assert isinstance(body["total"], int)


@pytest.mark.slow
async def test_tasks_list_stream_shape_is_slim_no_subtasks(
    client: AsyncClient, auth,
) -> None:
    await _seed_tasks(client, tenant_admin_headers=auth,
                       n=1, prefix="slim")
    received = await _collect(
        client, "/api/v1/tasks/stream?max_ticks=1", auth,
        count=1, timeout=3.0)
    body = json.loads(received[0])
    assert body["items"], "expected at least one task in the snapshot"
    for item in body["items"]:
        assert "subtasks" not in item, "list shape must stay slim (no subtasks)"
        assert "id" in item and "status" in item and "repo_id" in item
