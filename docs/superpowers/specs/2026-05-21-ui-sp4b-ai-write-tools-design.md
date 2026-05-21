# UI-SP4b — AI Copilot Write Tools + Confirmation Gate (Design)

> Second slice of the v2.1 AI Copilot (after SP4a read-only MVP). Adds
> the **headline "do X by asking" flow**: the assistant can propose
> write operations, the user confirms (or modifies/rejects) via a card,
> and only then does the operation execute — through the full
> service-layer validation path.
> Status: design self-approved per Rule #1; continuation of the SP4
> deferred slices (SP4b = inv 17/40).
> Branch: `feat/ui-sp4b-ai-write-tools`.

## 1. Context & Scope

SP4a shipped read-only tools (no side effects, no confirmation). SP4b
adds **write tools** behind a mandatory confirmation gate, honoring:

- 🔒 **Invariant 17**: every side-effecting tool call is shown to the
  user as a card and executes only on explicit confirm. No write
  without confirmation.
- 🔒 **Invariant 40**: when the user *modifies* the proposed input, the
  modified input re-runs the **full service-layer validation** (no
  reuse of the AI's rationale/conclusion); the audit entry records both
  `ai_proposed_input` and `user_final_input`.

**In scope (additive, 1 migration):**

1. **Migration** `ai_tool_calls` (confirmation tracking). Chains off
   `9a1b2c3d4e5f`.
2. **Write tool registry** (`src/dlw/ai/write_tools.py`): `dlw_cancel_task`
   (clean proof of the gate) + `dlw_create_task` (headline). Each:
   `requires_confirmation=True`, `execute(session, principal, **input)
   -> dict` reusing the **same service-layer path** as the REST handler
   (`cancel_task`; `check_quota_for_new_task` + project-resolve +
   `create_task`) so all validation runs naturally.
3. **Two-phase chat** (`src/dlw/ai/service.py`):
   - **Phase 1** (`POST /ai/chat {message}`): the runner may emit a
     `tool_call_pending_confirm` event for a write tool. The service
     persists a **pending** `ai_tool_calls` row and forwards the event —
     **it does NOT execute**. The turn ends at `done`.
   - **Phase 2** (`POST /ai/chat {conversation_id, tool_confirmation}`):
     the service (NOT the runner) loads the pending call (tenant+owner
     scoped), and on `approved`/`modified` executes via the service
     layer (full validation), on `rejected` records and skips. Audits
     `ai.tool.<name>` with `actor_kind="ai_copilot"`,
     `actor_user_id=<the confirming human>`, payload carrying
     `ai_proposed_input` + `user_final_input`. Emits `tool_result` +
     assistant message + `done`.
4. **Frontend**: a confirm card in the drawer (Approve / Reject /
   Modify-as-JSON) that fires the phase-2 request.

**Out of scope (later slices):** other write tools (retry/upgrade/
set_priority/gated — trivial adds once the gate exists); sandboxed MCP
(SP4c); token budget (SP4d); external-content tools (SP4e). `ai_token_usage`
table (SP4d).

## 2. The Two-Phase Confirmation Protocol (centerpiece)

### 2.1 `ai_tool_calls` (SP4b subset)

```python
op.create_table(
    "ai_tool_calls",
    sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),  # uuid4 app-side
    sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
              sa.ForeignKey("ai_conversations.id", ondelete="CASCADE"),
              nullable=False),                       # scoped lookup in phase 2
    sa.Column("message_id", postgresql.UUID(as_uuid=True),
              sa.ForeignKey("ai_messages.id", ondelete="CASCADE"),
              nullable=True),                        # the proposing assistant msg
    sa.Column("tool_name", sa.String(64), nullable=False),
    sa.Column("proposed_input", postgresql.JSONB(), nullable=False),   # inv 40: ai_proposed
    sa.Column("final_input", postgresql.JSONB(), nullable=True),       # inv 40: user_final
    sa.Column("output", postgresql.JSONB(), nullable=True),
    sa.Column("error_code", sa.String(64), nullable=True),
    sa.Column("requires_confirmation", sa.Boolean(), nullable=False,
              server_default=sa.true()),
    sa.Column("status", sa.String(16), nullable=False,
              server_default="pending"),  # pending|executed|rejected|error
    sa.Column("confirmed_by_user_id", sa.BigInteger(),
              sa.ForeignKey("users.id"), nullable=True),
    sa.Column("confirmation_decision", sa.String(16), nullable=True),  # approved|rejected|modified
    sa.Column("confirmation_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True),
              server_default=sa.func.now(), nullable=False),
)
op.create_index("idx_ai_tool_conv", "ai_tool_calls", ["conversation_id"])
```

Model `AIToolCall` in `src/dlw/db/models/ai.py`; registered in
`db/models/__init__.py` + `alembic/env.py`. **Python `default=uuid.uuid4`
PK, no `server_default` gen_random_uuid** (SP4a CRITICAL-1 lesson).
`tests/db/test_alembic.py::EXPECTED_TABLES` += `ai_tool_calls`.

### 2.2 SSE events (additive to SP4a's set)

- `tool_call_pending_confirm` — `{id, tool, input, rationale,
  estimated_quota_impact}` (id = the `ai_tool_calls.id`). Frontend shows
  a card; **terminal-ish**: the turn ends at the following `done`.
- Phase 2 reuses `tool_result` / `assistant.message_delta` / `error` /
  `done`.

### 2.3 Request shape

```python
class ToolConfirmation(BaseModel):
    call_id: uuid.UUID
    decision: Literal["approved", "rejected", "modified"]
    modified_input: dict | None = None

class ChatRequest(BaseModel):           # extends SP4a
    conversation_id: uuid.UUID | None = None
    message: str | None = None          # required iff tool_confirmation is None
    tool_confirmation: ToolConfirmation | None = None
```

Endpoint: if `tool_confirmation` is set → phase 2 (conversation_id
required); else → phase 1 (message required; 422 if blank).

### 2.4 Phase-2 execution (the invariant-40 core)

```
load AIToolCall by call_id JOIN ai_conversations (tenant_id==principal.tenant_id
    AND owner_user_id==principal.user_id); 404 if missing/cross-tenant.
if status != "pending": -> error "already resolved"
decision == rejected:
    status=rejected; confirmation_decision=rejected; confirmed_by; confirmation_at
    audit ai.tool.<name> outcome="rejected" payload{actor_kind, ai_proposed_input}
    emit assistant.message_delta "已取消该操作。"; done
decision in (approved, modified):
    final_input = proposed_input if approved else modified_input
    # inv 40: execute() ALWAYS runs the full service-layer path; modified_input
    # is validated end-to-end (repo/revision/license/quota) — no AI shortcut.
    out = await WRITE_TOOLS[name].execute(session, principal, **final_input)
    ok = "error" not in out
    final_input persisted; output=out; status="executed" if ok else "error"
    audit ai.tool.<name> outcome=success|error
        payload{actor_kind:"ai_copilot", ai_proposed_input, user_final_input}
        actor_user_id = principal.user_id   # the confirming human
    emit tool_result{ok, output}; assistant.message_delta(summary); done
```

`execute()` for each write tool maps service exceptions to
`{"error": ...}` (never raises into the stream).

## 3. Write Tools

`src/dlw/ai/write_tools.py`:

- `dlw_cancel_task(task_id)` — tenant gate (`tenant_filtered(select(
  DownloadTask.id)...)`) → `cancel_task(session, task_id)` → commit;
  `LookupError`→`{error:"task not found"}`, `ValueError`→`{error:
  "task not cancellable"}`. Returns `{task_id, status:"cancelling"}`.
- `dlw_create_task(repo_id, revision, storage_id, priority?,
  source_strategy?)` — `check_quota_for_new_task` (QuotaExceeded→error);
  resolve default project (inline min-project query; none→error);
  `create_task(session, TaskCreate(**), owner_user_id, tenant_id,
  project_id, hf_endpoint, hf_token)` → commit; map RepoNotFound/
  HfPrivateOrAuthRequired/HfNetworkError/EmptyRepo → `{error:...}`.
  Returns `{task_id, status, repo_id, revision}`.

Both are **tenant-scoped via `principal`** (invariant 15 still holds for
writes) and **audited** in phase 2 (invariant 16 + the proposed/final
distinction for 40).

## 4. Runner (stub two-phase) + build_runner

The `StubAgentRunner` (SP4a) gains write-proposal behavior:
- message contains "cancel"/"取消" + a uuid-ish token → emit
  `tool_call_pending_confirm` for `dlw_cancel_task` with the parsed
  task_id + rationale; then `assistant.message_delta` "请确认取消" → end.
- message contains "create"/"download"/"下载" + an `org/repo`-ish token →
  emit `tool_call_pending_confirm` for `dlw_create_task` (proposed
  input from parse or placeholders) + estimated_quota_impact → end.
- else → SP4a read-only behavior unchanged.

The runner only PROPOSES (emits pending_confirm); it never executes.
`OpenCodeRunner` is unchanged (plain Q&A; write-proposal via MCP is a
follow-on). The chat service is the sole executor (phase 2).

## 5. Frontend

- `aiClient`: `confirmTool(conversationId, {call_id, decision,
  modified_input?}, onEvent)` — same SSE POST with `tool_confirmation`.
- `useCopilot`: a `tool_call_pending_confirm` event becomes a
  `pendingConfirm` card on the assistant message; `confirm(decision,
  modifiedInput?)` fires phase 2 and appends the resulting events.
- `CopilotConfirmCard.vue` (in the drawer): shows tool name, input
  (pretty JSON), rationale, estimated impact; buttons **Approve** /
  **Reject** / **Modify** (Modify reveals an editable JSON textarea →
  sends `decision:"modified", modified_input`).
- i18n parity (`copilot.confirm.*`) in both locales.

## 6. Tests

**Backend** (`tests/ai/test_write_tools.py`, `tests/api/test_ai_confirm.py`):
- `dlw_cancel_task.execute` tenant isolation (cross-tenant → error,
  no cancel); cancels an owned task.
- `dlw_create_task.execute` happy path (use a simulation/echo repo or
  mock HF? — see §7 risk) + quota-exceeded → error.
- Phase 1: a "cancel <uuid>" message → SSE contains
  `tool_call_pending_confirm` (tool=dlw_cancel_task), NO cancel applied
  yet (task still running), one `ai_tool_calls` row status=pending.
- Phase 2 approved: `tool_confirmation{approved}` → task now cancelling;
  `ai_tool_calls` status=executed, confirmation_decision=approved,
  confirmed_by=user; an `ai.tool.dlw_cancel_task` audit row with
  `payload.actor_kind=="ai_copilot"`.
- Phase 2 rejected: task stays running; status=rejected; no cancel.
- Phase 2 modified: `modified_input` with a *different* task_id →
  execute re-validates against the new id (inv 40); audit payload has
  both `ai_proposed_input` and `user_final_input`.
- Cross-tenant/owner confirmation of someone else's pending call → 404
  / error (can't confirm a call you don't own).
- Unauth phase-1 and phase-2 → 401.

**Frontend** (`CopilotConfirmCard.spec.ts`, `useCopilot` extension):
pending_confirm → card renders; Approve calls `confirmTool`; Modify
sends modified_input.

## 7. Risks & Contingencies

- **`dlw_create_task` hits HuggingFace** in `create_task` (HF
  resolution). Tests must avoid live HF: use a **simulation task**
  (`is_simulation`) path if one exists, OR assert the create tool's
  *confirmation/audit machinery* with cancel_task (no network) as the
  primary gate proof, and test `dlw_create_task.execute` only for the
  **quota-exceeded** + **error-mapping** branches (which fail before HF)
  — confirm at execution time which create paths can run offline. The
  gate's correctness does not depend on create succeeding.
- **Pending-call scoping** (invariant 17 integrity): the phase-2 lookup
  MUST join through `ai_conversations` and filter tenant+owner, so a
  user cannot confirm/execute a write proposed in another user's
  conversation. Explicitly tested.
- **Double-execute**: `status != "pending"` guard makes confirmation
  idempotent (a second confirm of the same call → error).
- **Migration**: second AI migration; same checklist as SP4a
  (down_revision = current single head `9a1b2c3d4e5f`; register model in
  2 places; EXPECTED_TABLES; Python uuid PK no server_default; dev-DB
  upgrade for smoke).
- **openapi.yaml**: the `/ai/chat` contract already documents
  `tool_confirmation` + `tool_call_pending_confirm` (v2.0 contract) — no
  openapi change; and **no literal `null` example values** may be
  introduced (SP4a CI lesson).
- **Spectral pin**: already `@6.11.1` in CI (SP4a) — unaffected.

## 8. Self-Review

- **Placeholder scan**: the only deferred specific is the create-task
  offline-test strategy (§7) — bounded, resolved at execution against
  the actual `create_task`/simulation path.
- **Invariants now**: 17 (confirm gate — write tools never execute in
  phase 1; only phase-2 approved/modified executes), 40 (modified_input
  full re-validation via service layer + proposed/final audit), plus
  carried-over 15 (writes still tenant-scoped via principal) + 16
  (audited). Deferred: 37 (SP4c), 18 (SP4d), 19/41 (SP4e).
- **Consistency**: reuses SP4a's runner/service/SSE/drawer; the
  confirmation resolution is service-side (not LLM), so it's
  deterministic and testable with the stub.
- **Scope**: the confirmation machinery + 2 write tools. Larger than a
  read-only add, but the gate is the bulk; the 2nd tool is incremental.
