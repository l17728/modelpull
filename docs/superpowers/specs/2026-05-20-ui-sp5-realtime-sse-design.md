# UI-SP5 — Realtime SSE Swap (Design)

> Sub-project 5 of 5 (UI-SP1 = PR #19; UI-SP2 = PR #20 `de9573a`; UI-SP3 = PR #21 `a32f165`).
> UI-SP4 (AI-Copilot, v2.1) is **explicitly deferred** as it requires the full AI backend
> (LLM bridge, `/api/ai/chat` SSE, conversation tables, MCP→REST tool bridge) which is
> 0% implemented.
> Status: design self-approved per project Rule #1 (autonomous; recommended/conservative at every fork).
> Branch: `feat/ui-sp5-realtime-sse`.

## 1. Context & The Locked Promise

SP1, SP2, and SP3 specs made the SAME promise multiple times — verbatim across files:

- `2026-05-19-ui-sp1-shell-tasks-design.md` §5 line 54: *"All views consume this, never
  vue-query directly → SP5 swaps internals to SSE/WS with **zero view changes**."*
- `2026-05-20-ui-sp2-task-detail-design.md` §1 line 39 (out-of-scope): *"SSE/WS push
  (UI-SP5). `useLiveResource` stays the single live seam; SP5 **swaps internals
  view-free**."*
- `2026-05-20-ui-sp3-infra-governance-design.md` §2 line 86: *"`useLiveResource` is the
  **only** realtime seam (with the SP2-added optional `enabled`)."*

SP5's job is to **deliver that promise** — internally swap polling for a push
transport without touching a single view, composable consumer, or page.

## 2. Scope

**Conservative, smallest-credible delivery.**

**In scope (additive, zero migration):**

1. **Backend — one new SSE endpoint over the existing schema:**
   - `GET /api/v1/tasks/{id}/stream` — emits the same `TaskDetail` payload that
     `GET /api/v1/tasks/{id}` returns today, **on a 1 Hz server-side tick** until
     the client disconnects or the task reaches a terminal status. Hand-rolled
     `text/event-stream` via FastAPI `StreamingResponse` (same idiom as
     `src/dlw/api/hf_proxy.py:99-110` and `source_proxy.py`). New file
     `src/dlw/api/tasks_stream.py` (additive — doesn't touch `api/tasks.py`).
     Uses the existing `require_perm("/api/v1/tasks*", "GET")` + the **proven
     cancel-pattern tenant gate** (`tenant_filtered(select(DownloadTask.id)...)`
     → 404 if cross-tenant). Reuses the existing `_session` per-tick pattern (a
     fresh `async_sessionmaker(get_engine())` session per emit — matches the
     stateless model and avoids long-held connections).

2. **Frontend — additive `useLiveResource` extension + ONE consumer opt-in:**
   - **New file `frontend/src/api/sse.ts`**:
     - `parseSseChunk(buffer: string): { events: SseEvent[]; remainder: string }` —
       a **pure function** that parses one or more concatenated `data: …\n\n`
       blocks; handles comment lines (`:keep-alive`), multi-line `data:` fields,
       and a trailing partial block returned as `remainder`. The remainder is
       prepended to the next chunk by the caller.
     - `streamSse(url, token, onEvent, signal): Promise<void>` —
       `fetch(url, { headers: { Authorization: "Bearer <jwt>", Accept: "text/event-stream" }, signal })`
       → if 401, reject (caller logs out); else read `response.body.getReader()`,
       decode with `TextDecoder`, accumulate, parse via `parseSseChunk`, invoke
       `onEvent`. On non-401 disconnect, exponential backoff `1s → 2s → 4s → 8s
       → 16s → 30s cap` with ±20 % jitter, reset to 1 s after each successful
       chunk. Aborts when `signal.aborted`. **~70 lines incl. backoff; no new
       runtime dep.**
   - **Additive change to `frontend/src/composables/useLiveResource.ts`**:
     extend `LiveOptions<T>` with two optional fields:
     ```ts
     streamUrl?: string | Ref<string>
     applyEvent?: (prev: T | undefined, ev: SseEvent) => T
     ```
     Semantics: when both are set and `enabled !== false`, after the initial
     `useQuery` fetch lands the snapshot, the composable opens `streamSse`,
     calls `applyEvent` per event and `queryClient.setQueryData(key, next)`,
     and sets `refetchInterval: false` while streaming is healthy. On
     persistent stream error (3 consecutive backoff failures) it logs a console
     warning and lets the normal `computeInterval` polling resume. **View
     contract unchanged**: the returned `useQuery` result and `LiveOptions`
     defaults are untouched for every existing caller (`streamUrl === undefined`
     ⇒ identical behavior to today).
   - **ONE consumer opts in — `frontend/src/composables/useTaskDetail.ts`**: add
     `streamUrl: computed(() => '/api/v1/tasks/' + taskId.value + '/stream')`
     and `applyEvent: (_prev, ev) => JSON.parse(ev.data) as TaskDetail`. Nothing
     else changes — `useTaskDetail`'s return shape is identical, every consumer
     of `useTaskDetail` (the SP1 TaskDetail header, the rebuilt SP2 page) sees
     reactive `data` exactly as before.

3. **Headed Playwright smoke**: open Task Detail of a running task, observe at
   least 2 distinct snapshots arriving within 5 s; confirm zero console errors.

**Out of scope (intentional, documented):**

- The 4 SP2 sub-resource composables (`useSubtaskChunks` / `useSourceAllocation`
  / `useParticipatingExecutors` / `useTaskEvents`). Opting these in would
  require 4 more backend SSE endpoints or a multi-resource envelope (which
  would force fan-out in the page → **breaks view-free**). The polling at
  1.5 / 2 / 2 / 5 s is already responsive; the per-host browser connection
  cap (~6) becomes a concern if every Task Detail open consumes 5 SSE
  connections. Deferred to a future SP5b if real telemetry justifies it.
- The 5 lower-frequency composables (`useTaskList` 5 s, `useQuota` 30 s,
  `useExecutors` 5 s, `useAuditLog` 10 s, `useSystemHealth` 10 s) — push value
  is negligible at their cadences; cost (5 endpoints + 5 connections) is high.
  Deferred.
- WebSocket transport. The locked promise reads "SSE/WS"; the spec authoring
  agent confirms either satisfies the promise; SSE is the strictly simpler
  choice with the same UX outcome. WS is deferred.
- `EventSource` browser API (query-param token leaks JWT to logs/referrer);
  rejected in favor of fetch + ReadableStream.

## 3. Inherited Locked Decisions (binding on SP5)

- **`useLiveResource` is the only realtime seam.** SP5 preserves its return
  shape (`UseQueryReturnType<T, Error>`) byte-faithfully — `data`,
  `isLoading`, `isError`, `error`, `refetch`, `isFetching`, etc., all
  unchanged.
- `DataBoundary` wraps every view (unchanged).
- **Additive backend only**: new file + new route + new Pydantic DTO
  (`TaskStreamEvent` envelope) added to `openapi.yaml`; **no** schema /
  Alembic / existing-route changes.
- **No new runtime dep** on backend or frontend (hand-rolled SSE on
  `StreamingResponse`; fetch + ReadableStream on the client).
- Pass existing CI only.
- i18n parity (no new UI text in SP5).
- Tenant / RBAC server-side via the proven cancel-pattern gate + `require_perm`.
- `noUncheckedIndexedAccess` is on — every `arr[i]` guarded.
- Frontend tests: `vi.hoisted` plain holders + async `vi.mock(async () => { const { ref } = await import('vue'); … })` when a mock must return a real Vue ref (SP2 BLOCKER-fix pattern).
- Bash cwd persists across calls → `cd /d/download_weights && git …` for git;
  `cd /d/download_weights/frontend && pnpm …` for frontend.

## 4. Backend Design

### 4.1 Endpoint

```
GET /api/v1/tasks/{task_id}/stream
Authorization: Bearer <tenant-user JWT>
Accept: text/event-stream

→ 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no

: tick
data: <TaskDetail JSON>

data: <TaskDetail JSON>

…
```

- Initial event emitted **immediately** on connect (no client poll-then-stream
  race; the first `data:` block is the same payload as `GET /tasks/{id}`).
- Subsequent events every 1.0 s (configurable via `DLW_TASK_STREAM_INTERVAL`
  env, default 1.0 s; capped 0.2 s … 10 s).
- A `:keepalive` SSE comment is emitted every 15 s when no data changed, to
  defeat intermediary proxies that close idle connections.
- The handler terminates when (a) client disconnects, (b) task reaches a
  terminal status (`succeeded` / `failed` / `cancelled`) — emit one final
  payload and close, (c) the controller lifespan shuts down.
- Tenant gate is the proven cancel-pattern: `tenant_filtered(select(DownloadTask.id).where(id==task_id), DownloadTask, principal)` → 404 if cross-tenant. Re-checked on each tick (cheap; protects against revoke-during-stream).

### 4.2 OpenAPI contract

Add to `api/openapi.yaml` after the existing `/tasks/{taskId}/events` block:

```yaml
  /tasks/{taskId}/stream:
    parameters:
      - $ref: '#/components/parameters/TaskId'
    get:
      tags: [tasks]
      summary: Live TaskDetail stream (SSE; UI-SP5)
      operationId: streamTaskDetail
      responses:
        '200':
          description: SSE stream of TaskDetail snapshots
          content:
            text/event-stream:
              schema: {type: string}
```

No new schemas needed — the SSE payload is the existing `TaskDetail` schema
serialized into a `data:` line.

### 4.3 RBAC

`policy.csv` already grants `tenant_admin` / `tenant_operator` / `tenant_viewer`
GET on `/api/v1/tasks*` — the new `/stream` sub-resource is covered by the
wildcard. No `policy.csv` change required.

### 4.4 Tests

`tests/api/test_task_stream.py`:

1. **Unauth → 401** (no Authorization header).
2. **Cross-tenant → 404** (different tenant_id JWT).
3. **Happy path — receives at least 2 envelopes within 4 s** (override
   `DLW_TASK_STREAM_INTERVAL=0.5`); each envelope parses as JSON containing
   `id`, `status`, `subtasks`.
4. **Terminal-stop** — set the seeded task to `succeeded` (direct DB mutate);
   open the stream; assert exactly one envelope and the response closes (next
   `iter_lines` returns nothing).

httpx `AsyncClient.stream()` is the right test API — supports reading SSE chunks
incrementally.

## 5. Frontend Design

### 5.1 Pure parser (`frontend/src/api/sse.ts`)

```ts
export interface SseEvent { event: string; data: string; id?: string }

export function parseSseChunk(
  buf: string,
): { events: SseEvent[]; remainder: string } {
  const events: SseEvent[] = []
  const parts = buf.split(/\r?\n\r?\n/)
  const remainder = parts.pop() ?? ''
  for (const block of parts) {
    let event = 'message', data = '', id: string | undefined
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith(':')) continue
      const i = line.indexOf(':')
      const field = i === -1 ? line : line.slice(0, i)
      const value = i === -1 ? '' : line.slice(i + 1).replace(/^ /, '')
      if (field === 'event') event = value
      else if (field === 'data') data += (data ? '\n' : '') + value
      else if (field === 'id') id = value
    }
    if (data) events.push({ event, data, ...(id !== undefined ? { id } : {}) })
  }
  return { events, remainder }
}
```

### 5.2 Streamer

```ts
export interface StreamOptions {
  url: string
  token: string | null
  onEvent: (ev: SseEvent) => void
  onUnauthorized: () => void   // logout helper
  signal: AbortSignal
}
```

`streamSse(opts)`: fetch + ReadableStream + exponential backoff (`1s → 2s → 4s
→ 8s → 16s → 30s cap`, ±20% jitter). On 401, call `onUnauthorized()` and
reject; do NOT retry. On any other disconnect, retry until `signal.aborted`.
Reset backoff to 1 s after the first successful chunk.

### 5.3 `useLiveResource` opt-in

Augment `LiveOptions<T>` (the only file modified beyond additions):

```ts
export interface LiveOptions<T> {
  baseIntervalMs: number
  isTerminal?: (data: T) => boolean
  staleTime?: number
  enabled?: Ref<boolean> | boolean
  streamUrl?: string | Ref<string>            // NEW
  applyEvent?: (prev: T | undefined, ev: SseEvent) => T  // NEW
}
```

Internals: keep the existing `useQuery` block (fetcher fires once for the
snapshot, vue-query handles loading/cache/retries). After mount, when
`streamUrl` and `applyEvent` are both set and the query reports a first
success, set `refetchInterval: false` for vue-query and open `streamSse`. On
each event, compute `next = applyEvent(prev, ev)` and call
`queryClient.setQueryData(key, next)`. On 3 consecutive backoff failures, log a
warning and revert to polling (re-enable `refetchInterval`).

### 5.4 ONE consumer opts in

`frontend/src/composables/useTaskDetail.ts`:

```ts
import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import { TERMINAL_STATUSES, type TaskDetail } from '@/api/types'

export function useTaskDetail(taskId: Ref<string>) {
  const streamUrl = computed(() =>
    `/api/v1/tasks/${taskId.value}/stream`)
  return useLiveResource<TaskDetail>(
    ['task', taskId],
    async () => (await client.get<TaskDetail>(
      `/api/v1/tasks/${taskId.value}`)).data,
    {
      baseIntervalMs: 1_000,
      isTerminal: (d) => TERMINAL_STATUSES.has(d.status),
      streamUrl,
      applyEvent: (_prev, ev) => JSON.parse(ev.data) as TaskDetail,
    },
  )
}
```

This is THE ONLY consumer change. Every other composable + every view is
untouched.

### 5.5 Tests

- **`frontend/tests/unit/parseSseChunk.spec.ts`** — 6 cases: single event;
  two events in one buffer; event split across two buffers (remainder carry);
  comment lines ignored; custom `event:` field; multi-line `data:`.
- **`frontend/tests/unit/useLiveResource.stream.spec.ts`** — mock `streamSse`
  (extract into a parameter/module mock) and a fake `QueryClient`; assert:
  (a) initial snapshot via `fetcher`; (b) `data.value` updates after a mocked
  event via `queryClient.setQueryData`; (c) when the simulated stream
  give-up signal fires, polling resumes; (d) `enabled: false` opens neither
  poll nor stream.
- **Headed Playwright smoke**: open `/tasks/<id>` of a running task, capture
  two distinct snapshots within 5 s, assert zero console errors.

## 6. Milestones

- **M1 Backend**: `tasks_stream.py` + `openapi.yaml` path + 4 pytest tests + `main.py`
  router include + M1 gate (pytest + spectral + swagger + invariants).
- **M2 Frontend foundation**: `api/sse.ts` (pure + streamer) + `parseSseChunk` spec
  + M2 gate.
- **M3 Cutover**: `useLiveResource` accepts new options + `useTaskDetail` opts in +
  composable spec + full frontend gate + headed Playwright smoke + docs update.

## 7. Risks & Contingencies

- **The Playwright smoke might not surface a status change within the 5 s
  window** if the seeded task is idle. Mitigation: the test creates a *new* task
  via the existing API (already proven in SP2 smoke), then asserts that the
  *initial* snapshot arrives within 2 s (proves the stream opens and emits at
  least once). Status-flip on a busy executor is bonus.
- **Cancellation propagation**: if the FastAPI request is cancelled (client
  disconnects), the `async def _body()` generator's `finally` must close the
  DB session. Standard `async with` handles this; the pattern in `hf_proxy.py`
  is the proven reference.
- **vue-query v5 `queryClient.setQueryData` reactivity**: vue-query DOES
  notify watchers on `setQueryData`. Confirmed by reading the same surface
  used by `useTaskMutations`' optimistic update pattern. No risk.
- **Backoff with fake-timers in vitest**: any test that exercises `streamSse`'s
  backoff timing must run inside `vi.useFakeTimers()` + manual `vi.advanceTimersByTime`. The pure parser tests don't need timers; the composable
  spec mocks `streamSse` directly (no backoff execution in tests).
- **Contingency**: if the SSE endpoint slips integration, the spec's M3 can
  still merge with the seam fully wired but `useTaskDetail` left without
  `streamUrl` — the realtime seam is in place, polling stays in production,
  and the cutover lands as a one-line follow-up. (Same pattern recorded in
  the SP2 plan's §7.)

## 8. Self-Review

- **Placeholder scan**: none. Every endpoint, helper, composable change, and
  test has concrete shape.
- **Consistency**: the single-seam contract is preserved (§3, §5.3); zero view
  changes (§2, §5.4); additive backend (§4); inherits all SP1/SP2/SP3 locked
  decisions verbatim (§3).
- **Scope**: deliberately the smallest credible delivery — 1 backend endpoint,
  1 consumer opted in, all others unchanged. The decomposition documented SP5
  as "optional realtime upgrade"; this lands the architecture, demonstrates
  end-to-end live updates, and leaves a clear path for SP5b (per-resource
  streams) if telemetry justifies it.
- **Ambiguity**: endpoint path, DTO shape, tick interval, backoff schedule,
  auth (Bearer via fetch), terminal-stop semantics, and the consumer that opts
  in are all pinned. The contingency (M3 ships without `useTaskDetail` opt-in)
  is documented.
