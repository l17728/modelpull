"""Tests for GET /api/v1/tasks/{id}/stream (UI-SP5 SSE)."""
from __future__ import annotations

import asyncio
import json
import uuid

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
    monkeypatch.setenv("DLW_TASK_STREAM_INTERVAL_SECONDS", TICK)
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
                           base_url="http://test", timeout=10.0) as c:
        yield c


async def _make_task(client, auth) -> str:
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/stream", "revision": "3" * 40, "storage_id": 1,
    }, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _collect_events(client, url, headers, *, count, timeout=4.0):
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
async def test_stream_unauthenticated_401(client: AsyncClient) -> None:
    async with client.stream(
        "GET", f"/api/v1/tasks/{uuid.uuid4()}/stream",
    ) as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_stream_cross_tenant_404(client: AsyncClient, auth) -> None:
    tid = await _make_task(client, auth)
    other = principal_headers(secret=SECRET, role="tenant_admin",
                              user_id=9, tenant_id=2)
    async with client.stream(
        "GET", f"/api/v1/tasks/{tid}/stream", headers=other,
    ) as resp:
        assert resp.status_code == 404


@pytest.mark.slow
async def test_stream_emits_multiple_snapshots(
    client: AsyncClient, auth,
) -> None:
    tid = await _make_task(client, auth)
    # `?max_ticks=2` makes the body self-terminate after 2 snapshots —
    # required because httpx ASGITransport buffers chunks until the response
    # generator closes (well-known limitation). Production clients never
    # set this; the headed Playwright smoke validates the real streaming
    # behavior over a live uvicorn.
    received = await _collect_events(
        client, f"/api/v1/tasks/{tid}/stream?max_ticks=2", auth,
        count=2, timeout=4.0)
    assert len(received) >= 2
    for raw in received[:2]:
        payload = json.loads(raw)
        assert payload["id"] == tid
        assert "status" in payload
        assert "subtasks" in payload


@pytest.mark.slow
async def test_stream_terminates_on_terminal_status(
    client: AsyncClient, auth, engine,
) -> None:
    from dlw.db.models.task import DownloadTask
    tid = await _make_task(client, auth)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        task = await s.get(DownloadTask, uuid.UUID(tid))
        assert task is not None
        task.status = "succeeded"
        await s.commit()
    received: list[str] = []
    async with asyncio.timeout(3.0):
        async with client.stream(
            "GET", f"/api/v1/tasks/{tid}/stream", headers=auth,
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    received.append(line)
    assert len(received) == 1
    assert "succeeded" in received[0]
