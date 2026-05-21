# UI-SP4b — AI Copilot Write Tools + Confirmation Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** Add write tools (`dlw_cancel_task` + `dlw_create_task`) behind a two-phase confirmation gate (inv 17/40). Migration `ai_tool_calls`; phase-1 runner *proposes* (`tool_call_pending_confirm`, no execute); phase-2 `POST /ai/chat {tool_confirmation}` resolves (approve/modify/reject) service-side via the full service-layer path; audited with ai_proposed vs user_final. Frontend confirm card.

**Architecture:** See `docs/superpowers/specs/2026-05-21-ui-sp4b-ai-write-tools-design.md`. Write tools reuse `cancel_task` / `check_quota_for_new_task` + `create_task` services (full validation). The chat service is the sole executor (phase 2); the runner only proposes. Reuses SP4a's SSE/drawer/stub infra.

---

## Conventions

- **Branch:** `feat/ui-sp4b-ai-write-tools` (off `main` @ `5357a6d`, created).
- Bash cwd persists; `cd /d/download_weights && …` (git/py), `cd /d/download_weights/frontend && pnpm …`.
- **Migration checklist** (SP4a #51): down_revision = current single head `9a1b2c3d4e5f`; register `AIToolCall` in `db/models/__init__.py` + `alembic/env.py`; add `ai_tool_calls` to `tests/db/test_alembic.py::EXPECTED_TABLES`; Python `default=uuid.uuid4` PK, **no** `server_default=gen_random_uuid()`; `alembic.ini` at repo root (`-c alembic.ini`); after tests, `alembic upgrade head` on dev DB for smoke.
- **No literal `null` example values** anywhere (SP4a CI lesson). openapi.yaml unchanged (`/ai/chat` confirmation already in v2.0 contract).
- `write_audit(session, *, action, resource_type, resource_id, outcome, tenant_id, actor_user_id, payload)`.
- `create_task` hits HF (`list_repo_tree`) — tests monkeypatch `dlw.services.task_service.list_repo_tree` with `RepoFile(path,size,sha256)` (pattern from `tests/api/test_tasks.py`).

---

## File Structure

**Backend create:** `src/dlw/ai/write_tools.py`, migration `…_p3sp4b_ai_tool_calls.py`, `tests/ai/test_write_tools.py`, `tests/api/test_ai_confirm.py`.
**Backend modify:** `src/dlw/db/models/ai.py` (+`AIToolCall`), `db/models/__init__.py`, `alembic/env.py`, `src/dlw/ai/runner.py` (stub write-proposal), `src/dlw/ai/service.py` (two-phase), `src/dlw/api/ai.py` (ChatRequest + tool_confirmation), `tests/db/test_alembic.py` (EXPECTED_TABLES).
**Frontend create:** `frontend/src/components/copilot/CopilotConfirmCard.vue`, `frontend/tests/unit/CopilotConfirmCard.spec.ts`.
**Frontend modify:** `frontend/src/api/aiClient.ts` (+`confirmTool`), `frontend/src/composables/useCopilot.ts` (pendingConfirm + confirm), `frontend/src/components/copilot/CopilotDrawer.vue` (render card), `frontend/tests/unit/useCopilot.spec.ts` (extend), locale files.
**Docs:** `docs/operator/web-ui.md` (SP4b section).

---

# Milestone M1 — Backend

### Task 1: Migration + AIToolCall model

- [ ] **Step 1**: Add `AIToolCall` to `src/dlw/db/models/ai.py`:

```python
class AIToolCall(Base):
    __tablename__ = "ai_tool_calls"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_messages.id", ondelete="CASCADE"), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_input: Mapped[dict] = mapped_column(JSONB, nullable=False)
    final_input: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requires_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending")
    confirmed_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True)
    confirmation_decision: Mapped[str | None] = mapped_column(
        String(16), nullable=True)
    confirmation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
```

- [ ] **Step 2**: Register `AIToolCall` in `db/models/__init__.py` (import + `__all__`) and `src/dlw/alembic/env.py` (import list).

- [ ] **Step 3**: Create migration `src/dlw/alembic/versions/a2b3c4d5e6f7_p3sp4b_ai_tool_calls.py` (down_revision `9a1b2c3d4e5f`) with the §2.1 DDL (no `server_default` on `id`; `idx_ai_tool_conv` index). downgrade drops index + table.

- [ ] **Step 4**: `tests/db/test_alembic.py` — add `"ai_tool_calls"` to `EXPECTED_TABLES`.

- [ ] **Step 5**: Verify + commit.

```bash
cd /d/download_weights && uv run python -c "from dlw.db.models import AIToolCall; print('ok')"
DLW_DB_PORT=5433 DLW_DB_USER=postgres DLW_DB_NAME=dlw uv run alembic -c alembic.ini heads 2>&1 | tail -2
git add src/dlw/db/models/ai.py src/dlw/db/models/__init__.py src/dlw/alembic/env.py src/dlw/alembic/versions/a2b3c4d5e6f7_p3sp4b_ai_tool_calls.py tests/db/test_alembic.py
git commit -q -m "UI-SP4b M1: ai_tool_calls model + migration (confirmation tracking; proposed/final input for inv 40)"
```
Expected: import ok; single head `a2b3c4d5e6f7`.

---

### Task 2: Write tool registry

**Files:** Create `src/dlw/ai/write_tools.py`.

- [ ] **Step 1**: Create `src/dlw/ai/write_tools.py`:

```python
"""AI Copilot WRITE tool registry (UI-SP4b). Each tool requires
confirmation; execute() reuses the same service-layer path as the REST
handler so all validation (incl. invariant 40 on modified input) runs
naturally. Tenant-scoped via `principal` (inv 15); audited by the chat
service in phase 2 (inv 16 + ai_proposed/user_final for inv 40)."""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.auth.principal import Principal
from dlw.config import get_settings
from dlw.db.models.task import DownloadTask
from dlw.db.models.tenant import Project
from dlw.db.tenant_scope import tenant_filtered
from dlw.schemas.task import TaskCreate
from dlw.services.quota import QuotaExceeded, check_quota_for_new_task
from dlw.services.task_service import cancel_task, create_task


@dataclass
class WriteTool:
    name: str
    description: str
    input_schema: dict
    run: Callable[..., Awaitable[dict]]


async def _cancel(session: AsyncSession, principal: Principal, *,
                  task_id: str) -> dict:
    try:
        tid = uuid.UUID(task_id)
    except (ValueError, TypeError):
        return {"error": "invalid task_id"}
    owned = await session.scalar(
        tenant_filtered(select(DownloadTask.id).where(DownloadTask.id == tid),
                        DownloadTask, principal))
    if owned is None:
        return {"error": "task not found"}
    try:
        task = await cancel_task(session, tid)
    except LookupError:
        return {"error": "task not found"}
    except ValueError:
        return {"error": "task not cancellable (terminal)"}
    await session.commit()
    return {"task_id": str(task.id), "status": task.status}


async def _create(session: AsyncSession, principal: Principal, *,
                  repo_id: str, revision: str, storage_id: int,
                  priority: int = 1,
                  source_strategy: str = "auto_balance") -> dict:
    from dlw.services.hf_metadata import (EmptyRepo, HfNetworkError,
                                          HfPrivateOrAuthRequired, RepoNotFound)
    try:
        await check_quota_for_new_task(session, principal.tenant_id)
    except QuotaExceeded as e:
        return {"error": "quota_exceeded", "metric": e.metric}
    project_id = await session.scalar(
        select(func.min(Project.id)).where(
            Project.tenant_id == principal.tenant_id))
    if project_id is None:
        return {"error": "tenant has no project"}
    body = TaskCreate(repo_id=repo_id, revision=revision,
                      storage_id=int(storage_id), priority=int(priority),
                      source_strategy=source_strategy)
    settings = get_settings()
    try:
        task = await create_task(
            session, body, owner_user_id=principal.user_id,
            tenant_id=principal.tenant_id, project_id=int(project_id),
            hf_endpoint=settings.hf_endpoint, hf_token=settings.hf_token)
    except RepoNotFound:
        return {"error": "repo or revision not found"}
    except HfPrivateOrAuthRequired:
        return {"error": "repo is private / requires auth"}
    except HfNetworkError:
        return {"error": "huggingface unreachable"}
    except EmptyRepo:
        return {"error": "repo has no files at this revision"}
    await session.commit()
    return {"task_id": str(task.id), "status": task.status,
            "repo_id": task.repo_id, "revision": task.revision}


WRITE_TOOLS: dict[str, WriteTool] = {
    "dlw_cancel_task": WriteTool(
        "dlw_cancel_task", "Cancel a running download task.",
        {"type": "object", "required": ["task_id"],
         "properties": {"task_id": {"type": "string"}}}, _cancel),
    "dlw_create_task": WriteTool(
        "dlw_create_task", "Create a new download task (consumes quota).",
        {"type": "object", "required": ["repo_id", "revision", "storage_id"],
         "properties": {"repo_id": {"type": "string"},
                        "revision": {"type": "string"},
                        "storage_id": {"type": "integer"},
                        "priority": {"type": "integer"},
                        "source_strategy": {"type": "string"}}}, _create),
}
```

- [ ] **Step 2**: Commit (tests in Task 5).

```bash
git add src/dlw/ai/write_tools.py
git commit -q -m "UI-SP4b M1: write tool registry (dlw_cancel_task + dlw_create_task; reuse service layer, tenant-scoped, error-mapped)"
```

---

### Task 3: Runner — stub write-proposal

**Files:** Modify `src/dlw/ai/runner.py`.

- [ ] **Step 1**: Extend `StubAgentRunner.run` (BEFORE the existing task-keyword/echo branches) with write-proposal detection. Add module constants + logic:

```python
import re
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_REPO_RE = re.compile(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+")
```

In `run()`, first:

```python
        low = msg.lower()
        if ("cancel" in low or "取消" in low) and _UUID_RE.search(msg):
            tid = _UUID_RE.search(msg).group(0)
            yield AgentEvent("tool_call_pending_confirm", {
                "id": "", "tool": "dlw_cancel_task",
                "input": {"task_id": tid},
                "rationale": f"Cancel task {tid}.",
                "estimated_quota_impact": {}})
            yield AgentEvent("assistant.message_delta",
                             {"text": "Please confirm the cancellation."})
            return
        if ("create" in low or "download" in low or "下载" in low) and _REPO_RE.search(msg):
            repo = _REPO_RE.search(msg).group(0)
            yield AgentEvent("tool_call_pending_confirm", {
                "id": "", "tool": "dlw_create_task",
                "input": {"repo_id": repo,
                          "revision": "0" * 40, "storage_id": 1},
                "rationale": f"Create a download task for {repo}.",
                "estimated_quota_impact": {"bytes": 0}})
            yield AgentEvent("assistant.message_delta",
                             {"text": "Please confirm the new task."})
            return
```

(The `id` is filled by the service when it persists the pending call; the stub emits `""` and the service overwrites it.)

- [ ] **Step 2**: Commit.

```bash
git add src/dlw/ai/runner.py
git commit -q -m "UI-SP4b M1: stub runner proposes write tools (tool_call_pending_confirm for cancel/create), never executes"
```

---

### Task 4: Two-phase chat service + confirmation handling

**Files:** Modify `src/dlw/ai/service.py`.

- [ ] **Step 1**: In `run_chat`, when forwarding runner events, intercept `tool_call_pending_confirm`: persist a pending `AIToolCall` row (conversation_id, tool_name, proposed_input=input, status="pending"), set the event's `data["id"]` to the new row id BEFORE yielding, and record it (so the assistant message content includes it). Do NOT execute. Pseudocode inside the runner loop:

```python
            elif ev.event == "tool_call_pending_confirm":
                async with session_maker() as ps:
                    call = AIToolCall(
                        conversation_id=conv_id,
                        tool_name=ev.data.get("tool"),
                        proposed_input=ev.data.get("input") or {},
                        requires_confirmation=True, status="pending")
                    ps.add(call); await ps.commit()
                    call_id = str(call.id)
                ev.data["id"] = call_id
                tool_calls.append({"id": call_id, "tool": ev.data.get("tool"),
                                   "input": ev.data.get("input"),
                                   "pending_confirm": True})
            yield ev
```

- [ ] **Step 2**: Add a `run_confirmation` async generator (phase 2), and route to it from the endpoint when `tool_confirmation` is present:

```python
from dlw.ai.write_tools import WRITE_TOOLS
from dlw.db.models.ai import AIConversation, AIToolCall

async def run_confirmation(
    *, session_maker, principal, conversation_id, call_id, decision,
    modified_input,
) -> AsyncIterator[AgentEvent]:
    async with session_maker() as s:
        call = (await s.execute(
            select(AIToolCall).join(
                AIConversation, AIToolCall.conversation_id == AIConversation.id)
            .where(AIToolCall.id == call_id,
                   AIToolCall.conversation_id == conversation_id,
                   AIConversation.tenant_id == principal.tenant_id,
                   AIConversation.owner_user_id == principal.user_id))
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
        # mark decision metadata
        call.confirmation_decision = decision
        call.confirmed_by_user_id = principal.user_id
        call.confirmation_at = datetime.now(UTC)
        if decision == "rejected":
            call.status = "rejected"
            await s.commit()
        await s.commit() if decision == "rejected" else None

    if decision == "rejected":
        async with session_maker() as a:
            await write_audit(
                a, action=f"ai.tool.{tool_name}", resource_type="ai_tool",
                resource_id=str(call_id), outcome="rejected",
                tenant_id=principal.tenant_id, actor_user_id=principal.user_id,
                payload={"actor_kind": "ai_copilot",
                         "ai_proposed_input": proposed})
            await a.commit()
        yield AgentEvent("assistant.message_delta",
                         {"text": "Operation cancelled."})
        yield AgentEvent("done", {"conversation_id": str(conversation_id),
                                  "ai_message_id": "", "tokens_used": 0})
        return

    # approved | modified — execute via the service layer (inv 40 full revalidation)
    final_input = proposed if decision == "approved" else (modified_input or {})
    tool = WRITE_TOOLS.get(tool_name)
    async with session_maker() as xs:
        if tool is None:
            out = {"error": f"unknown tool {tool_name}"}
        else:
            try:
                out = await tool.run(xs, principal, **final_input)
            except Exception as exc:  # noqa: BLE001
                out = {"error": str(exc)}
    ok = "error" not in out
    async with session_maker() as us:
        call2 = await us.get(AIToolCall, call_id)
        call2.final_input = final_input
        call2.output = out
        call2.status = "executed" if ok else "error"
        if not ok:
            call2.error_code = str(out.get("error"))[:64]
        await us.commit()
    async with session_maker() as a:
        await write_audit(
            a, action=f"ai.tool.{tool_name}", resource_type="ai_tool",
            resource_id=str(call_id), outcome="success" if ok else "error",
            tenant_id=principal.tenant_id, actor_user_id=principal.user_id,
            payload={"actor_kind": "ai_copilot", "ai_proposed_input": proposed,
                     "user_final_input": final_input})
        await a.commit()
    yield AgentEvent("tool_result", {"id": str(call_id), "ok": ok,
                                     "output": out})
    yield AgentEvent("assistant.message_delta",
                     {"text": "Done." if ok else f"Failed: {out.get('error')}"})
    yield AgentEvent("done", {"conversation_id": str(conversation_id),
                              "ai_message_id": "", "tokens_used": 0})
```

(Clean up the rejected-commit double-call when implementing — commit once. The pseudocode above is illustrative; the implementer simplifies the rejected branch to a single commit.)

- [ ] **Step 3**: Commit.

```bash
git add src/dlw/ai/service.py
git commit -q -m "UI-SP4b M1: two-phase chat — phase1 persists pending tool call (no execute); phase2 run_confirmation executes via service layer (inv 40) + audits proposed/final"
```

---

### Task 5: Endpoint wiring (ChatRequest + tool_confirmation route)

**Files:** Modify `src/dlw/api/ai.py`.

- [ ] **Step 1**: Extend `ChatRequest` with `tool_confirmation: ToolConfirmation | None = None` and make `message: str | None = None`. Add the `ToolConfirmation` model (call_id: uuid, decision: Literal[...], modified_input: dict | None). In `chat()`:
  - if `body.tool_confirmation` is set: require `conversation_id`; stream via `run_confirmation(...)`.
  - else: require non-blank `message` (422); stream via `run_chat(...)` as before.
  Keep the same SSE framing + `build_runner` (only needed for phase 1).

- [ ] **Step 2**: Import smoke + commit.

```bash
cd /d/download_weights && uv run python -c "from dlw.api.ai import router; print('ok')"
git add src/dlw/api/ai.py
git commit -q -m "UI-SP4b M1: /ai/chat accepts tool_confirmation → phase-2 confirmation route"
```

---

### Task 6: Backend tests

**Files:** Create `tests/ai/test_write_tools.py`, `tests/api/test_ai_confirm.py`.

- [ ] **Step 1**: `tests/ai/test_write_tools.py` — bootstrap (2 tenants + tasks). Tests: `_cancel` cross-tenant → `{"error":"task not found"}` (task still running); `_cancel` owned → status cancelling; `_create` with `monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake)` (returns 1 `RepoFile`) → `{task_id,...}`; `_create` quota-exceeded (seed Tenant with `quota_concurrent=0` + an active task, or monkeypatch `check_quota_for_new_task` to raise) → `{"error":"quota_exceeded"}`.

- [ ] **Step 2**: `tests/api/test_ai_confirm.py` — bootstrap (Tenant1/2 + a running task T1 in tenant 1). `_set_env` sets secret + `DLW_AI_BACKEND=stub`. Helper `_collect_events` (reuse SP4a's). Tests:
  - **phase1 proposes, no execute**: POST `{message:"cancel <T1-uuid>"}` → events contain `tool_call_pending_confirm` (tool=dlw_cancel_task, data.id is a uuid); T1 still `running`; exactly one `ai_tool_calls` row status=pending. Capture `call_id = pending.data["id"]`.
  - **phase2 approved**: POST `{conversation_id, tool_confirmation:{call_id, decision:"approved"}}` → `tool_result.ok==true`; T1 now `cancelling`; `ai_tool_calls` status=executed, confirmation_decision=approved, confirmed_by_user_id=1; an `ai.tool.dlw_cancel_task` audit row `payload.actor_kind=="ai_copilot"` with `ai_proposed_input` + `user_final_input`.
  - **phase2 rejected** (fresh task T2): propose cancel → reject → T2 still running; status=rejected; no audit success.
  - **phase2 modified** (inv 40): propose cancel for T-a; confirm with `modified_input:{task_id: T-b}` → T-b cancelled (not T-a); audit payload `ai_proposed_input.task_id==T-a` and `user_final_input.task_id==T-b`.
  - **cross-tenant confirm**: tenant-2 principal confirms tenant-1's pending call_id → error/404; the call stays pending.
  - **double-confirm**: approve then approve again → second → `already_resolved` error.
  - **unauth** phase1 + phase2 → 401.

- [ ] **Step 3**: Run + commit.

```bash
cd /d/download_weights && uv run pytest tests/ai/test_write_tools.py tests/api/test_ai_confirm.py -q 2>&1 | tail -12
git add tests/ai/test_write_tools.py tests/api/test_ai_confirm.py
git commit -q -m "UI-SP4b M1: backend tests — write-tool tenant isolation, two-phase propose/approve/reject/modify, cross-tenant + double-confirm guards, audit proposed/final"
```

### M1 full backend gate + dev-DB migration

- [ ] **Step 1**: `cd /d/download_weights && uv run pytest -q 2>&1 | tail -3` — all green.
- [ ] **Step 2**: `DLW_DB_PORT=5433 DLW_DB_USER=postgres DLW_DB_NAME=dlw uv run alembic -c alembic.ini upgrade head 2>&1 | tail -3` — dev DB → `a2b3c4d5e6f7`.

---

# Milestone M2 — Frontend

### Task 7: aiClient.confirmTool + useCopilot pendingConfirm/confirm + tests

- [ ] **Step 1**: `aiClient.ts` — add `confirmTool({conversationId, callId, decision, modifiedInput, onEvent})` (same fetch-SSE POST with `{conversation_id, tool_confirmation:{call_id, decision, modified_input}}`).
- [ ] **Step 2**: `useCopilot.ts` — on `tool_call_pending_confirm`, attach a `pendingConfirm = {callId, tool, input, rationale, estimatedImpact}` to the current assistant message (and stop, awaiting user). Add `confirm(decision, modifiedInput?)`: clears the pendingConfirm, opens phase-2 stream via `confirmTool`, appends `tool_result`/delta into a new assistant message.
- [ ] **Step 3**: Extend `useCopilot.spec.ts`: a `tool_call_pending_confirm` event → message gets `pendingConfirm`; `confirm("approved")` calls `confirmTool` and assembles the result.
- [ ] **Step 4**: lint + tsc + run specs; commit.

```bash
cd /d/download_weights/frontend && pnpm lint && pnpm exec vue-tsc --noEmit && pnpm vitest run tests/unit/aiClient tests/unit/useCopilot 2>&1 | tail -8
cd /d/download_weights && git add frontend/src/api/aiClient.ts frontend/src/composables/useCopilot.ts frontend/tests/unit/useCopilot.spec.ts
git commit -q -m "UI-SP4b M2: aiClient.confirmTool + useCopilot pendingConfirm/confirm (two-phase)"
```

### Task 8: CopilotConfirmCard + drawer wiring + i18n + spec

- [ ] **Step 1**: `CopilotConfirmCard.vue` — props `{tool, input, rationale, estimatedImpact}` + emits `approve` / `reject` / `modify(modifiedInput)`. Renders tool name, pretty-JSON input, rationale, impact; buttons Approve/Reject/Modify; Modify reveals an editable JSON `<el-input type="textarea">` (parse on submit; on parse error show inline message).
- [ ] **Step 2**: `CopilotDrawer.vue` — when an assistant message has `pendingConfirm`, render `<CopilotConfirmCard>` wired to `copilot.confirm(...)`. Disable the composer while a confirm is outstanding.
- [ ] **Step 3**: i18n `copilot.confirm.*` (title/approve/reject/modify/rationale/impact/modifyHint) in both locales at parity.
- [ ] **Step 4**: `CopilotConfirmCard.spec.ts` — renders input/rationale; Approve emits `approve`; Modify with edited JSON emits `modify` with parsed object.
- [ ] **Step 5**: Full frontend gate + commit.

```bash
cd /d/download_weights/frontend && pnpm lint && pnpm exec vue-tsc --noEmit && pnpm vitest run 2>&1 | tail -8 && pnpm build 2>&1 | tail -3
cd /d/download_weights && git add frontend/src/components/copilot/CopilotConfirmCard.vue frontend/src/components/copilot/CopilotDrawer.vue frontend/tests/unit/CopilotConfirmCard.spec.ts frontend/src/locale/en-US.json frontend/src/locale/zh-CN.json
git commit -q -m "UI-SP4b M2: CopilotConfirmCard (approve/reject/modify) + drawer wiring + i18n parity"
```

---

# Milestone M3 — Smoke + docs

### Task 9: headed Playwright smoke + operator docs

- [ ] **Step 1**: Restart `:8011` (stub, after dev-DB `alembic upgrade head`) + Vite (clear `.vite`).
- [ ] **Step 2**: curl two-phase smoke: phase1 `{"message":"cancel <a real running task uuid from tenant 1>"}` → see `tool_call_pending_confirm` + grab `id`; phase2 `{"conversation_id":"...","tool_confirmation":{"call_id":"<id>","decision":"approved"}}` → see `tool_result` ok + the task flips to cancelling (verify via `GET /api/v1/tasks/<id>`). (Pick a cancellable task or submit a sim one.)
- [ ] **Step 3**: `.run/pw/sp4b-smoke.mjs` — open drawer, send "cancel <uuid>", assert a confirm card appears (`data-test="copilot-confirm-card"`), click Approve, assert a `tool_result`/assistant follow-up + a phase-2 `/ai/chat` request with `tool_confirmation`. Screenshot.
- [ ] **Step 4**: Run smoke. Expected OK.
- [ ] **Step 5**: Append SP4b section to `docs/operator/web-ui.md` (write tools, confirmation gate, inv 17/40, two-phase protocol). Commit docs (NOT `.run/`).

```bash
git add docs/operator/web-ui.md
git commit -q -m "UI-SP4b M3: operator docs — AI write tools + confirmation gate (inv 17/40)"
```

---

# Final cycle (controller-driven)

1. 1 opus reviewer (two-phase flow + inv 17/40 + pending-call isolation + migration; ≤700 words).
2. Fix HIGH; record MEDIUM/LOW in PR body.
3. push; `gh pr create`; CI-wait green; squash-merge `--delete-branch`; sync main.
4. Update `reference_l17728_modelpull.md` (SP4b merge; alembic head `a2b3c4d5e6f7`; SP4c-e still deferred) + `feedback_subagent_driven_dev.md` (two-phase confirmation pattern; service-side-not-LLM resolution; inv-40 via always-full-service-execute).

---

## Self-Review

- **Spec coverage**: migration→T1, write tools→T2, stub propose→T3, two-phase service→T4, endpoint→T5, tests→T6, frontend→T7/T8, smoke/docs→T9. ✓
- **Placeholder scan**: the Task 4 `run_confirmation` rejected-branch double-commit is flagged as illustrative ("simplify to one commit") — the implementer cleans it. The create-task offline test uses the proven `list_repo_tree` monkeypatch. No vague TODOs.
- **Invariants now**: 17 (phase-1 never executes; only phase-2 approved/modified does), 40 (execute always runs full service layer on final_input; audit records proposed+final), 15 (writes tenant-scoped via principal), 16 (audited). Pending-call lookup joins ai_conversations for tenant+owner scope (can't confirm others' calls).
- **Migration**: SP4a #51 checklist applied (down_revision `9a1b2c3d4e5f`; 2-place registration; EXPECTED_TABLES; Python uuid PK no server_default).
- **No openapi change / no literal nulls** (SP4a CI lesson).
