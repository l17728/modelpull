"""Tests for GET /api/v1/tasks/{task_id}/participating-executors/stream (UI-SP5i SSE)."""
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

TASK_T1 = uuid.UUID("11111111-1111-1111-1111-111111111111")
TASK_T2 = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.task import DownloadTask
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Tenant(id=1, slug="t1", display_name="T1"))
        session.add(Tenant(id=2, slug="t2", display_name="T2"))
        await session.flush()
        session.add_all([
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
        ])
        await session.flush()
        session.add_all([
            DownloadTask(id=TASK_T1, tenant_id=1, project_id=1,
                         owner_user_id=1, storage_id=1,
                         repo_id="org/m1", revision="0" * 40,
                         path_template="hf/{model}/{revision}/{file}",
                         status="running"),
            DownloadTask(id=TASK_T2, tenant_id=2, project_id=2,
                         owner_user_id=2, storage_id=2,
                         repo_id="org/m2", revision="1" * 40,
                         path_template="hf/{model}/{revision}/{file}",
                         status="running"),
        ])
        await session.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_TASK_EXECUTORS_STREAM_INTERVAL_SECONDS", TICK)
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
async def test_executors_stream_unauthenticated_401(
    client: AsyncClient,
) -> None:
    async with client.stream(
        "GET", f"/api/v1/tasks/{TASK_T1}/participating-executors/stream",
    ) as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_executors_stream_cross_tenant_404(
    client: AsyncClient, auth,
) -> None:
    async with client.stream(
        "GET",
        f"/api/v1/tasks/{TASK_T2}/participating-executors/stream?max_ticks=1",
        headers=auth,
    ) as resp:
        assert resp.status_code == 404


@pytest.mark.slow
async def test_executors_stream_single_snapshot(
    client: AsyncClient, auth,
) -> None:
    received = await _collect(
        client,
        f"/api/v1/tasks/{TASK_T1}/participating-executors/stream?max_ticks=1",
        auth, count=1, timeout=3.0)
    body = json.loads(received[0])
    assert "items" in body
    assert isinstance(body["items"], list)


@pytest.mark.slow
async def test_executors_stream_multi_snapshot(
    client: AsyncClient, auth,
) -> None:
    received = await _collect(
        client,
        f"/api/v1/tasks/{TASK_T1}/participating-executors/stream?max_ticks=2",
        auth, count=2, timeout=3.0)
    assert len(received) >= 2
    for raw in received[:2]:
        body = json.loads(raw)
        assert "items" in body
        assert isinstance(body["items"], list)
