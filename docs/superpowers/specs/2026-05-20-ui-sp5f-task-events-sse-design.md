# UI-SP5f — Task Events SSE Stream (Design)

> 6th application of the view-free SSE template (after SP5 task-detail,
> SP5b executors, SP5c tasks-list, SP5d audit-log, SP5e quota). The
> Events tab on TaskDetail becomes real-time — the most activity-rich
> SP2 sub-resource and the one with the strongest "watch downloads
> progress live" use case.
> Status: design self-approved per Rule #1.
> Branch: `feat/ui-sp5f-task-events-sse`.

## 1. Context & Scope

`useTaskEvents(taskId, enabled, terminal)` currently polls
`/api/v1/tasks/{task_id}/events?limit=50` at 5 s (tab-gated by
`enabled`, terminated by `terminal`). The Events tab is the most
activity-rich tab on TaskDetail — `subtask.completed`,
`subtask.failed`, `task.cancelled`, etc. — and is the natural next
SP5* candidate after SP5e proved single-page-stream + reactive
streamUrl + page-1 cursor split.

**In scope (additive, zero migration, zero new dep):**

1. **Backend**: `GET /api/v1/tasks/{task_id}/events/stream` — hand-rolled
   SSE mirroring `audit_stream.py` (SP5d, the closest prior-art:
   cursor-paginated audit-log → page-1 SSE stream). Reuses
   `_td.events_for_task(session, task_id, tenant_id, limit=50,
   cursor=None)` (services/task_detail.py — SP2-introduced; already a
   service, no extraction needed). Pre-stream tenant gate via
   `tenant_filtered(select(DownloadTask.id) … )` (the same 5-line
   inline pattern from `tasks_stream.py`) — 404 cross-tenant before
   `:open`. New file `src/dlw/api/tasks_events_stream.py`. 5 s default
   tick (env `DLW_TASK_EVENTS_STREAM_INTERVAL_SECONDS`, clamped
   `[0.5, 60.0]` at runtime). Same `?max_ticks=N` testability hatch.

2. **Seam evolution (forced by SP5f)**: `useLiveResource`'s gating
   currently evaluates `streaming = shouldStream(...)` ONCE at
   call-time. All 5 prior SP5* consumers (SP5/SP5b/SP5c/SP5d/SP5e)
   passed `enabled = undefined` or a Ref that started `true`, so this
   was fine. **SP5f's `useTaskEvents` is the first SSE consumer with
   an `enabled` Ref that starts `false`** (the Events tab is inactive
   at TaskDetail mount; user clicks to activate). With the current
   seam, `streaming` would be locked to `false` for the lifetime of
   the composable instance and the SSE would never open. Fix: convert
   `streaming` to a `computed(() => shouldStream(...))` and watch BOTH
   `q.data.value` (first useQuery success) AND `streaming.value`
   (enabled flips true) before opening the SSE. Existing 5 consumers'
   behavior is unchanged because their `streaming.value` is `true`
   from start. Regression-proof: all existing seam tests + 1 new
   `enabled-flips-true` test.

3. **Frontend**: `useTaskEvents` opts in via the SP5 seam. `streamUrl`
   is a **`computed` over the `taskId` ref** — mirror SP5d's reactive
   pattern (here only 1 ref instead of 4, but same idiom). `applyEvent`
   JSON-parses the snapshot. Signature/return-shape UNCHANGED;
   `fetchOlderEvents` (one-shot "Load older" pagination) is untouched.

**Out of scope (intentional, deferred):**

- The other 3 SP2 sub-resource composables
  (`useSubtaskChunks`/`useSourceAllocation`/`useParticipatingExecutors`)
  — also viable SP5* candidates, but lower activity rate per visit and
  the connection-cap concern (see §6) compounds. Defer each as a
  potential SP5g/SP5h/SP5i unit if user value justifies.
- `useSystemHealth` — confirmed bad template fit in SP5e (auth-free,
  non-`/api/v1`, 503-on-not-leader). Permanently deferred.

## 2. Inherited Locked Decisions

All from SP1-SP5e. Most relevant for SP5f:

- `useLiveResource`'s `streamUrl`+`applyEvent` IS the seam (additive
  options, view-free).
- **Route collision rule** (SP5c BLOCKER): the new path
  `/api/v1/tasks/{task_id}/events/stream` is **5 segments deep**; the
  existing `tasks_router` has `/{task_id}/events` (4 deep) and
  `/{task_id}/stream` (4 deep, SP5). FastAPI routes are matched by full
  path including segment count — `/{task_id}/events/stream` is a
  distinct path from `/{task_id}/events`, no shadowing risk. To stay
  consistent with SP5c/SP5d/SP5e's defensive pattern, register
  `tasks_events_stream_router` BEFORE `tasks_router` in `main.py` with
  the same explanatory comment.
- **httpx ASGITransport buffering** (SP5): mitigated by `?max_ticks=N`.
- **Runtime clamp pattern** (SP5+): `_clamped_interval()` applies
  `max(0.5, min(60.0, raw))` regardless of pydantic Field bounds.
- **Pre-stream 404 pattern** (SP5e): tenant-gate query runs BEFORE
  `StreamingResponse` is constructed; `HTTPException(404)` is raised
  before `:open` is sent. The SP5 `tasks_stream.py` uses this pattern
  too — SP5f mirrors it directly.
- **Dead-keepalive code** (SP5e learning point #38): SP5/SP5b/SP5c/SP5d
  all have a `_KEEPALIVE_EVERY_TICKS` block where `ticks_since_data` is
  reset after every data emit, making the keepalive unreachable. SP5e
  removed it. SP5f follows SP5e — no keepalive block.

## 3. Backend Design

### 3.1 Endpoint

```
GET /api/v1/tasks/{task_id}/events/stream
Authorization: Bearer <jwt>
Accept: text/event-stream

→ 200 OK text/event-stream
: open

data: { "items": [ TaskEvent, ... ], "next_cursor": "…" | null }

data: { "items": [...], "next_cursor": "…" }
…
```

- Reuses `events_for_task(session, task_id, tenant_id, limit=50,
  cursor=None)` — the stream **always** passes `cursor=None`, live = page 1.
- 5 s default tick; clamp `[0.5, 60.0]`.
- Stream terminates on client disconnect / shutdown / pre-stream 404.
  Does NOT auto-terminate on task terminal status (the Events tab still
  shows historical events after the task succeeds/fails; the parent
  TaskDetail page's `useTaskDetail` already handles terminal-status
  stream closure for the header aggregator).
- Per-tick `async with session_maker() as s:` fresh session.

### 3.2 RBAC

Reuses existing `require_perm("/api/v1/tasks*", "GET")` — the wildcard
covers `/{task_id}/events/stream`. **No policy.csv change.**

### 3.3 OpenAPI

Add `/tasks/{task_id}/events/stream` after the existing
`/tasks/{task_id}/events` block:

```yaml
  /tasks/{task_id}/events/stream:
    get:
      tags: [tasks]
      summary: Live task-events SSE stream (UI-SP5f)
      operationId: streamTaskEvents
      parameters:
        - in: path
          name: task_id
          required: true
          schema: {type: string, format: uuid}
      responses:
        '200':
          description: SSE stream of TaskEventsResponse snapshots
          content:
            text/event-stream:
              schema:
                type: string
                description: |
                  Each event is `data: <TaskEventsResponse JSON>\n\n`
                  ({items, next_cursor}). Live feed always pushes
                  page 1 (newest first); the client's "Load older"
                  button still uses the one-shot
                  /tasks/{task_id}/events?cursor=… endpoint. Stream
                  terminates only on client disconnect or shutdown.
        '401': {$ref: '#/components/responses/Unauthenticated'}
        '404':
          description: Task not found (or not in caller's tenant)
```

### 3.4 Config

Add 1 setting in `src/dlw/config.py`:

```python
# UI-SP5f — SSE tick rate for /tasks/{id}/events/stream (clamped at runtime).
task_events_stream_interval_seconds: float = Field(default=5.0)
```

Runtime clamp `[0.5, 60.0]` in
`tasks_events_stream.py:_clamped_interval()`.

### 3.5 Tests (`tests/api/test_task_events_stream.py`)

Mirror SP5d's `test_audit_log_stream.py` (4 tests, the closest
prior-art):

1. **Unauth → 401**.
2. **Cross-tenant → 404** (pre-stream gate). Seed task with
   `tenant_id=2`; stream as tenant 1; assert `resp.status_code == 404`
   (NOT a 200+empty-payload — the gate is pre-stream).
3. **`?max_ticks=1` single snapshot** — seed task in tenant 1 with N
   audit events on the task and its subtasks; stream `?max_ticks=1`;
   payload has `items` (≥ 1 entry, ordered newest first) and
   `next_cursor`.
4. **`?max_ticks=2` multi-snapshot** — receive ≥ 2 envelopes; both
   have `items` + `next_cursor` keys.

Module bootstrap mirrors `test_audit_log_stream.py` — seeds Tenant +
Project + User + StorageBackend, then per-test seeds `DownloadTask`
rows + `AuditLog` rows (for tenant 1 and tenant 2 as needed).

## 4. Frontend Design

### 4.1 `useTaskEvents` opt-in

Modify `frontend/src/composables/useTaskEvents.ts` — add a reactive
`streamUrl` (`computed` over `taskId` ref) and `applyEvent` to the
`LiveOptions`. `fetchOlderEvents` is unchanged. Full new file:

```ts
import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { TaskEventsResponse } from '@/api/types'

export function useTaskEvents(
  taskId: Ref<string>, enabled: Ref<boolean>, terminal: Ref<boolean>,
) {
  const streamUrl = computed(
    () => `/api/v1/tasks/${taskId.value}/events/stream`)
  return useLiveResource<TaskEventsResponse>(
    ['task-events', taskId],
    async () => (await client.get<TaskEventsResponse>(
      `/api/v1/tasks/${taskId.value}/events?limit=50`)).data,
    {
      baseIntervalMs: 5_000,
      enabled,
      isTerminal: () => terminal.value,
      streamUrl,
      applyEvent: (_prev, ev) =>
        JSON.parse(ev.data) as TaskEventsResponse,
    },
  )
}

/** One-shot "load older" page (not live; appended in the page). */
export async function fetchOlderEvents(
  taskId: string, cursor: string,
): Promise<TaskEventsResponse> {
  return (await client.get<TaskEventsResponse>(
    `/api/v1/tasks/${taskId}/events?limit=50&cursor=${encodeURIComponent(cursor)}`,
  )).data
}
```

### 4.2 Tests

Add `frontend/tests/unit/useTaskEventsStream.spec.ts` — assert
opt-in: seam captures `streamUrl` (reactive to `taskId` ref change) +
`applyEvent`; `applyEvent` JSON-parses. Mirror
`useAuditLogStream.spec.ts` (SP5d, 3 tests).

### 4.3 View-free proof

- `frontend/src/pages/TaskDetail.vue` — NOT modified.
- `frontend/src/components/taskdetail/SwimLane.vue` (or whichever
  component renders the events tab) — NOT modified.
- Existing `frontend/tests/unit/TaskDetailSP2.spec.ts` and
  `useTaskEvents.spec.ts` (if any) — NOT modified; must pass
  unchanged.

### 4.4 Headed smoke

Same recipe: fresh `:8011` controller with SP5f code + Vite proxying
to it + headed Playwright navigate to a TaskDetail page, click the
Events tab, observe `/tasks/{id}/events/stream` SSE request in
DevTools network.

## 5. Milestones

- **M1 Backend**: openapi + config + `tasks_events_stream.py` + 4
  tests + router include + M1 gate.
- **M2 Frontend cutover**: `useTaskEvents` opts in + new composable
  spec + view-free re-verification + full frontend gate.
- **M3 Smoke + docs**: headed Playwright smoke + append SP5f section
  to `docs/operator/web-ui.md`.

## 6. Risks & Contingencies

- **Browser per-host connection cap**: TaskDetail with the Events tab
  active will hold **2 concurrent SSE connections** to the controller
  origin (the SP5 task-detail stream `/tasks/{id}/stream` for the
  header aggregator + the new SP5f events stream
  `/tasks/{id}/events/stream` for the tab). Combined with the global
  SSE consumers (executors / tasks-list / audit / quota, each gated by
  page focus or visibility), a user with multiple tabs open could
  approach the HTTP/1.1 6-per-origin limit. **Mitigation**: this is
  not a SP5f-specific concern; the cap is real but unlikely to hit in
  practice (tab-gated composables typically have only the active tab's
  stream open; HTTP/2 in production raises the cap to ~100). Document
  but do not engineer around.
- **`useTaskEvents` is `enabled`-gated** by the active tab. See §1 item
  2 — this is the structural reason the seam needs to evolve. Without
  the fix, `streamUrl`+`enabled=false` would silently never open the
  stream because `streaming` is locked at call-time. With the fix,
  `streaming` is reactive and the SSE opens on the first
  `enabled === true` AND `q.data` defined transition.
- **httpx ASGITransport buffering** — same as prior SSEs; mitigated by
  `?max_ticks=N`.
- **Route collision** — N/A (the new path has a distinct depth from
  any sibling).
- **Pre-stream 404 timing** — SP5/SP5e both prove the pattern works
  (HTTPException raised before `StreamingResponse` constructed).

## 7. Self-Review

- **Placeholder scan**: none.
- **Consistency**: mirrors SP5d (4th application) which mirrored SP5c
  which mirrored SP5b. SP5f's only new dimension is the **path-param**
  reactive streamUrl (vs SP5d's filter-ref reactive streamUrl), which
  is structurally identical (one `computed`, one `Ref` consumed).
- **Scope**: smallest credible follow-on (1 endpoint + 1 composable
  opt-in + 4 backend tests + 3 frontend tests). Same size as SP5d. No
  service extraction needed (`events_for_task` is already a service).
- **Ambiguity**: endpoint path, tick rate + clamp, RBAC reuse, response
  shape (reuse `TaskEventsResponse`), test set, no-cursor-in-stream
  rule, no-auto-terminate-on-task-terminal — all pinned.
- **Connection-cap concern**: documented in §6 as a known
  multi-stream consideration; not a SP5f-blocking issue.
