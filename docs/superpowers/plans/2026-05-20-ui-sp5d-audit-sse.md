# UI-SP5d — Audit Log SSE Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** Add `GET /api/v1/audit/log/stream` + opt `useAuditLog` in to the SP5 seam. 4th application of the view-free SSE template.

**Architecture:** Backend mirrors `tasks_list_stream.py`; reuses SP3's `search_audit_log` service; stream always passes `cursor=None` (live = page 1; client's "Load older" button stays on the one-shot `fetchOlderAudit`). Frontend `useAuditLog` opts in with a reactive `streamUrl` `computed` over the 4 filter refs. `Audit.vue` page and `AuditPage.spec.ts` UNCHANGED.

---

## Conventions (apply to every task — same as SP5c)

- **Branch:** `feat/ui-sp5d-audit-sse` (off `main` @ `f9ac9ce`).
- **Bash cwd persists**. Always `cd /d/download_weights && git …` for git; `cd /d/download_weights/frontend && pnpm …` for frontend.
- **Tenant gate**: reuse SP3's `search_audit_log` (filters by `AuditLog.tenant_id == principal.tenant_id`).
- **`?max_ticks=N`**: same SP5/SP5b/SP5c testability hatch.
- **Route placement**: SP5d's new path `/api/v1/audit/log/stream` is static-sibling of static `/log` (both under `/api/v1/audit` prefix) — no collision risk. Plan still puts the new router BEFORE the existing audit router for defensive consistency (cheap, future-proof).

---

## File Structure

**Backend (create):**
- `src/dlw/api/audit_stream.py` — `GET /api/v1/audit/log/stream` route.
- `tests/api/test_audit_log_stream.py` — 4 tests.

**Backend (modify):**
- `api/openapi.yaml` — add `/audit/log/stream` path block.
- `src/dlw/main.py` — register router BEFORE existing `audit_router` (defensive consistency with SP5c lesson).
- `src/dlw/config.py` — add `audit_stream_interval_seconds` setting.

**Frontend (modify):**
- `frontend/src/composables/useAuditLog.ts` — opt-in + new `buildStreamUrl` helper.

**Frontend (create):**
- `frontend/tests/unit/useAuditLogStream.spec.ts` — new spec.

**Docs (modify):** `docs/operator/web-ui.md` (append SP5d section).

---

# Milestone M1 — Backend

### Task 1: OpenAPI + config

- [ ] **Step 1**: In `api/openapi.yaml`, find the existing `/audit/log:` GET block (around line 1266 — SP3-added). Insert `/audit/log/stream:` block immediately after the response of `/audit/log` and before the next sibling path (look for the `# ==========` section header that follows `/audit/log`).

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

- [ ] **Step 2**: In `src/dlw/config.py`, add the new setting next to the other `*_stream_interval_seconds` fields:

```python
    audit_stream_interval_seconds: float = Field(default=10.0)
```

- [ ] **Step 3**: Validate.

```bash
cd /d/download_weights
npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error
npx --yes @apidevtools/swagger-cli validate api/openapi.yaml
```

- [ ] **Step 4**: Commit.

```bash
cd /d/download_weights && git add api/openapi.yaml src/dlw/config.py && git commit -q -m "UI-SP5d M1: openapi /audit/log/stream path + audit_stream_interval_seconds setting"
```

---

### Task 2: SSE route + 4 tests

- [ ] **Step 1**: Write the failing test. Create `tests/api/test_audit_log_stream.py`:

```python
"""Tests for GET /api/v1/audit/log/stream (UI-SP5d SSE)."""
from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import make_app_with_state, principal_headers

SECRET = "unit-secret"
TICK = "0.1"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Tenant(id=1, slug="default", display_name="Default"))
        session.add(Tenant(id=2, slug="other", display_name="Other"))
        await session.flush()
        session.add(Project(id=1, tenant_id=1, name="default"))
        session.add(User(id=1, tenant_id=1, oidc_subject="dev",
                         email="d@l", role="tenant_admin"))
        session.add(StorageBackend(id=1, tenant_id=1, name="default",
                                   backend_type="s3", config_encrypted=b""))
        await session.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_AUDIT_STREAM_INTERVAL_SECONDS", TICK)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def auth() -> dict[str, str]:
    return principal_headers(secret=SECRET, role="tenant_admin")


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test", timeout=10.0) as c:
        yield c


async def _seed_audit(engine, *, tenant_id: int, n: int,
                       action: str = "task.note",
                       base: dt.datetime | None = None):
    from dlw.db.models.audit import AuditLog
    base = base or dt.datetime(2026, 5, 20, 12, 0, 0, tzinfo=dt.UTC)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        for i in range(n):
            s.add(AuditLog(
                occurred_at=base + dt.timedelta(seconds=i),
                tenant_id=tenant_id, actor_user_id=1, action=action,
                resource_type="task", resource_id=f"r{i}",
                outcome="success", payload={"i": i}, self_hash="0" * 64))
        await s.commit()


async def _collect(client, url, headers, *, count, timeout=4.0):
    received: list[str] = []
    async with asyncio.timeout(timeout):
        async with client.stream("GET", url, headers=headers) as resp:
            assert resp.status_code == 200, await resp.aread()
            ctype = resp.headers.get("content-type", "")
            assert "text/event-stream" in ctype, ctype
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    received.append(line[len("data: "):])
                    if len(received) >= count:
                        return received
    return received


@pytest.mark.slow
async def test_audit_stream_unauthenticated_401(client: AsyncClient) -> None:
    async with client.stream("GET", "/api/v1/audit/log/stream") as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_audit_stream_tenant_isolation(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed_audit(engine, tenant_id=2, n=2,
                       base=dt.datetime(2026, 5, 20, 9, 0, tzinfo=dt.UTC))
    await _seed_audit(engine, tenant_id=1, n=3,
                       base=dt.datetime(2026, 5, 20, 10, 0, tzinfo=dt.UTC))
    received = await _collect(
        client, "/api/v1/audit/log/stream?max_ticks=1", auth,
        count=1, timeout=3.0)
    body = json.loads(received[0])
    assert all(it["tenant_id"] == 1 for it in body["items"])
    assert len(body["items"]) == 3


@pytest.mark.slow
async def test_audit_stream_multi_snapshot(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed_audit(engine, tenant_id=1, n=2,
                       base=dt.datetime(2026, 5, 20, 11, 0, tzinfo=dt.UTC))
    received = await _collect(
        client, "/api/v1/audit/log/stream?max_ticks=2", auth,
        count=2, timeout=3.0)
    assert len(received) >= 2
    for raw in received[:2]:
        body = json.loads(raw)
        assert "items" in body
        assert "next_cursor" in body  # field present even if null
        assert isinstance(body["items"], list)


@pytest.mark.slow
async def test_audit_stream_action_filter(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed_audit(engine, tenant_id=1, n=2, action="task.created",
                       base=dt.datetime(2026, 5, 20, 13, 0, tzinfo=dt.UTC))
    await _seed_audit(engine, tenant_id=1, n=2, action="task.cancelled",
                       base=dt.datetime(2026, 5, 20, 13, 30, tzinfo=dt.UTC))
    received = await _collect(
        client,
        "/api/v1/audit/log/stream?action=task.created&max_ticks=1",
        auth, count=1, timeout=3.0)
    body = json.loads(received[0])
    assert body["items"], "expected at least one matching entry"
    assert all(it["action"].startswith("task.created")
               for it in body["items"])
```

- [ ] **Step 2**: Run to verify it fails.

`uv run pytest tests/api/test_audit_log_stream.py -v` → 4 FAIL (route 404).

- [ ] **Step 3**: Create the route. `src/dlw/api/audit_stream.py`:

```python
"""GET /api/v1/audit/log/stream — SSE audit-log live page-1 stream (UI-SP5d).

Hand-rolled text/event-stream; reuses SP3's search_audit_log service.
Stream always passes cursor=None (live = page 1); the client's "Load older"
button keeps using the one-shot /audit/log?cursor=… endpoint.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.session import get_engine
from dlw.schemas.audit import AuditSearchResponse
from dlw.services.audit_query import search_audit_log

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])

_KEEPALIVE_EVERY_TICKS = 3  # vestigial (cf. SP5+); kept for parity


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "audit_stream_interval_seconds", 10.0))
    return max(0.5, min(60.0, raw))


@router.get("/log/stream")
async def stream_audit_log(
    request: Request,
    actor_user_id: int | None = Query(default=None),
    action: str | None = Query(default=None, description="prefix match"),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    max_ticks: int | None = Query(default=None, ge=1, le=10000),
    principal: Principal = Depends(require_perm("/api/v1/audit*", "GET")),
) -> StreamingResponse:
    session_maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    interval = _clamped_interval()

    async def _body() -> AsyncIterator[bytes]:
        yield b":open\n\n"
        ticks_since_data = 0
        tick_count = 0
        try:
            while True:
                if await request.is_disconnected():
                    return
                async with session_maker() as s:
                    items, next_cursor = await search_audit_log(
                        s, principal.tenant_id,
                        actor_user_id=actor_user_id,
                        action_prefix=action,
                        from_=from_, to=to, cursor=None, limit=50)
                payload = AuditSearchResponse(
                    items=items, next_cursor=next_cursor)
                yield (f"data: {payload.model_dump_json()}"
                       "\n\n").encode("utf-8")
                ticks_since_data = 0
                tick_count += 1
                if max_ticks is not None and tick_count >= max_ticks:
                    return
                slept = 0.0
                while slept < interval:
                    if await request.is_disconnected():
                        return
                    chunk = min(0.05, interval - slept)
                    await asyncio.sleep(chunk)
                    slept += chunk
                ticks_since_data += 1
                if ticks_since_data >= _KEEPALIVE_EVERY_TICKS:
                    yield b":keepalive\n\n"
                    ticks_since_data = 0
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        _body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
```

- [ ] **Step 4**: Register router in `src/dlw/main.py`. **Defensive consistency with SP5c**: include BEFORE the existing `audit_router`. Find this block inside `create_app()`:

```python
    from dlw.api.audit import router as audit_router
    app.include_router(audit_router)
```

Replace with:

```python
    # SP5d: register the audit stream router BEFORE audit_router so any
    # future static-vs-parameterized collisions under the /audit prefix
    # default to the safe ordering (cf. SP5c lesson).
    from dlw.api.audit_stream import router as audit_stream_router
    app.include_router(audit_stream_router)
    from dlw.api.audit import router as audit_router
    app.include_router(audit_router)
```

- [ ] **Step 5**: Run → PASS.

`uv run pytest tests/api/test_audit_log_stream.py -v` → 4 PASS.

- [ ] **Step 6**: Commit.

```bash
cd /d/download_weights && git add src/dlw/api/audit_stream.py src/dlw/main.py tests/api/test_audit_log_stream.py && git commit -q -m "UI-SP5d M1: GET /audit/log/stream SSE endpoint (reuses search_audit_log; cursor-less live feed)"
```

---

### Task 3: M1 gate

- [ ] `uv run pytest tests/ -q` → baseline (459 prior + 4 new); 0 failures.
- [ ] `npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error` → 0 errors.
- [ ] `npx --yes @apidevtools/swagger-cli validate api/openapi.yaml` → valid.
- [ ] `python tools/lint_invariants.py` → OK.
- [ ] `python tools/lint_no_direct_status_write.py` → OK.

---

# Milestone M2 — Frontend cutover

### Task 4: `useAuditLog` opts in

- [ ] **Step 1**: Replace `frontend/src/composables/useAuditLog.ts` with:

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

- [ ] **Step 2**: Create `frontend/tests/unit/useAuditLogStream.spec.ts`:

```ts
import { describe, expect, test, vi } from 'vitest'
import { ref } from 'vue'

const { captured } = vi.hoisted(() => ({
  captured: [] as Array<{ key: unknown; opts: Record<string, unknown> }>,
}))
vi.mock('@/composables/useLiveResource', () => ({
  useLiveResource: (key: unknown, fetcher: () => unknown,
                   opts: Record<string, unknown>) => {
    captured.push({ key, opts })
    return { __fetcher: fetcher }
  },
}))
vi.mock('@/api/client', () => ({ client: { get: vi.fn() } }))

import { useAuditLog } from '@/composables/useAuditLog'

const mkFilters = () => ({
  action: ref(''),
  actor: ref<number | null>(null),
  from: ref<string | null>(null),
  to: ref<string | null>(null),
})

describe('useAuditLog SSE opt-in (SP5d)', () => {
  test('passes streamUrl + applyEvent to the seam', () => {
    captured.length = 0
    useAuditLog(mkFilters())
    const last = captured[captured.length - 1]
    expect(last?.opts.streamUrl).toBeDefined()
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(10_000)
  })
  test('streamUrl is reactive to filters', () => {
    captured.length = 0
    const f = mkFilters()
    useAuditLog(f)
    const last = captured[captured.length - 1]
    const url0 = (last?.opts.streamUrl as { value: string }).value
    expect(url0).toBe('/api/v1/audit/log/stream')
    f.action.value = 'task.'
    const url1 = (last?.opts.streamUrl as { value: string }).value
    expect(url1).toBe('/api/v1/audit/log/stream?action=task.')
    f.actor.value = 42
    const url2 = (last?.opts.streamUrl as { value: string }).value
    expect(url2).toContain('action=task.')
    expect(url2).toContain('actor_user_id=42')
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useAuditLog(mkFilters())
    const apply = (captured[captured.length - 1]?.opts.applyEvent) as
      (prev: unknown, ev: { data: string }) =>
        { items: unknown[]; next_cursor: string | null }
    const out = apply(undefined, {
      data: '{"items":[{"id":1}],"next_cursor":null}',
    })
    expect(out.items).toEqual([{ id: 1 }])
    expect(out.next_cursor).toBeNull()
  })
})
```

- [ ] **Step 3**: Verify existing tests still pass (view-free proof).

```bash
cd /d/download_weights/frontend
pnpm test:unit -- useAuditLog useAuditLogStream AuditPage sp3Composables
```
Expected: ALL PASS. The SP3 `AuditPage.spec.ts` mocks `useAuditLog` entirely; the SP3 `sp3Composables.spec.ts::useAuditLog builds query from filters` test asserts the fetcher URL only — both stay green.

- [ ] **Step 4**: Full frontend gate.

```bash
cd /d/download_weights/frontend && pnpm test:unit && pnpm typecheck && pnpm lint:fix && pnpm lint && pnpm build
```

- [ ] **Step 5**: Commit.

```bash
cd /d/download_weights && git add frontend/src/composables/useAuditLog.ts frontend/tests/unit/useAuditLogStream.spec.ts && git commit -q -m "UI-SP5d M2: useAuditLog opts in to SSE (view-free; Audit.vue + SP3 spec untouched)"
```

---

# Milestone M3 — Smoke + docs

### Task 5: Headed Playwright smoke + docs

- [ ] **Step 1**: Restart `:8011` with SP5d code (per the SP5/SP5b/SP5c recipe — kill PID, relaunch). Restart Vite with `DLW_API_PROXY=http://localhost:8011`. Mint a fresh tenant JWT to `.run/sp5d-token.txt`.

- [ ] **Step 2**: Direct live check.

```bash
TOK=$(cat .run/sp5d-token.txt)
curl -s -m 1 -H "Authorization: Bearer $TOK" \
  "http://localhost:8011/api/v1/audit/log/stream?max_ticks=1" | head -c 300
```
Expected: `:open\n\ndata: {"items":[...],"next_cursor":...}`.

- [ ] **Step 3**: Create `.run/pw/sp5d-smoke.mjs`:

```js
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'

const TOKEN = readFileSync('.run/sp5d-token.txt', 'utf8').trim()
const VITE = process.env.SP5D_VITE ?? 'http://localhost:5173'
const errors = []
const b = await chromium.launch({ headless: false })
const pg = await b.newPage()
pg.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
pg.on('pageerror', (e) => errors.push(String(e)))

const sseReqs = []
pg.on('request', (r) => {
  if (r.url().includes('/audit/log/stream')) sseReqs.push(r.url())
})

await pg.goto(`${VITE}/login`)
await pg.fill('input', TOKEN)
await pg.click('button[type="submit"]')
await pg.waitForURL('**/')
await pg.goto(`${VITE}/audit`)
await pg.waitForSelector('.audit-row, .empty-state', { timeout: 10_000 })
await pg.screenshot({ path: '.run/pw/sp5d-audit.png' })
await pg.waitForTimeout(4000)
await pg.screenshot({ path: '.run/pw/sp5d-after-stream.png' })

await b.close()
const real = errors.filter((e) =>
  !e.includes('/health/active') && !e.includes('Failed to load resource'))
if (real.length) {
  console.log('SP5d smoke: real errors:\n' + real.join('\n'))
  process.exit(1)
}
if (sseReqs.length === 0) {
  console.log('SP5d smoke: no /audit/log/stream SSE request observed')
  process.exit(1)
}
console.log(`SP5d smoke OK — observed ${sseReqs.length} /audit/log/stream request(s)`)
sseReqs.forEach((u) => console.log('  -> ' + u))
```

- [ ] **Step 4**: Run.

```bash
SP5D_VITE=http://localhost:5173 node .run/pw/sp5d-smoke.mjs
```

Expected: `SP5d smoke OK — observed ≥1 /audit/log/stream request(s)`.

- [ ] **Step 5**: Append to `docs/operator/web-ui.md` (under SP5c section):

```markdown

### UI-SP5d — Audit-log SSE follow-on

Fourth application of the view-free SSE template (after SP5/SP5b/SP5c). The
Audit log page (`useAuditLog`, consumed by `Audit.vue`) now talks SSE via
`GET /api/v1/audit/log/stream` (10 s default tick; `DLW_AUDIT_STREAM_INTERVAL_SECONDS`
overrides; clamped `[0.5, 60.0]`). The page is unchanged; the "Load older"
button keeps using the one-shot `/api/v1/audit/log?cursor=…` endpoint
(stream is live page-1 only, no cursor needed). SP3's existing tests pass
unmodified.

The stream accepts the same filter query params as the REST endpoint
(`action`, `actor_user_id`, `from`, `to`) so the visible filtered view is
what's pushed in real-time. Tenant filtering reuses SP3's `search_audit_log`
service (own-tenant only).
```

- [ ] **Step 6**: Commit.

```bash
cd /d/download_weights && git add docs/operator/web-ui.md && git commit -q -m "UI-SP5d M3: operator docs for the audit-log SSE follow-on"
```

---

## Self-Review

**Spec coverage:** §3 backend endpoint + RBAC reuse + OpenAPI + config + 4 tests → Tasks 1-2 ✓. §4 frontend opt-in + new spec + view-free proof → Task 4 ✓. §5 M1/M2/M3 with gates → Tasks 3, 5 ✓.

**Placeholders:** none.

**Type consistency:**
- Backend reuses `AuditSearchResponse` (SP3) and `search_audit_log` (SP3). ✓
- Frontend `streamUrl: ComputedRef<string>` assignable to `Ref<string>`. ✓
- `applyEvent` returns `AuditSearchResponse`. ✓
- 10 s default + `[0.5, 60.0]` clamp matches `useAuditLog`'s existing `baseIntervalMs: 10_000`. ✓

**View-free proof chain:**
- `Audit.vue` — NOT in any modify list.
- `AuditPage.spec.ts` — NOT modified; mocks `useAuditLog` entirely → unaffected.
- SP3 `sp3Composables.spec.ts::useAuditLog builds query from filters` test — asserts fetcher URL, NOT `opts` shape → unaffected by the additive `streamUrl`+`applyEvent`. ✓
