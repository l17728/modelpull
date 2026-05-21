"""AI Copilot HTTP API (UI-SP4a): chat SSE + conversation history."""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.ai.runner import AIBackendUnavailable, build_runner
from dlw.ai.service import run_chat, run_confirmation
from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.models.ai import AIConversation, AIMessage
from dlw.db.session import get_engine

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


class ToolConfirmation(BaseModel):
    call_id: uuid.UUID
    decision: Literal["approved", "rejected", "modified"]
    modified_input: dict | None = None

    @model_validator(mode="after")
    def _require_modified_input(self) -> "ToolConfirmation":
        # inv 40: a 'modified' decision MUST carry modified_input, else the
        # audit would record user_final_input={} (a falsehood).
        if self.decision == "modified" and not self.modified_input:
            raise ValueError("modified_input is required when decision='modified'")
        return self


class ChatRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str | None = None
    tool_confirmation: ToolConfirmation | None = None

    @model_validator(mode="after")
    def _phase_consistency(self) -> "ChatRequest":
        if self.tool_confirmation is not None:
            if self.conversation_id is None:
                raise ValueError(
                    "conversation_id is required with tool_confirmation")
        elif self.message is None or not self.message.strip():
            raise ValueError("message is required (and non-blank)")
        return self


@router.post("/chat")
async def chat(
    body: ChatRequest,
    principal: Principal = Depends(require_perm("/api/v1/ai*", "POST")),
) -> StreamingResponse:
    session_maker = async_sessionmaker(get_engine(), expire_on_commit=False)

    if body.tool_confirmation is not None:
        # Phase 2 — confirmation resolution (service-side; no runner).
        tc = body.tool_confirmation

        async def _body() -> AsyncIterator[bytes]:
            yield b":open\n\n"
            async for ev in run_confirmation(
                    session_maker=session_maker, principal=principal,
                    conversation_id=body.conversation_id, call_id=tc.call_id,
                    decision=tc.decision, modified_input=tc.modified_input):
                yield (f"event: {ev.event}\n"
                       f"data: {json.dumps(ev.data, default=str)}\n\n"
                       ).encode("utf-8")

        return StreamingResponse(
            _body(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Phase 1 — runner turn.
    try:
        runner = build_runner(get_settings())
    except AIBackendUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    async def _body() -> AsyncIterator[bytes]:
        yield b":open\n\n"
        async for ev in run_chat(
                session_maker=session_maker, principal=principal,
                runner=runner, conversation_id=body.conversation_id,
                message=body.message or ""):
            yield (f"event: {ev.event}\n"
                   f"data: {json.dumps(ev.data, default=str)}\n\n"
                   ).encode("utf-8")

    return StreamingResponse(
        _body(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/conversations")
async def list_conversations(
    principal: Principal = Depends(require_perm("/api/v1/ai*", "GET")),
) -> dict:
    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with sm() as s:
        rows = (await s.execute(
            select(AIConversation).where(
                AIConversation.tenant_id == principal.tenant_id,
                AIConversation.owner_user_id == principal.user_id,
                AIConversation.archived.is_(False))
            .order_by(AIConversation.last_message_at.desc()))).scalars().all()
    return {"items": [{"id": str(c.id), "title": c.title,
                       "last_message_at": c.last_message_at.isoformat(),
                       "backend": c.backend, "model_name": c.model_name}
                      for c in rows]}


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID,
    principal: Principal = Depends(require_perm("/api/v1/ai*", "GET")),
) -> dict:
    sm = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with sm() as s:
        conv = (await s.execute(
            select(AIConversation).where(
                AIConversation.id == conversation_id,
                AIConversation.tenant_id == principal.tenant_id,
                AIConversation.owner_user_id == principal.user_id))
        ).scalar_one_or_none()
        if conv is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        msgs = (await s.execute(
            select(AIMessage).where(AIMessage.conversation_id == conv.id)
            .order_by(AIMessage.created_at))).scalars().all()
    return {"conversation": {"id": str(conv.id), "title": conv.title,
                             "backend": conv.backend,
                             "model_name": conv.model_name},
            "messages": [{"id": str(m.id), "role": m.role,
                          "content": m.content,
                          "created_at": m.created_at.isoformat()}
                         for m in msgs]}
