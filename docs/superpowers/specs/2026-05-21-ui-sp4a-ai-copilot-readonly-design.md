# UI-SP4a — AI Copilot Read-Only MVP (Design)

> First slice of the v2.1 AI Copilot flagship (`docs/v2.0/12-ai-copilot.md`).
> First migration-bearing UI sub-project (every prior UI SP was additive /
> zero-migration). Delivers a working, testable conversational read-only
> assistant end-to-end; write tools, confirmation gates, sandboxed MCP,
> token budgets, and external-content tools are deferred to later SP4
> slices.
> Status: design self-approved per Rule #1; scope (read-only MVP) +
> live backend (OpenCode) confirmed with the user.
> Branch: `feat/ui-sp4a-ai-copilot-readonly`.

## 1. Context & Scope

The AI Copilot lets users drive modelpull in natural language ("which
tasks failed last week?", "show me the DeepSeek-V3 download progress").
The full v2.1 design is a multi-SP epic with heavy security machinery.
SP4a is the smallest credible end-to-end slice: a **read-only**
assistant that can answer questions by calling existing read-only
services in the user's own JWT scope, with the conversation persisted
and streamed to a Vue chat drawer.

**In scope:**

1. **Migration** (`ai_conversations`, `ai_messages`) — chains off
   `7636b35e4881`. First UI migration. Tool calls/results are stored
   inside `ai_messages.content` JSONB (the separate `ai_tool_calls`
   confirmation-tracking table is deferred to the write-ops slice).

2. **`AgentRunner` abstraction** (`src/dlw/ai/runner.py`) selected by
   `DLW_AI_BACKEND`:
   - `stub` — `StubAgentRunner`: deterministic, scripted; drives all
     tests + CI with **no secret and no subprocess**. Always available.
   - `opencode` — `OpenCodeRunner`: spawns `opencode run …` as a
     subprocess (the user-chosen live backend). Live only when the
     `opencode` binary is on PATH; CI uses `stub`.
   - `claude_code`, `openai_compat` — **structural stubs** in this SP:
     the registry recognizes them and raises a clear
     `AIBackendUnavailable` ("backend X not wired in SP4a") so the
     abstraction is proven extensible without implementing all
     subprocess/HTTP loops now.

3. **Read-only tool registry** (`src/dlw/ai/tools.py`) — 4 tools, each
   an async fn `(session, principal, **input) -> dict`, executed
   **in-process** in the caller's tenant scope (invariant 15) and
   **audited** with an `ai.tool.<name>` action (invariant 16):
   - `dlw_list_tasks(status?, limit?)`
   - `dlw_get_task(task_id)`
   - `dlw_get_task_events(task_id, limit?)`
   - `dlw_quota_current()`

4. **`POST /api/v1/ai/chat`** — SSE endpoint (reuses the proven SSE
   idiom). `GET /api/v1/ai/conversations` + `GET
   /api/v1/ai/conversations/{id}` for history.

5. **Vue chat drawer** — right `el-drawer` opened from the AppShell +
   ⌘K (CommandPalette already exists). Conversation list, message
   bubbles, read-only tool-call cards, streaming via a small fetch-SSE
   client.

**Out of scope (deferred — explicit follow-on slices):**

- **SP4b**: write tools (`dlw_create_task`/`cancel`/`retry`/`upgrade`)
  + `tool_call_pending_confirm` protocol + `ai_tool_calls` table
  (invariants 17, 40).
- **SP4c**: sandboxed-MCP subprocess (invariant 37). SP4a calls tools
  **in-process** — documented as an MVP simplification; the tool
  registry is structured so a future MCP server wraps the same fns.
- **SP4d**: token-budget quota (`tenants.quota_ai_tokens_month`,
  invariant 18). SP4a records `tokens_input/output` on `ai_messages`
  (columns present) but does not enforce a budget.
- **SP4e**: T2 external-content tools (`hf_model_card`,
  `fetch_user_content`, `web_search`) + prompt-injection sanitization
  (invariants 19, 41). SP4a's tools return **internal** data only (no
  external user content enters the LLM context), so the sanitization
  machinery is not yet needed.
- `dlw_search_models` / `dlw_source_status` — no backing endpoint
  exists yet; out of MVP.

## 2. Locked Decisions

- **Runtime prefix `/api/v1/ai/*`** (not the doc's `/api/ai/*`) — the
  runtime is `/api/v1`-based everywhere; the static `api/openapi.yaml`
  contract is `/api/v2`-based (kept internally consistent, linted
  separately) — same convention as all prior SPs.
- **Tools call services/queries in-process**, passing `principal` —
  reuses the exact `tenant_filtered(...)` queries the REST handlers
  use (invariant 15 is automatic; no service-token, no privilege
  escalation).
- **CI never needs a secret or a subprocess**: `DLW_AI_BACKEND=stub`
  in tests; the stub deterministically scripts the SSE event sequence
  and actually executes the read-only tools, so the whole pipeline
  (persistence, tool exec, audit, SSE framing) is covered.
- **SSE reuse**: the chat endpoint uses `StreamingResponse` +
  `text/event-stream` + the `:open\n\n` first-byte flush + `?max_ticks`
  is N/A (chat streams to a natural `done`), but a per-turn hard cap on
  tool-loop iterations (`_MAX_TOOL_ITERS = 8`) prevents runaway loops.
- **No new runtime dep for the stub/opencode path**: `opencode` is an
  external binary (subprocess), not a Python dep. `claude_code` /
  `openai_compat` are structural-only (no `anthropic`/`httpx`-to-LLM
  dep added in SP4a).

## 3. Data Model & Migration

`src/dlw/alembic/versions/<rev>_p3sp4a_ai_copilot.py` (down_revision
`7636b35e4881`):

```python
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
    op.drop_table("ai_messages")
    op.drop_table("ai_conversations")
```

SQLAlchemy models in `src/dlw/db/models/ai.py` (`AIConversation`,
`AIMessage`) mirroring the table; registered in `Base.metadata` via
the models package import.

`ai_messages.content` JSONB shape (per role):
- `user`: `{"text": "..."}`
- `assistant`: `{"text": "...", "tool_calls": [{"id","tool","input","output","ok"}]}`
  (tool calls/results folded in for the read-only MVP)

## 4. AgentRunner Abstraction

`src/dlw/ai/runner.py`:

```python
class AgentEvent(BaseModel):
    event: str           # assistant.thinking | tool_call | tool_result
                         # | assistant.message_delta | error | done
    data: dict

class AgentContext(BaseModel):
    conversation_id: uuid.UUID
    history: list[dict]  # prior ai_messages content, oldest→newest
    user_message: str

class AgentRunner(ABC):
    backend_name: str
    model_name: str
    @abstractmethod
    def run(self, ctx: AgentContext, *, tools: ToolRegistry,
            principal: Principal, session_maker) -> AsyncIterator[AgentEvent]: ...

def build_runner(settings) -> AgentRunner:
    b = settings.ai_backend
    if b == "stub": return StubAgentRunner()
    if b == "opencode": return OpenCodeRunner(settings)
    if b in ("claude_code", "openai_compat"):
        raise AIBackendUnavailable(b)   # structural; wired in later SP
    raise AIBackendUnavailable(b)
```

- **`StubAgentRunner`** — deterministic. Heuristic: if the user
  message contains a task-ish keyword ("task"/"任务"/"download"), emit
  `assistant.thinking` → `tool_call(dlw_list_tasks)` →
  (registry executes it) → `tool_result` → `assistant.message_delta`
  (a templated summary of the tool output) → `done`. Otherwise emit a
  single `assistant.message_delta` echo + `done`. No randomness; tests
  assert the exact event sequence.
- **`OpenCodeRunner`** — spawns `opencode run --print … ` with the
  read-only tools advertised via a generated MCP config (or
  `--prompt`-embedded tool descriptions; exact CLI flags resolved at
  implementation time against the installed `opencode` version).
  Parses its streamed output into `AgentEvent`s. Raises
  `AIBackendUnavailable` with a clear message if the binary is absent.
  **Live-tested only**; CI uses the stub.

The chat endpoint drives the runner, persists each event into the
conversation, audits tool calls, and forwards events as SSE frames.

## 5. Tool Registry

`src/dlw/ai/tools.py`:

```python
@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict           # JSON schema
    run: Callable[..., Awaitable[dict]]   # (session, principal, **input) -> dict

READONLY_TOOLS: dict[str, Tool] = { ... }   # the 4 tools
```

Each tool's `run` opens a fresh session (or uses an injected one),
runs the same `tenant_filtered(...)` query as the matching REST
handler, and returns a plain JSON-able dict. The chat service wraps
every execution in:
1. `write_audit(action=f"ai.tool.{name}", actor_user_id=principal.user_id,
   tenant_id=principal.tenant_id, payload={"actor_kind": "ai_copilot",
   "input": <input>}, outcome="success"|"error")` — invariant 16.
2. error isolation: a tool exception becomes a `tool_error` event +
   audited `outcome="error"`, never crashes the stream.

## 6. Protocol — `POST /api/v1/ai/chat`

Request:
```json
{ "conversation_id": "uuid-or-null", "message": "which tasks failed?" }
```

Response: `text/event-stream`, frames are
`event: <type>\ndata: <json>\n\n`. Event types (subset of the doc's
§4.3): `assistant.thinking`, `tool_call`, `tool_result`, `tool_error`,
`assistant.message_delta`, `error`, `done` (with
`{conversation_id, ai_message_id, tokens_used}`). `?max_ticks` is not
used; the stub ends at `done`; a `_MAX_TOOL_ITERS` cap bounds loops.

`GET /api/v1/ai/conversations` → `{items: [{id,title,last_message_at,
backend,model_name}]}` (tenant+owner scoped). `GET
/api/v1/ai/conversations/{id}` → `{conversation, messages: [...]}`
(404 cross-tenant/owner).

RBAC: add to `policy.csv` for tenant_admin/operator/viewer:
`/api/v1/ai*` `^(GET|POST)$` `tenant_match`. Chat is POST; conversations
are GET.

## 7. Frontend

- `frontend/src/api/aiClient.ts` — `streamChat(message, conversationId,
  onEvent)` using fetch + ReadableStream (mirror `sse.ts` parsing) +
  `listConversations()` / `getConversation(id)`.
- `frontend/src/composables/useCopilot.ts` — Pinia-free local state:
  messages, streaming flag, current conversation; appends deltas.
- `frontend/src/components/copilot/CopilotDrawer.vue` — right
  `el-drawer`; message list (`UserBubble`/`AssistantBubble`/
  `ToolCallCard` read-only display); input box; conversation switcher.
- AppShell: a "Copilot" button + ⌘K command entry (CommandPalette)
  toggles the drawer. View-additive — no existing page changes beyond
  mounting the drawer + the toggle control in the shell.
- i18n: both `en-US.json` / `zh-CN.json` at parity for the new strings.

## 8. Tests

**Backend** (`tests/api/test_ai_chat.py`, `tests/ai/test_tools.py`,
`tests/ai/test_stub_runner.py`):
- migration round-trips (tables exist after `create_all`).
- `dlw_list_tasks`/`get_task`/`get_task_events`/`quota_current` —
  tenant isolation (tenant-1 principal never sees tenant-2 rows).
- chat unauth → 401; chat with stub backend → exact SSE event
  sequence for a task query (thinking→tool_call→tool_result→delta→done);
  conversation persisted (a row in `ai_conversations` + 2 `ai_messages`).
- audit: a `dlw_list_tasks` tool call writes an `ai.tool.dlw_list_tasks`
  audit row with `payload.actor_kind == "ai_copilot"`.
- conversations list/get tenant+owner isolation (cross-tenant → 404).
- `build_runner("claude_code")` raises `AIBackendUnavailable`.

**Frontend** (`useCopilot.spec.ts`, `aiClient.spec.ts`,
`CopilotDrawer.spec.ts`): SSE parse → message assembly; drawer renders
user/assistant/tool-card; conversation switch loads history (mocked
client).

## 9. Milestones

- **M1 Backend**: migration + models + tool registry + AgentRunner +
  stub + chat SSE + conversations endpoints + policy.csv + tests +
  M1 full backend gate (incl. `alembic upgrade head` on the dev DB).
- **M2 Frontend**: aiClient + useCopilot + CopilotDrawer + shell/⌘K
  wiring + i18n parity + tests + full frontend gate.
- **M3 Smoke + docs**: headed Playwright (open drawer, send "list my
  tasks", observe SSE chat with a tool-call card) using the stub
  backend; optional `opencode` live check if the binary is present;
  append an AI-Copilot section to `docs/operator/web-ui.md`.

## 10. Risks & Contingencies

- **Migration on a live dev DB**: the ephemeral controller + tests use
  `Base.metadata.create_all` (picks up the new models). The dev PG
  (`:5433`) needs `alembic upgrade head` once; M1 runs it. CI's pytest
  uses `create_all`, so no alembic dependency in CI tests.
- **`gen_random_uuid()`**: available built-in in `pg_catalog` on PG 13+
  (no `pgcrypto` extension needed; dev/prod is PG 18). Regardless, the
  ORM model uses a Python `default=uuid.uuid4` so inserts never depend
  on the DB default (mirrors `DownloadTask.id`); the `server_default`
  is documentation / raw-SQL-insert safety.
- **OpenAPI already documents `/ai/*`**: the static `api/openapi.yaml`
  (v2.0 contract) already carries `/ai/chat`, `/ai/conversations[/{id}]`
  + `AIChatRequest` (with the full v2.1 vision incl. `context` /
  `tool_confirmation`). The runtime `ChatRequest` is the SP4a subset —
  the same static-ahead-of-runtime split as every prior SP. SP4a does
  NOT modify `api/openapi.yaml`.
- **OpenCode CLI surface is evolving**: `OpenCodeRunner`'s exact flags
  are resolved at implementation against the installed version; if the
  binary/flags differ, the runner raises `AIBackendUnavailable` and the
  stub remains the tested path. The MVP does not block on opencode.
- **Tool-loop runaway**: `_MAX_TOOL_ITERS = 8` hard cap.
- **SSE buffering in tests**: same httpx ASGITransport caveat as prior
  SSE SPs — the stub emits a bounded sequence and closes, so the test
  collector reads all frames after generator close.
- **No external content in MVP** → invariants 19/41 (sanitization) are
  genuinely not triggered; documented so the final reviewer doesn't
  flag their absence.

## 11. Self-Review

- **Placeholder scan**: the only deliberately-deferred specifics are
  the `OpenCodeRunner` exact CLI flags (resolved at impl against the
  live binary) and `claude_code`/`openai_compat` (structural stubs) —
  both explicitly bounded, not vague TODOs.
- **Consistency**: SSE reuses the established idiom; tenant scope
  reuses `tenant_filtered`; audit reuses `write_audit`; migration
  follows the `7636b35e4881` template; frontend reuses the shell/i18n/
  Element Plus conventions.
- **Scope**: the smallest credible end-to-end AI slice; every
  deferred piece is named with its follow-on SP. Larger than an SSE
  SP (migration + new subsystem) but bounded.
- **Invariants honored now**: 15 (in-process tools in user scope), 16
  (audit). Deferred-with-owner: 17/40 (SP4b), 37 (SP4c), 18 (SP4d),
  19/41 (SP4e).
- **CI safety**: stub backend ⇒ no secret, no subprocess, full
  pipeline coverage.
