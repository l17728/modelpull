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
