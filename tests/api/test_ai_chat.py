"""POST /api/v1/ai/chat SSE (UI-SP4a) — stub backend."""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import make_app_with_state, principal_headers

SECRET = "unit-secret"
TASK_T1 = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.task import DownloadTask
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1"))
        await s.flush()
        s.add_all([
            Project(id=1, tenant_id=1, name="d1"),
            User(id=1, tenant_id=1, oidc_subject="u1", email="u1@t",
                 role="tenant_admin"),
            StorageBackend(id=1, tenant_id=1, name="s1",
                           backend_type="s3", config_encrypted=b""),
        ])
        await s.flush()
        s.add(DownloadTask(
            id=TASK_T1, tenant_id=1, project_id=1, owner_user_id=1,
            storage_id=1, repo_id="org/m1", revision="0" * 40,
            path_template="hf/{model}/{revision}/{file}", status="running"))
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
def auth() -> dict[str, str]:
    return principal_headers(secret=SECRET, role="tenant_admin",
                             user_id=1, tenant_id=1)


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test", timeout=10.0) as c:
        yield c


async def _collect_events(client, headers, body, *, timeout=6.0):
    evs: list[dict] = []
    async with asyncio.timeout(timeout):
        async with client.stream("POST", "/api/v1/ai/chat",
                                 headers=headers, json=body) as r:
            assert r.status_code == 200, await r.aread()
            assert "text/event-stream" in r.headers.get("content-type", "")
            cur: dict = {}
            async for line in r.aiter_lines():
                if line.startswith("event: "):
                    cur["event"] = line[len("event: "):]
                elif line.startswith("data: "):
                    cur["data"] = json.loads(line[len("data: "):])
                    evs.append(cur)
                    if cur.get("event") == "done":
                        return evs
                    cur = {}
    return evs


async def test_chat_unauthenticated_401(client: AsyncClient) -> None:
    async with client.stream("POST", "/api/v1/ai/chat",
                             json={"message": "hi"}) as r:
        assert r.status_code == 401


async def test_chat_empty_message_422(client: AsyncClient, auth) -> None:
    r = await client.post("/api/v1/ai/chat", headers=auth,
                          json={"message": "   "})
    assert r.status_code == 422


async def test_chat_task_query_event_sequence(client: AsyncClient, auth) -> None:
    evs = await _collect_events(client, auth, {"message": "list my tasks"})
    kinds = [e["event"] for e in evs]
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert "assistant.message_delta" in kinds
    assert kinds[-1] == "done"
    tc = next(e for e in evs if e["event"] == "tool_call")
    assert tc["data"]["tool"] == "dlw_list_tasks"
    done = evs[-1]
    assert done["data"]["conversation_id"]


async def test_chat_persists_conversation_and_audit(
    client: AsyncClient, auth, engine,
) -> None:
    evs = await _collect_events(client, auth, {"message": "show tasks please"})
    conv_id = uuid.UUID(evs[-1]["data"]["conversation_id"])
    from dlw.db.models.ai import AIConversation, AIMessage
    from dlw.db.models.audit import AuditLog
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        conv = await s.get(AIConversation, conv_id)
        assert conv is not None
        assert conv.tenant_id == 1 and conv.owner_user_id == 1
        n_msgs = await s.scalar(
            select(func.count()).select_from(AIMessage)
            .where(AIMessage.conversation_id == conv_id))
        assert n_msgs >= 2  # user + assistant
        n_audit = await s.scalar(
            select(func.count()).select_from(AuditLog)
            .where(AuditLog.action == "ai.tool.dlw_list_tasks"))
        assert n_audit >= 1
        row = (await s.execute(
            select(AuditLog).where(
                AuditLog.action == "ai.tool.dlw_list_tasks"))).scalars().first()
        assert row.payload.get("actor_kind") == "ai_copilot"


async def test_chat_over_budget_blocks(client, auth, engine):
    # Seed month-to-date usage above the tenant's default quota (1_000_000).
    from sqlalchemy import delete, func, select

    from dlw.db.models.ai import AIConversation, AITokenUsage
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        before = await s.scalar(
            select(func.count()).select_from(AIConversation))
        s.add(AITokenUsage(
            tenant_id=1, user_id=1, conversation_id=None,
            model_name="stub", tokens_input=10**9, tokens_output=0))
        await s.commit()
    evs = await _collect_events(client, auth, {"message": "hello there"})
    assert evs[0]["event"] == "quota_exceeded"
    assert evs[0]["data"]["metric"] == "ai_tokens"
    assert evs[0]["data"]["remaining"] == 0
    async with f() as s:
        after = await s.scalar(
            select(func.count()).select_from(AIConversation))
    assert after == before
    # Cleanup the seed so later tests in the module aren't over budget.
    async with f() as s:
        await s.execute(
            delete(AITokenUsage).where(AITokenUsage.tokens_input == 10**9))
        await s.commit()


async def test_chat_model_card_sse_returns_t2_wrapped(client, auth, monkeypatch):
    async def fake_card(repo_id, *, hf_endpoint, hf_token):
        return "# Card\nhello"
    monkeypatch.setattr("dlw.ai.tools.fetch_model_card", fake_card)
    evs = await _collect_events(
        client, auth, {"message": "show the model card for org/m"})
    kinds = [e["event"] for e in evs]
    assert "tool_call" in kinds
    tr = next(e for e in evs if e["event"] == "tool_result")
    assert 'external_user_content trust_level="t2"' in tr["data"]["output"]["sanitized"]
    assert kinds[-1] == "done"


async def test_chat_under_budget_records_usage(client, auth, engine):
    from sqlalchemy import select

    from dlw.db.models.ai import AIMessage, AITokenUsage
    evs = await _collect_events(client, auth, {"message": "just chatting"})
    assert evs[-1]["event"] == "done"
    assert evs[-1]["data"]["tokens_used"] > 0
    conv_id = uuid.UUID(evs[-1]["data"]["conversation_id"])
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        rows = (await s.execute(
            select(AITokenUsage).where(
                AITokenUsage.conversation_id == conv_id))).scalars().all()
        assert len(rows) == 1
        assert rows[0].tokens_input > 0
        am = (await s.execute(
            select(AIMessage).where(
                AIMessage.conversation_id == conv_id,
                AIMessage.role == "assistant"))).scalars().one()
        assert am.tokens_input == rows[0].tokens_input
        assert am.tokens_output == rows[0].tokens_output
