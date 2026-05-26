# Web UI — Operator/User Guide (UI-SP1)

> UI-SP1 ships the **app shell + auth + Dashboard + Task List + Task Create**.
> It is **frontend-only** (no backend/API change) and runs on the existing
> controller. The full 9-page vision is decomposed — see §5.
> Spec: `docs/superpowers/specs/2026-05-19-ui-sp1-shell-tasks-design.md`.
> Local deploy of the controller: `docs/operator/local-deployment.md`.

---

## 1. What UI-SP1 delivers

- **App shell**: collapsible sidebar + topbar, tenant/role chip (from JWT),
  dark-mode toggle, zh/en locale toggle, **command palette (Ctrl/⌘+K)** for
  nav + "create task" + "open task by id".
- **Auth**: paste a tenant-user JWT, or "Sign in with OIDC" button
  (`/api/v1/auth/login`). 401 → auto sign-out.
- **Dashboard** (`/`): KPI cards (in-progress/completed/failed/total,
  client-aggregated from the task list), a 24h created-count sparkline,
  quota summary (`/api/v1/quota/current`), recent tasks.
- **Task List** (`/tasks`): client-side status filter + repo/id search,
  per-row actions (view / cancel non-terminal / delete terminal) with
  optimistic refresh.
- **Task Create** (`/tasks/new`): repo / revision (40-hex) / storage_id /
  priority / source-strategy / upgrade-from / trust-non-hf, validation,
  friendly error mapping (409/422/429/403/5xx), success → task detail.
- Realtime via a single `useLiveResource` seam (adaptive polling: faster
  on detail, slower on lists, ×3 when the tab is hidden, stops at terminal,
  backs off on error). UI-SP5 will swap this to SSE/WS with **zero view
  changes**.

## 2. Run it

```bash
# controller (browser-friendly plain-HTTP instance) — see local-deployment.md
#   → http://localhost:8001
cd frontend && pnpm install && pnpm dev      # → http://localhost:5173
#   Vite proxies /api,/health → DLW_API_PROXY (default http://localhost:8001)
```

Open `http://localhost:5173`, paste a **tenant-user JWT** on the login page.

## 3. The token (important)

Use a **tenant-user JWT** (`user_id` matching a real `users` row), **not
the system-admin service token**: the admin token is `user_id=0` and
`download_tasks.owner_user_id` has an FK to `users` — creating a task with
it fails (HTTP 500). The Task Create page detects a service token and
**disables submit** with a clear warning. Mint a tenant-user JWT:

```bash
uv run python -c "from dlw.auth.principal import issue_system_jwt; \
  print(issue_system_jwt(secret='dev-system-jwt-change-me', user_id=1, \
  tenant_id=1, role='tenant_admin', project_ids=[], ttl_seconds=2592000))"
```

(30-day token for convenience during manual testing; production uses OIDC.)

## 4. Keyboard / UX notes

- **Ctrl/⌘+K** — command palette (navigate, create task, open task by id).
- Dark mode + locale persist (localStorage), default from
  `prefers-color-scheme`.
- Every page has uniform loading / empty / error / forbidden states
  (`DataBoundary`).

## 5. Decomposition — what's deferred (and why)

UI-SP1 is the first of 5 UI sub-projects (the full design needs additive
backend endpoints that don't exist yet):

| Sub-project | Scope | Backend it needs |
|---|---|---|
| **UI-SP1** (this) | shell + auth + dashboard + list + create | none (existing API) |
| UI-SP2 | download-manager Task Detail (aggregate ring → per-source bar → virtualized chunk-segmented file table → executor swimlanes → event log) + task/file/chunk actions | new read endpoints: subtask-chunks, source-allocation, participating-executors, task-events |
| UI-SP3 | Executors (host-grouped, drain/restart), Quota metering, Audit log, Settings | `GET /executors`, audit query endpoint |
| UI-SP4 | AI-Copilot conversational UI (right slide-over, SSE, tool-call/confirm cards, ⌘K) | full AI backend (`/api/ai/chat` SSE, conversation persistence, LLM bridge, MCP→REST tool bridge) |
| UI-SP5 | realtime upgrade: `useLiveResource` → SSE/WS, zero view change | backend SSE/WS |

**Known UI-SP1 scope limits:** Task Detail is still the simple scaffold view
(UI-SP2 makes it the download-manager view); no Executors/Search/Quota-mgmt/
Audit/Settings/Copilot pages; Dashboard aggregates are client-side; tenant
chip is read-only (no tenant switcher); list filtering is client-side
(no server-side filter endpoint yet).

Cross-ref: `docs/getting-started.md`, `docs/operator/cli-sdk.md`.

## UI-SP2 — Download-manager Task Detail

`/tasks/:id` is a full download-accelerator view backed by four additive
read-only endpoints (zero migration):

- `GET /api/v1/tasks/{id}/subtask-chunks` — per-file chunk segments
- `GET /api/v1/tasks/{id}/source-allocation` — per-source contribution + chunk routing
- `GET /api/v1/tasks/{id}/participating-executors` — executor swimlanes
- `GET /api/v1/tasks/{id}/events` — audit-derived event log (cursor-paginated)

The page has a header (basic info + aggregate progress ring + client-derived
speed/ETA + cancel/delete) and four tabs (Files & chunks, Sources, Executors,
Events). All polling flows through the single `useLiveResource` seam; only the
active tab polls (others paused via the `enabled` option).

Known limitations (intentional, deferred): speed/ETA are derived client-side
from successive byte-count polls (no backend speed source); retry/pause/upgrade
actions are not exposed (no endpoints); the file table uses a height-capped
`el-table` (true `el-table-v2` windowing is a documented follow-up); the event
log reads existing `audit_log` rows only; real-time push (SSE/WS) arrives in
UI-SP5 with no view changes.

## UI-SP3 — Infrastructure & Governance

Four new pages backed by **two additive read-only endpoints** (zero migration):

- `GET /api/v1/audit/log` — tenant-scoped audit search (filters: action prefix,
  actor_user_id, from/to time range; cursor-paginated; matches the on-disk
  `searchAuditLog` contract).
- `GET /api/v1/executors` — browser-facing executor list (own-tenant +
  shared-infra view; `system_admin` sees all). Lives in a new module
  (`src/dlw/api/executors_read.py`) because the existing `api/executors.py` is
  mTLS-only per `tools/lint_invariants.py:check_no_bearer_on_executor_routes`.

Pages: **/executors** (host-grouped list + status filter), **/audit** (filterable +
cursor pagination), **/quota** (3 cards over the existing `/quota/current`),
**/settings** (frontend-only: principal info from `stores/session.ts`,
theme/locale from `stores/ui.ts`, controller state from `/health/active`).

**Known deferrals** (intentional, no backend support today): executor
drain/restart, metrics history, heartbeat history; HF-token rotation;
license-policy CRUD; source-driver registration; maintenance mode;
`/quota/usage` (declared but no backing tables); ML forecast; chargeback PDF;
real-time audit tail (UI-SP5).

## UI-SP5 — Realtime SSE swap (delivered)

`useLiveResource` now supports an opt-in SSE transport. The locked promise
recorded in SP1/SP2/SP3 — *"SP5 swaps internals to SSE/WS with zero view
changes"* — is honored: every view, page, and other composable is unchanged.

- **Backend**: `GET /api/v1/tasks/{id}/stream` — hand-rolled
  `text/event-stream` via FastAPI `StreamingResponse` (same idiom as
  `hf_proxy.py`). 1 Hz default tick rate (env
  `DLW_TASK_STREAM_INTERVAL_SECONDS` overrides; clamped `[0.1, 10.0]`).
  Tenant-scoped via the proven cancel-pattern (404 cross-tenant). Terminates
  on terminal task status, client disconnect, or controller shutdown. A
  `?max_ticks=N` query param is supported for testability (httpx
  `ASGITransport` buffers the response body until the generator closes, so
  multi-tick tests need a natural-termination hatch).
- **Frontend**: `frontend/src/api/sse.ts` (pure `parseSseChunk` + `streamSse`
  fetch + ReadableStream with exponential backoff, Bearer-via-header). The
  `useLiveResource` composable gains optional `streamUrl` + `applyEvent`
  fields; when both are set, the composable opens an SSE connection after
  the first snapshot and writes events into the vue-query cache. On 3
  consecutive failures the stream gives up and polling resumes automatically.
  On 401 the streamer calls `auth.logout()`; on 403/404 it fails fast (no
  backoff burn).
- **One consumer opts in**: `useTaskDetail`. Every other composable
  (`useTaskList`, `useQuota`, the 4 SP2 sub-resource composables, the 3 SP3
  composables) stays on polling.

**At a glance**: open `/tasks/<id>` in DevTools Network — you'll see one
long-lived `text/event-stream` connection per visit instead of a 1 Hz polling
loop.

**Deferrals**: WebSocket transport (SSE delivers the same outcome simpler);
streaming for the 4 SP2 sub-resources (would force a multi-resource envelope
that breaks view-free). UI-SP4 (AI-Copilot) remains the v2.1 follow-up.

### UI-SP5b — Executors SSE follow-on

Same architecture, second consumer. `useExecutors` (the `/executors` page)
now talks SSE via `GET /api/v1/executors/stream` (5 s default tick;
`DLW_EXECUTORS_STREAM_INTERVAL_SECONDS` overrides; clamped `[0.5, 60.0]`).
The page (`Executors.vue`) is unchanged; only the composable opts in. SP3's
existing tests (`ExecutorsPage.spec.ts` + the `sp3Composables.spec.ts`
useExecutors wiring test) pass without modification — the view-free property
holds against a second consumer.

Tenant filtering reuses SP3's `list_executors_for_principal` (own-tenant +
shared-infra; `system_admin`/service-token bypass).

### UI-SP5c — Tasks-list SSE follow-on

Third application of the view-free SSE template. The Tasks landing page
(`useTaskList`, consumed by both `TaskList.vue` and the Dashboard "recent
tasks" widget) now talks SSE via `GET /api/v1/tasks/stream` (5 s default
tick; `DLW_TASKS_LIST_STREAM_INTERVAL_SECONDS` overrides; clamped
`[0.5, 60.0]`). The page and composable consumers are unchanged; SP1's
existing tests pass unmodified.

Tenant filtering reuses the existing `list_tasks` aggregation
(`tenant_filtered(select(DownloadTask))`) — same RBAC and slim shape
(`{items: TaskRead[], total: int}`, no subtasks) as the `GET /api/v1/tasks`
endpoint.

**Routing note**: `tasks_list_stream_router` is registered BEFORE
`tasks_router` in `src/dlw/main.py` so the static `/stream` path wins over
the parameterized `/{task_id}` route (FastAPI iterates routers in include
order; this was a first-of-kind issue caught by the SP5c pre-review gate
— SP5/SP5b didn't expose it because their stream paths had no sibling
parameterized routes under the same prefix).

### UI-SP5d — Audit-log SSE follow-on

Fourth application of the view-free SSE template. The Audit page
(`useAuditLog`, consumed by `Audit.vue` — including the SP3 "Load older"
cursor pagination, which is untouched) now talks SSE via
`GET /api/v1/audit/log/stream` (10 s default tick;
`DLW_AUDIT_STREAM_INTERVAL_SECONDS` overrides; clamped `[0.5, 60.0]`).
The view and the SP3 spec are unchanged; SP3's existing audit tests pass
unmodified.

The stream sends page-1 only — it reuses `search_audit_log(..., cursor=None,
limit=50)` and wraps the result as `AuditSearchResponse`. Older pages are
still fetched via the cursor-paginated `GET /api/v1/audit/log` endpoint
(SP3-introduced); "Load older" continues to advance `olderCursor` against
`data.value?.next_cursor` (the SP3 cursor-not-advancing fix).

The stream URL is **reactive to filters**: `useAuditLog` derives
`streamUrl` from the same `(action, actor, from, to)` filter refs the
fetcher uses, so changing any filter both re-fetches and re-streams under
the new URL — preserving the "filter ⇒ live tail of that filter" semantics
SP3 established. Same routing precaution as SP5c: `audit_stream_router` is
registered BEFORE `audit_router` in `src/dlw/main.py`.

### UI-SP5e — Quota snapshot SSE follow-on

Fifth application of the view-free SSE template. The Quota page
(`useQuota`, consumed by `QuotaPage.vue` and the `QuotaCard` infra
component) now talks SSE via `GET /api/v1/quota/current/stream`
(15 s default tick; `DLW_QUOTA_STREAM_INTERVAL_SECONDS` overrides;
clamped `[0.5, 60.0]`). The view-side consumers are unchanged.

The endpoint and the existing one-shot `GET /api/v1/quota/current` share
a single service function `get_quota_snapshot(session, tenant_id)` in
`src/dlw/services/quota_read.py` (extracted as part of SP5e — the prior
in-handler query was replaced by a call to the service; the existing
`tests/api/test_quota_api.py` is the regression proof of the refactor).
This is the first SP5* application that introduces a service extraction
alongside the SSE wrapper — DRY justified by adding a second caller.

The stream emits the **first snapshot immediately** as part of opening
(using the same pre-stream tenant existence check that issues 404 when
the tenant is gone), so the UI gets data on `:open`+1 instead of waiting
the full 15 s tick. Subsequent snapshots tick at
`quota_stream_interval_seconds`. Same routing precaution as SP5c/SP5d:
`quota_stream_router` is registered BEFORE `quota_router` in
`src/dlw/main.py`. The Settings page does NOT consume `useQuota` (it
only reads `useSystemHealth`); SP5e does not change Settings behavior.

### UI-SP5f — Task Events SSE follow-on

Sixth application of the view-free SSE template, and the first SP2
sub-resource composable to graduate from polling to SSE. The Events
tab on TaskDetail (`useTaskEvents`, consumed by the events panel in
`TaskDetail.vue`) now talks SSE via
`GET /api/v1/tasks/{task_id}/events/stream` (5 s default tick;
`DLW_TASK_EVENTS_STREAM_INTERVAL_SECONDS` overrides; clamped
`[0.5, 60.0]`). The view-side consumers are unchanged; the SP2
"Load older" cursor pagination (`fetchOlderEvents`) is untouched.

The stream sends page 1 only — it reuses
`events_for_task(session, task_id, tenant_id, limit=50, cursor=None)`
(SP2-introduced; already a service, no extraction needed) and wraps
the result as `TaskEventsResponse`. Older pages still come from the
one-shot `GET /api/v1/tasks/{task_id}/events?cursor=…` endpoint. The
stream URL is **reactive to the `taskId` ref**: `useTaskEvents`
derives `streamUrl` from a `computed` over the `taskId` prop. Same
routing precaution as SP5c/SP5d/SP5e: `tasks_events_stream_router`
is registered after `tasks_list_stream_router` and BEFORE
`tasks_router` in `src/dlw/main.py`.

**Seam evolution forced by SP5f**: `useLiveResource`'s
`streaming` gate was a `const` evaluated once at composable
call-time (SP5-SP5e all passed `enabled = undefined` or a Ref
starting `true`, so this was fine). SP5f's `useTaskEvents` is the
first SSE consumer whose `enabled` Ref starts `false` (Events tab is
inactive at TaskDetail mount; user clicks to activate). With the old
seam, the SSE would never open. SP5f converts `streaming` to a
`computed(() => shouldStream(...))` and adds a second
`watch(streaming, tryStart)` alongside the existing
`watch(() => q.data.value, tryStart, { immediate: true })` so the
SSE lazy-opens on first `enabled === true` AND data-available
transition. Existing 5 consumers are unaffected (their
`streaming.value` is `true` from start, so the immediate data
watcher opens the SSE exactly as before). Regression-proof:
`useLiveResourceEnabledSse.spec.ts` covers all three timing cases
(enabled=false-permanent, enabled=false-then-true, enabled=true-at-mount).

**Browser connection-cap note**: a TaskDetail page with the Events
tab active will hold two concurrent SSE connections to the
controller origin (the SP5 task-detail stream `/tasks/{id}/stream`
for the header aggregator + the new SP5f events stream
`/tasks/{id}/events/stream`). Well within the per-origin HTTP/2 cap
(~100); the HTTP/1.1 6-stream cap (e.g. Vite dev proxy) is unlikely
to be exhausted in practice given tab-gated lifecycle. No mitigation
implemented; documented for awareness.

### UI-SP5g — Subtask-chunks SSE follow-on

Seventh application of the view-free SSE template, and the second SP2
sub-resource composable to graduate from polling to SSE. The Files
tab on TaskDetail (`useSubtaskChunks`) now talks SSE via
`GET /api/v1/tasks/{task_id}/subtask-chunks/stream` (2 s default tick;
`DLW_TASK_CHUNKS_STREAM_INTERVAL_SECONDS` overrides; clamped
`[0.5, 60.0]`). The view-side consumers are unchanged.

The stream reuses `chunks_for_task(session, task_id, tenant_id)`
(SP2-introduced; a plain list, no cursor) wrapped as
`SubtaskChunkReport`. No seam change was needed — SP5f's reactive
`streaming` gate already supports `enabled`-gated consumers. Because
the Files tab is **default-active** on TaskDetail, the chunks SSE
opens on landing (the "enabled === true at mount" seam path, same as
the original 5 always-on consumers), unlike SP5f's Events tab which
required a click. Same routing precaution as SP5c-SP5f:
`tasks_chunks_stream_router` is registered after
`tasks_events_stream_router` and BEFORE `tasks_router` in
`src/dlw/main.py`.

**Connection-cap status (monitored)**: a TaskDetail page where the
user has visited both the Files and Events tabs now holds 3
concurrent SSE connections (header + events + chunks), still well
under the HTTP/1.1 6-per-origin cap. The remaining 2 SP2 tabs
(Sources, Executors) are SP5h/SP5i candidates; when the 4th tab
stream is added the worst case reaches 5, at which point a seam
"close-on-disable" change (bounding concurrent streams to 2: header +
active tab) becomes worthwhile. Deferred until then (YAGNI).

### UI-SP5h — Source-allocation SSE follow-on

Eighth application of the view-free SSE template, third SP2
sub-resource to graduate to SSE. The Sources tab on TaskDetail
(`useSourceAllocation`) now talks SSE via
`GET /api/v1/tasks/{task_id}/source-allocation/stream` (2 s default
tick; `DLW_TASK_SOURCE_ALLOC_STREAM_INTERVAL_SECONDS` overrides;
clamped `[0.5, 60.0]`). The view-side consumers are unchanged.

The stream reuses `source_allocation_for_task(session, task_id,
tenant_id)` (SP2-introduced), which returns a `SourceAllocation`
directly — emitted via `model_dump_json()` with no wrapper and no
cursor (the simplest stream body yet). No seam change was needed.
The Sources tab is NOT default-active, so the SSE opens on tab click
(the SP5f "enabled flips true → lazy-open" path, re-validated for a
2nd gated consumer). A freshly-submitted task may show an empty
`SourceAllocation` until sources are assigned — subtasks with a null
`source_id` are intentionally excluded from `sources_used` (pre-
existing SP2 service behavior; both the stream and the one-shot
endpoint behave identically). Same routing precaution as SP5c-SP5g:
`tasks_source_alloc_stream_router` is registered after
`tasks_chunks_stream_router` and BEFORE `tasks_router` in
`src/dlw/main.py`.

**Connection-cap status (monitored)**: a TaskDetail page where the
user has visited Files + Events + Sources tabs now holds 4 concurrent
SSE connections (header + 3 tab streams), still under the HTTP/1.1
6-per-origin cap. **SP5i (the last SP2 tab, Executors) would reach 5
— that SP must bundle the seam "close-on-disable" change** (bounding
concurrent streams to 2: header + active tab). This is the trigger
condition recorded in SP5g's YAGNI deferral.

### UI-SP5i — Participating-executors SSE + seam close-on-disable (SP5* SSE conversion complete)

Ninth and final application of the view-free SSE template — the last
SP2 sub-resource. The Executors tab on TaskDetail
(`useParticipatingExecutors`) now talks SSE via
`GET /api/v1/tasks/{task_id}/participating-executors/stream` (2 s tick;
`DLW_TASK_EXECUTORS_STREAM_INTERVAL_SECONDS`; clamped `[0.5, 60.0]`),
reusing `executors_for_task` wrapped as `ParticipatingExecutors`.

**Seam close-on-disable**: SP5i replaced `useLiveResource`'s permanent
`started` latch with an open/close lifecycle keyed on the reactive
`streaming` gate. When a tab-gated consumer deactivates (`streaming`
flips false), its SSE is aborted; reactivating reopens it. This bounds
the concurrent SSE count on TaskDetail to **2** (the always-on header
stream + the one active tab's stream), regardless of how many tabs the
user has visited — resolving the connection-cap concern tracked since
SP5g. Because `streamSse` resolves on both abort and giveup, the seam
uses an `if (ac === controller)` identity guard to distinguish "we
aborted it" (reopen later) from "it gave up after 3 failures"
(`gaveUp`). `gaveUp` resets on `closeStream`, so a transient outage on
one tab visit does not permanently downgrade that tab to polling —
reactivation retries SSE fresh. The 5 always-on consumers
(SP5/SP5b/SP5c/SP5d/SP5e) are unaffected: their `streaming` never flips
false, so they open once and stay open exactly as before (regression
proven by the unchanged consumer specs + a 6-case seam lifecycle test).

With SP5i merged, **the SP5* SSE conversion is complete**: every
TaskDetail tab (chunks/sources/executors/events) and every global
consumer (task-detail header, tasks-list, executors, audit, quota)
streams via SSE through the single `useLiveResource` seam, with
concurrency bounded to 2 per page and polling as the automatic
fallback.

## UI-SP4a — AI Copilot (read-only MVP)

First slice of the v2.1 AI Copilot, and the **first migration-bearing
UI sub-project** (`ai_conversations` / `ai_messages`; alembic head
advances to `9a1b2c3d4e5f`). A right-side chat drawer (open via the
topbar 🤖 button or the ⌘K palette "Open AI Copilot" entry) lets users
ask read-only questions in natural language; the assistant answers by
calling existing read-only services **in the caller's own JWT scope**.

**Backend**: `POST /api/v1/ai/chat` streams SSE
(`assistant.thinking` / `tool_call` / `tool_result` /
`assistant.message_delta` / `error` / `done`); `GET
/api/v1/ai/conversations[/{id}]` returns history. RBAC: `/api/v1/ai*`
granted to tenant_admin/operator/viewer (read-only this slice).

**Pluggable agent backend** via `DLW_AI_BACKEND`:
- `stub` (default) — deterministic, scripted; drives CI/tests with no
  secret and no subprocess. Exercises the full pipeline (persistence,
  tool execution, audit, SSE framing).
- `opencode` — the only live backend. modelpull spawns the `opencode`
  CLI as a subprocess (binary on PATH; `DLW_AI_OPENCODE_BIN` overrides)
  and parses its stdout. Whichever underlying LLM opencode is configured
  to use is opencode's own concern; modelpull is LLM-agnostic. As of
  SP4f the Skills bridge (`ai/opencode_skills.py`) makes tools available
  via opencode's bash without a separate MCP server.

Any other `ai_backend` value raises `AIBackendUnavailable` → 503.

**Tools** (read-only, in-process, tenant-scoped, audited
`ai.tool.*` with `payload.actor_kind="ai_copilot"`):
`dlw_list_tasks`, `dlw_get_task`, `dlw_get_task_events`,
`dlw_quota_current` — each reuses the same `tenant_filtered(...)`
queries / services as the REST handlers, so **invariant 15** (AI runs
within the caller's permissions, never a service credential) and
**invariant 16** (all AI tool calls audited) hold automatically.

**Deferred to later SP4 slices** (named with their owning invariants):
SP4b write tools + `tool_call_pending_confirm` confirmation gate (inv
17/40); SP4c sandboxed-MCP subprocess (inv 37 — the MVP calls tools
in-process); SP4d token-budget quota (inv 18 — `tokens_input/output`
columns exist but are not enforced); SP4e external-content tools +
prompt-injection sanitization (inv 19/41 — the MVP's tools return only
internal data, so no external content enters the LLM context).

## UI-SP4b — AI Copilot write tools + confirmation gate

Second AI slice: the Copilot can now **propose** write operations
(`dlw_cancel_task`, `dlw_create_task`), but they execute **only after
explicit user confirmation** (🔒 invariant 17). Adds the `ai_tool_calls`
table (alembic head → `a2b3c4d5e6f7`).

**Two-phase protocol**:
- **Phase 1** — `POST /api/v1/ai/chat {message}`: the runner emits a
  `tool_call_pending_confirm` event (tool, input, rationale, estimated
  impact). The service persists a **pending** `ai_tool_calls` row and
  forwards the event; **nothing executes**. The drawer shows a confirm
  card (Approve / Modify / Reject) and locks the composer until resolved.
- **Phase 2** — `POST /api/v1/ai/chat {conversation_id,
  tool_confirmation:{call_id, decision, modified_input?}}`: the service
  (not the LLM) loads the pending call **scoped to the caller's
  tenant+owner** (you cannot confirm another user's proposal), locks it
  `FOR UPDATE` (race-safe double-confirm guard), and on
  `approved`/`modified` executes via the **same service-layer path** as
  the REST handler (`cancel_task` / `check_quota` + `create_task`) — so
  a *modified* input is re-validated end-to-end (🔒 invariant 40, no
  reuse of the AI's proposal). The whole turn (business write + call-row
  update + confirmation metadata) commits in **one atomic transaction**.
  Every resolution is audited `ai.tool.<name>` with
  `payload.actor_kind="ai_copilot"`, `actor_user_id`=the confirming
  human, and both `ai_proposed_input` + `user_final_input` (🔒
  invariants 16 + 40).

Write tools stay tenant-scoped via the principal (🔒 invariant 15) and
map service exceptions to error results (never crash the stream). The
stub backend models the two-phase flow deterministically (CI-safe; no
secret). **Deferred** (named slices): SP4c sandboxed-MCP subprocess (inv
37), SP4d token-budget quota (inv 18), SP4e external-content tools +
sanitization (inv 19/41).

## UI-SP4d — AI Copilot token-budget quota

Third AI slice: a **per-tenant monthly LLM token budget** (🔒 invariant
18), independent of the download-traffic quota. Adds the column
`tenants.quota_ai_tokens_month` (default **1,000,000**, NOT NULL) and the
append-only ledger table `ai_token_usage` (alembic head →
`b3c4d5e6f7a8`).

**Enforcement** is at the start of each chat turn: before any
conversation is created or the LLM is invoked, the service sums the
tenant's token usage **for the current calendar month (UTC)** and
compares it to the quota. When the budget is exhausted the turn is
**blocked** — the SSE stream emits a single `quota_exceeded` event
(`{metric:"ai_tokens", remaining:0}`) and returns, creating no
conversation and persisting no message. The drawer surfaces this as a
warning note in the assistant bubble. **Downloads are unaffected** — only
AI chat is gated; a download task proposed earlier and confirmed via the
SP4b flow is still independently bound by the download quota.

**Recording**: each completed turn writes one `ai_token_usage` row and
stamps `tokens_input`/`tokens_output` on the assistant message. Recording
is best-effort/isolated — a ledger write failure never loses the turn.
The monthly window resets implicitly on the 1st (no job; the sum simply
ignores prior-month rows).

**Honest scope note**: the deterministic `stub` backend consumes no real
LLM tokens, so the service records a **nominal estimate** (≈ characters ÷
4) per turn — enough to accumulate usage and make the budget testable. A
real backend (e.g. opencode, or a future Anthropic runner) would report
actual token counts at the same hook. The enforcement + ledger machinery
is the deliverable; the per-token accuracy follows the backend.

**Deferred** (named slices): SP4c sandboxed-MCP subprocess (inv 37),
SP4e external-content tools + prompt-injection sanitization (inv 19/41).

## UI-SP4e — AI Copilot external-content tools + prompt-injection sanitization

Fourth AI slice: the Copilot can now pull **external content** about a
HuggingFace repo, and all external-origin text is **sanitized before it
enters the LLM context** (🔒 invariants 19/36/41). Two new read-only tools
are added (read-only ⇒ no confirmation gate, inv 17): `hf_api_metadata`
returns HF structured metadata (commit sha, file list, last-modified) and
`hf_model_card` fetches a repo's model-card / README text. Both fetch only
through the configured HuggingFace endpoint — there is no arbitrary-URL
egress.

**Sanitization** lives in `src/dlw/ai/sanitize.py`. Every external string
is NFKC-normalized, screened for Bidi/RTL override characters (which cause
an outright refusal — the content is dropped, not forwarded), stripped of
zero-width and other format characters, scanned for mixed-script homoglyph
confusables, for imperative-plus-tool-name and "repeat your instructions"
injection patterns, and for suspiciously long base64 runs, then truncated
and wrapped in a boundary tag. Trusted structured metadata (T1) is wrapped
`<external_content source="…">`; user-authored content such as a model
card (T2, inv 41) is truncated to 8 KB and wrapped
`<external_user_content trust_level="t2" source="…">` with a leading line
instructing that the content was uploaded by an arbitrary user and must
not be treated as instructions. The same pass also now sanitizes the
executor-reported `error_message` and task-event messages returned by the
existing `dlw_get_task` / `dlw_get_task_events` tools, which were the other
external-origin fields flowing into the model.

**Honest caveat**: complete prompt-injection defense is an unsolved
problem. SP4e raises the bar with a structural data/instruction boundary
plus the scans above, but the real lines of defense remain the user
confirmation gate (SP4b), the audit trail (SP4a), and the token budget
(SP4d). The confusables check uses in-house cross-script detection (no new
dependency); it catches Latin/Cyrillic/Greek mixing but not within-script
look-alikes, and it emits a warning rather than refusing.

**Deferred** (named slices): SP4c sandboxed-MCP subprocess (inv 37, not
feasible on the Windows dev environment); `fetch_user_content` / `web_search`
arbitrary-egress tools (need an egress allowlist and admin enable); and a
non-bypassable structural sanitization choke point (today each external
tool calls the sanitizer itself — correct for the tools that exist, with a
declarative choke point tracked as a follow-on).

## UI-SP4f — AI Copilot tool expansion + decision-chain transparency (2026-05)

Fifth AI slice: the read-only MVP's 4 tools + SP4b's 2 write tools have
grown to **18 total** (11 read + 7 write), and every reply now surfaces
the AI's full decision path so users can verify a fact came from a real
tool call rather than model hallucination.

**Tools added** (all tenant-scoped; same audit + confirm gates):

- **Read**: `dlw_list_storages` (so the AI can pick a valid `storage_id`
  before proposing `dlw_create_task`); `search_huggingface_models` and
  `search_modelscope_models` (keyword search the two model registries;
  preferred over `web_search` when the user mentions HF / ModelScope).
- **Write** (every requires user confirmation via SP4b's gate):
  `dlw_delete_task` (terminal-only), `dlw_retry_task` (creates a new
  task with the same params), `dlw_upgrade_task` (creates a new task at
  a new revision — unchanged files inherit via the SP3 `diff_and_dedup`
  mechanism), `dlw_patch_task` (mutates priority / source_strategy /
  source_blacklist on non-terminal tasks; backed by the new
  `services/task_patch.py` with `SELECT FOR UPDATE` for concurrent-patch
  safety), `dlw_create_local_user` / `dlw_reset_local_password` /
  `dlw_set_tenant_quota` (system_admin only; the last two are backed by
  `services/tenant_quota.py` which audit-logs only-on-change).
- **`dlw_create_task` smoothing**: `revision` defaults to `"main"`;
  `storage_id` auto-resolves to the tenant's `is_default=true` backend
  if omitted — so end-to-end "下载 deepseek-ai/DeepSeek-R1" becomes a
  one-shot tool call.
- **`web_search` is now default-on** at the config level
  (`ai_web_search_enabled: True`); without an API key the tool still
  no-ops with a clear "no API key" error, so flipping the flag is
  harmless absent credentials. Tool description tells the LLM the
  intended priority: domain tools (HF / ModelScope / internal) → 
  `web_search` → model knowledge.

**New REST endpoints** (the AI tools delegate to these):

- `PATCH /api/v1/tasks/{id}` — mutates the same fields as
  `dlw_patch_task`; tenant-scoped; rejects terminal tasks with `409
  TASK_TERMINAL`; rejects invalid strategy with `422 INVALID_PATCH`.
- `PUT /api/v1/tenants/{id}/quota` — system_admin only; writes an
  audit log entry with `{field: {before, after}}` payload.

**Decision-chain UI** (`CopilotMessageBubble.vue`,
`CopilotToolsHelp.vue`, `CopilotToolCard.vue`):

- Assistant replies are rendered as Markdown (`marked` with
  `<script>/<iframe>/<object>/<embed>/<style>` strip as a
  defense-in-depth on top of the existing T2 sanitizer).
- A **source-attribution badge** appears under every reply, colored
  per category: `💭 from model knowledge (may be outdated)` (warning) /
  `🤗 via Hugging Face` (success) / `🔍 via ModelScope` (primary) /
  `🌐 via web search` (primary) / `📊 via internal data` (success) /
  red `⚠ tool call failed` if any tool returned `ok: false`. The
  badge is driven by which tools the runner actually emitted, so it's
  factually grounded — a reply with only the model badge means **no
  tool was called and the answer should not be trusted at face value**.
- A **decision-chain panel** sits ABOVE the reply, rendering the
  `assistant.thinking` and `tool_call`/`tool_result` events in
  emit-time order (previously `assistant.thinking` was dropped by the
  composable). Each tool entry is a collapsible card showing the tool
  name + summarized input args + status tag + one-line result summary;
  click to expand the full input/output JSON.
- A **tools-help panel** (default expanded) above the message list
  lists all 18 tools with category tag (read / write / external),
  one-line description, and an example question — so users know what
  to ask.

**Sidebar refactor** (companion to SP4f): the **Help** menu item was
promoted from a Settings card to a permanent sidebar footer item using
`position: sticky` so it stays anchored to the viewport bottom even
when the main column scrolls (previously the aside grew with content
and pushed Help below the fold).

**Skills bridge to opencode** (shipped — no MCP server needed):
`OpenCodeRunner` now prepends a generated MANIFEST.md (`ai/opencode_skills.py`)
to every turn that lists all 18 tools with shell-command recipes. The
LLM that the operator's opencode instance is configured against —
modelpull is LLM-agnostic; opencode's own config decides — sees the
catalog and invokes tools by shelling out to the `dlw` CLI
(`dlw show <id>`, `dlw submit <repo>`, `dlw cancel <id>` ...) or
`curl` against the REST endpoints. Auth flows through
`$DLW_BEARER_TOKEN` in the subprocess env.

- Manifest is ~2600 tokens, generated from `READONLY_TOOLS` + `WRITE_TOOLS`
  (single source of truth) — adding a tool to either registry surfaces
  it in opencode automatically.
- `ai_opencode_inject_skills: True` is the default; flip to False to
  disable for tiny-context models or once opencode gets native MCP.
- Decision-chain UI gap: opencode streams plain text (no
  `tool_call` / `tool_result` events), so the SP4f chronological steps
  panel stays empty for opencode-backed turns. The shell commands the
  LLM runs ARE visible inline in the streamed reply, so the user still
  sees what was invoked — but a future iteration will parse opencode's
  output for "Running: dlw ..." lines and synthesize tool events so the
  decision-chain panel populates the same way as for the stub runner.
- **MCP server removed from roadmap** (2026-05-26): the original SP4a
  plan was to expose tools via a sandboxed MCP server (inv 37). The
  Skills-bridge approach above achieves the same end (LLM discovers +
  invokes tools without modelpull-specific code in the LLM client) at
  a fraction of the engineering cost and without MCP-over-stdio's known
  Windows / SelectorEventLoop hazards. The v2.0 design doc
  (`docs/v2.0/12-ai-copilot.md`) and invariant 37 are historical
  records of the earlier design choice; new work targets the Skills
  bridge, not an MCP server.
