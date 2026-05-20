# UI-SP5e — Quota Snapshot SSE Stream (Design)

> 5th application of the view-free SSE template (after SP5 task-detail,
> SP5b executors, SP5c tasks-list, SP5d audit-log). The Quota page becomes
> real-time — bytes/storage/concurrent counters tick live during active
> downloads.
> Status: design self-approved per Rule #1 (smallest conservative
> follow-on; useSystemHealth was the original SP5e target but pivoted —
> `/health/active` is auth-free, non-`/api/v1`, returns 503 when not
> leader → bad SSE template fit; `useQuota` matches SP5* template
> precisely).
> Branch: `feat/ui-sp5e-quota-sse`.

## 1. Context & Scope

`useQuota()` currently polls `/api/v1/quota/current` at 30 s. During an
active download the user sits on Quota / Settings pages and watches
`bytes_used_month` / `storage_gb_used` / `concurrent_tasks` advance —
exactly the use case where push beats poll.

**In scope (additive, zero migration, zero new dep):**

1. **Backend**: `GET /api/v1/quota/current/stream` — hand-rolled SSE
   mirroring `audit_stream.py`. Same tenant principal, same data shape as
   the existing one-shot endpoint. New file
   `src/dlw/api/quota_stream.py`. 15 s default tick (env
   `DLW_QUOTA_STREAM_INTERVAL_SECONDS`, clamped `[0.5, 60.0]` at runtime).
   Same `?max_ticks=N` testability hatch.

2. **Service extraction**: pull the 6-line snapshot query out of
   `quota.py:current` into a new service helper
   `src/dlw/services/quota_read.py::get_quota_snapshot(session, tenant_id)
   -> dict | None` (returns `None` when tenant missing — the stream and
   the one-shot endpoint handle the `None` case identically: 404). Both
   the existing `/current` route and the new `/current/stream` call it.
   DRY justification: 6 lines × 2 callers = abstraction earns its keep.

3. **Frontend**: `useQuota()` opts in via the SP5 seam. `streamUrl` is a
   literal string (no filters). `applyEvent` JSON-parses snapshots.
   Signature/return-shape UNCHANGED. `baseIntervalMs` stays at 30_000 (the
   polling fallback cadence).

**Out of scope (intentional, deferred):**

- `useSystemHealth` (10 s) — the `/health/active` endpoint is auth-free,
  not under `/api/v1`, and returns 503 when not leader. Wrapping it in
  SSE requires either a non-template auth-free SSE handler or a state-
  emitting wrapper that always returns 200; neither earns the cost given
  health-pill UX is fine at 10 s.
- The 4 SP2 sub-resource composables — browser per-host connection cap
  concern remains; defer.

## 2. Inherited Locked Decisions

All from SP1-SP5d. Most relevant for SP5e:

- `useLiveResource`'s `streamUrl`+`applyEvent` IS the seam (additive
  options, view-free).
- **Route collision rule** (SP5c BLOCKER): not applicable here —
  `/api/v1/quota/current` is static, `/api/v1/quota/current/stream` is
  static, no parameterized sibling. Either include order works. To stay
  consistent with SP5d's defensive pattern, register `quota_stream_router`
  BEFORE `quota_router` in `main.py` with the same explanatory comment.
- **httpx ASGITransport buffering** (SP5): mitigated by `?max_ticks=N`.
- **Runtime clamp pattern** (SP5+): `_clamped_interval()` reads the
  setting via `getattr(get_settings(), …)` and applies
  `max(0.5, min(60.0, raw))` — the runtime clamp is the authority, the
  pydantic Field is documentation.

## 3. Backend Design

### 3.1 Endpoint

```
GET /api/v1/quota/current/stream
Authorization: Bearer <jwt>
Accept: text/event-stream

→ 200 OK text/event-stream
: open

data: { "tenant_id": 1, "bytes_used_month": 123, "bytes_quota_month": ...,
        "storage_gb_used": ..., "storage_gb_quota": ...,
        "concurrent_tasks": ..., "concurrent_quota": ... }

data: { "tenant_id": 1, "bytes_used_month": 456, ... }
…
```

- No query parameters except `?max_ticks=N` (test hatch).
- 15 s default tick; clamp `[0.5, 60.0]`.
- No natural terminal state — only client disconnect / shutdown closes.
- Per-tick `async with session_maker() as s:` fresh session.
- If `get_quota_snapshot` returns `None` (tenant deleted mid-stream — not
  expected, but defensive), the stream ends with no further data (close
  generator). Initial pre-stream tenant lookup raises 404 if missing at
  open time, before `:open` is sent — same as the existing endpoint.

### 3.2 RBAC

`policy.csv` already grants tenant_admin/operator/viewer GET on
`/api/v1/quota*` (added by SP3 / SP1). The wildcard covers
`/current/stream`. **No policy.csv change.**

### 3.3 OpenAPI

Add `/quota/current/stream` after the existing `/quota/current:` block:

```yaml
  /quota/current/stream:
    get:
      tags: [quota]
      summary: Live quota snapshot SSE stream (UI-SP5e)
      operationId: streamQuotaCurrent
      responses:
        '200':
          description: SSE stream of quota snapshots
          content:
            text/event-stream:
              schema:
                type: string
                description: |
                  Each event is `data: <quota-snapshot JSON>\n\n`
                  ({tenant_id, bytes_used_month, bytes_quota_month,
                   storage_gb_used, storage_gb_quota,
                   concurrent_tasks, concurrent_quota}). Stream
                  terminates only on client disconnect or shutdown.
        '401':
          description: Unauthenticated
        '404':
          description: Tenant not found
```

### 3.4 Config

Add 1 setting in `src/dlw/config.py`:

```python
# UI-SP5e — SSE tick rate for /quota/current/stream (clamped at runtime).
quota_stream_interval_seconds: float = Field(default=15.0)
```

Runtime clamp `[0.5, 60.0]` in `quota_stream.py:_clamped_interval()`.

### 3.5 Service extraction

`src/dlw/services/quota_read.py` (new file):

```python
"""Shared quota-snapshot read used by /quota/current (one-shot) and
/quota/current/stream (SSE)."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.tenant import Tenant
from dlw.db.models.usage import QuotaSnapshot


async def get_quota_snapshot(
    session: AsyncSession, tenant_id: int,
) -> dict | None:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return None
    snap = await session.get(QuotaSnapshot, tenant_id)
    return {
        "tenant_id": tenant.id,
        "bytes_used_month": snap.bytes_used_month if snap else 0,
        "bytes_quota_month": tenant.quota_bytes_month,
        "storage_gb_used": snap.storage_gb_used if snap else 0,
        "storage_gb_quota": tenant.quota_storage_gb,
        "concurrent_tasks": snap.concurrent_tasks if snap else 0,
        "concurrent_quota": tenant.quota_concurrent,
    }
```

`src/dlw/api/quota.py:current` rewritten to:

```python
@router.get("/current")
async def current(
    principal: Principal = Depends(require_perm("/api/v1/quota*", "GET")),
    session: AsyncSession = Depends(_session),
) -> dict:
    snap = await get_quota_snapshot(session, principal.tenant_id)
    if snap is None:
        raise HTTPException(404, detail="tenant not found")
    return snap
```

(Import added at top: `from dlw.services.quota_read import get_quota_snapshot`.)

### 3.6 Tests (`tests/api/test_quota_stream.py`)

Mirror the 3-test shape of the simpler SSE endpoints:

1. **Unauth → 401**.
2. **Tenant isolation** — seed tenant 1 + tenant 2 with distinct
   `QuotaSnapshot.bytes_used_month`; stream as tenant 1 with
   `?max_ticks=1`; snapshot has `tenant_id=1` AND matches tenant-1
   values (not tenant-2's). This proves the principal's tenant_id is
   used.
3. **`?max_ticks=2` multi-snapshot** — receive ≥ 2 envelopes; both have
   the 7 expected keys (`tenant_id`, `bytes_used_month`,
   `bytes_quota_month`, `storage_gb_used`, `storage_gb_quota`,
   `concurrent_tasks`, `concurrent_quota`).

Module bootstrap mirrors SP3/SP5b — single seed of `Tenant`,
`StorageBackend`, `Project`, `User` for tenant 1 (and tenant 2 for the
isolation test).

The existing `tests/api/test_quota.py` (one-shot endpoint) MUST keep
passing unchanged after the service extraction — that's the regression
proof of the refactor.

## 4. Frontend Design

### 4.1 `useQuota()` opt-in

Modify `frontend/src/composables/useQuota.ts`:

```ts
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { QuotaCurrent } from '@/api/types'

export function useQuota() {
  return useLiveResource<QuotaCurrent>(
    ['quota'],
    async () => (await client.get<QuotaCurrent>('/api/v1/quota/current')).data,
    {
      baseIntervalMs: 30_000,
      staleTime: 30_000,
      streamUrl: '/api/v1/quota/current/stream',
      applyEvent: (_prev, ev) => JSON.parse(ev.data) as QuotaCurrent,
    },
  )
}
```

### 4.2 Tests

Add `frontend/tests/unit/useQuotaStream.spec.ts` — mirror SP5c's
`useTaskListStream.spec.ts` (the simplest case — no filters, no reactive
URL). Assert: opt-in fields present on opts; applyEvent JSON-parses; key
stays `['quota']`.

### 4.3 View-free proof

- `frontend/src/pages/QuotaPage.vue` — NOT modified.
- `frontend/src/components/infra/QuotaCard.vue` — NOT modified.
- The Settings page also reads `useQuota()` — NOT modified.
- Existing `frontend/tests/unit/sp3Composables.spec.ts::useQuota`
  (if it exists) — NOT modified; must pass unchanged.

### 4.4 Headed smoke

Same recipe: fresh `:8011` controller with SP5e code + Vite proxying to
it + headed Playwright navigate to `/quota` (or `/settings`), observe
`/quota/current/stream` SSE request in DevTools network.

## 5. Milestones

- **M1 Backend**: openapi + config + service extraction + `quota_stream.py`
  + 3 tests + router include + M1 gate (full backend pytest must show
  `test_quota.py` still green to prove the refactor is safe).
- **M2 Frontend cutover**: `useQuota` opts in + new composable spec +
  view-free re-verification + full frontend gate.
- **M3 Smoke + docs**: headed Playwright smoke + append SP5e section to
  `docs/operator/web-ui.md`.

## 6. Risks & Contingencies

- **Service extraction breaks `test_quota.py`** — the refactor is
  semantically identical (the dict construction is moved, not changed);
  M1 gate catches any drift. If it does break, the test result IS the
  regression; fix in the extracted function.
- **httpx ASGITransport buffering** — same as prior SSEs; mitigated by
  `?max_ticks=N`.
- **Route collision** — N/A (no parameterized siblings).
- **404 on missing tenant** — the existing endpoint raises 404; the new
  stream MUST too (pre-stream tenant lookup before `:open`).

## 7. Self-Review

- **Placeholder scan**: none.
- **Consistency**: mirrors SP5d (4th application) which mirrored SP5c
  which mirrored SP5b. Same idiom (`StreamingResponse` + 50 ms-grain
  cancel-aware sleep + per-tick fresh session + `:open` first byte +
  `?max_ticks` hatch + runtime clamp).
- **Scope**: smallest credible follow-on (1 endpoint + 1 composable
  opt-in + 1 service extraction). Smaller than SP5d.
- **Ambiguity**: endpoint path, tick rate + clamp, RBAC reuse, response
  shape (raw dict, same as existing endpoint), test set, no filters all
  pinned.
- **Service-extraction necessity**: 6 lines × 2 callers — borderline
  but on the right side of the line (a second caller is the inflection
  point where DRY stops being premature).
