"""AI Copilot chat service (UI-SP4a): drives an AgentRunner, executes
read-only tools (tenant-scoped + audited), persists the conversation,
and yields SSE-ready AgentEvents."""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.ai._sanitize_apply import apply_external_fields, sanitize_error_key
from dlw.ai.runner import AgentContext, AgentEvent, AgentRunner
from dlw.ai.tools import READONLY_TOOLS
from dlw.ai.write_tools import WRITE_TOOLS
from dlw.auth.principal import Principal
from dlw.db.models.ai import AIConversation, AIMessage, AIToolCall
from dlw.services.ai_quota import (
    AITokenBudgetExceeded,
    check_ai_token_budget,
    record_ai_token_usage,
)
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
    # SP4d: pre-turn budget gate (invariant 18). Over budget → block the AI
    # call entirely: emit quota_exceeded and return WITHOUT creating a
    # conversation, persisting a message, or invoking the runner.
    async with session_maker() as q:
        try:
            await check_ai_token_budget(q, principal.tenant_id)
        except AITokenBudgetExceeded as exc:
            yield AgentEvent("quota_exceeded",
                             {"metric": "ai_tokens", "remaining": exc.remaining})
            return

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
        # SP4e follow-on: structural sanitize choke point.
        # - Declared external_fields: sanitized on success path only.
        # - Error key: sanitized unconditionally (handles _hf_* error strings
        #   like f"hf_network: {e}" where `e` carries external content).
        if outcome == "success" and tool.external_fields:
            apply_external_fields(
                out, tool.external_fields, source=f"tool:{name}")
        sanitize_error_key(out, source=f"tool:{name}:error")
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
            elif ev.event == "tool_call_pending_confirm":
                # SP4b: persist a pending tool call (NO execution) and stamp
                # the real call id into the event before forwarding it.
                async with session_maker() as ps:
                    call = AIToolCall(
                        conversation_id=conv_id,
                        tool_name=ev.data.get("tool"),
                        proposed_input=ev.data.get("input") or {},
                        requires_confirmation=True, status="pending")
                    ps.add(call)
                    await ps.commit()
                    call_id = str(call.id)
                ev.data["id"] = call_id
                tool_calls.append({"id": call_id, "tool": ev.data.get("tool"),
                                   "input": ev.data.get("input"),
                                   "pending_confirm": True})
            yield ev
    except Exception as exc:  # noqa: BLE001
        # On runner failure: emit a terminal `error` and return — do NOT
        # persist an empty assistant message or emit `done`.
        yield AgentEvent("error", {"code": "runner_failed",
                                   "message": str(exc)})
        return

    # 4. Persist the assistant message + close. Wrapped so a persistence
    # failure still emits a terminal event (the client waits for error|done).
    # SP4d: nominal token estimate (≈ chars/4), computed BEFORE the persist
    # try so they're always bound. A real backend would report actual counts;
    # the stub consumes no real tokens, so this keeps usage accumulating and
    # the budget exercisable.
    text_out = "".join(assistant_text)
    tin = max(1, len(message) // 4)
    tout = max(0, len(text_out) // 4)
    try:
        async with session_maker() as s:
            am = AIMessage(
                conversation_id=conv_id, role="assistant",
                content={"text": text_out, "tool_calls": tool_calls},
                tokens_input=tin, tokens_output=tout)
            s.add(am)
            await s.commit()
            ai_message_id = str(am.id)
        # SP4d: best-effort/isolated usage recording (must not lose the turn).
        try:
            async with session_maker() as u:
                await record_ai_token_usage(
                    u, tenant_id=principal.tenant_id,
                    user_id=principal.user_id, conversation_id=conv_id,
                    model_name=runner.model_name,
                    tokens_input=tin, tokens_output=tout)
                await u.commit()
        except Exception:  # noqa: BLE001 — usage recording is best-effort
            pass
        yield AgentEvent("done", {"conversation_id": str(conv_id),
                                  "ai_message_id": ai_message_id,
                                  "tokens_used": tin + tout})
    except Exception as exc:  # noqa: BLE001
        yield AgentEvent("error", {"code": "persist_failed",
                                   "message": str(exc)})


async def run_confirmation(
    *, session_maker: async_sessionmaker, principal: Principal,
    conversation_id: uuid.UUID, call_id: uuid.UUID, decision: str,
    modified_input: dict | None,
) -> AsyncIterator[AgentEvent]:
    """SP4b phase 2: resolve a pending write-tool call. Single transaction —
    lock the call FOR UPDATE, check status, execute via the service layer
    (inv 40: full re-validation of final_input), update the row + metadata,
    one atomic commit. Audit is best-effort/isolated afterward (inv 16)."""
    proposed: dict = {}
    tool_name = ""
    final_input: dict = {}
    out: dict = {}
    resolved_status = ""   # rejected | executed | error

    async with session_maker() as s:
        call = (await s.execute(
            select(AIToolCall).join(
                AIConversation, AIToolCall.conversation_id == AIConversation.id)
            .where(AIToolCall.id == call_id,
                   AIToolCall.conversation_id == conversation_id,
                   AIConversation.tenant_id == principal.tenant_id,
                   AIConversation.owner_user_id == principal.user_id)
            .with_for_update(of=AIToolCall))
        ).scalar_one_or_none()
        if call is None:
            yield AgentEvent("error", {"code": "not_found",
                                       "message": "pending call not found"})
            return
        if call.status != "pending":
            yield AgentEvent("error", {"code": "already_resolved",
                                       "message": f"call is {call.status}"})
            return
        proposed = call.proposed_input
        tool_name = call.tool_name
        call.confirmation_decision = decision
        call.confirmed_by_user_id = principal.user_id
        call.confirmation_at = datetime.now(UTC)
        if decision == "rejected":
            call.status = resolved_status = "rejected"
        elif decision == "modified" and not modified_input:
            # Defense-in-depth (final-review HIGH): the API validator already
            # requires modified_input for a 'modified' decision, but guard at
            # the service layer too — executing with {} would falsely audit
            # user_final_input={} (inv 40). Leave the call pending.
            yield AgentEvent("error",
                             {"code": "bad_request",
                              "message": "modified_input required for "
                                         "decision=modified"})
            return
        else:
            final_input = proposed if decision == "approved" \
                else dict(modified_input)
            tool = WRITE_TOOLS.get(tool_name)
            if tool is None:
                out = {"error": f"unknown tool {tool_name}"}
            else:
                try:
                    out = await tool.run(s, principal, **final_input)
                except Exception as exc:  # noqa: BLE001
                    out = {"error": str(exc)}
            ok = "error" not in out
            call.final_input = final_input
            call.output = out
            call.status = resolved_status = "executed" if ok else "error"
            if not ok:
                call.error_code = str(out.get("error"))[:64]
        await s.commit()

    # Best-effort isolated audit (inv 16 + proposed/final for inv 40).
    try:
        async with session_maker() as a:
            payload: dict = {"actor_kind": "ai_copilot",
                             "ai_proposed_input": proposed}
            if resolved_status != "rejected":
                payload["user_final_input"] = final_input
            await write_audit(
                a, action=f"ai.tool.{tool_name}", resource_type="ai_tool",
                resource_id=str(call_id),
                outcome="rejected" if resolved_status == "rejected"
                else ("success" if resolved_status == "executed" else "error"),
                tenant_id=principal.tenant_id, actor_user_id=principal.user_id,
                payload=payload)
            await a.commit()
    except Exception:  # noqa: BLE001
        pass

    if resolved_status == "rejected":
        yield AgentEvent("assistant.message_delta",
                         {"text": "Operation cancelled."})
    else:
        ok = resolved_status == "executed"
        yield AgentEvent("tool_result",
                         {"id": str(call_id), "ok": ok, "output": out})
        yield AgentEvent("assistant.message_delta",
                         {"text": "Done." if ok
                          else f"Failed: {out.get('error')}"})
    yield AgentEvent("done", {"conversation_id": str(conversation_id),
                              "ai_message_id": "", "tokens_used": 0})
