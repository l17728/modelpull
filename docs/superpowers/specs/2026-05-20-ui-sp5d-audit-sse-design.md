# UI-SP5d — Audit Log SSE Stream (Design)

> 4th application of the view-free SSE template (after SP5 task-detail, SP5b
> executors, SP5c tasks-list). The Audit Log page becomes real-time —
> compliance/security flagship UX win.
> Status: design self-approved per Rule #1.
> Branch: `feat/ui-sp5d-audit-sse`.

## 1. Context & Scope

The view-free single-seam architecture has shipped against 3 consumers (SP5,
SP5b, SP5c). SP5d targets the Audit Log page (`useAuditLog`, polled at
10 s). Live audit events visibly improve the page's compliance/security use
case.

**In scope (additive, zero migration, zero new dep):**

1. **Backend**: `GET /api/v1/audit/log/stream` — hand-rolled SSE mirroring
   `tasks_stream.py`/`executors_stream.py`/`tasks_list_stream.py`. Reuses
   SP3's `search_audit_log` service (tenant filter, action/actor/from/to
   filters). Server pushes the **first page** (newest 50 + next_cursor) at
   each tick — same shape as the existing `GET /audit/log` returns. **No
   cursor query param on the stream** (the live feed is always page 1; the
   client's "Load older" button still uses the one-shot
   `fetchOlderAudit(...)`). New file `src/dlw/api/audit_stream.py`. 10 s
   default tick (env `DLW_AUDIT_STREAM_INTERVAL_SECONDS`, clamped
   `[0.5, 60.0]`). Same `?max_ticks=N` testability hatch.

2. **Frontend**: `useAuditLog(f)` opts in via the SP5 seam. The streamUrl is
   a reactive `computed` over the 4 filter refs (`action`, `actor`, `from`,
   `to`); the applyEvent JSON-parses the snapshot. Signature/return-shape
   UNCHANGED; the page's "Load older" button (`fetchOlderAudit`) is
   untouched.

**Out of scope (intentional, deferred):**

- `useQuota` (30 s) / `useSystemHealth` (10 s) — push value is marginal at
  these cadences; opt-in is a 1-line follow-on when justified.
- The 4 SP2 sub-resource composables (chunks/source-alloc/executors/events).
  Browser per-host connection cap concern remains; defer.

## 2. Inherited Locked Decisions

All from SP1-SP5/SP5b/SP5c — unchanged. Most relevant for SP5d:

- `useLiveResource`'s `streamUrl`+`applyEvent` IS the seam.
- **Route collision rule** (SP5c BLOCKER): when a new static route shares
  prefix with an existing parameterized route, the static-path router must
  be `include_router`'d FIRST. For SP5d: the existing `audit_router` has
  prefix `/api/v1/audit` and routes `/log`; SP5d's new `audit_stream_router`
  same prefix + route `/log/stream`. **No collision** (both paths are
  static; `/log` and `/log/stream` are siblings, not parent/parameterized).
  Either include-order works. To be defensive and consistent with SP5c, the
  plan still puts the new router BEFORE the existing audit router.
- **Cross-tenant test fixture rule** (SP5c BLOCKER): if a test POSTs as a
  non-default tenant, bootstrap must seed the complete owner-FK chain
  (Tenant+Project+User+StorageBackend). SP5d's tests only seed audit rows
  directly (no POST as tenant 2 needed for cross-tenant verification —
  insert audit rows with `tenant_id=2` and verify they don't appear in
  tenant 1's stream). Simpler.

## 3. Backend Design

### 3.1 Endpoint

```
GET /api/v1/audit/log/stream?action=…&actor_user_id=…&from=…&to=…
Authorization: Bearer <jwt>
Accept: text/event-stream

→ 200 OK text/event-stream
: open

data: { "items": [ AuditEntry, ... ], "next_cursor": "…" | null }

data: { "items": [...], "next_cursor": "…" }
…
```

- Reuses `search_audit_log(session, tenant_id, actor_user_id, action_prefix,
  from_, to, cursor=None, limit=50)` from `services/audit_query.py` (SP3).
  The stream **always** passes `cursor=None` — live = page 1.
- Same `from_: datetime | None = Query(default=None, alias="from")` pattern
  as the SP3 endpoint.
- 10 s default tick; clamp `[0.5, 60.0]`.
- No natural terminal state — only client disconnect / shutdown closes.
- Per-tick `async with session_maker() as s:` fresh session.

### 3.2 RBAC

`policy.csv` already grants tenant_admin/operator/viewer GET on
`/api/v1/audit*` (added by SP3). The wildcard covers `/log/stream`. No
policy.csv change required.

### 3.3 OpenAPI

Add `/audit/log/stream` after the existing `/audit/log:` block:

```yaml
  /audit/log/stream:
    get:
      tags: [audit]
      summary: Live audit-log SSE stream (UI-SP5d)
      operationId: streamAuditLog
      parameters:
        - in: query
          name: actor_user_id
          schema: {type: integer, format: int64}
        - in: query
          name: action
          schema: {type: string}
        - in: query
          name: from
          schema: {type: string, format: date-time}
        - in: query
          name: to
          schema: {type: string, format: date-time}
      responses:
        '200':
          description: SSE stream of AuditSearchResponse snapshots
          content:
            text/event-stream:
              schema:
                type: string
                description: |
                  Each event is `data: <AuditSearchResponse JSON>\n\n`
                  ({items, next_cursor}). Live feed always pushes page 1
                  (newest first); the client's "Load older" button still
                  uses the one-shot /audit/log?cursor=… endpoint. Stream
                  terminates only on client disconnect or shutdown.
```

### 3.4 Config

Add 1 setting in `src/dlw/config.py`:

```python
audit_stream_interval_seconds: float = Field(default=10.0)
```

Runtime clamp `[0.5, 60.0]` in `audit_stream.py:_clamped_interval()`.

### 3.5 Tests (`tests/api/test_audit_log_stream.py`)

Mirror the 4-test shape of `test_executors_stream.py` / `test_tasks_list_stream.py`:

1. **Unauth → 401**.
2. **Tenant isolation** — seed audit rows with `tenant_id=1` AND
   `tenant_id=2`; stream as tenant 1; snapshot only contains tenant-1 rows.
3. **`?max_ticks=2` multi-snapshot** — receive ≥ 2 envelopes; both have
   `items` + `next_cursor` keys.
4. **Action prefix filter** — stream `?action=task.created&max_ticks=1`;
   snapshot only contains rows whose action starts with `task.created`.

Module bootstrap mirrors SP3's `test_audit_search.py`.

## 4. Frontend Design

### 4.1 `useAuditLog` opt-in

Modify `frontend/src/composables/useAuditLog.ts` — add `streamUrl` (a
`computed` over the 4 filter refs) and `applyEvent` to the `LiveOptions`.
`fetchOlderAudit` is unchanged (still used by the page's "Load older"
button). `buildQuery` helper now also builds the **stream URL** when
`forStream=true` flag is passed (omits the `cursor` and `limit` params for
the stream path).

Concretely (full replacement of the file):

```ts
import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { AuditSearchResponse } from '@/api/types'

export interface AuditFilters {
  action: Ref<string>
  actor: Ref<number | null>
  from: Ref<string | null>
  to: Ref<string | null>
}
export interface AuditFiltersPlain {
  action: string
  actor: number | null
  from: string | null
  to: string | null
}

function buildQuery(
  f: AuditFiltersPlain, cursor: string | null,
): string {
  const p = new URLSearchParams()
  p.set('limit', '50')
  if (f.action) p.set('action', f.action)
  if (typeof f.actor === 'number' && Number.isFinite(f.actor)) {
    p.set('actor_user_id', String(f.actor))
  }
  if (f.from) p.set('from', f.from)
  if (f.to) p.set('to', f.to)
  if (cursor) p.set('cursor', cursor)
  return `/api/v1/audit/log?${p.toString()}`
}

function buildStreamUrl(f: AuditFiltersPlain): string {
  const p = new URLSearchParams()
  if (f.action) p.set('action', f.action)
  if (typeof f.actor === 'number' && Number.isFinite(f.actor)) {
    p.set('actor_user_id', String(f.actor))
  }
  if (f.from) p.set('from', f.from)
  if (f.to) p.set('to', f.to)
  const qs = p.toString()
  return `/api/v1/audit/log/stream${qs ? '?' + qs : ''}`
}

export function useAuditLog(f: AuditFilters) {
  const streamUrl = computed(() => buildStreamUrl({
    action: f.action.value, actor: f.actor.value,
    from: f.from.value, to: f.to.value,
  }))
  return useLiveResource<AuditSearchResponse>(
    ['audit', f.action, f.actor, f.from, f.to],
    async () => (await client.get<AuditSearchResponse>(buildQuery({
      action: f.action.value, actor: f.actor.value,
      from: f.from.value, to: f.to.value,
    }, null))).data,
    {
      baseIntervalMs: 10_000,
      streamUrl,
      applyEvent: (_prev, ev) => JSON.parse(ev.data) as AuditSearchResponse,
    },
  )
}

export async function fetchOlderAudit(
  f: AuditFiltersPlain, cursor: string,
): Promise<AuditSearchResponse> {
  return (await client.get<AuditSearchResponse>(buildQuery(f, cursor))).data
}
```

### 4.2 Tests

Add `frontend/tests/unit/useAuditLogStream.spec.ts` — assert opt-in
(streamUrl + applyEvent passed to seam, streamUrl reactive to filter
changes, applyEvent parses JSON). Mirror SP5b's `useExecutorsStream.spec.ts`.

### 4.3 View-free proof

- `frontend/src/pages/Audit.vue` — NOT modified.
- `frontend/tests/unit/AuditPage.spec.ts` — NOT modified; must pass
  unchanged (it mocks `useAuditLog` entirely so the opt-in is invisible).

Note: the SP3 `sp3Composables.spec.ts::useAuditLog builds query from filters`
test mocks `useLiveResource` and asserts the captured key, fetcher path, and
filters. **My change adds `streamUrl` + `applyEvent` to opts** — the SP3 test
asserts on `last.key` and the fetcher URL, NOT on full options shape, so the
additive change doesn't break it. Re-verify at execution time.

### 4.4 Headed smoke

Same recipe: fresh `:8011` controller with SP5d code + Vite proxying to it +
headed Playwright navigate to `/audit`, observe `/audit/log/stream` SSE
request in DevTools network.

## 5. Milestones

- **M1 Backend**: openapi + config + `audit_stream.py` + 4 tests + router
  include + M1 gate.
- **M2 Frontend cutover**: `useAuditLog` opts in + new composable spec +
  view-free re-verification + full frontend gate.
- **M3 Smoke + docs**: headed Playwright smoke + append to
  `docs/operator/web-ui.md`.

## 6. Risks & Contingencies

- **httpx ASGITransport buffering** — same as prior SSEs; mitigated by
  `?max_ticks=N`.
- **`useAuditLog` ALSO has `fetchOlderAudit`** (one-shot pagination
  function) — unchanged. The stream covers live page-1 only.
- **SP3 spec breakage from additive opts** — verified compatible; re-run at
  execution time.
- **Route collision** — not applicable (audit prefix has no parameterized
  sibling routes).

## 7. Self-Review

- **Placeholder scan**: none.
- **Consistency**: mirrors SP5c (3rd application) which mirrored SP5b which
  mirrored SP5. All same idiom.
- **Scope**: smallest credible follow-on (1 endpoint + 1 composable
  opt-in).
- **Ambiguity**: endpoint path, tick rate + clamp, RBAC reuse, DTO shape
  (reuse `AuditSearchResponse`), test set, no-cursor-in-stream rule all
  pinned.
