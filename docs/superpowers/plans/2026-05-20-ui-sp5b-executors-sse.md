# UI-SP5b — Executors SSE Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add 1 additive SSE endpoint `GET /api/v1/executors/stream` + opt `useExecutors` in to the SP5 seam. Validates the view-free architecture against a SECOND consumer.

**Architecture:** Backend mirrors `tasks_stream.py` (hand-rolled `text/event-stream` via `StreamingResponse`; reuses SP3's `list_executors_for_principal` service for tenant filter + `system_admin` bypass). Frontend opts `useExecutors` in via the SP5-added `streamUrl`+`applyEvent` options — signature, return shape, queryKey UNCHANGED. `Executors.vue` and `ExecutorsPage.spec.ts` NOT modified.

**Tech Stack:** Same as SP5. Zero new runtime deps; zero Alembic migration.

---

## Conventions (apply to every task — same as SP5)

- **Branch:** `feat/ui-sp5b-executors-sse` (created off `main` @ `8c77ca1`; spec committed `6dd783a`).
- **Bash cwd persists**. Always `cd /d/download_weights && git …` for git; `cd /d/download_weights/frontend && pnpm …` for frontend.
- **Tenant gate**: reuse `list_executors_for_principal(session, principal, status)` from `src/dlw/services/executors_read.py` — already enforces tenant filtering with `system_admin`/`is_service` bypass.
- **Test hatch**: `?max_ticks=N` (`Query(default=None, ge=1, le=10000)`) — same SP5 rationale (httpx ASGITransport buffers body until generator close).
- **Vue 3.5 `watch` TDZ**: not relevant here — SP5b only adds opt-in options to `useExecutors`, doesn't touch `useLiveResource` internals.

---

## File Structure

**Backend (create):**
- `src/dlw/api/executors_stream.py` — `GET /api/v1/executors/stream` route module.
- `tests/api/test_executors_stream.py` — 4 tests.

**Backend (modify):**
- `api/openapi.yaml` — add `/executors/stream` path block.
- `src/dlw/main.py` — register the new router (2-line lazy import + include_router).
- `src/dlw/config.py` — add `executors_stream_interval_seconds` setting.

**Frontend (modify):**
- `frontend/src/composables/useExecutors.ts` — opt in via `streamUrl`+`applyEvent`.

**Frontend (create):**
- `frontend/tests/unit/useExecutorsStream.spec.ts` — 1 targeted test proving the opt-in (additive; doesn't touch the SP3 spec).

**Docs (modify):** `docs/operator/web-ui.md`.

---

# Milestone M1 — Backend

### Task 1: OpenAPI + config setting

**Files:** `api/openapi.yaml`, `src/dlw/config.py`.

- [ ] **Step 1**: In `api/openapi.yaml`, find the `/executors:` GET block (line ~666) and the next sibling path `/executors/register:` (line ~684). Insert the new `/executors/stream:` block between them (after the GET block's closing brace + blank line, before `/executors/register:`):

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

- [ ] **Step 2**: In `src/dlw/config.py`, add 1 setting next to `task_stream_interval_seconds`:

```python
    executors_stream_interval_seconds: float = Field(default=5.0)
```

- [ ] **Step 3**: Validate.

```bash
cd /d/download_weights
npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error  # exit 0
npx --yes @apidevtools/swagger-cli validate api/openapi.yaml  # valid
```

- [ ] **Step 4**: Commit.

```bash
cd /d/download_weights && git add api/openapi.yaml src/dlw/config.py && git commit -q -m "UI-SP5b M1: openapi /executors/stream path + executors_stream_interval_seconds setting"
```

---

### Task 2: SSE route + 4 tests

**Files:** create `src/dlw/api/executors_stream.py`, `tests/api/test_executors_stream.py`. Modify `src/dlw/main.py` (register router).

- [ ] **Step 1**: Write the failing test. Create `tests/api/test_executors_stream.py`:

```python
"""Tests for GET /api/v1/executors/stream (UI-SP5b SSE)."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid

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
    monkeypatch.setenv("DLW_EXECUTORS_STREAM_INTERVAL_SECONDS", TICK)
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


async def _seed(engine):
    from sqlalchemy import text
    from dlw.db.models.executor import Executor
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(text("DELETE FROM executors"))
        s.add(Executor(id="t1-w1", host_id="host-a", cert_fingerprint="fp1",
                       status="healthy", epoch=1, health_score=95,
                       tenant_id=1, nic_speed_gbps=10,
                       disk_free_gb=500, disk_total_gb=1000,
                       last_heartbeat_at=dt.datetime(
                           2026, 5, 20, 12, 0, tzinfo=dt.UTC)))
        s.add(Executor(id="t2-w1", host_id="host-b", cert_fingerprint="fp2",
                       status="degraded", epoch=2, health_score=60,
                       tenant_id=2))
        s.add(Executor(id="shared-1", host_id="host-c", cert_fingerprint="fp3",
                       status="healthy", epoch=1, health_score=100,
                       tenant_id=None))
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
async def test_executors_stream_unauthenticated_401(
    client: AsyncClient,
) -> None:
    async with client.stream(
        "GET", "/api/v1/executors/stream",
    ) as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_executors_stream_tenant_filter(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed(engine)
    received = await _collect(
        client, "/api/v1/executors/stream?max_ticks=1", auth,
        count=1, timeout=3.0)
    body = json.loads(received[0])
    ids = {it["id"] for it in body["items"]}
    assert "t1-w1" in ids
    assert "shared-1" in ids
    assert "t2-w1" not in ids


@pytest.mark.slow
async def test_executors_stream_multi_snapshot(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed(engine)
    received = await _collect(
        client, "/api/v1/executors/stream?max_ticks=2", auth,
        count=2, timeout=3.0)
    assert len(received) >= 2
    for raw in received[:2]:
        body = json.loads(raw)
        assert "items" in body
        assert isinstance(body["items"], list)


@pytest.mark.slow
async def test_executors_stream_status_filter(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed(engine)
    received = await _collect(
        client, "/api/v1/executors/stream?status=healthy&max_ticks=1",
        auth, count=1, timeout=3.0)
    body = json.loads(received[0])
    assert all(it["status"] == "healthy" for it in body["items"])
    assert {it["id"] for it in body["items"]} >= {"t1-w1", "shared-1"}
```

- [ ] **Step 2**: Run to verify it fails.

`uv run pytest tests/api/test_executors_stream.py -v` → 4 FAIL (route 404).

- [ ] **Step 3**: Create the route. Create `src/dlw/api/executors_stream.py`:

```python
"""GET /api/v1/executors/stream — SSE executors-list stream (UI-SP5b).

NOT in src/dlw/api/executors.py because that file is mTLS-only per
tools/lint_invariants.py:check_no_bearer_on_executor_routes. Uses require_perm.
Same hand-rolled text/event-stream idiom as tasks_stream.py.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.session import get_engine
from dlw.schemas.executor_read import ExecutorListResponse, ExecutorRead
from dlw.services.executors_read import list_executors_for_principal

router = APIRouter(prefix="/api/v1/executors", tags=["executors"])

_StatusLit = Literal["joining", "healthy", "degraded", "suspect", "faulty"]

_KEEPALIVE_EVERY_TICKS = 6  # at 5 Hz default, every ~30 s


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "executors_stream_interval_seconds", 5.0))
    return max(0.5, min(60.0, raw))


@router.get("/stream")
async def stream_executors(
    request: Request,
    status: _StatusLit | None = Query(default=None),
    max_ticks: int | None = Query(default=None, ge=1, le=10000),
    principal: Principal = Depends(require_perm("/api/v1/executors*", "GET")),
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
                    rows = await list_executors_for_principal(
                        s, principal, status)
                payload = ExecutorListResponse(
                    items=[ExecutorRead.model_validate(r) for r in rows])
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

- [ ] **Step 4**: Register the router in `src/dlw/main.py`. Append immediately after the SP5 `tasks_stream_router` line (inside `create_app()`, same 4-space indent):

```python
    from dlw.api.executors_stream import router as executors_stream_router
    app.include_router(executors_stream_router)
```

- [ ] **Step 5**: Run → PASS.

`uv run pytest tests/api/test_executors_stream.py -v` → 4 PASS.

- [ ] **Step 6**: Commit.

```bash
cd /d/download_weights && git add src/dlw/api/executors_stream.py src/dlw/main.py tests/api/test_executors_stream.py && git commit -q -m "UI-SP5b M1: GET /executors/stream SSE endpoint (reuses SP3 tenant filter)"
```

---

### Task 3: M1 gate

- [ ] `uv run pytest tests/ -q` → baseline (record beforehand via `uv run pytest tests/ --collect-only -q | tail -1`) + 4 new; 0 failures.
- [ ] `npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error` → 0 errors.
- [ ] `npx --yes @apidevtools/swagger-cli validate api/openapi.yaml` → valid.
- [ ] `python tools/lint_invariants.py` → OK.
- [ ] `python tools/lint_no_direct_status_write.py` → OK.

---

# Milestone M2 — Frontend cutover

### Task 4: `useExecutors` opts in to SSE

**Files:** `frontend/src/composables/useExecutors.ts` (replace), `frontend/tests/unit/useExecutorsStream.spec.ts` (create).

- [ ] **Step 1**: Replace `frontend/src/composables/useExecutors.ts` with:

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

- [ ] **Step 2**: Create `frontend/tests/unit/useExecutorsStream.spec.ts`:

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

import { useExecutors } from '@/composables/useExecutors'

describe('useExecutors SSE opt-in (SP5b)', () => {
  test('passes streamUrl + applyEvent to the seam', () => {
    captured.length = 0
    const status = ref<string | null>(null)
    useExecutors(status)
    const last = captured[captured.length - 1]
    expect(last?.opts.streamUrl).toBeDefined()
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(5_000)
  })
  test('streamUrl is reactive to the status filter', () => {
    captured.length = 0
    const status = ref<string | null>('healthy')
    useExecutors(status)
    const last = captured[captured.length - 1]
    // streamUrl is a ComputedRef<string>; read via .value (or toValue).
    const url = (last?.opts.streamUrl as { value: string }).value
    expect(url).toBe('/api/v1/executors/stream?status=healthy')
    status.value = null
    const url2 = (last?.opts.streamUrl as { value: string }).value
    expect(url2).toBe('/api/v1/executors/stream')
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useExecutors(ref<string | null>(null))
    const apply = (captured[captured.length - 1]?.opts.applyEvent) as
      (prev: unknown, ev: { data: string }) => { items: unknown[] }
    const out = apply(undefined, { data: '{"items":[{"id":"x"}]}' })
    expect(out.items).toEqual([{ id: 'x' }])
  })
})
```

- [ ] **Step 3**: Verify all existing tests still pass (view-free proof).

```bash
cd /d/download_weights/frontend
pnpm test:unit -- useExecutors useExecutorsStream ExecutorsPage sp3Composables
```
Expected: ALL PASS — `ExecutorsPage.spec.ts` and the SP3 `sp3Composables.spec.ts::useExecutors wires key + status query + interval` test stay green unchanged.

- [ ] **Step 4**: Full frontend gate.

```bash
cd /d/download_weights/frontend && pnpm test:unit && pnpm typecheck && pnpm lint:fix && pnpm lint && pnpm build
```
All green.

- [ ] **Step 5**: Commit.

```bash
cd /d/download_weights && git add frontend/src/composables/useExecutors.ts frontend/tests/unit/useExecutorsStream.spec.ts && git commit -q -m "UI-SP5b M2: useExecutors opts in to SSE (view-free; SP3 spec untouched)"
```

---

# Milestone M3 — Smoke + docs

### Task 5: Headed Playwright smoke + docs

**Files:** `docs/operator/web-ui.md` (modify).

- [ ] **Step 1**: Restart the ephemeral `:8011` controller with current SP5b code (per the SP5 recipe — kill existing PID, relaunch via the SP5 incantation). Restart Vite with `DLW_API_PROXY=http://localhost:8011`. Mint a fresh tenant JWT to `.run/sp5b-token.txt`.

- [ ] **Step 2**: Direct live check.

```bash
TOK=$(cat .run/sp5b-token.txt)
curl -s -m 1 -H "Authorization: Bearer $TOK" "http://localhost:8011/api/v1/executors/stream?max_ticks=1" | head -c 300
```
Expected: `:open\n\ndata: {"items":[...]}` with real executor JSON.

- [ ] **Step 3**: Create `.run/pw/sp5b-smoke.mjs`:

```js
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'

const TOKEN = readFileSync('.run/sp5b-token.txt', 'utf8').trim()
const VITE = process.env.SP5B_VITE ?? 'http://localhost:5173'
const errors = []
const b = await chromium.launch({ headless: false })
const pg = await b.newPage()
pg.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
pg.on('pageerror', (e) => errors.push(String(e)))

const sseReqs = []
pg.on('request', (r) => {
  if (r.url().includes('/executors/stream')) sseReqs.push(r.url())
})

await pg.goto(`${VITE}/login`)
await pg.fill('input', TOKEN)
await pg.click('button[type="submit"]')
await pg.waitForURL('**/')
await pg.goto(`${VITE}/executors`)
await pg.waitForSelector('.host-group, .empty-state', { timeout: 10_000 })
await pg.screenshot({ path: '.run/pw/sp5b-executors.png' })
await pg.waitForTimeout(4000)
await pg.screenshot({ path: '.run/pw/sp5b-after-stream.png' })

await b.close()
const real = errors.filter((e) =>
  !e.includes('/health/active') && !e.includes('Failed to load resource'))
if (real.length) {
  console.log('SP5b smoke: real errors:\n' + real.join('\n'))
  process.exit(1)
}
if (sseReqs.length === 0) {
  console.log('SP5b smoke: no SSE request observed')
  process.exit(1)
}
console.log(`SP5b smoke OK — observed ${sseReqs.length} /executors/stream request(s)`)
sseReqs.forEach((u) => console.log('  -> ' + u))
```

- [ ] **Step 4**: Run.

```bash
SP5B_VITE=http://localhost:5173 node .run/pw/sp5b-smoke.mjs
```
Expected: `SP5b smoke OK — observed >=1 /executors/stream request(s)`.

- [ ] **Step 5**: Append to `docs/operator/web-ui.md` (under the existing SP5 section):

```markdown

### UI-SP5b — Executors SSE follow-on

Same architecture, second consumer. `useExecutors` (the `/executors` page)
now talks SSE via `GET /api/v1/executors/stream` (5 s default tick;
`DLW_EXECUTORS_STREAM_INTERVAL_SECONDS` overrides; clamped `[0.5, 60.0]`).
The page (`Executors.vue`) is unchanged; only the composable opts in. SP3's
existing tests (`ExecutorsPage.spec.ts` + the `sp3Composables.spec.ts`
useExecutors wiring test) pass without modification — the view-free property
holds against a second consumer.

Tenant filtering reuses SP3's `list_executors_for_principal` (own-tenant +
shared-infra; `system_admin`/service-token bypass).
```

- [ ] **Step 6**: Commit.

```bash
cd /d/download_weights && git add docs/operator/web-ui.md && git commit -q -m "UI-SP5b M3: operator docs for the executors SSE follow-on"
```

---

## Self-Review

**1. Spec coverage:**
- §3 backend endpoint + RBAC reuse + OpenAPI + config + 4 tests → Tasks 1-2. ✓
- §4 frontend opt-in + new spec + view-free proof of existing tests → Task 4. ✓
- §5 milestones M1/M2/M3 with gates → Tasks 3,5. ✓
- §6 risks (httpx ASGITransport buffering — mitigated by `?max_ticks` hatch; SP3 spec stability — verified by re-running it). ✓

**2. Placeholder scan:** No "TBD/similar to SP5". Every code step is complete copy-paste-grade.

**3. Type consistency:**
- `ExecutorListResponse` shape identical across backend `executors_stream.py`, the existing SP3 `executors_read.py`, frontend `api/types.ts` (unchanged), and `useExecutors`'s `applyEvent` return type. ✓
- `streamUrl: ComputedRef<string>` is structurally assignable to `LiveOptions.streamUrl: string | Ref<string>` (vue 3.5 `ComputedRef<T>` extends `Ref<T>`). ✓
- `_StatusLit` Literal type same in `executors_read.py` (SP3) and `executors_stream.py` (SP5b). ✓
- `?max_ticks=N` hatch identical in shape across the 2 SSE endpoints. ✓
- 5 s default + clamp `[0.5, 60.0]` matches the existing `useExecutors` `baseIntervalMs: 5_000`. ✓

**4. View-free proof chain:**
- `frontend/src/pages/Executors.vue` — NOT in any modify list.
- `frontend/tests/unit/ExecutorsPage.spec.ts` — NOT in any modify list; Task 4 Step 3 explicitly re-runs it to prove it stays green.
- `frontend/tests/unit/sp3Composables.spec.ts` — NOT in any modify list; Task 4 Step 3 re-runs the `useExecutors wires key + status query + interval` test in it; asserts on `last.key` and `last.opts.baseIntervalMs`, NOT on full options shape, so the additive `streamUrl`+`applyEvent` don't break it. ✓
