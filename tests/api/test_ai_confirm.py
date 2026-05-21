"""Two-phase write-tool confirmation gate (UI-SP4b, inv 17/40)."""
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
TA = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
TB = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
TC = uuid.UUID("cccccccc-0000-0000-0000-000000000003")
TD1 = uuid.UUID("dddddddd-0000-0000-0000-000000000004")
TD2 = uuid.UUID("dddddddd-0000-0000-0000-000000000005")


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
        s.add(Tenant(id=2, slug="t2", display_name="T2"))
        await s.flush()
        s.add_all([
            Project(id=1, tenant_id=1, name="d1"),
            Project(id=2, tenant_id=2, name="d2"),
            User(id=1, tenant_id=1, oidc_subject="u1", email="u1@t",
                 role="tenant_admin"),
            User(id=2, tenant_id=2, oidc_subject="u2", email="u2@t",
                 role="tenant_admin"),
            StorageBackend(id=1, tenant_id=1, name="s1",
                           backend_type="s3", config_encrypted=b""),
            StorageBackend(id=2, tenant_id=2, name="s2",
                           backend_type="s3", config_encrypted=b""),
        ])
        await s.flush()
        for tid in (TA, TB, TC, TD1, TD2):
            s.add(DownloadTask(
                id=tid, tenant_id=1, project_id=1, owner_user_id=1,
                storage_id=1, repo_id="org/m", revision="0" * 40,
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


async def _events(client, headers, body, *, timeout=6.0):
    evs: list[dict] = []
    async with asyncio.timeout(timeout):
        async with client.stream("POST", "/api/v1/ai/chat",
                                 headers=headers, json=body) as r:
            assert r.status_code == 200, await r.aread()
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


async def _task_status(engine, tid):
    from dlw.db.models.task import DownloadTask
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        t = await s.get(DownloadTask, tid)
        return t.status


async def _propose_cancel(client, auth, tid):
    evs = await _events(client, auth, {"message": f"cancel {tid}"})
    pend = next(e for e in evs if e["event"] == "tool_call_pending_confirm")
    return evs, pend["data"]["id"], pend["data"]


async def test_phase1_proposes_without_executing(client, auth, engine):
    evs, call_id, data = await _propose_cancel(client, auth, TA)
    assert data["tool"] == "dlw_cancel_task"
    assert uuid.UUID(call_id)
    assert await _task_status(engine, TA) == "running"   # NOT cancelled yet
    from dlw.db.models.ai import AIToolCall
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        call = await s.get(AIToolCall, uuid.UUID(call_id))
        assert call.status == "pending"
        conv_id = call.conversation_id
    # conversation_id from done event
    done = evs[-1]["data"]
    assert done["conversation_id"] == str(conv_id)


async def test_phase2_approved_executes_and_audits(client, auth, engine):
    evs, call_id, _ = await _propose_cancel(client, auth, TB)
    conv_id = evs[-1]["data"]["conversation_id"]
    evs2 = await _events(client, auth, {
        "conversation_id": conv_id,
        "tool_confirmation": {"call_id": call_id, "decision": "approved"}})
    tr = next(e for e in evs2 if e["event"] == "tool_result")
    assert tr["data"]["ok"] is True
    assert await _task_status(engine, TB) == "cancelling"
    from dlw.db.models.ai import AIToolCall
    from dlw.db.models.audit import AuditLog
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        call = await s.get(AIToolCall, uuid.UUID(call_id))
        assert call.status == "executed"
        assert call.confirmation_decision == "approved"
        assert call.confirmed_by_user_id == 1
        row = (await s.execute(
            select(AuditLog).where(
                AuditLog.action == "ai.tool.dlw_cancel_task",
                AuditLog.resource_id == call_id))).scalars().first()
        assert row is not None
        assert row.payload["actor_kind"] == "ai_copilot"
        assert "ai_proposed_input" in row.payload
        assert "user_final_input" in row.payload


async def test_phase2_rejected_does_not_execute(client, auth, engine):
    evs, call_id, _ = await _propose_cancel(client, auth, TC)
    conv_id = evs[-1]["data"]["conversation_id"]
    evs2 = await _events(client, auth, {
        "conversation_id": conv_id,
        "tool_confirmation": {"call_id": call_id, "decision": "rejected"}})
    assert all(e["event"] != "tool_result" for e in evs2)
    assert await _task_status(engine, TC) == "running"  # untouched
    from dlw.db.models.ai import AIToolCall
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        call = await s.get(AIToolCall, uuid.UUID(call_id))
        assert call.status == "rejected"


async def test_phase2_modified_revalidates_other_task(client, auth, engine):
    # Propose cancel for TD1; user modifies to cancel TD2 instead (inv 40).
    evs, call_id, _ = await _propose_cancel(client, auth, TD1)
    conv_id = evs[-1]["data"]["conversation_id"]
    evs2 = await _events(client, auth, {
        "conversation_id": conv_id,
        "tool_confirmation": {"call_id": call_id, "decision": "modified",
                              "modified_input": {"task_id": str(TD2)}}})
    tr = next(e for e in evs2 if e["event"] == "tool_result")
    assert tr["data"]["ok"] is True
    assert await _task_status(engine, TD1) == "running"     # proposed, NOT cancelled
    assert await _task_status(engine, TD2) == "cancelling"  # the modified target
    from dlw.db.models.audit import AuditLog
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        row = (await s.execute(
            select(AuditLog).where(
                AuditLog.action == "ai.tool.dlw_cancel_task",
                AuditLog.resource_id == call_id))).scalars().first()
        assert row.payload["ai_proposed_input"]["task_id"] == str(TD1)
        assert row.payload["user_final_input"]["task_id"] == str(TD2)


async def test_cross_tenant_cannot_confirm(client, auth, engine):
    evs, call_id, _ = await _propose_cancel(client, auth, TA)  # TA already cancelling? use fresh
    conv_id = evs[-1]["data"]["conversation_id"]
    other = principal_headers(secret=SECRET, role="tenant_admin",
                              user_id=2, tenant_id=2)
    evs2 = await _events(client, other, {
        "conversation_id": conv_id,
        "tool_confirmation": {"call_id": call_id, "decision": "approved"}})
    assert evs2[0]["event"] == "error"
    from dlw.db.models.ai import AIToolCall
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        call = await s.get(AIToolCall, uuid.UUID(call_id))
        assert call.status == "pending"   # untouched by the cross-tenant attempt


async def test_double_confirm_is_error(client, auth, engine):
    evs, call_id, _ = await _propose_cancel(client, auth, TD2)
    conv_id = evs[-1]["data"]["conversation_id"]
    body = {"conversation_id": conv_id,
            "tool_confirmation": {"call_id": call_id, "decision": "approved"}}
    await _events(client, auth, body)
    evs2 = await _events(client, auth, body)
    assert evs2[0]["event"] == "error"
    assert evs2[0]["data"]["code"] == "already_resolved"


async def test_validation_422(client, auth) -> None:
    # tool_confirmation without conversation_id
    r = await client.post("/api/v1/ai/chat", headers=auth, json={
        "tool_confirmation": {"call_id": str(TA), "decision": "approved"}})
    assert r.status_code == 422
    # modified without modified_input
    r2 = await client.post("/api/v1/ai/chat", headers=auth, json={
        "conversation_id": str(TA),
        "tool_confirmation": {"call_id": str(TA), "decision": "modified"}})
    assert r2.status_code == 422
    # blank message
    r3 = await client.post("/api/v1/ai/chat", headers=auth,
                           json={"message": "   "})
    assert r3.status_code == 422


async def test_unauth_401(client) -> None:
    async with client.stream("POST", "/api/v1/ai/chat",
                             json={"message": "cancel x"}) as r:
        assert r.status_code == 401
