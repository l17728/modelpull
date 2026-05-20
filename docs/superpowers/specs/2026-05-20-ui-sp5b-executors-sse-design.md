# UI-SP5b — Executors SSE Stream (Design)

> Follow-on to UI-SP5 (PR #22 `8c77ca1`). The view-free single-seam architecture
> is in place; SP5b validates it against a SECOND consumer by extending SSE to
> the Executors page.
> Status: design self-approved per project Rule #1 (autonomous; recommended/conservative).
> Branch: `feat/ui-sp5b-executors-sse`.

## 1. Context & Scope

UI-SP5 proved the view-free seam by opting `useTaskDetail` in to SSE while
leaving every other view, page, and composable unchanged. SP5b takes the
**next-most-valuable consumer** — `useExecutors`, polled at 5 s — and opts it
in via the SAME mechanism. **No new architecture; no view changes.**

**In scope (additive, zero migration, zero new runtime dep):**

1. **Backend**: 1 new endpoint `GET /api/v1/executors/stream` (text/event-stream).
   Mirrors `tasks_stream.py` shape: hand-rolled SSE via `StreamingResponse`;
   default 5 s tick (env `DLW_EXECUTORS_STREAM_INTERVAL_SECONDS`, clamped
   `[0.5, 60.0]` to match Executors' polling rhythm); reuses the existing
   `list_executors_for_principal` service (tenant filter +
   `system_admin`/`is_service` bypass — same RBAC as the GET); never
   terminates on data state (executors are a long-lived collection — only
   client-disconnect or shutdown closes); same `:open\n\n` first-byte flush;
   same `?max_ticks=N` testability hatch for the httpx ASGITransport
   buffering issue. New file `src/dlw/api/executors_stream.py` — must NOT
   live in `src/dlw/api/executors.py` per the
   `lint_no_bearer_on_executor_routes` invariant (executors.py is mTLS-only).

2. **Frontend**: `useExecutors` opts in via the SP5-added
   `streamUrl`+`applyEvent` options. The composable signature, return shape,
   and queryKey are UNCHANGED. Existing `Executors.vue` page and
   `ExecutorsPage.spec.ts` are NOT touched.

3. **i18n / nav / router**: untouched. SP5b introduces no new UI text.

**Out of scope (intentional, deferred):**

- SSE for the 4 SP2 sub-resource composables (chunks / source-allocation /
  participating-executors / events). Same reasoning as SP5: opting them in
  requires either 4 separate connections (browser per-host connection cap
  concern with all 4 on the same Task Detail page) or a multi-resource
  envelope that breaks view-free. Defer to a future SP5c if telemetry
  justifies the multi-stream design.
- SSE for the remaining low-cadence composables (`useTaskList` 5s,
  `useAuditLog` 10s, `useQuota` 30s, `useSystemHealth` 10s). Their push value
  is marginal; opt-in is now a 1-line addition per composable when telemetry
  argues for it.
- WebSocket transport. SSE delivers the same outcome.

## 2. Inherited Locked Decisions

All from SP1/SP2/SP3/SP5 — unchanged. The most relevant for SP5b:

- `useLiveResource`'s `streamUrl` + `applyEvent` extension (SP5) is the seam.
- `DataBoundary`, the 9 status semantic colors, RBAC server-side, additive
  backend only, no new runtime dep, no migration, i18n parity.
- Route placement rule: browser-facing executor routes go in a NEW file (NOT
  `src/dlw/api/executors.py` — mTLS-only per
  `tools/lint_invariants.py:check_no_bearer_on_executor_routes`).
- Pre-review gate: 2 opus reviewers; final opus review; controller-direct per
  task; headed-Playwright smoke on the local stack.

## 3. Backend Design

### 3.1 Endpoint

```
GET /api/v1/executors/stream?status=<healthy|degraded|suspect|faulty|joining>
Authorization: Bearer <jwt>
Accept: text/event-stream

→ 200 OK Content-Type: text/event-stream
: open

data: {"items": [ ExecutorRead, ... ]}

data: {"items": [...]}
…
```

- Reuses `list_executors_for_principal(session, principal, status)` from
  SP3's `services/executors_read.py` — exact same RBAC and tenant filtering.
  Wraps the result list in `ExecutorListResponse` and serializes via Pydantic
  `model_dump_json()`.
- Default tick 5 s (matches the existing `useExecutors` polling cadence);
  clamped `[0.5, 60.0]` at runtime.
- No natural terminal state (executors lifecycle is long); only client
  disconnect or lifespan shutdown closes.
- Per-tick session via `async_sessionmaker(get_engine(), expire_on_commit=False)()`.
- `?max_ticks=N` test hatch (`Query(default=None, ge=1, le=10000)`) — same
  rationale as SP5.

### 3.2 RBAC

`policy.csv` already grants tenant_admin/operator/viewer GET on
`/api/v1/executors*` (added by SP3). The wildcard covers `/stream` — no
policy.csv change required. Verified in SP5b §3.1's request shape.

### 3.3 OpenAPI

Add one path block after the existing `/executors/register:` block (or
adjacent to the SP3 `/executors` GET — file inspection at plan time pins
the exact insertion site):

```yaml
  /executors/stream:
    get:
      tags: [executors]
      summary: Live executors-list SSE stream (UI-SP5b)
      operationId: streamExecutors
      parameters:
        - in: query
          name: status
          schema:
            type: string
            enum: [joining, healthy, degraded, suspect, faulty]
      responses:
        '200':
          description: SSE stream of ExecutorListResponse snapshots
          content:
            text/event-stream:
              schema:
                type: string
                description: |
                  Each event is `data: <ExecutorListResponse JSON>\n\n`. Stream
                  terminates only on client disconnect or controller shutdown.
                  Keep-alive comment lines (`:keepalive`) may appear.
```

### 3.4 Config

Add 1 setting in `src/dlw/config.py` alongside `task_stream_interval_seconds`:

```python
executors_stream_interval_seconds: float = Field(default=5.0)
```

Runtime clamping `[0.5, 60.0]` lives in `executors_stream.py:_clamped_interval()`.

### 3.5 Tests (`tests/api/test_executors_stream.py`)

Mirror the 4-test shape of `test_task_stream.py`:

1. **Unauth → 401**.
2. **Tenant filter** — seed 3 executors (tenant 1, tenant 2, shared/null);
   stream as tenant 1 → snapshot includes only tenant 1 + shared.
3. **`?max_ticks=2` multi-snapshot** — receive at least 2 envelopes within
   2 s using `DLW_EXECUTORS_STREAM_INTERVAL_SECONDS=0.1`.
4. **Status filter** — `?status=healthy` snapshot includes only healthy ones.

Module-scoped bootstrap mirrors the SP3 `test_executors_list.py` pattern
(drop+create, seed Tenant 1+2, etc.). Inserts go via a direct session like
SP3 (with `DELETE FROM executors` upfront to handle test ordering).

## 4. Frontend Design

### 4.1 `useExecutors` opt-in

Replace `frontend/src/composables/useExecutors.ts` with:

```ts
import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { ExecutorListResponse } from '@/api/types'

export function useExecutors(status: Ref<string | null>) {
  const streamUrl = computed(() => {
    const q = status.value ? `?status=${encodeURIComponent(status.value)}` : ''
    return `/api/v1/executors/stream${q}`
  })
  return useLiveResource<ExecutorListResponse>(
    ['executors', status],
    async () => {
      const q = status.value ? `?status=${encodeURIComponent(status.value)}` : ''
      return (await client.get<ExecutorListResponse>(
        `/api/v1/executors${q}`)).data
    },
    {
      baseIntervalMs: 5_000,
      streamUrl,
      applyEvent: (_prev, ev) => JSON.parse(ev.data) as ExecutorListResponse,
    },
  )
}
```

### 4.2 Tests

The existing `frontend/tests/unit/sp3Composables.spec.ts::useExecutors wires
key + status query + interval` test mocks `useLiveResource`; it remains valid
because `useExecutors`'s signature and key-shape are unchanged. The new
options (`streamUrl`, `applyEvent`) appear in the captured `opts` and the
test's "wires key, baseIntervalMs" assertions stay green (they don't assert
shape equality on `opts`).

**Add one targeted SP5b test**: assert `useExecutors` now passes a `streamUrl`
+ `applyEvent` to the seam — proves the opt-in. Append to
`sp3Composables.spec.ts` or create a small `useExecutorsStream.spec.ts` —
the latter to keep the SP3 spec read-only (additive philosophy).

### 4.3 View-free proof

- `frontend/src/pages/Executors.vue` — NOT modified.
- `frontend/tests/unit/ExecutorsPage.spec.ts` — NOT modified; runs unchanged
  and stays green (mocks the composable's data, doesn't care about transport).
- All other SP1/SP2/SP3/SP5 composables/pages — NOT touched.

### 4.4 Headed smoke

Reuse the SP5 smoke recipe — boot a fresh `:8011` controller with current
code, restart Vite proxying to it, navigate to `/executors`, observe `/stream`
SSE request in DevTools network, screenshot.

## 5. Milestones

- **M1 Backend**: openapi + config + `executors_stream.py` + 4 tests + router
  include + M1 gate.
- **M2 Frontend cutover**: `useExecutors` opts in + 1 new composable spec +
  full frontend gate.
- **M3 Smoke + docs**: headed Playwright smoke + append to
  `docs/operator/web-ui.md`.

## 6. Risks & Contingencies

- **httpx ASGITransport buffering** — same as SP5. Mitigated by `?max_ticks=N`
  hatch (proven in SP5).
- **`useExecutors` test stability** — the SP3 `sp3Composables.spec.ts` mocks
  `useLiveResource` and asserts the captured args. Adding `streamUrl`/`applyEvent`
  doesn't break those assertions (the SP3 test asserts `last.key` and
  `last.opts.baseIntervalMs`, not full equality). Re-verify at execution time.

## 7. Self-Review

- **Placeholder scan**: none. Every endpoint, test case, and frontend change
  has concrete code or a clear contract.
- **Consistency**: mirror SP5 exactly — same SSE idiom, same hatch, same
  view-free contract. Inherits all SP1-SP5 locked decisions.
- **Scope**: deliberately the smallest credible follow-on (1 endpoint, 1
  composable opt-in). Demonstrates the architecture's incremental-upgrade
  capability and leaves a clear path for SP5c (more composables) if value
  justifies.
- **Ambiguity**: endpoint path, tick rate + clamp, RBAC reuse (no policy.csv
  change), DTO shape (reuse ExecutorListResponse), test set, and the single
  opt-in composable are all pinned.
