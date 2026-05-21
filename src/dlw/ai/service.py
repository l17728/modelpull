"""AI Copilot chat service (UI-SP4a): drives an AgentRunner, executes
read-only tools (tenant-scoped + audited), persists the conversation,
and yields SSE-ready AgentEvents."""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.ai.runner import AgentContext, AgentEvent, AgentRunner
from dlw.ai.tools import READONLY_TOOLS
from dlw.auth.principal import Principal
from dlw.db.models.ai import AIConversation, AIMessage
from dlw.services.audit import write_audit


async def _load_conversation(session, conv_id: uuid.UUID,
                             principal: Principal) -> AIConversation | None:
    return (await session.execute(
        select(AIConversation).where(
            AIConversation.id == conv_id,
            AIConversation.tenant_id == principal.tenant_id,
            AIConversation.owner_user_id == principal.user_id))
    ).scalar_one_or_none()


async def run_chat(
    *, session_maker: async_sessionmaker, principal: Principal,
    runner: AgentRunner, conversation_id: uuid.UUID | None,
    message: str,
) -> AsyncIterator[AgentEvent]:
    # 1. Resolve / create the conversation + persist the user message.
    async with session_maker() as s:
        if conversation_id is not None:
            conv = await _load_conversation(s, conversation_id, principal)
            if conv is None:
                yield AgentEvent("error",
                                 {"code": "not_found",
                                  "message": "conversation not found"})
                return
        else:
            conv = AIConversation(
                tenant_id=principal.tenant_id,
                owner_user_id=principal.user_id,
                title=message[:80], backend=runner.backend_name,
                model_name=runner.model_name)
            s.add(conv)
            await s.flush()
        history = [m.content for m in (await s.execute(
            select(AIMessage).where(AIMessage.conversation_id == conv.id)
            .order_by(AIMessage.created_at))).scalars().all()]
        s.add(AIMessage(conversation_id=conv.id, role="user",
                        content={"text": message}))
        conv.last_message_at = datetime.now(UTC)
        await s.commit()
        conv_id = conv.id

    # 2. Tool executor: tenant-scoped + audited (invariants 15, 16).
    async def call_tool(name: str, tool_input: dict) -> dict:
        tool = READONLY_TOOLS.get(name)
        if tool is None:
            return {"error": f"unknown tool {name}"}
        async with session_maker() as ts:
            try:
                out = await tool.run(ts, principal, **tool_input)
                outcome = "error" if isinstance(out, dict) and "error" in out \
                    else "success"
            except Exception as exc:  # noqa: BLE001 — isolate tool errors
                out = {"error": str(exc)}
                outcome = "error"
            # Best-effort audit (invariant 16). Isolate an audit/commit failure
            # so it can't propagate as a runner exception or hold the session:
            # the tool result still returns; the `async with` closes ts cleanly.
            try:
                await write_audit(
                    ts, action=f"ai.tool.{name}", resource_type="ai_tool",
                    resource_id=str(conv_id), outcome=outcome,
                    tenant_id=principal.tenant_id,
                    actor_user_id=principal.user_id,
                    payload={"actor_kind": "ai_copilot", "input": tool_input})
                await ts.commit()
            except Exception:  # noqa: BLE001
                await ts.rollback()
        return out

    # 3. Drive the runner, collecting assistant text + tool calls for persist.
    assistant_text: list[str] = []
    tool_calls: list[dict] = []
    try:
        async for ev in runner.run(
                AgentContext(history=history, user_message=message),
                call_tool=call_tool):
            if ev.event == "assistant.message_delta":
                assistant_text.append(ev.data.get("text", ""))
            elif ev.event == "tool_call":
                tool_calls.append({"id": ev.data.get("id"),
                                   "tool": ev.data.get("tool"),
                                   "input": ev.data.get("input")})
            elif ev.event == "tool_result":
                for tc in tool_calls:
                    if tc.get("id") == ev.data.get("id"):
                        tc["ok"] = ev.data.get("ok")
                        tc["output"] = ev.data.get("output")
            yield ev
    except Exception as exc:  # noqa: BLE001
        # On runner failure: emit a terminal `error` and return — do NOT
        # persist an empty assistant message or emit `done`.
        yield AgentEvent("error", {"code": "runner_failed",
                                   "message": str(exc)})
        return

    # 4. Persist the assistant message + close. Wrapped so a persistence
    # failure still emits a terminal event (the client waits for error|done).
    try:
        async with session_maker() as s:
            am = AIMessage(
                conversation_id=conv_id, role="assistant",
                content={"text": "".join(assistant_text),
                         "tool_calls": tool_calls})
            s.add(am)
            await s.commit()
            ai_message_id = str(am.id)
        yield AgentEvent("done", {"conversation_id": str(conv_id),
                                  "ai_message_id": ai_message_id,
                                  "tokens_used": 0})
    except Exception as exc:  # noqa: BLE001
        yield AgentEvent("error", {"code": "persist_failed",
                                   "message": str(exc)})
