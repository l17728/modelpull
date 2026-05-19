# UI-SP2 — Download-Manager-Grade Task Detail (Design)

> Sub-project 2 of the Web UI decomposition (UI-SP1 merged PR #19 `8203c58`).
> Status: design self-approved per project Rule #1 (autonomous; recommended/conservative at every fork).
> Branch: `feat/ui-sp2-task-detail`.

## 1. Context & Scope

UI-SP1 shipped the app shell, auth, Dashboard, Task List, Task Create — **frontend-only**, on the
~11 existing v1 endpoints. It deliberately left `/tasks/:id` as a thin scaffold and recorded that
"UI-SP2 upgrades it" and that **UI-SP2 is the sub-project that adds backend read-endpoints**.

UI-SP2 is **full-stack but additive**: it adds four read-only `GET` endpoints over the *existing*
schema (verified: **zero Alembic migration** — every column already exists) and rebuilds
`/tasks/:id` into a download-accelerator-grade detail view.

**In scope**

- Backend: 4 additive read-only endpoints (RBAC + tenant-scoped, contract-faithful):
  1. `GET /api/v1/tasks/{id}/subtask-chunks` — per-file chunk segments (NEW path in contract)
  2. `GET /api/v1/tasks/{id}/source-allocation` — per-source contribution + chunk routing
     (**contract already declares** `getSourceAllocation` + `SourceAllocation` schema — implement to match)
  3. `GET /api/v1/tasks/{id}/participating-executors` — executor swimlanes (NEW path in contract)
  4. `GET /api/v1/tasks/{id}/events` — audit-derived event log, paginated
     (**contract already declares** `getTaskEvents` + `TaskEvent` + Limit/Cursor — implement to match)
- Frontend: redesigned `/tasks/:id` — header (basic info + aggregate progress ring + client-derived
  speed/ETA + cancel/delete) and an `el-tabs` body: **Files & Chunks** (virtualized `el-table-v2`
  with inline segmented chunk bars), **Source Allocation** (per-source segmented bar + table),
  **Executors** (swimlane rows), **Event Log** (level filter + cursor pagination).
- An additive, view-transparent `enabled` option on `useLiveResource` so inactive tabs pause polling.

**Out of scope (deferred, documented)**

- Live byte-rate from the backend (no speed column exists). Speed/ETA are **client-derived** from
  successive `bytes_downloaded` polls. Documented limitation.
- Retry / pause / upgrade actions (no endpoints exist). Only cancel + delete are wired (UI-SP1 endpoints).
- Real event emission / per-chunk event rows. Event Log reads existing `audit_log` rows only
  (task.created / task.cancelled / quota.exceeded / permission_denied / subtask.* if present).
- SSE/WS push (UI-SP5). `useLiveResource` stays the single live seam; SP5 swaps internals view-free.
- ECharts. All visuals are inline SVG / Element Plus (no new runtime dep).
- A 163-cell canvas file matrix (v2.0 §3.3 Panel 4). The virtualized chunk-segmented table conveys
  the same per-file state richly; the canvas matrix is YAGNI-trimmed for SP2.

## 2. Inherited Locked Decisions (from UI-SP1 §8 — binding on SP2)

- `useLiveResource` is the **only** realtime seam; views never touch `vue-query` directly.
- `DataBoundary` wraps every view (loading skeleton / empty / error / forbidden).
- `el-table-v2` (ships in Element Plus 2.8.4 — **no new dep**) is the SP2 virtualization primitive.
- 9 status semantic colors from `styles/tokens.scss`; never color-only (icon + label + color); ≥4.5:1.
- Tenant/role from JWT via `stores/session.ts` (read-only chip); RBAC server-side.
- Additive philosophy: frontend stays in `frontend/**`; backend changes are additive read endpoints
  + `api/openapi.yaml` + Pydantic DTOs only — **no** schema/alembic/existing-route changes.
- Pass existing CI only (`frontend-lint`, `frontend-build`, backend `pytest`, `OpenAPI`,
  `Invariant`); **no new CI gate**.
- i18n: `en-US.json` + `zh-CN.json` must stay at exact key parity.

## 3. Backend Design

### 3.1 Contract (`api/openapi.yaml`)

The static contract is the documented intent and is CI-linted by `spectral lint --fail-severity=error`
+ `swagger-cli validate` (CI does **not** diff it against the runtime app — but the project lesson is
contract-faithful). Servers basePath is `/api/v2` in the static doc; **runtime FastAPI routes use the
existing `/api/v1/tasks` prefix** (consistent with every implemented route). We:

- Implement `getSourceAllocation` and `getTaskEvents` to **exactly match the already-declared
  schemas** (`SourceAllocation`, `TaskEvent`, `Limit`, `Cursor`, `next_cursor`). No contract churn.
- **Add** two paths + schemas, in the existing style (path under `# Tasks` section, `tags: [tasks]`,
  camelCase `operationId`, `$ref: '#/components/parameters/TaskId'`, `'200'` + reuse existing
  `Unauthenticated`/`RbacDenied` response refs where the file already uses them):
  - `/tasks/{taskId}/subtask-chunks` → `getSubtaskChunks` → `SubtaskChunkReport`
  - `/tasks/{taskId}/participating-executors` → `getParticipatingExecutors` → `ParticipatingExecutors`

### 3.2 Endpoints (router: existing `src/dlw/api/tasks.py`, `prefix="/api/v1/tasks"`)

Every endpoint follows the **proven cancel-pattern tenant gate** (tasks.py:143–148): resolve
`DownloadTask.id` via `tenant_filtered(select(DownloadTask.id).where(id==task_id), DownloadTask,
principal)`; `None` → `HTTPException(404, "task not found")` (cross-tenant must 404, never leak).
Auth: `Depends(require_perm("/api/v1/tasks*", "GET"))`. Session: `Depends(_session)`. Sub-resource
queries are then joined through `FileSubTask.task_id == task_id` and also filtered by
`FileSubTask.tenant_id == principal.tenant_id` (defence-in-depth; `file_subtasks.tenant_id` is denormalized).

| Endpoint | Source columns | Response DTO (Pydantic, `from_attributes` where ORM-mapped) |
|---|---|---|
| `GET /{id}/subtask-chunks` | `file_subtasks`(id, filename, file_size, status, bytes_downloaded, is_chunked, chunks_total, chunks_completed) + `subtask_chunks`(chunk_index, byte_start, byte_end, source_id, status, bytes_done) | `SubtaskChunkReport{ items: [ SubtaskChunkRow{ subtask_id, filename, file_size\|None, status, bytes_downloaded, is_chunked, chunks_total\|None, chunks_completed, chunks: [ ChunkSeg{ chunk_index, byte_start, byte_end, source_id, status, bytes_done } ] } ] }` |
| `GET /{id}/source-allocation` | `file_subtasks`(source_id, file_size, bytes_downloaded) + `subtask_chunks`(source_id, byte_start, byte_end, bytes_done) | `SourceAllocation{ task_id, sources_used:[{ source_id, bytes_assigned, percent, measured_speed_bps }], chunk_level_routing:[{ filename, chunks:[{ chunk_index, byte_start, byte_end, source_id, status, bytes_done }] }] }` — **matches existing contract schema**. `measured_speed_bps` = `0.0` (no live speed source; documented; field kept for contract fidelity). `percent` = source bytes ÷ task total bytes ×100. `chunk_level_routing` only for `is_chunked` files. |
| `GET /{id}/participating-executors` | `file_subtasks`(executor_id, status, bytes_downloaded, assigned_at, last_heartbeat_seen_at) + `executors`(id, status, health_score, last_heartbeat_at) | `ParticipatingExecutors{ items:[ ParticipatingExecutor{ executor_id, executor_status\|None, health_score\|None, last_heartbeat_at\|None, assigned_subtasks, active_subtasks, bytes_downloaded } ] }` — left-join executors (a subtask may reference an executor row that was pruned → null exec fields, still listed). |
| `GET /{id}/events` | `audit_log`(occurred_at, action, resource_type, resource_id, outcome, payload) where `tenant_id==principal.tenant_id` and (`resource_type='task'` and `resource_id==str(task_id)`) or (`resource_type='subtask'` and `resource_id` in this task's subtask ids) | `{ items:[ TaskEvent{ ts, type, message, details } ], next_cursor: str\|None }` — **matches existing contract**. `type`=`action`; `message` synthesized (`outcome=='denied'`→prefix); `details`=`payload or {}`. Cursor = opaque base64 of `occurred_at` iso + `id` (stable order `occurred_at DESC, id DESC`); `Limit` default 50, max 200 (reuse contract `Limit`/`Cursor` params). |

DTOs live in a new `src/dlw/schemas/task_detail.py` (keeps `schemas/task.py` focused; imported by
the router). All `int64` byte fields are Python `int`. Datetimes serialize ISO-8601 (Pydantic default).

### 3.3 Service layer

Read aggregation is thin; put query helpers in a new `src/dlw/services/task_detail.py`
(`async def chunks_for_task / source_allocation_for_task / executors_for_task / events_for_task`,
each takes `(session, task_id, tenant_id, ...)`, returns DTO-ready data). Router stays declarative
(tenant gate → service call → DTO). No write paths, no state machine, no audit writes.

### 3.4 Backend tests (`tests/api/`)

One file per endpoint (`test_task_detail_chunks.py`, `_source_alloc.py`, `_executors.py`,
`_events.py`), module-scoped bootstrap mirroring `tests/api/test_tasks.py`. Each: **happy path**
(seed task + subtasks [+ chunks/executor/audit rows], assert shape & aggregation),
**cross-tenant → 404**, **unauthenticated → 401**, plus one aggregation-correctness assertion
(e.g. `percent` sums ≈100; events cursor paginates; terminal task still returns rows). Use the
existing `principal_headers` / `auth` fixtures and seed via the public `POST /api/v1/tasks` plus
direct ORM inserts for sub-rows (chunks/audit) within the test session.

## 4. Frontend Design

### 4.1 Route & page

`/tasks/:id` (name `taskDetail`, `props:true`) **unchanged** — `pages/TaskDetail.vue` rebuilt in
place. Top-level `<DataBoundary>` on the parent task query (`useTaskDetail`, already exists, polls
via `useLiveResource`). 404 → `DataBoundary` empty state ("task not found"), not an error (fixes the
UI-SP1 bounded-404 LOW). Header: basic info grid (repo, revision, status badge, priority, created,
completed/error), `<AggregateRing>`, `<SpeedEta>`, and cancel/delete buttons via existing
`useTaskMutations` (`canCancel`/`canDelete`). Body: `<el-tabs>` with 4 lazy panes; only the active
pane's composable is `enabled` (others paused) — gated by the new `useLiveResource` `enabled` option.

### 4.2 Components (`frontend/src/components/taskdetail/`)

- `AggregateRing.vue` — inline SVG donut (props: `percent`, `filesDone`, `filesTotal`,
  `bytesDone`, `bytesTotal`). Pure; uses status tokens. Unit-tested via a pure `ringDash(percent,r)`.
- `SpeedEta.vue` — consumes `useDownloadRate` (client-derived). Shows current/avg B/s + ETA;
  "—" when indeterminate (rate 0 / terminal).
- `SourceBar.vue` — stacked segmented bar from `sources_used` (inline divs/SVG, % widths, legend).
- `ChunkBar.vue` — per-file inline SVG segmented bar from `chunks[]` (segment width ∝ byte span,
  fill ∝ `bytes_done/(byte_end-byte_start+1)`, color by chunk `status` + source).
- `SwimLane.vue` — one executor row (health badge, status, counts, bytes).
- `EventRow.vue` — ts + level chip + message; level from `type`/message.

### 4.3 Composables (`frontend/src/composables/`) — all wrap `useLiveResource`

`useSubtaskChunks(idRef, enabledRef)`, `useSourceAllocation(idRef, enabledRef)`,
`useParticipatingExecutors(idRef, enabledRef)`, `useTaskEvents(idRef, enabledRef, cursorRef)` —
each: `useLiveResource<T>(['<key>', idRef], () => client.get<T>('/api/v1/tasks/'+id+'/<path>').then(r=>r.data), { baseIntervalMs, enabled: enabledRef, isTerminal: () => parentTerminalRef.value })`.
Intervals: chunks 1500ms, source-alloc 2000ms, executors 2000ms, events 5000ms. Terminal-stop
keyed off the parent task's terminal status (passed in). Types added to `frontend/src/api/types.ts`.

`useDownloadRate(bytesDoneRef, bytesTotalRef)` — keeps a small in-memory ring of
`{t, bytes}` samples (cap ~30), exposes `currentBps` (EWMA over last ~10s), `avgBps`,
`etaSeconds|null`. Pure rate math extracted to `computeRate(samples)` for unit tests. No store.

### 4.4 `useLiveResource` additive change

Add `enabled?: Ref<boolean> | boolean` to `LiveOptions<T>`; pass `enabled: opts.enabled` straight
into `useQuery`. View-transparent, single-seam preserved, vue-query v5 unwraps the ref. Existing
callers unaffected (optional, defaults undefined ⇒ vue-query treats as enabled).

### 4.5 i18n & tokens

New keys under `tasks.detail.*` (tabs, columns, event levels, source/exec labels, speed/eta,
"task not found") added to **both** locales at parity. Reuse the 9 status colors + tokens; no new palette.

### 4.6 Frontend tests (`frontend/tests/unit/`)

Pure-function specs: `computeRate`, `ringDash`, chunk-segment geometry helper, event-level
classifier, source-percent formatter. Component specs (mount + ElementPlus + i18n + Pinia, happy-dom,
`vi.hoisted` mocks of `@/api/client`): `TaskDetail` (renders tabs; 404→empty; cancel disabled when
terminal), `ChunkBar`, `SwimLane`, `EventRow`, tab-gating (only active tab composable enabled). Match
UI-SP1 conventions exactly (findComponent by name; no layout reliance).

## 5. Data Flow & Error Handling

`useLiveResource` is the only seam. Cancel/delete reuse `useTaskMutations` (optimistic → rollback →
invalidate). axios 401 interceptor (existing) → logout+redirect. Each tab pane wrapped in its own
`<DataBoundary>` (independent loading/empty/error/forbidden) inside the page-level boundary. 403 on a
sub-endpoint → that pane shows forbidden, page still usable. Cross-tenant/unknown id → page empty
("task not found"). No new Pinia store; all server state is query state.

## 6. Milestones (preview for writing-plans)

- **M1 Backend endpoints + contract**: schemas/task_detail.py, services/task_detail.py, 4 routes,
  openapi.yaml (implement 2 declared + add 2), per-endpoint pytest (happy/cross-tenant/unauth/agg).
  Gate: `pytest`, `spectral`, `swagger-cli`, `lint_invariants`.
- **M2 Frontend foundation**: `useLiveResource.enabled`; api/types; 4 composables + `useDownloadRate`;
  pure-fn helpers + their specs. Gate: lint/typecheck/vitest.
- **M3 Visual components**: AggregateRing, SpeedEta, SourceBar, ChunkBar, SwimLane, EventRow +
  component specs. Gate: lint/typecheck/vitest.
- **M4 Page assembly + i18n + smoke**: rebuild TaskDetail.vue (header + el-tabs + DataBoundary +
  tab-gating + el-table-v2 chunk table + cursor-paginated events), both locales, TaskDetail spec.
  Gate: full backend pytest + frontend lint/typecheck/vitest/build; headed-Playwright smoke against
  local stack (controller :8001, Vite :5173, 30-day tenant JWT); docs.

## 7. Risks

- **el-table-v2 first use**: confirm full `app.use(ElementPlus)` registers it (it ships with the
  full plugin in 2.8.4); column-based API differs from `el-table` — plan pins the exact API. If a
  blocker, fall back to plain `el-table` with windowed slice (still satisfies "virtualized intent"
  at expected file counts) — documented contingency, not default.
- **Static contract `/api/v2` vs runtime `/api/v1`**: pre-existing intentional split; we keep both
  styles internally consistent (matches all current code). spectral/swagger-cli lint the static doc
  only — adding well-formed paths/schemas in the existing style passes.
- **Speed/ETA fidelity**: client-derived from poll deltas — coarse vs the wireframe's live rate.
  Accepted, documented; SP5 (SSE) improves it without view changes.

## 8. Self-Review

- Placeholder scan: none (every endpoint has concrete columns + DTO; every component has props).
- Consistency: single live seam (§2/§4.3/§4.4), additive backend (§1/§3), DataBoundary everywhere
  (§4.1/§5), contract fidelity (§3.1) — no contradictions.
- Scope: one plan (4 additive endpoints + one page) — appropriately sized; canvas-matrix &
  retry/upgrade & live-speed explicitly deferred to keep it single-plan.
- Ambiguity: endpoint paths/DTOs pinned to the on-disk contract (verified lines 495–530, 1829–1868);
  tenant gate pinned to tasks.py:143–148; `enabled` semantics pinned to vue-query v5.
