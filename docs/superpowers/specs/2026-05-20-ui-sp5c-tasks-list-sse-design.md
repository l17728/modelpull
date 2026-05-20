# UI-SP5c — Tasks-List SSE Stream (Design)

> Third application of the SP5 view-free SSE template (after SP5 `useTaskDetail`
> and SP5b `useExecutors`). Confirms the pattern is now N-repeatable.
> Status: design self-approved per project Rule #1.
> Branch: `feat/ui-sp5c-tasks-list-sse`.

## 1. Context & Scope

The view-free single-seam architecture (SP5) is proven against two consumers
(SP5 / SP5b). SP5c takes the **next-most-user-visible** target — the Tasks
landing page — and opts `useTaskList` in.

**In scope (additive, zero migration, zero new dep):**

1. **Backend**: `GET /api/v1/tasks/stream` — hand-rolled SSE mirroring
   `tasks_stream.py` (the SP5 endpoint) and `executors_stream.py` (SP5b).
   Reuses the existing `list_tasks` aggregation logic verbatim — same
   `tenant_filtered(select(DownloadTask)…)` + `TaskList(items=[…], total=N)`.
   New file `src/dlw/api/tasks_list_stream.py`. 5 s default tick
   (env `DLW_TASKS_LIST_STREAM_INTERVAL_SECONDS`, clamped `[0.5, 60.0]`).
   Same `?max_ticks=N` testability hatch.

2. **Frontend**: `useTaskList` opts in via the SP5-shipped `streamUrl`
   + `applyEvent` seam options. Signature/return-shape UNCHANGED — every
   consumer of `useTaskList` (the SP1 `TaskList.vue` page, the SP1 Dashboard
   "recent tasks" widget) keeps working.

**Out of scope (intentional, deferred):**

- The 4 SP2 sub-resource composables (chunks/source-alloc/executors/events
  on Task Detail). Their concurrent open-stream count + browser per-host cap
  remains the documented concern; defer to a future SP5d if telemetry
  justifies.
- `useQuota` (30 s) / `useAuditLog` (10 s) / `useSystemHealth` (10 s).
  Marginal push value; the opt-in is now a 1-line addition per composable
  when justified.
- WebSocket transport, real-time audit tail (SSE delivers similar UX).

## 2. Inherited Locked Decisions

All from SP1/SP2/SP3/SP5/SP5b — unchanged. Most relevant:

- `useLiveResource`'s `streamUrl` + `applyEvent` extension IS the seam.
- Route placement: `tasks_list_stream.py` is a NEW file — `src/dlw/api/tasks.py`
  is not "mTLS-only" like `executors.py`/`subtasks.py`, so technically the
  new route COULD live in `tasks.py`. But the SP5/SP5b precedent ("each SSE
  endpoint lives in its own `*_stream.py` file") is the cleaner convention
  and matches the file-cohesion-by-responsibility rule. **A new file it is.**
- Tenant gate inline (no parent gate — like SP5b's executors-list).
- `:open\n\n` first-byte flush; per-tick `is_disconnected()`; 50 ms sleep
  slicing; `asyncio.CancelledError` handling — same idiom as both prior SSE
  endpoints.
- Pass existing CI only. i18n / RBAC / DTO shape — untouched.

## 3. Backend Design

### 3.1 Endpoint

```
GET /api/v1/tasks/stream
Authorization: Bearer <jwt>
Accept: text/event-stream

→ 200 OK text/event-stream
: open

data: { "items": [ TaskRead, ... ], "total": N }

data: { "items": [...], "total": N }
…
```

- Reuses the existing `list_tasks` query: `tenant_filtered(select(DownloadTask), DownloadTask, principal).order_by(created_at.desc())` + `total` via `func.count()`. Wrapped in `TaskList(items=[TaskRead.model_validate(r) for r in rows], total=int(total or 0))`.
- 5 s default tick (matches `useTaskList`'s existing 5 s polling cadence);
  runtime clamp `[0.5, 60.0]`.
- No natural terminal state — only client disconnect or shutdown closes.
- Per-tick session via `async_sessionmaker(get_engine(), expire_on_commit=False)()`.
- `?max_ticks=N` `Query(default=None, ge=1, le=10000)` — same SP5 rationale.

### 3.2 RBAC

`policy.csv` already grants tenant_admin/operator/viewer GET on
`/api/v1/tasks*` (added before SP1 — verified). The wildcard covers `/stream`.
No policy.csv change required.

### 3.3 OpenAPI

Add `/tasks/stream` path block immediately after the existing
`/tasks/{taskId}/stream` block (SP5 added this in the tasks section):

```yaml
  /tasks/stream:
    get:
      tags: [tasks]
      summary: Live tenant-scoped tasks-list SSE stream (UI-SP5c)
      operationId: streamTaskList
      responses:
        '200':
          description: SSE stream of TaskList snapshots (text/event-stream)
          content:
            text/event-stream:
              schema:
                type: string
                description: |
                  Each event is `data: <TaskList JSON>\n\n` ({items, total}).
                  Stream terminates only on client disconnect or controller
                  shutdown. Keep-alive comment lines (`:keepalive`) may appear.
```

### 3.4 Config

Add 1 setting in `src/dlw/config.py` next to `task_stream_interval_seconds`
and `executors_stream_interval_seconds`:

```python
tasks_list_stream_interval_seconds: float = Field(default=5.0)
```

Runtime clamp `[0.5, 60.0]` in `tasks_list_stream.py:_clamped_interval()`.

### 3.5 Tests (`tests/api/test_tasks_list_stream.py`)

Mirror the 4-test shape of `test_executors_stream.py`:

1. **Unauth → 401**.
2. **Tenant isolation** — seed 2 tasks (1 tenant 1, 1 tenant 2); stream as
   tenant 1 → snapshot has 1 item (only the tenant-1 task).
3. **`?max_ticks=2` multi-snapshot** — receive ≥ 2 envelopes within 2 s
   using `DLW_TASKS_LIST_STREAM_INTERVAL_SECONDS=0.1`.
4. **Shape correctness** — snapshot has `{items: TaskRead[], total: int}`;
   no `subtasks` field (the list is slim per SP1's invariant 1).

Module bootstrap mirrors `test_tasks.py` / `test_executors_list.py`.

## 4. Frontend Design

### 4.1 `useTaskList` opt-in

Replace `frontend/src/composables/useTaskList.ts`:

```ts
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { TaskListResponse } from '@/api/types'

export function useTaskList() {
  return useLiveResource<TaskListResponse>(
    ['tasks'],
    async () => (await client.get<TaskListResponse>('/api/v1/tasks')).data,
    {
      baseIntervalMs: 5_000,
      staleTime: 5_000,
      streamUrl: '/api/v1/tasks/stream',
      applyEvent: (_prev, ev) => JSON.parse(ev.data) as TaskListResponse,
    },
  )
}
```

Note: `streamUrl` is a plain string (not a Ref) because `useTaskList` takes
no parameters. The seam accepts `string | Ref<string>`.

### 4.2 Tests

Add `frontend/tests/unit/useTaskListStream.spec.ts` — assert the new opt-in
mirrors the SP5b spec pattern. The SP1 `TaskList.vue` page test and any other
spec touching `useTaskList` remain unmodified and pass unchanged.

### 4.3 View-free proof

- `frontend/src/pages/TaskList.vue` — NOT modified.
- `frontend/src/pages/Dashboard.vue` — NOT modified (also consumes
  `useTaskList`).
- Any existing test that touches these — re-run unmodified and stays green.

### 4.4 Headed smoke

Same recipe: fresh `:8011` SP5c controller + Vite proxying to it + headed
Playwright navigating to `/tasks`; observe `/tasks/stream` SSE request in
DevTools network.

## 5. Milestones

- **M1 Backend**: openapi + config + `tasks_list_stream.py` + 4 tests + router
  include + M1 gate.
- **M2 Frontend cutover**: `useTaskList` opts in + new composable spec + view-free
  re-verification + full frontend gate.
- **M3 Smoke + docs**: headed Playwright smoke + append to
  `docs/operator/web-ui.md`.

## 6. Risks & Contingencies

- **httpx ASGITransport buffering** — same as SP5/SP5b. Mitigated by
  `?max_ticks=N`.
- **`useTaskList` consumed by both TaskList page AND Dashboard** — confirm
  both consumers' behavior is unchanged (they read `data.value.items` /
  `data.value.total` from the returned `useQuery` result; SSE writes via
  `setQueryData` update the same cache entry both consumers see). ✓ by
  design.
- **Inherited SP5b MEDIUM** (filter-mid-stream URL not reopened): not
  applicable to SP5c because `useTaskList` has no parameter → `streamUrl` is
  a constant string, never changes.

## 7. Self-Review

- **Placeholder scan**: none.
- **Consistency**: mirror SP5b exactly — same SSE idiom, same testability
  hatch, same view-free contract. Inherits all SP1-SP5/SP5b locked decisions.
- **Scope**: deliberately the smallest credible follow-on (1 endpoint,
  1 composable opt-in). Third demonstration of the architecture's
  incremental-upgrade capability.
- **Ambiguity**: endpoint path, tick rate + clamp, RBAC reuse, DTO shape
  (reuse `TaskList`), test set, single opt-in composable — all pinned.
