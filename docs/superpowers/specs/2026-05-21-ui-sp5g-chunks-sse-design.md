# UI-SP5g — Subtask-Chunks SSE Stream (Design)

> 7th application of the view-free SSE template (after SP5/SP5b/SP5c/
> SP5d/SP5e task-detail/executors/tasks-list/audit/quota, SP5f
> task-events). Second SP2 sub-resource composable to graduate from
> polling to SSE — the Files tab's chunk-level progress, the most live
> data on TaskDetail during an active download.
> Status: design self-approved per Rule #1.
> Branch: `feat/ui-sp5g-chunks-sse`.

## 1. Context & Scope

`useSubtaskChunks(taskId, enabled, terminal)` polls
`/api/v1/tasks/{task_id}/subtask-chunks` at 1.5 s (tab-gated by
`enabled`, terminated by `terminal`). The Files tab is the
default-active tab on TaskDetail and shows per-file chunk-download
progress — the highest-frequency live data during a download.

SP5f already evolved the `useLiveResource` seam to support
`enabled`-gated SSE consumers (reactive `streaming` + lazy-open).
SP5g is a **pure template repeat** of SP5f's `useTaskEvents` opt-in —
no seam change needed.

**In scope (additive, zero migration, zero new dep, zero seam change):**

1. **Backend**: `GET /api/v1/tasks/{task_id}/subtask-chunks/stream` —
   hand-rolled SSE mirroring `tasks_events_stream.py` (SP5f). Reuses
   `_td.chunks_for_task(session, task_id, tenant_id)` (returns a plain
   `list[SubtaskChunkRow]`, **no cursor** — simpler than events). Wraps
   as `SubtaskChunkReport(items=…)`. Pre-stream tenant gate via the
   same 5-line `tenant_filtered(select(DownloadTask.id) … )` inline
   block — 404 cross-tenant before `:open`. New file
   `src/dlw/api/tasks_chunks_stream.py`. 2 s default tick (env
   `DLW_TASK_CHUNKS_STREAM_INTERVAL_SECONDS`, clamped `[0.5, 60.0]` at
   runtime). Same `?max_ticks=N` testability hatch.

2. **Frontend**: `useSubtaskChunks` opts in via the SP5 seam.
   `streamUrl` is a `computed` over the `taskId` ref (mirror SP5f).
   `applyEvent` JSON-parses the snapshot. Signature/return-shape
   UNCHANGED.

**Out of scope (intentional, deferred):**

- The other 2 SP2 sub-resource composables
  (`useSourceAllocation` / `useParticipatingExecutors`) — viable
  SP5h/SP5i candidates; deferred (see §6 connection-cap).
- **Seam close-on-disable** — when a tab deactivates, the SSE stays
  open (the seam's `started` latch is permanent). After SP5g the
  worst-case concurrent SSE count on TaskDetail is 3 (header + events
  + chunks, if the user visits both tabs), well under the HTTP/1.1
  6-per-origin cap. Close-on-disable becomes worthwhile only when all
  4 tab streams exist (after SP5i would reach 5). Deferred to the SP
  that introduces the 4th stream — YAGNI until the cap is actually
  pressured.
- `useSystemHealth` — permanently deferred (bad template fit, SP5e
  learning #35).

## 2. Inherited Locked Decisions

All from SP1-SP5f. Most relevant for SP5g:

- `useLiveResource`'s reactive `streaming` gate + `streamUrl` +
  `applyEvent` IS the seam (SP5f evolution — already supports
  `enabled`-gated consumers; SP5g needs NO seam change).
- **Route collision rule** (SP5c): the new path
  `/api/v1/tasks/{task_id}/subtask-chunks/stream` is a distinct depth
  from `/{task_id}/subtask-chunks` and any other `/{task_id}/*` route;
  no shadowing. Register `tasks_chunks_stream_router` BEFORE
  `tasks_router` (after the other tasks-prefixed stream routers) for
  defensive consistency.
- **httpx ASGITransport buffering** (SP5): mitigated by `?max_ticks=N`.
- **Runtime clamp pattern** (SP5+): `_clamped_interval()` applies
  `max(0.5, min(60.0, raw))`.
- **Pre-stream 404 pattern** (SP5/SP5e/SP5f): tenant-gate query runs
  BEFORE `StreamingResponse` is constructed; `HTTPException(404)`
  raised before `:open`.
- **No dead keepalive block** (SP5e learning #38): SP5g omits it
  (matching SP5e/SP5f).
- **Test fixture FK ordering** (SP5f learning #43): seed parents
  (Tenant→Project/User/StorageBackend) with an explicit `flush()`
  BEFORE inserting `DownloadTask`, then `commit()`.
- **Headed smoke uses stable id selectors** (SP5f learning #42): the
  Files tab is `id="tab-files"` — locale-stable (avoid the i18n label
  "文件与分块" / "Files & Chunks").

## 3. Backend Design

### 3.1 Endpoint

```
GET /api/v1/tasks/{task_id}/subtask-chunks/stream
Authorization: Bearer <jwt>
Accept: text/event-stream

→ 200 OK text/event-stream
: open

data: { "items": [ SubtaskChunkRow, ... ] }

data: { "items": [...] }
…
```

- Reuses `chunks_for_task(session, task_id, tenant_id)` — a plain list
  (no cursor; the Files tab shows ALL subtasks, no pagination).
- 2 s default tick; clamp `[0.5, 60.0]`.
- Stream terminates on client disconnect / shutdown / pre-stream 404.
  Does NOT auto-terminate on task terminal status (the Files tab still
  shows the final chunk layout after completion).
- Per-tick `async with session_maker() as s:` fresh session.

### 3.2 RBAC

Reuses `require_perm("/api/v1/tasks*", "GET")` — wildcard covers
`/{task_id}/subtask-chunks/stream`. **No policy.csv change.**

### 3.3 OpenAPI

Add `/tasks/{taskId}/subtask-chunks/stream` after the existing
`/tasks/{taskId}/subtask-chunks` block:

```yaml
  /tasks/{taskId}/subtask-chunks/stream:
    parameters:
      - $ref: '#/components/parameters/TaskId'
    get:
      tags: [tasks]
      summary: Live subtask-chunks SSE stream (UI-SP5g)
      operationId: streamSubtaskChunks
      responses:
        '200':
          description: SSE stream of SubtaskChunkReport snapshots
          content:
            text/event-stream:
              schema:
                type: string
                description: |
                  Each event is `data: <SubtaskChunkReport JSON>\n\n`
                  ({items}). Stream terminates only on client
                  disconnect or shutdown.
        '401': {$ref: '#/components/responses/Unauthenticated'}
        '404':
          description: Task not found (or not in caller's tenant)
```

### 3.4 Config

Add 1 setting in `src/dlw/config.py`:

```python
# UI-SP5g — SSE tick rate for /tasks/{id}/subtask-chunks/stream (clamped at runtime).
task_chunks_stream_interval_seconds: float = Field(default=2.0)
```

Runtime clamp `[0.5, 60.0]` in
`tasks_chunks_stream.py:_clamped_interval()`.

### 3.5 Tests (`tests/api/test_task_chunks_stream.py`)

Mirror SP5f's `test_task_events_stream.py` (4 tests):

1. **Unauth → 401**.
2. **Cross-tenant → 404** (pre-stream gate). Seed task with
   `tenant_id=2`; stream as tenant 1; assert 404.
3. **`?max_ticks=1` single snapshot** — seed task in tenant 1 with N
   `FileSubTask` rows; stream `?max_ticks=1`; payload has `items`
   (a list; ≥ 1 row given the seed).
4. **`?max_ticks=2` multi-snapshot** — receive ≥ 2 envelopes; both
   have an `items` list.

Module bootstrap mirrors SP5f — seed parents with an explicit flush
before `DownloadTask` (FK ordering, learning #43), then `FileSubTask`
rows for the chunk report.

## 4. Frontend Design

### 4.1 `useSubtaskChunks` opt-in

Modify `frontend/src/composables/useSubtaskChunks.ts` (full new file):

```ts
import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { SubtaskChunkReport } from '@/api/types'

export function useSubtaskChunks(
  taskId: Ref<string>, enabled: Ref<boolean>, terminal: Ref<boolean>,
) {
  const streamUrl = computed(
    () => `/api/v1/tasks/${taskId.value}/subtask-chunks/stream`)
  return useLiveResource<SubtaskChunkReport>(
    ['task-chunks', taskId],
    async () => (await client.get<SubtaskChunkReport>(
      `/api/v1/tasks/${taskId.value}/subtask-chunks`)).data,
    {
      baseIntervalMs: 1_500,
      enabled,
      isTerminal: () => terminal.value,
      streamUrl,
      applyEvent: (_prev, ev) =>
        JSON.parse(ev.data) as SubtaskChunkReport,
    },
  )
}
```

### 4.2 Tests

Add `frontend/tests/unit/useSubtaskChunksStream.spec.ts` — mirror
SP5f's `useTaskEventsStream.spec.ts` (3 tests: opt-in fields present +
reactive streamUrl over taskId + applyEvent JSON parse).

### 4.3 View-free proof

- `frontend/src/pages/TaskDetail.vue` — NOT modified.
- The Files-tab component(s) (`ChunkBar.vue` / `SwimLane.vue` etc.) —
  NOT modified.
- Existing `frontend/tests/unit/TaskDetailSP2.spec.ts` and any
  `useSubtaskChunks` test — NOT modified; must pass unchanged.

### 4.4 Headed smoke

Same recipe: fresh `:8011` controller with SP5g code + Vite proxying
to it + headed Playwright navigate to a TaskDetail page (Files tab is
default-active, so the chunks SSE opens on landing — no tab click
needed). Observe `/tasks/{id}/subtask-chunks/stream` SSE request.

## 5. Milestones

- **M1 Backend**: openapi + config + `tasks_chunks_stream.py` + 4
  tests + router include + M1 gate.
- **M2 Frontend cutover**: `useSubtaskChunks` opts in + new composable
  spec + view-free re-verification + full frontend gate.
- **M3 Smoke + docs**: headed Playwright smoke + append SP5g section
  to `docs/operator/web-ui.md`.

## 6. Risks & Contingencies

- **Browser connection-cap (monitored)**: after SP5g, a TaskDetail
  page where the user visits both Files and Events tabs holds 3
  concurrent SSE connections to the controller origin (header +
  events + chunks). Still well under HTTP/1.1's 6-per-origin cap.
  The cap only becomes pressured if SP5h + SP5i add the remaining 2
  tab streams (worst case 5). **The seam close-on-disable fix (which
  would bound concurrent streams to 2: header + active tab) is
  deferred to the SP that introduces the 4th stream** — YAGNI until
  the cap is actually approached.
- **Files tab is default-active**: unlike SP5f's Events tab (which
  required a click), the chunks SSE opens immediately on TaskDetail
  landing (`onFiles` starts `true`). This means the SP5f seam path
  exercised here is the "enabled === true at mount" path (same as the
  original 5 consumers), NOT the "enabled flips true" path — so SP5g
  is actually a *less* novel seam exercise than SP5f. Good.
- **httpx ASGITransport buffering** — mitigated by `?max_ticks=N`.
- **Route collision** — N/A (distinct depth).
- **Pre-stream 404 timing** — proven by SP5/SP5e/SP5f.

## 7. Self-Review

- **Placeholder scan**: none.
- **Consistency**: mirrors SP5f (6th application) exactly, minus the
  cursor (chunks has no pagination) and minus the seam change (already
  done in SP5f). Strictly simpler than SP5f.
- **Scope**: smallest credible follow-on (1 endpoint + 1 composable
  opt-in + 4 backend tests + 3 frontend tests). No seam change, no
  service extraction (`chunks_for_task` already a service).
- **Ambiguity**: endpoint path, tick rate + clamp, RBAC reuse,
  response shape (reuse `SubtaskChunkReport`, no cursor), test set,
  no-auto-terminate, default-active-tab implication — all pinned.
