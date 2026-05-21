# UI-SP5h — Source-Allocation SSE Stream (Design)

> 8th application of the view-free SSE template (after SP5/SP5b/SP5c/
> SP5d/SP5e/SP5f/SP5g). Third SP2 sub-resource composable to graduate
> from polling to SSE — the Sources tab's multi-source allocation.
> Status: design self-approved per Rule #1.
> Branch: `feat/ui-sp5h-source-alloc-sse`.

## 1. Context & Scope

`useSourceAllocation(taskId, enabled, terminal)` polls
`/api/v1/tasks/{task_id}/source-allocation` at 2 s (tab-gated by
`enabled`). The Sources tab shows which sources serve which bytes/
chunks — changes during multi-source rebalancing.

SP5f evolved the seam to support `enabled`-gated SSE consumers; SP5g
proved a clean template repeat against `useSubtaskChunks`. SP5h is the
same repeat against `useSourceAllocation` — **even simpler** than SP5g
because `source_allocation_for_task` returns a `SourceAllocation`
object directly (no list-wrapping, no cursor).

**In scope (additive, zero migration, zero new dep, zero seam change):**

1. **Backend**: `GET /api/v1/tasks/{task_id}/source-allocation/stream`
   — hand-rolled SSE mirroring `tasks_chunks_stream.py` (SP5g). Reuses
   `_td.source_allocation_for_task(session, task_id, tenant_id)` which
   returns a `SourceAllocation` directly (emit
   `payload.model_dump_json()` — no wrapper). Pre-stream tenant gate
   via the same 5-line `tenant_filtered(select(DownloadTask.id) … )`
   block. New file `src/dlw/api/tasks_source_alloc_stream.py`. 2 s
   default tick (env `DLW_TASK_SOURCE_ALLOC_STREAM_INTERVAL_SECONDS`,
   clamped `[0.5, 60.0]`). Same `?max_ticks=N` hatch.

2. **Frontend**: `useSourceAllocation` opts in via the seam.
   `streamUrl` is a `computed` over `taskId`; `applyEvent` JSON-parses.
   Signature/return-shape UNCHANGED.

**Out of scope (intentional, deferred):**

- `useParticipatingExecutors` — the last SP2 sub-resource (SP5i
  candidate). When that 4th tab stream is added, worst-case concurrent
  SSE on TaskDetail reaches 5; **the seam close-on-disable change
  (bounding concurrent streams to 2: header + active tab) should be
  bundled into SP5i** (the trigger condition from SP5g's deferral).
- `useSystemHealth` — permanently deferred (SP5e learning #35).

## 2. Inherited Locked Decisions

All from SP1-SP5g. Most relevant:

- `useLiveResource`'s reactive `streaming` gate + `streamUrl` +
  `applyEvent` IS the seam (SP5f evolution — no change needed).
- **Route collision rule** (SP5c): `/{task_id}/source-allocation/
  stream` is distinct depth from `/{task_id}/source-allocation` and
  `/{task_id}`; no shadowing. Register `tasks_source_alloc_stream_
  router` BEFORE `tasks_router` (after the other tasks-prefixed stream
  routers) for defensive consistency.
- **httpx ASGITransport buffering** (SP5): mitigated by `?max_ticks=N`.
- **Runtime clamp pattern** (SP5+): `max(0.5, min(60.0, raw))`.
- **Pre-stream 404 pattern** (SP5/SP5e-g): tenant gate before
  `StreamingResponse`.
- **No dead keepalive block** (SP5e learning #38).
- **Test fixture FK ordering** (SP5f learning #43): flush parents
  before `DownloadTask`. (SP5h's test needs NO `FileSubTask` rows —
  an empty `SourceAllocation` is valid: `task_id` + empty lists — so
  the seed is simpler than SP5g.)
- **DownloadTask columns** (SP5f B1): `repo_id`/`revision`/
  `path_template`.

## 3. Backend Design

### 3.1 Endpoint

```
GET /api/v1/tasks/{task_id}/source-allocation/stream
Authorization: Bearer <jwt>
Accept: text/event-stream

→ 200 OK text/event-stream
: open

data: { "task_id": "...", "sources_used": [...], "chunk_level_routing": [...] }

data: { ... }
…
```

- Reuses `source_allocation_for_task(session, task_id, tenant_id)` →
  `SourceAllocation` (emitted directly).
- 2 s default tick; clamp `[0.5, 60.0]`.
- Terminates on client disconnect / shutdown / pre-stream 404. Does
  NOT auto-terminate on task terminal status.
- Per-tick fresh session.

### 3.2 RBAC

Reuses `require_perm("/api/v1/tasks*", "GET")`. No policy.csv change.

### 3.3 OpenAPI

`/tasks/{taskId}/source-allocation/stream` block (added alongside this
spec commit — see `api/openapi.yaml`).

### 3.4 Config

`task_source_alloc_stream_interval_seconds: float = Field(default=2.0)`
(added alongside spec commit). Runtime clamp `[0.5, 60.0]`.

### 3.5 Tests (`tests/api/test_task_source_alloc_stream.py`)

Mirror SP5g's 4 tests:

1. **Unauth → 401**.
2. **Cross-tenant → 404** (pre-stream gate; task in tenant 2, stream
   as tenant 1).
3. **`?max_ticks=1` single snapshot** — stream a tenant-1 task (NO
   `FileSubTask` rows needed); payload has `task_id` == the task UUID,
   plus `sources_used` and `chunk_level_routing` (lists, may be empty).
4. **`?max_ticks=2` multi-snapshot** — ≥ 2 envelopes; each has the 3
   keys.

Bootstrap: seed Tenant/Project/User/StorageBackend (flush) →
DownloadTask (T1 tenant 1, T2 tenant 2). No `FileSubTask` needed.

## 4. Frontend Design

### 4.1 `useSourceAllocation` opt-in

```ts
import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { SourceAllocation } from '@/api/types'

export function useSourceAllocation(
  taskId: Ref<string>, enabled: Ref<boolean>, terminal: Ref<boolean>,
) {
  const streamUrl = computed(
    () => `/api/v1/tasks/${taskId.value}/source-allocation/stream`)
  return useLiveResource<SourceAllocation>(
    ['task-source-alloc', taskId],
    async () => (await client.get<SourceAllocation>(
      `/api/v1/tasks/${taskId.value}/source-allocation`)).data,
    {
      baseIntervalMs: 2_000,
      enabled,
      isTerminal: () => terminal.value,
      streamUrl,
      applyEvent: (_prev, ev) => JSON.parse(ev.data) as SourceAllocation,
    },
  )
}
```

### 4.2 Tests

`frontend/tests/unit/useSourceAllocationStream.spec.ts` — mirror SP5g
(3 tests: opt-in fields present + reactive streamUrl + applyEvent).

### 4.3 View-free proof

- `frontend/src/pages/TaskDetail.vue` — NOT modified.
- The Sources-tab component(s) (`SourceBar.vue` etc.) — NOT modified.
- Existing SP2 tests — NOT modified; pass unchanged.

### 4.4 Headed smoke

Fresh `:8011` controller + Vite + headed Playwright: navigate to a
TaskDetail page, click the **Sources** tab (id-stable selector
`#tab-sources` — NOT the i18n label, per SP5f learning #42), observe
`/tasks/{id}/source-allocation/stream` SSE request. (Sources is NOT
the default tab — so this exercises the SP5f "enabled flips true"
seam path, like SP5f's Events tab.)

## 5. Milestones

- **M1 Backend**: openapi + config + `tasks_source_alloc_stream.py` +
  4 tests + router include + M1 gate.
- **M2 Frontend cutover**: `useSourceAllocation` opts in + new
  composable spec + view-free re-verification + full frontend gate.
- **M3 Smoke + docs**: headed Playwright smoke + append SP5h section
  to `docs/operator/web-ui.md`.

## 6. Risks & Contingencies

- **Browser connection-cap (monitored)**: after SP5h, a TaskDetail
  page where the user visits Files + Events + Sources tabs holds 4
  concurrent SSE connections (header + 3 tab streams). Still under the
  HTTP/1.1 6-per-origin cap. **SP5i (executors, 4th tab stream) would
  reach 5 — that SP must bundle the seam close-on-disable change.**
- **Sources tab NOT default-active**: unlike SP5g (Files default),
  SP5h's Sources tab requires a click → exercises the SP5f "enabled
  flips true → lazy-open" seam path. This is already proven by SP5f;
  SP5h re-validates it for a 2nd gated consumer.
- **httpx ASGITransport buffering** — mitigated by `?max_ticks=N`.
- **Route collision** — N/A (distinct depth).
- **Pre-stream 404 timing** — proven by SP5/SP5e-g.

## 7. Self-Review

- **Placeholder scan**: none.
- **Consistency**: mirrors SP5g; even simpler (no list-wrap, no
  FileSubTask seed). The only difference from SP5g is that the Sources
  tab is not default-active → re-exercises the "enabled flips true"
  path (already proven by SP5f).
- **Scope**: smallest credible follow-on (1 endpoint + 1 composable
  opt-in + 4 backend tests + 3 frontend tests). No seam change, no
  service extraction.
- **Ambiguity**: endpoint path, tick rate + clamp, RBAC reuse,
  response shape (emit `SourceAllocation` directly), test set,
  no-FileSubTask-seed, not-default-tab implication — all pinned.
