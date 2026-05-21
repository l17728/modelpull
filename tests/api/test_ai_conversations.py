"""GET /api/v1/ai/conversations[/{id}] (UI-SP4a)."""
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


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1"))
        s.add(Tenant(id=2, slug="t2", display_name="T2"))
        await s.flush()
        s.add_all([
            Project(id=1, tenant_id=1, name="d1"),
            Project(id=2, tenant_id=2, name="d2"),
            User(id=1, tenant_id=1, oidc_subject="u1", email="u1@t",
                 role="tenant_admin"),
            User(id=2, tenant_id=2, oidc_subject="u2", email="u2@t",
                 role="tenant_admin"),
        ])
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_AI_BACKEND", "stub")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test", timeout=10.0) as c:
        yield c


async def _chat(client, headers, message) -> str:
    async with asyncio.timeout(6.0):
        async with client.stream("POST", "/api/v1/ai/chat",
                                 headers=headers,
                                 json={"message": message}) as r:
            assert r.status_code == 200
            conv_id = None
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    data = json.loads(line[len("data: "):])
                    if "conversation_id" in data:
                        conv_id = data["conversation_id"]
            return conv_id


async def test_conversations_list_and_get_owner_scoped(
    client: AsyncClient,
) -> None:
    t1 = principal_headers(secret=SECRET, role="tenant_admin",
                           user_id=1, tenant_id=1)
    conv_id = await _chat(client, t1, "hello copilot")
    assert conv_id

    r = await client.get("/api/v1/ai/conversations", headers=t1)
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(c["id"] == conv_id for c in items)

    r2 = await client.get(f"/api/v1/ai/conversations/{conv_id}", headers=t1)
    assert r2.status_code == 200
    body = r2.json()
    assert body["conversation"]["id"] == conv_id
    assert len(body["messages"]) >= 2

    # Cross-tenant principal cannot see it.
    t2 = principal_headers(secret=SECRET, role="tenant_admin",
                           user_id=2, tenant_id=2)
    r3 = await client.get(f"/api/v1/ai/conversations/{conv_id}", headers=t2)
    assert r3.status_code == 404
    r4 = await client.get("/api/v1/ai/conversations", headers=t2)
    assert all(c["id"] != conv_id for c in r4.json()["items"])
