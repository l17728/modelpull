# UI-SP4a — AI Copilot Read-Only MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** First slice of the AI Copilot: a read-only conversational assistant. Migration (`ai_conversations`/`ai_messages`) + pluggable `AgentRunner` (stub for CI + opencode subprocess live) + in-process read-only tool registry (tenant-scoped, audited) + `POST /api/v1/ai/chat` SSE + conversations endpoints + Vue chat drawer.

**Architecture:** See `docs/superpowers/specs/2026-05-21-ui-sp4a-ai-copilot-readonly-design.md`. Backend tools reuse the exact `tenant_filtered(...)` queries / existing services (`events_for_task`, `get_quota_snapshot`) in the user's JWT scope. Chat endpoint reuses the SSE `StreamingResponse` idiom. Frontend reuses the shell/i18n/Element Plus conventions + a fetch-SSE client mirroring `sse.ts`.

---

## Conventions

- **Branch:** `feat/ui-sp4a-ai-copilot-readonly` (off `main` @ `de9fc36`, created).
- Bash cwd persists; prefix `cd /d/download_weights && …` (git/py) or `cd /d/download_weights/frontend && pnpm …`.
- `write_audit(session, *, action, resource_type, resource_id, outcome, tenant_id, actor_user_id, payload)` — note `resource_type` + `resource_id` are required (resource_id may be None).
- `Principal` fields: `user_id`, `tenant_id`, `role`.
- New models MUST be imported in `src/dlw/db/models/__init__.py` so `Base.metadata.create_all` (used by tests) sees them.
- Models use Python-side `default=uuid.uuid4` for UUID PKs (never depend on the DB `gen_random_uuid()` default) — mirror `DownloadTask`.
- `.run/` is gitignored — never commit smoke scripts.

---

## File Structure

**Backend create:** `src/dlw/db/models/ai.py`, `src/dlw/alembic/versions/<rev>_p3sp4a_ai_copilot.py`, `src/dlw/ai/__init__.py`, `src/dlw/ai/tools.py`, `src/dlw/ai/runner.py`, `src/dlw/ai/service.py`, `src/dlw/api/ai.py`, `tests/ai/__init__.py`, `tests/ai/test_tools.py`, `tests/ai/test_stub_runner.py`, `tests/api/test_ai_chat.py`, `tests/api/test_ai_conversations.py`.
**Backend modify:** `src/dlw/db/models/__init__.py`, `src/dlw/alembic/env.py` (add new models to import list), `src/dlw/config.py`, `src/dlw/main.py`, `src/dlw/authz/policy.csv`. (`api/openapi.yaml` is NOT modified — the `/ai/*` paths already exist in the v2.0 contract.)
**Frontend create:** `frontend/src/api/aiClient.ts`, `frontend/src/composables/useCopilot.ts`, `frontend/src/components/copilot/CopilotDrawer.vue` (+ small bubble components inline), `frontend/tests/unit/aiClient.spec.ts`, `frontend/tests/unit/useCopilot.spec.ts`, `frontend/tests/unit/CopilotDrawer.spec.ts`.
**Frontend modify:** the AppShell (toggle button), CommandPalette (⌘K entry), `locale/en-US.json`, `locale/zh-CN.json`.
**Docs modify:** `docs/operator/web-ui.md`.

---

# Milestone M1 — Backend

### Task 1: Config + models + migration

- [ ] **Step 1**: `src/dlw/config.py` — after `gc_grace_seconds` add:

```python
    # UI-SP4a — AI Copilot
    ai_backend: str = Field(default="stub")   # stub | opencode | claude_code | openai_compat
    ai_model_name: str = Field(default="stub-model")
    ai_opencode_bin: str = Field(default="opencode")
    ai_max_tool_iters: int = Field(default=8, ge=1, le=50)
```

- [ ] **Step 2**: Create `src/dlw/db/models/ai.py`:

```python
"""AI Copilot ORM models (UI-SP4a)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer,
                        String, func)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dlw.db.base import Base


class AIConversation(Base):
    __tablename__ = "ai_conversations"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=False)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    messages: Mapped[list["AIMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan")


class AIMessage(Base):
    __tablename__ = "ai_messages"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_conversations.id", ondelete="CASCADE"),
        nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tokens_input: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0)
    tokens_output: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    conversation: Mapped[AIConversation] = relationship(
        back_populates="messages")
```

- [ ] **Step 3**: `src/dlw/db/models/__init__.py` — add import + `__all__` entries:

```python
from dlw.db.models.ai import AIConversation, AIMessage
```
(add `"AIConversation", "AIMessage",` to `__all__`.)

**ALSO** (pre-review B2) add `AIConversation, AIMessage` to the explicit model import list in `src/dlw/alembic/env.py` (the `from dlw.db.models import (...)` block, ~line 14) so `target_metadata` / `alembic check` see the new tables and don't report drift.

- [ ] **Step 4**: Create the migration. Generate a revision id (any 12-hex; use `p3sp4a` mnemonic) — file `src/dlw/alembic/versions/9a1b2c3d4e5f_p3sp4a_ai_copilot.py`:

```python
"""p3sp4a ai copilot

Revision ID: 9a1b2c3d4e5f
Revises: 7636b35e4881
Create Date: 2026-05-21
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9a1b2c3d4e5f"
down_revision: str | None = "7636b35e4881"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.BigInteger(),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("backend", sa.String(32), nullable=False),
        sa.Column("model_name", sa.String(64), nullable=False),
    )
    op.create_index("idx_ai_conv_owner", "ai_conversations",
                    ["owner_user_id", "last_message_at"])
    op.create_index("idx_ai_conv_tenant", "ai_conversations",
                    ["tenant_id", "last_message_at"])
    op.create_table(
        "ai_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ai_conversations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("tokens_input", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("tokens_output", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_ai_msg_conv", "ai_messages",
                    ["conversation_id", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_ai_msg_conv", table_name="ai_messages")
    op.drop_table("ai_messages")
    op.drop_index("idx_ai_conv_tenant", table_name="ai_conversations")
    op.drop_index("idx_ai_conv_owner", table_name="ai_conversations")
    op.drop_table("ai_conversations")
```

- [ ] **Step 5**: Verify models import + alembic linear history:

```bash
cd /d/download_weights && uv run python -c "from dlw.db.models import AIConversation, AIMessage; print('ok')"
DLW_DB_PORT=5433 DLW_DB_USER=postgres DLW_DB_NAME=dlw uv run alembic -c alembic.ini heads 2>&1 | tail -3
```
Expected: import ok; a single head `9a1b2c3d4e5f`. (alembic.ini is at the repo root — pre-review B1; `script_location = src/dlw/alembic`.)

- [ ] **Step 6**: Commit.

```bash
git add src/dlw/config.py src/dlw/db/models/ai.py src/dlw/db/models/__init__.py src/dlw/alembic/versions/9a1b2c3d4e5f_p3sp4a_ai_copilot.py
git commit -q -m "UI-SP4a M1: ai_conversations/ai_messages models + migration + AI config"
```

---

### Task 2: Read-only tool registry

**Files:** Create `src/dlw/ai/__init__.py` (empty), `src/dlw/ai/tools.py`.

- [ ] **Step 1**: Create `src/dlw/ai/tools.py`:

```python
"""AI Copilot read-only tool registry (UI-SP4a). Tools run in-process in
the caller's tenant scope (invariant 15) — they reuse the same
tenant_filtered queries / services as the REST handlers. Audit (invariant
16) is applied by the chat service, not here."""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from dlw.auth.principal import Principal
from dlw.db.models.task import DownloadTask
from dlw.db.tenant_scope import tenant_filtered
from dlw.schemas.task import TaskRead
from dlw.services.quota_read import get_quota_snapshot
from dlw.services.task_detail import events_for_task


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    run: Callable[..., Awaitable[dict]]


async def _list_tasks(session: AsyncSession, principal: Principal, *,
                      status: str | None = None, limit: int = 20) -> dict:
    stmt = tenant_filtered(select(DownloadTask), DownloadTask, principal)
    if status:
        stmt = stmt.where(DownloadTask.status == status)
    stmt = stmt.order_by(DownloadTask.created_at.desc()).limit(
        max(1, min(int(limit), 100)))
    rows = (await session.execute(stmt)).scalars().all()
    return {"items": [TaskRead.model_validate(r).model_dump(mode="json")
                      for r in rows]}


async def _get_task(session: AsyncSession, principal: Principal, *,
                    task_id: str) -> dict:
    tid = uuid.UUID(task_id)
    row = (await session.execute(
        tenant_filtered(select(DownloadTask).where(DownloadTask.id == tid),
                        DownloadTask, principal)
        .options(selectinload(DownloadTask.subtasks)))).scalar_one_or_none()
    if row is None:
        return {"error": "task not found"}
    return TaskRead.model_validate(row).model_dump(mode="json")


async def _get_task_events(session: AsyncSession, principal: Principal, *,
                           task_id: str, limit: int = 20) -> dict:
    tid = uuid.UUID(task_id)
    owned = await session.scalar(
        tenant_filtered(select(DownloadTask.id)
                        .where(DownloadTask.id == tid),
                        DownloadTask, principal))
    if owned is None:
        return {"error": "task not found"}
    items, next_cursor = await events_for_task(
        session, tid, principal.tenant_id,
        max(1, min(int(limit), 50)), None)
    return {"items": [it.model_dump(mode="json") for it in items],
            "next_cursor": next_cursor}


async def _quota_current(session: AsyncSession, principal: Principal) -> dict:
    snap = await get_quota_snapshot(session, principal.tenant_id)
    return snap or {"error": "tenant not found"}


READONLY_TOOLS: dict[str, Tool] = {
    "dlw_list_tasks": Tool(
        "dlw_list_tasks",
        "List the caller's download tasks (optionally filtered by status).",
        {"type": "object", "properties": {
            "status": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100}}},
        _list_tasks),
    "dlw_get_task": Tool(
        "dlw_get_task",
        "Get one download task by id (uuid).",
        {"type": "object", "required": ["task_id"], "properties": {
            "task_id": {"type": "string"}}},
        _get_task),
    "dlw_get_task_events": Tool(
        "dlw_get_task_events",
        "Get recent events for a task.",
        {"type": "object", "required": ["task_id"], "properties": {
            "task_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
        _get_task_events),
    "dlw_quota_current": Tool(
        "dlw_quota_current",
        "Get the caller tenant's current quota usage.",
        {"type": "object", "properties": {}},
        _quota_current),
}
```

- [ ] **Step 2**: Commit (tests come in Task 6 alongside the chat endpoint).

```bash
git add src/dlw/ai/__init__.py src/dlw/ai/tools.py
git commit -q -m "UI-SP4a M1: read-only tool registry (list_tasks/get_task/get_task_events/quota_current; tenant-scoped)"
```

---

### Task 3: AgentRunner abstraction + stub + opencode

**Files:** Create `src/dlw/ai/runner.py`.

- [ ] **Step 1**: Create `src/dlw/ai/runner.py`:

```python
"""AgentRunner abstraction + backends (UI-SP4a).

stub      — deterministic, scripted; CI/tests; no secret, no subprocess.
opencode  — `opencode` CLI subprocess (live backend; binary must be on PATH).
claude_code / openai_compat — structural only in SP4a (raise AIBackendUnavailable).
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


class AIBackendUnavailable(RuntimeError):
    pass


@dataclass
class AgentEvent:
    event: str          # assistant.thinking | tool_call | tool_result
                        # | tool_error | assistant.message_delta | error | done
    data: dict


@dataclass
class AgentContext:
    history: list[dict] = field(default_factory=list)
    user_message: str = ""


class AgentRunner(ABC):
    backend_name: str
    model_name: str

    @abstractmethod
    def run(self, ctx: AgentContext, *, call_tool) -> AsyncIterator[AgentEvent]:
        """Yield the assistant turn's events. `call_tool(name, input) -> dict`
        is supplied by the chat service (tenant-scoped + audited). The stub
        uses it; OpenCodeRunner accepts it but does not yet dispatch tools
        (plain Q&A via subprocess stdout; MCP tool bridge is a follow-on).
        Pre-review I1: call_tool is in the ABC signature so all backends share
        one contract."""
        ...


_TASK_KEYWORDS = ("task", "任务", "download", "下载", "job", "失败", "fail")


class StubAgentRunner(AgentRunner):
    """Deterministic. If the message mentions tasks, calls dlw_list_tasks
    and summarizes; else echoes. Used for all CI/tests."""

    def __init__(self, model_name: str = "stub-model"):
        self.backend_name = "stub"
        self.model_name = model_name

    async def run(self, ctx: AgentContext, *, call_tool) -> AsyncIterator[AgentEvent]:
        msg = ctx.user_message
        if any(k in msg.lower() for k in _TASK_KEYWORDS):
            yield AgentEvent("assistant.thinking",
                             {"text": "Looking up your tasks…"})
            yield AgentEvent("tool_call",
                             {"id": "call_1", "tool": "dlw_list_tasks",
                              "input": {"limit": 20},
                              "requires_confirmation": False})
            result = await call_tool("dlw_list_tasks", {"limit": 20})
            yield AgentEvent("tool_result",
                             {"id": "call_1", "ok": "error" not in result,
                              "output": result})
            n = len(result.get("items", []))
            yield AgentEvent("assistant.message_delta",
                             {"text": f"You have {n} task(s)."})
        else:
            yield AgentEvent("assistant.message_delta",
                             {"text": f"(stub) You said: {msg}"})


class OpenCodeRunner(AgentRunner):
    """`opencode` CLI subprocess. Live backend; binary must be on PATH.
    Exact flags resolved at deploy time against the installed version.
    Raises AIBackendUnavailable if the binary is missing or errors."""

    def __init__(self, settings):
        self.backend_name = "opencode"
        self.model_name = getattr(settings, "ai_model_name", "opencode")
        self._bin = getattr(settings, "ai_opencode_bin", "opencode")

    async def run(self, ctx: AgentContext, *, call_tool) -> AsyncIterator[AgentEvent]:
        import asyncio
        import shutil
        if shutil.which(self._bin) is None:
            raise AIBackendUnavailable(
                f"opencode binary '{self._bin}' not found on PATH")
        # Minimal viable invocation: pass the user message as a prompt and
        # stream stdout lines as message deltas. Tool-use wiring via MCP is a
        # follow-on; SP4a's tested path is the stub. This keeps the live
        # backend usable for plain Q&A while the MCP bridge lands later.
        proc = await asyncio.create_subprocess_exec(
            self._bin, "run", "--print", ctx.user_message,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert proc.stdout is not None
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line:
                    yield AgentEvent("assistant.message_delta", {"text": line})
        finally:
            rc = await proc.wait()
            if rc != 0:
                err = (await proc.stderr.read()).decode("utf-8", "replace") \
                    if proc.stderr else ""
                yield AgentEvent("error",
                                 {"code": "opencode_failed",
                                  "message": err[:500] or f"exit {rc}"})


def build_runner(settings) -> AgentRunner:
    b = getattr(settings, "ai_backend", "stub")
    if b == "stub":
        return StubAgentRunner(getattr(settings, "ai_model_name", "stub-model"))
    if b == "opencode":
        return OpenCodeRunner(settings)
    raise AIBackendUnavailable(f"AI backend '{b}' not wired in SP4a")
```

- [ ] **Step 2**: Commit.

```bash
git add src/dlw/ai/runner.py
git commit -q -m "UI-SP4a M1: AgentRunner abstraction + StubAgentRunner (CI) + OpenCodeRunner (live) + build_runner"
```

---

### Task 4: Chat service (drives runner, persists, audits)

**Files:** Create `src/dlw/ai/service.py`.

- [ ] **Step 1**: Create `src/dlw/ai/service.py`:

```python
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
from dlw.db.tenant_scope import tenant_filtered  # noqa: F401 (parity import)
from dlw.services.audit import write_audit


async def _load_conversation(session, conv_id: uuid.UUID,
                             principal: Principal) -> AIConversation | None:
    row = (await session.execute(
        select(AIConversation).where(
            AIConversation.id == conv_id,
            AIConversation.tenant_id == principal.tenant_id,
            AIConversation.owner_user_id == principal.user_id))
    ).scalar_one_or_none()
    return row


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
        # Pre-review I2: plain Python datetime (assigning func.now() leaves a
        # SQL-expression object on the attr post-commit under expire_on_commit=False).
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
            await write_audit(
                ts, action=f"ai.tool.{name}", resource_type="ai_tool",
                resource_id=str(conv_id), outcome=outcome,
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                payload={"actor_kind": "ai_copilot", "input": tool_input})
            await ts.commit()
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
        # Pre-review I2 (rev#1): on runner failure, emit `error` and RETURN —
        # do NOT persist an empty assistant message or emit `done`. `error` is
        # a stream-terminal event the frontend treats like `done`.
        yield AgentEvent("error", {"code": "runner_failed",
                                   "message": str(exc)})
        return

    # 4. Persist the assistant message + close. Pre-review #1 (rev#2): the
    # persist + `done` are wrapped so a persistence failure still emits a
    # terminal event (the frontend would otherwise hang waiting for `done`).
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
```

- [ ] **Step 2**: Commit.

```bash
git add src/dlw/ai/service.py
git commit -q -m "UI-SP4a M1: chat service — drives runner, tenant-scoped+audited tool exec, conversation persistence"
```

---

### Task 5: API endpoints + router + RBAC + openapi

**Files:** Create `src/dlw/api/ai.py`; modify `src/dlw/main.py`, `src/dlw/authz/policy.csv`, `api/openapi.yaml`.

- [ ] **Step 1**: Create `src/dlw/api/ai.py`:

```python
"""AI Copilot HTTP API (UI-SP4a): chat SSE + conversation history."""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.ai.runner import AIBackendUnavailable, build_runner
from dlw.ai.service import run_chat
from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.models.ai import AIConversation, AIMessage
from dlw.db.session import get_engine

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


class ChatRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str


@router.post("/chat")
async def chat(
    body: ChatRequest,
    principal: Principal = Depends(require_perm("/api/v1/ai*", "POST")),
) -> StreamingResponse:
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="empty message")
    try:
        runner = build_runner(get_settings())
    except AIBackendUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    session_maker = async_sessionmaker(get_engine(), expire_on_commit=False)

    async def _body() -> AsyncIterator[bytes]:
        yield b":open\n\n"
        async for ev in run_chat(
                session_maker=session_maker, principal=principal,
                runner=runner, conversation_id=body.conversation_id,
                message=body.message):
            import json
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
```

- [ ] **Step 2**: `src/dlw/main.py` — register the router (anywhere among the `/api/v1` includes; no collision):

```python
    from dlw.api.ai import router as ai_router
    app.include_router(ai_router)
```

- [ ] **Step 3**: `src/dlw/authz/policy.csv` — add 6 grants (after the executors block):

```
p, role:tenant_admin, /api/v1/ai*, ^(GET|POST)$, tenant_match
p, role:tenant_operator, /api/v1/ai*, ^(GET|POST)$, tenant_match
p, role:tenant_viewer, /api/v1/ai*, ^(GET|POST)$, tenant_match
```

(viewer gets AI too — it is read-only in SP4a; revisit when write tools land.)

- [ ] **Step 4**: `api/openapi.yaml` — **NO CHANGE** (pre-review rev#2 #5). The static contract ALREADY documents `/ai/chat`, `/ai/conversations`, `/ai/conversations/{conversationId}` + `AIChatRequest` (lines ~1673-1790, from the v2.0 contract). The static openapi is the aspirational `/api/v2`-based contract (it carries the full v2.1 vision incl. `context`/`tool_confirmation`); the runtime `ChatRequest` is the SP4a subset — same static-ahead-of-runtime split as every prior SP. **Do NOT add or trim openapi paths** (trimming `AIChatRequest` could break other examples; adding would duplicate). Just confirm `swagger-cli`/spectral still pass unchanged.

- [ ] **Step 5**: Import smoke + commit (note: `api/openapi.yaml` NOT in the add — it is unchanged).

```bash
cd /d/download_weights && uv run python -c "from dlw.api.ai import router; print([r.path for r in router.routes])"
git add src/dlw/api/ai.py src/dlw/main.py src/dlw/authz/policy.csv
git commit -q -m "UI-SP4a M1: /api/v1/ai chat SSE + conversations endpoints + RBAC (openapi /ai/* already in v2.0 contract)"
```
Expected: prints the 3 ai routes.

---

### Task 6: Backend tests

**Files:** Create `tests/ai/__init__.py`, `tests/ai/test_tools.py`, `tests/ai/test_stub_runner.py`, `tests/api/test_ai_chat.py`, `tests/api/test_ai_conversations.py`.

- [ ] **Step 1**: `tests/ai/test_stub_runner.py` — drive `StubAgentRunner` with a fake `call_tool`; assert the exact event sequence for a task query (thinking→tool_call→tool_result→message_delta) and the echo path for a non-task message; assert `build_runner` returns Stub for `stub` and raises `AIBackendUnavailable` for `claude_code`.

- [ ] **Step 2**: `tests/ai/test_tools.py` — bootstrap (Tenant1/2 + Project + User + StorageBackend + a DownloadTask in each tenant, flush parents before tasks per SP5f #43). Assert `dlw_list_tasks` as tenant-1 principal returns only tenant-1 tasks; `dlw_get_task` cross-tenant returns `{"error": ...}`; `dlw_quota_current` returns the 7-key snapshot.

- [ ] **Step 3**: `tests/api/test_ai_chat.py` — module bootstrap (Tenant/Project/User/Storage + 1 task tenant-1); `_set_env` sets `DLW_SYSTEM_JWT_SECRET` + `DLW_AI_BACKEND=stub`. Tests:
  - unauth → 401.
  - POST `{message:"list my tasks"}` (stub) → collect SSE frames; assert event order contains `tool_call` (dlw_list_tasks) then `tool_result` then `assistant.message_delta` then `done`; `done.data.conversation_id` present.
  - after the call, a row exists in `ai_conversations` (owner=1,tenant=1) and ≥2 `ai_messages` (user+assistant).
  - an audit row `ai.tool.dlw_list_tasks` exists with `payload.actor_kind=="ai_copilot"`.
  - empty message → 422.

  Use the `_collect`-style SSE reader from the prior stream tests, but parse `event:`+`data:` frame pairs (not just `data:`). A helper:
```python
async def _collect_events(client, url, headers, body, *, timeout=5.0):
    evs = []
    async with asyncio.timeout(timeout):
        async with client.stream("POST", url, headers=headers, json=body) as r:
            assert r.status_code == 200, await r.aread()
            cur = {}
            async for line in r.aiter_lines():
                if line.startswith("event: "): cur["event"] = line[7:]
                elif line.startswith("data: "):
                    cur["data"] = json.loads(line[6:]); evs.append(cur); cur = {}
                if evs and evs[-1].get("event") == "done": break
    return evs
```

- [ ] **Step 4**: `tests/api/test_ai_conversations.py` — create a conversation via one chat call, then `GET /conversations` (returns ≥1, tenant+owner scoped) and `GET /conversations/{id}` (returns messages); cross-tenant principal → 404 / empty list.

- [ ] **Step 5**: Run the new tests, then commit.

```bash
cd /d/download_weights && uv run pytest tests/ai tests/api/test_ai_chat.py tests/api/test_ai_conversations.py -q 2>&1 | tail -15
git add tests/ai tests/api/test_ai_chat.py tests/api/test_ai_conversations.py
git commit -q -m "UI-SP4a M1: backend tests — tools tenant-isolation, stub runner sequence, chat SSE + persistence + audit, conversations"
```
Expected: all pass.

### M1 full backend gate + dev-DB migration

- [ ] **Step 1**: `cd /d/download_weights && uv run pytest -q 2>&1 | tail -3` — all green (2 known Windows-local failover flakes may appear; CI arbiter).
- [ ] **Step 2**: Apply the migration to the dev DB so the ephemeral controller (M3 smoke) has the tables:
```bash
cd /d/download_weights && DLW_DB_PORT=5433 DLW_DB_USER=postgres DLW_DB_NAME=dlw uv run alembic -c alembic.ini upgrade head 2>&1 | tail -5
```
Expected: upgrades to `9a1b2c3d4e5f`. (alembic.ini at repo root; `script_location = src/dlw/alembic`.)

---

# Milestone M2 — Frontend

### Task 7: aiClient + useCopilot + tests

**Files:** Create `frontend/src/api/aiClient.ts`, `frontend/src/composables/useCopilot.ts`, `frontend/tests/unit/aiClient.spec.ts`, `frontend/tests/unit/useCopilot.spec.ts`.

- [ ] **Step 1**: `aiClient.ts` — `streamChat({message, conversationId, token, onEvent, signal})` using fetch + ReadableStream, parsing `event:`/`data:` frame pairs (reuse the `parseSseChunk` idea but keyed on `event:`; or a local parser). `listConversations()` / `getConversation(id)` via the axios `client`.
- [ ] **Step 2**: `useCopilot.ts` — local reactive state: `messages: Ref<Msg[]>`, `streaming: Ref<boolean>`, `conversationId: Ref<string|null>`; `send(text)` appends a user msg, opens the stream, appends assistant deltas into a growing assistant msg, renders tool-call cards from `tool_call`/`tool_result` events; `loadConversation(id)`.
- [ ] **Step 3**: Specs: `aiClient.spec.ts` (mock fetch ReadableStream → assert parsed event objects); `useCopilot.spec.ts` (mock aiClient.streamChat → assert messages assemble: 1 user + 1 assistant with concatenated deltas + a tool card).
- [ ] **Step 4**: lint + typecheck + run the 2 specs; commit.

```bash
cd /d/download_weights/frontend && pnpm lint && pnpm exec vue-tsc --noEmit && pnpm vitest run tests/unit/aiClient tests/unit/useCopilot 2>&1 | tail -8
cd /d/download_weights && git add frontend/src/api/aiClient.ts frontend/src/composables/useCopilot.ts frontend/tests/unit/aiClient.spec.ts frontend/tests/unit/useCopilot.spec.ts
git commit -q -m "UI-SP4a M2: aiClient (fetch-SSE chat) + useCopilot composable + specs"
```

---

### Task 8: CopilotDrawer + shell/⌘K wiring + i18n + spec

**Files:** Create `frontend/src/components/copilot/CopilotDrawer.vue`, `frontend/tests/unit/CopilotDrawer.spec.ts`; modify the AppShell, CommandPalette, both locale files.

- [ ] **Step 1**: `CopilotDrawer.vue` — right `el-drawer` (`v-model` open prop). Header (title + conversation switcher dropdown). Scrollable message list rendering user bubbles, assistant bubbles (markdown-as-text is fine; no new dep), and read-only tool-call cards (tool name + collapsed JSON output). Footer input + send button (disabled while `streaming`). Uses `useCopilot`.
- [ ] **Step 2**: Mount the drawer in the AppShell with a toggle button ("Copilot"); add a CommandPalette ⌘K entry "Open Copilot" that toggles it. These are additive shell changes (no existing page modified).
- [ ] **Step 3**: i18n — add `copilot.*` keys to both `en-US.json` and `zh-CN.json` at parity (title, placeholder, send, empty-state, toolCall).
- [ ] **Step 4**: `CopilotDrawer.spec.ts` — mount with a mocked `useCopilot`; assert it renders a user bubble, an assistant bubble, and a tool-call card; send button calls `useCopilot.send`.
- [ ] **Step 5**: Full frontend gate + commit.

```bash
cd /d/download_weights/frontend && pnpm lint && pnpm exec vue-tsc --noEmit && pnpm vitest run 2>&1 | tail -8 && pnpm build 2>&1 | tail -3
cd /d/download_weights && git add frontend/src/components/copilot/CopilotDrawer.vue frontend/tests/unit/CopilotDrawer.spec.ts frontend/src/<shell> frontend/src/components/CommandPalette.vue frontend/src/locale/en-US.json frontend/src/locale/zh-CN.json
git commit -q -m "UI-SP4a M2: CopilotDrawer + shell/Cmd-K toggle + i18n parity + spec"
```

---

# Milestone M3 — Smoke + docs

### Task 9: headed Playwright smoke + operator docs

- [ ] **Step 1**: Restart `:8011` controller (`DLW_AI_BACKEND=stub`) with SP4a code (after `alembic upgrade head` on dev DB); restart Vite (clear `node_modules/.vite`).
- [ ] **Step 2**: curl smoke:
```bash
cd /d/download_weights && JWT=$(uv run python -c "from dlw.auth.principal import issue_system_jwt; print(issue_system_jwt(secret='dev-system-jwt-change-me', user_id=1, tenant_id=1, role='tenant_admin', project_ids=[]))" 2>/dev/null | tail -1)
curl -s -N -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" -d '{"message":"list my tasks"}' "http://127.0.0.1:8011/api/v1/ai/chat" | head -c 600; echo
```
Expected: `:open` + `event: assistant.thinking` … `event: tool_call` (dlw_list_tasks) … `event: tool_result` … `event: assistant.message_delta` … `event: done`.
- [ ] **Step 3**: `.run/pw/sp4a-smoke.mjs` — login, open the Copilot drawer (toggle button or ⌘K), type "list my tasks", send, wait, assert an assistant bubble + a `dlw_list_tasks` tool card appear and a `/api/v1/ai/chat` request fired. Screenshot.
- [ ] **Step 4**: Run the smoke (`node .run/pw/sp4a-smoke.mjs`). Expected: OK with the chat request observed.
- [ ] **Step 5**: Append an "AI Copilot (SP4a read-only MVP)" section to `docs/operator/web-ui.md` (backends via `DLW_AI_BACKEND`; read-only tools; invariants 15/16 honored; deferred SP4b-e). Commit docs (NOT the `.run/` script).

```bash
git add docs/operator/web-ui.md
git commit -q -m "UI-SP4a M3: operator docs — AI Copilot read-only MVP"
```

---

# Final cycle (controller-driven)

1. 1 opus whole-impl reviewer (migration + AI pipeline + invariants 15/16 + tenant isolation + CI-without-secret focus; ≤700 words).
2. Fix any HIGH; record MEDIUM/LOW in PR body.
3. `git push -u origin feat/ui-sp4a-ai-copilot-readonly`.
4. `gh pr create` against `main`.
5. Poller waits CI all-green.
6. `gh pr merge <N> --squash --delete-branch`; `git checkout main && git pull --ff-only`.
7. Update `reference_l17728_modelpull.md` (SP4a merge; first migration-bearing UI SP; AgentRunner pluggable; deferred SP4b-e; bump `main` + note alembic head advanced to `9a1b2c3d4e5f`).
8. Update `feedback_subagent_driven_dev.md` (first non-additive UI SP; stub-backend = CI-without-secret pattern; in-process-tools-in-user-scope honoring inv 15; deferring security infra with named follow-on slices).

---

## Self-Review

- **Spec coverage**: every spec section maps to a task (migration→T1, tools→T2, runner→T3, service→T4, api→T5, tests→T6, frontend→T7/T8, smoke/docs→T9). ✓
- **Placeholder scan**: `OpenCodeRunner` flags + openapi block prose + frontend component bodies are described with enough specificity for execution; the only genuinely deferred specifics are opencode CLI flags (resolved against the live binary; stub is the tested path) — bounded, not a vague TODO.
- **Invariants now**: 15 (tools take `principal`, use `tenant_filtered`/tenant_id), 16 (`write_audit ai.tool.*` with `actor_kind`). Deferred-with-owner: 17/40 (SP4b), 37 (SP4c), 18 (SP4d), 19/41 (SP4e).
- **CI safety**: `DLW_AI_BACKEND=stub` in tests; no secret, no subprocess; stub executes real tools so persistence + audit + SSE are covered. alembic only touched on the dev DB (M1 step) — CI pytest uses `create_all`.
- **First migration-bearing UI SP**: down_revision pinned to `7636b35e4881`; models registered in `__init__.py`; Python-side UUID defaults so inserts don't depend on `gen_random_uuid()`.
- **Naming/consistency**: `/api/v1/ai/*` prefix; SSE idiom; `tenant_filtered`; `write_audit`; alembic template; Element Plus drawer; i18n parity.
