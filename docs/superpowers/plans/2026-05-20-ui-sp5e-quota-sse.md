# UI-SP5e — Quota Snapshot SSE Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** Add `GET /api/v1/quota/current/stream` + opt `useQuota` in to the SP5 seam. 5th application of the view-free SSE template.

**Architecture:** Backend mirrors `audit_stream.py`; extracts a shared `get_quota_snapshot(session, tenant_id)` service used by BOTH the existing one-shot `/quota/current` and the new SSE endpoint. Frontend `useQuota` opts in with a literal `streamUrl` (no filters). `QuotaPage.vue` / `QuotaCard.vue` / `Settings.vue` UNCHANGED; existing `test_quota_api.py` MUST keep passing (regression proof of the service extraction).

---

## Conventions (apply to every task — same as SP5d)

- **Branch:** `feat/ui-sp5e-quota-sse` (off `main` @ `9df3de8`, already created).
- **Bash cwd persists**. Always `cd /d/download_weights && git …` for git; `cd /d/download_weights/frontend && pnpm …` for frontend.
- **Tenant gate**: the new service reads `Tenant.get(tenant_id)` + `QuotaSnapshot.get(tenant_id)` where `tenant_id = principal.tenant_id` — same as the existing endpoint. There is NO cross-tenant query path; isolation is enforced by the principal injection.
- **`?max_ticks=N`**: same SP5/SP5b/SP5c/SP5d testability hatch.
- **Route placement**: SP5e's new path `/api/v1/quota/current/stream` is sibling of static `/quota/current` — no parameterized route under the prefix. Plan still includes the new router BEFORE the existing quota router for defensive consistency (cheap, future-proof). See `main.py` precedent block from SP5c/SP5d.
- **Service extraction safety**: the refactor moves 7 lines from `quota.py:current` into `services/quota_read.py::get_quota_snapshot`. Semantically identical. `tests/api/test_quota_api.py` is the regression proof — it MUST pass unchanged after the refactor.

---

## File Structure

**Backend (create):**
- `src/dlw/services/quota_read.py` — `get_quota_snapshot(session, tenant_id) -> dict | None`.
- `src/dlw/api/quota_stream.py` — `GET /api/v1/quota/current/stream` route.
- `tests/api/test_quota_stream.py` — 3 tests.

**Backend (modify):**
- `api/openapi.yaml` — add `/quota/current/stream` path block at line 1318 (right after the `/quota/current` block closes, before `/quota/usage`).
- `src/dlw/api/quota.py` — replace inline query with `get_quota_snapshot` call (no behavior change).
- `src/dlw/main.py` — register `quota_stream_router` BEFORE `quota_router` (defensive consistency with SP5c/SP5d lesson).
- `src/dlw/config.py` — add `quota_stream_interval_seconds: float = Field(default=15.0)` setting.

**Frontend (modify):**
- `frontend/src/composables/useQuota.ts` — add `streamUrl` + `applyEvent` to opts.

**Frontend (create):**
- `frontend/tests/unit/useQuotaStream.spec.ts` — new spec (mirror SP5c's `useTaskListStream.spec.ts`).

**Docs (modify):** `docs/operator/web-ui.md` (append SP5e section).

---

# Milestone M1 — Backend

### Task 1: OpenAPI + config

**Files:**
- Modify: `api/openapi.yaml:1318` (after `/quota/current` block, before `/quota/usage`)
- Modify: `src/dlw/config.py:49` (after `audit_stream_interval_seconds`)

- [ ] **Step 1**: In `api/openapi.yaml`, find the existing `/quota/current` block (line 1300-1318). Insert `/quota/current/stream:` block immediately after, before `/quota/usage:`:

```yaml

  /quota/current/stream:
    get:
      tags: [quota]
      summary: Live quota snapshot SSE stream (UI-SP5e)
      operationId: streamQuotaCurrent
      description: Live SSE feed of the caller's tenant quota snapshot. Requires bearer system-JWT.
      responses:
        '200':
          description: SSE stream of QuotaCurrent snapshots
          content:
            text/event-stream:
              schema:
                type: string
                description: |
                  Each event is `data: <QuotaCurrent JSON>\n\n`
                  ({tenant_id, bytes_used_month, bytes_quota_month,
                   storage_gb_used, storage_gb_quota, concurrent_tasks,
                   concurrent_quota}). Stream terminates only on client
                  disconnect or shutdown.
        '401': {$ref: '#/components/responses/Unauthenticated'}
        '403':
          description: Insufficient role (RBAC denied)
          content:
            application/json:
              schema: {$ref: '#/components/schemas/RbacDenied'}
        '404':
          description: Tenant not found
```

- [ ] **Step 2**: In `src/dlw/config.py`, after the `audit_stream_interval_seconds` field (line 49), add:

```python
    # UI-SP5e — SSE tick rate for /quota/current/stream (clamped at runtime).
    quota_stream_interval_seconds: float = Field(default=15.0)
```

- [ ] **Step 3**: Lint sanity for openapi:

```bash
cd /d/download_weights && uv run swagger-cli validate api/openapi.yaml 2>&1 | tail -5
```

Expected: `api/openapi.yaml is valid`.

- [ ] **Step 4**: Commit.

```bash
git add api/openapi.yaml src/dlw/config.py
git commit -q -m "UI-SP5e M1: openapi /quota/current/stream path + quota_stream_interval_seconds setting"
```

---

### Task 2: Service extraction

**Files:**
- Create: `src/dlw/services/quota_read.py`
- Modify: `src/dlw/api/quota.py`

- [ ] **Step 1**: Create `src/dlw/services/quota_read.py`:

```python
"""Shared quota-snapshot read used by /quota/current (one-shot) and
/quota/current/stream (SSE) — UI-SP5e."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.tenant import Tenant
from dlw.db.models.usage import QuotaSnapshot


async def get_quota_snapshot(
    session: AsyncSession, tenant_id: int,
) -> dict | None:
    """Read tenant + quota_snapshots row → flat dict; None when tenant gone."""
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

- [ ] **Step 2**: Modify `src/dlw/api/quota.py` — replace inline tenant+snapshot query with `get_quota_snapshot` call. Full new file:

```python
"""GET /api/v1/quota/current (Phase 3 SP1; security §7.5, no forecast)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.db.session import get_engine
from dlw.services.quota_read import get_quota_snapshot

router = APIRouter(prefix="/api/v1/quota", tags=["quota"])


async def _session():
    f = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with f() as s:
        yield s


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

- [ ] **Step 3**: Run the existing quota test as regression proof:

```bash
cd /d/download_weights && uv run pytest tests/api/test_quota_api.py -q 2>&1 | tail -5
```

Expected: all tests pass unchanged.

- [ ] **Step 4**: Commit.

```bash
git add src/dlw/services/quota_read.py src/dlw/api/quota.py
git commit -q -m "UI-SP5e M1: extract get_quota_snapshot service (shared by /current and /current/stream)"
```

---

### Task 3: SSE endpoint

**Files:**
- Create: `src/dlw/api/quota_stream.py`
- Modify: `src/dlw/main.py` (register router BEFORE quota_router)

- [ ] **Step 1**: Create `src/dlw/api/quota_stream.py`:

```python
"""GET /api/v1/quota/current/stream — SSE quota-snapshot live stream (UI-SP5e).

Hand-rolled text/event-stream; reuses the get_quota_snapshot service
shared with the one-shot /api/v1/quota/current endpoint.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import json
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.session import get_engine
from dlw.services.quota_read import get_quota_snapshot

router = APIRouter(prefix="/api/v1/quota", tags=["quota"])

_KEEPALIVE_EVERY_TICKS = 3  # vestigial (cf. SP5+); kept for parity


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "quota_stream_interval_seconds", 15.0))
    return max(0.5, min(60.0, raw))


@router.get("/current/stream")
async def stream_quota_current(
    request: Request,
    max_ticks: int | None = Query(default=None, ge=1, le=10000),
    principal: Principal = Depends(require_perm("/api/v1/quota*", "GET")),
) -> StreamingResponse:
    session_maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    interval = _clamped_interval()

    # Pre-stream tenant existence check — raise 404 BEFORE :open is sent,
    # mirroring the one-shot /current endpoint's contract. After this,
    # the generator only emits snapshots (a tenant deletion mid-stream is
    # not a realistic case and would just end the stream).
    async with session_maker() as s:
        initial = await get_quota_snapshot(s, principal.tenant_id)
    if initial is None:
        raise HTTPException(404, detail="tenant not found")

    async def _body() -> AsyncIterator[bytes]:
        yield b":open\n\n"
        # First tick = the pre-stream snapshot (already fetched; cheap).
        yield (f"data: {json.dumps(initial)}\n\n").encode("utf-8")
        ticks_since_data = 0
        tick_count = 1
        if max_ticks is not None and tick_count >= max_ticks:
            return
        try:
            while True:
                slept = 0.0
                while slept < interval:
                    if await request.is_disconnected():
                        return
                    chunk = min(0.05, interval - slept)
                    await asyncio.sleep(chunk)
                    slept += chunk
                if await request.is_disconnected():
                    return
                async with session_maker() as s:
                    snap = await get_quota_snapshot(
                        s, principal.tenant_id)
                if snap is None:
                    return  # tenant gone mid-stream — close cleanly
                yield (f"data: {json.dumps(snap)}\n\n").encode("utf-8")
                ticks_since_data = 0
                tick_count += 1
                if max_ticks is not None and tick_count >= max_ticks:
                    return
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

- [ ] **Step 2**: Modify `src/dlw/main.py` — register `quota_stream_router` BEFORE the existing `quota_router`. Find the existing `from dlw.api.quota import router as quota_router` / `app.include_router(quota_router)` pair (around line 308-309) and insert the new pair before it:

```python
    # SP5e: quota stream router registered BEFORE quota_router for
    # defensive consistency with SP5c/SP5d lesson (static paths win on
    # include order; harmless if no parameterized sibling exists today).
    from dlw.api.quota_stream import router as quota_stream_router
    app.include_router(quota_stream_router)
    from dlw.api.quota import router as quota_router
    app.include_router(quota_router)
```

- [ ] **Step 3**: Sanity: import smoke.

```bash
cd /d/download_weights && uv run python -c "from dlw.api.quota_stream import router; print(router.routes)"
```

Expected: one route with path `/api/v1/quota/current/stream`.

- [ ] **Step 4**: Commit.

```bash
git add src/dlw/api/quota_stream.py src/dlw/main.py
git commit -q -m "UI-SP5e M1: GET /quota/current/stream SSE endpoint (reuses get_quota_snapshot)"
```

---

### Task 4: Backend tests

**Files:**
- Create: `tests/api/test_quota_stream.py`

- [ ] **Step 1**: Create `tests/api/test_quota_stream.py`:

```python
"""Tests for GET /api/v1/quota/current/stream (UI-SP5e SSE)."""
from __future__ import annotations

import asyncio
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
    from dlw.db.models.usage import QuotaSnapshot
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1",
                     quota_bytes_month=1000, quota_concurrent=5,
                     quota_storage_gb=10))
        s.add(Tenant(id=2, slug="t2", display_name="T2",
                     quota_bytes_month=2000, quota_concurrent=7,
                     quota_storage_gb=20))
        await s.flush()
        s.add_all([
            Project(id=1, tenant_id=1, name="d1"),
            Project(id=2, tenant_id=2, name="d2"),
            User(id=1, tenant_id=1, oidc_subject="u1",
                 email="u1@t", role="tenant_admin"),
            User(id=2, tenant_id=2, oidc_subject="u2",
                 email="u2@t", role="tenant_admin"),
            StorageBackend(id=1, tenant_id=1, name="s1",
                           backend_type="s3", config_encrypted=b""),
            StorageBackend(id=2, tenant_id=2, name="s2",
                           backend_type="s3", config_encrypted=b""),
            QuotaSnapshot(tenant_id=1, bytes_used_month=111,
                          storage_gb_used=1, concurrent_tasks=1),
            QuotaSnapshot(tenant_id=2, bytes_used_month=222,
                          storage_gb_used=2, concurrent_tasks=2),
        ])
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_QUOTA_STREAM_INTERVAL_SECONDS", TICK)
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
async def test_quota_stream_unauthenticated_401(client: AsyncClient) -> None:
    async with client.stream("GET", "/api/v1/quota/current/stream") as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_quota_stream_tenant_isolation(
    client: AsyncClient, auth,
) -> None:
    received = await _collect(
        client, "/api/v1/quota/current/stream?max_ticks=1", auth,
        count=1, timeout=3.0)
    body = json.loads(received[0])
    assert body["tenant_id"] == 1
    assert body["bytes_used_month"] == 111
    assert body["bytes_quota_month"] == 1000
    assert body["storage_gb_used"] == 1
    assert body["storage_gb_quota"] == 10
    assert body["concurrent_tasks"] == 1
    assert body["concurrent_quota"] == 5


@pytest.mark.slow
async def test_quota_stream_multi_snapshot(
    client: AsyncClient, auth,
) -> None:
    received = await _collect(
        client, "/api/v1/quota/current/stream?max_ticks=2", auth,
        count=2, timeout=3.0)
    assert len(received) >= 2
    expected_keys = {
        "tenant_id", "bytes_used_month", "bytes_quota_month",
        "storage_gb_used", "storage_gb_quota",
        "concurrent_tasks", "concurrent_quota",
    }
    for raw in received[:2]:
        body = json.loads(raw)
        assert set(body.keys()) == expected_keys
```

- [ ] **Step 2**: Run the new tests:

```bash
cd /d/download_weights && uv run pytest tests/api/test_quota_stream.py -q 2>&1 | tail -10
```

Expected: 3 passed.

- [ ] **Step 3**: Commit.

```bash
git add tests/api/test_quota_stream.py
git commit -q -m "UI-SP5e M1: backend SSE endpoint tests (unauth-401, tenant-isolation, multi-snapshot)"
```

---

### M1 Full backend gate

- [ ] **Step 1**: Run full backend pytest:

```bash
cd /d/download_weights && uv run pytest -q 2>&1 | tail -5
```

Expected: all SP1-SP5d tests + new SP5e tests pass. The 2 known Windows-local `test_failover_drill.py` flakes may appear; ignore (CI is the arbiter).

---

# Milestone M2 — Frontend cutover

### Task 5: `useQuota` opts in to SSE seam

**Files:**
- Modify: `frontend/src/composables/useQuota.ts`
- Create: `frontend/tests/unit/useQuotaStream.spec.ts`

- [ ] **Step 1**: Modify `frontend/src/composables/useQuota.ts` (full new file):

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

- [ ] **Step 2**: Create `frontend/tests/unit/useQuotaStream.spec.ts`:

```ts
import { describe, expect, test, vi } from 'vitest'

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

import { useQuota } from '@/composables/useQuota'

describe('useQuota SSE opt-in (SP5e)', () => {
  test('passes streamUrl + applyEvent to the seam', () => {
    captured.length = 0
    useQuota()
    const last = captured[captured.length - 1]
    expect(last?.opts.streamUrl).toBe('/api/v1/quota/current/stream')
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(30_000)
    expect(last?.opts.staleTime).toBe(30_000)
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useQuota()
    const apply = (captured[captured.length - 1]?.opts.applyEvent) as
      (prev: unknown, ev: { data: string }) => Record<string, unknown>
    const out = apply(undefined, {
      data: '{"tenant_id":1,"bytes_used_month":42,"bytes_quota_month":1000}',
    })
    expect(out.tenant_id).toBe(1)
    expect(out.bytes_used_month).toBe(42)
  })
  test('key stays ["quota"] (no filter axes)', () => {
    captured.length = 0
    useQuota()
    const last = captured[captured.length - 1]
    expect(last?.key).toEqual(['quota'])
  })
})
```

- [ ] **Step 3**: Run full frontend gate:

```bash
cd /d/download_weights/frontend && pnpm lint && pnpm exec vue-tsc --noEmit && pnpm vitest run 2>&1 | tail -10
```

Expected: lint clean, typecheck clean, all tests pass including the 3 new ones.

- [ ] **Step 4**: Build proof:

```bash
cd /d/download_weights/frontend && pnpm build 2>&1 | tail -5
```

Expected: built successfully.

- [ ] **Step 5**: Commit.

```bash
git add frontend/src/composables/useQuota.ts frontend/tests/unit/useQuotaStream.spec.ts
git commit -q -m "UI-SP5e M2: useQuota opts in to SSE (view-free; QuotaPage/Card/Settings untouched)"
```

---

# Milestone M3 — Smoke + docs

### Task 6: headed Playwright smoke + operator docs

**Files:**
- Create: `.run/pw/sp5e-smoke.mjs`
- Modify: `docs/operator/web-ui.md` (append SP5e section)

- [ ] **Step 1**: Restart `:8011` ephemeral controller with SP5e code; restart Vite proxying to it (kill stale Vite process first via `netstat -ano | grep :5173`).

- [ ] **Step 2**: Smoke the endpoint with curl:

```bash
cd /d/download_weights && JWT=$(cat .run/sp5d-token.txt 2>/dev/null || uv run python -c "from dlw.auth.principal import issue_system_jwt; print(issue_system_jwt(secret='dev-system-jwt-change-me', user_id=1, tenant_id=1, role='tenant_admin', project_ids=[]))" 2>/dev/null | tail -1)
curl -s -H "Authorization: Bearer $JWT" "http://127.0.0.1:8011/api/v1/quota/current/stream?max_ticks=1" | head -c 300
```

Expected: `:open\n\ndata: {"tenant_id":1,…}\n\n`.

- [ ] **Step 3**: Save token + create `.run/pw/sp5e-smoke.mjs` (mirror sp5d-smoke.mjs):

```js
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'

const TOKEN = readFileSync('.run/sp5d-token.txt', 'utf8').trim()
const VITE = process.env.SP5E_VITE ?? 'http://localhost:5173'
const errors = []
const b = await chromium.launch({ headless: false })
const pg = await b.newPage()
pg.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
pg.on('pageerror', (e) => errors.push(String(e)))

const sseReqs = []
pg.on('request', (r) => {
  if (/\/api\/v1\/quota\/current\/stream(\?|$)/.test(r.url())) {
    sseReqs.push(r.url())
  }
})

await pg.goto(`${VITE}/login`)
await pg.fill('input', TOKEN)
await pg.click('button[type="submit"]')
await pg.waitForURL('**/')
await pg.goto(`${VITE}/quota`)
await pg.waitForSelector('.quota-card, .quota-empty, table', { timeout: 10_000 })
await pg.screenshot({ path: '.run/pw/sp5e-quota.png' })
await pg.waitForTimeout(4000)
await pg.screenshot({ path: '.run/pw/sp5e-after-stream.png' })

await b.close()
const real = errors.filter((e) =>
  !e.includes('/health/active') && !e.includes('Failed to load resource'))
if (real.length) {
  console.log('SP5e smoke: real errors:\n' + real.join('\n'))
  process.exit(1)
}
if (sseReqs.length === 0) {
  console.log('SP5e smoke: no /quota/current/stream SSE request observed')
  process.exit(1)
}
console.log(`SP5e smoke OK — observed ${sseReqs.length} /quota/current/stream request(s)`)
sseReqs.forEach((u) => console.log('  -> ' + u))
```

- [ ] **Step 4**: Run the smoke:

```bash
cd /d/download_weights && node .run/pw/sp5e-smoke.mjs 2>&1 | tail -5
```

Expected: `SP5e smoke OK — observed N /quota/current/stream request(s)`.

- [ ] **Step 5**: Append SP5e section to `docs/operator/web-ui.md` after the SP5d section (end of file).

```markdown

### UI-SP5e — Quota snapshot SSE follow-on

Fifth application of the view-free SSE template. The Quota page
(`useQuota`, consumed by `QuotaPage.vue`, the `QuotaCard` infra
component, and the Settings page) now talks SSE via
`GET /api/v1/quota/current/stream` (15 s default tick;
`DLW_QUOTA_STREAM_INTERVAL_SECONDS` overrides; clamped `[0.5, 60.0]`).
The view-side consumers are unchanged.

The endpoint and the existing one-shot `GET /api/v1/quota/current` share
a single service function `get_quota_snapshot(session, tenant_id)` in
`src/dlw/services/quota_read.py` (extracted as part of SP5e — the prior
in-handler query has been replaced by a call to the service; the
existing `tests/api/test_quota_api.py` is the regression proof of the
refactor).

The stream emits the **first snapshot immediately** (using the same
pre-stream tenant existence check that issues 404 when the tenant is
gone), so the UI gets data on `:open`+1 instead of waiting the full 15 s
tick. Subsequent snapshots tick at `quota_stream_interval_seconds`.
Same routing precaution as SP5c/SP5d: `quota_stream_router` is
registered BEFORE `quota_router` in `src/dlw/main.py`.
```

- [ ] **Step 6**: Commit.

```bash
git add .run/pw/sp5e-smoke.mjs docs/operator/web-ui.md
git commit -q -m "UI-SP5e M3: operator docs — quota SSE follow-on + headed smoke"
```

---

# Final cycle (controller-driven, not part of the per-task plan)

After all 6 tasks complete and both gates are green:

1. Dispatch 1 opus reviewer for whole-impl review (HIGH/MEDIUM/LOW; ≤600 words).
2. Fix any HIGH; record MEDIUM/LOW in PR body.
3. `git push -u origin feat/ui-sp5e-quota-sse`.
4. `gh pr create` against `main` with the standard summary template.
5. Background poller waits for CI all-green.
6. `gh pr merge <N> --squash --delete-branch`, then `git checkout main && git pull --ff-only`.
7. Update `reference_l17728_modelpull.md` (record SP5e merge, bump `main` commit).
8. Update `feedback_subagent_driven_dev.md` with any new learning points (SP5e specifically: service-extraction-as-DRY-inflection — first SP5* application that adds a service refactor; useSystemHealth-pivot learning — not every "polling composable" fits the SSE template, the auth/scope shape matters).

---

## Self-Review

- **Spec coverage**: every section of the spec maps to a task. ✓
- **Placeholder scan**: none.
- **Type consistency**: `get_quota_snapshot` signature consistent across service file, quota.py call, quota_stream.py call. `QuotaCurrent` type unchanged on the frontend. ✓
- **Naming**: env var `DLW_QUOTA_STREAM_INTERVAL_SECONDS`, setting `quota_stream_interval_seconds`, function `_clamped_interval`, helper `get_quota_snapshot` — all consistent with SP5d pattern.
- **Test coverage**: 3 backend tests + 3 frontend tests + 1 smoke = mirror SP5b's coverage (the simplest sibling, no filters).
- **Service-extraction safety**: `tests/api/test_quota_api.py` is the regression proof — explicitly called out in M1 Task 2.
- **Route precedence**: not needed (no parameterized siblings) but applied for SP5d-consistency.
