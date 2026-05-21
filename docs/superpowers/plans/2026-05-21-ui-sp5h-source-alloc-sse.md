# UI-SP5h — Source-Allocation SSE Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** Add `GET /api/v1/tasks/{task_id}/source-allocation/stream` + opt `useSourceAllocation` in to the SP5 seam. 8th application of the view-free SSE template; 3rd SP2 sub-resource. NO seam change (SP5f), NO cursor, NO list-wrap (the service returns `SourceAllocation` directly).

**Architecture:** Backend mirrors `tasks_chunks_stream.py` (SP5g) but emits the service's `SourceAllocation` object directly (no `SubtaskChunkReport`-style wrapping). Pre-stream tenant gate (5-line `tenant_filtered`). Frontend `useSourceAllocation` opts in with `computed(() => \`/api/v1/tasks/${taskId.value}/source-allocation/stream\`)`. `TaskDetail.vue` + Sources-tab components UNCHANGED. Sources tab is NOT default-active → re-exercises SP5f's "enabled flips true" seam path.

---

## Conventions (apply to every task — same as SP5g)

- **Branch:** `feat/ui-sp5h-source-alloc-sse` (off `main` @ `8c66301`, already created).
- **Bash cwd persists**. `cd /d/download_weights && git …`; `cd /d/download_weights/frontend && pnpm …`.
- **Tenant gate**: copy the 5-line `tenant_filtered(select(DownloadTask.id).where(DownloadTask.id == task_id), DownloadTask, principal)` from `tasks_chunks_stream.py`. Do NOT import `tasks.py:_task_in_tenant`.
- **`?max_ticks=N`**: same testability hatch.
- **Route placement**: register `tasks_source_alloc_stream_router` BEFORE `tasks_router` (after `tasks_chunks_stream_router`) in `main.py`. Distinct depth → no collision; defensive consistency.
- **No dead keepalive block** (SP5e #38).
- **Test fixture FK ordering** (SP5f #43): flush parents before `DownloadTask`. SP5h needs NO `FileSubTask` rows (empty `SourceAllocation` is valid).
- **`DownloadTask` columns** (SP5f B1): `repo_id`/`revision`/`path_template`.

---

## File Structure

**Backend (create):**
- `src/dlw/api/tasks_source_alloc_stream.py` — the route.
- `tests/api/test_task_source_alloc_stream.py` — 4 tests.

**Backend (modify):**
- `api/openapi.yaml` — `/tasks/{taskId}/source-allocation/stream` block (DONE in spec commit — verify present).
- `src/dlw/main.py` — register router after `tasks_chunks_stream_router`, before `tasks_router`.
- `src/dlw/config.py` — `task_source_alloc_stream_interval_seconds` (DONE in spec commit — verify present).

**Frontend (modify):**
- `frontend/src/composables/useSourceAllocation.ts` — opt-in.

**Frontend (create):**
- `frontend/tests/unit/useSourceAllocationStream.spec.ts`.

**Docs (modify):** `docs/operator/web-ui.md` (append SP5h section).

---

# Milestone M1 — Backend

### Task 1: Verify openapi + config (added in spec commit)

- [ ] **Step 1**:

```bash
cd /d/download_weights && grep -n "source-allocation/stream" api/openapi.yaml && grep -n "task_source_alloc_stream_interval_seconds" src/dlw/config.py
```

Expected: both present.

### Task 2: SSE endpoint

**Files:**
- Create: `src/dlw/api/tasks_source_alloc_stream.py`
- Modify: `src/dlw/main.py`

- [ ] **Step 1**: Create `src/dlw/api/tasks_source_alloc_stream.py`:

```python
"""GET /api/v1/tasks/{task_id}/source-allocation/stream — SSE source-allocation live stream (UI-SP5h).

Hand-rolled text/event-stream; reuses SP2's source_allocation_for_task
service which returns a SourceAllocation directly (no wrapper).
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.models.task import DownloadTask
from dlw.db.session import get_engine
from dlw.db.tenant_scope import tenant_filtered
from dlw.services import task_detail as _td

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "task_source_alloc_stream_interval_seconds", 2.0))
    return max(0.5, min(60.0, raw))


@router.get("/{task_id}/source-allocation/stream")
async def stream_source_allocation(
    task_id: uuid.UUID,
    request: Request,
    max_ticks: int | None = Query(default=None, ge=1, le=10000),
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
) -> StreamingResponse:
    session_maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with session_maker() as s:
        owned = await s.scalar(
            tenant_filtered(select(DownloadTask.id)
                            .where(DownloadTask.id == task_id),
                            DownloadTask, principal))
    if owned is None:
        raise HTTPException(status_code=404, detail="task not found")

    interval = _clamped_interval()

    async def _body() -> AsyncIterator[bytes]:
        yield b":open\n\n"
        tick_count = 0
        try:
            while True:
                if await request.is_disconnected():
                    return
                async with session_maker() as s:
                    payload = await _td.source_allocation_for_task(
                        s, task_id, principal.tenant_id)
                yield (f"data: {payload.model_dump_json()}"
                       "\n\n").encode("utf-8")
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

- [ ] **Step 2**: Modify `src/dlw/main.py`. The existing block reads:

```python
    from dlw.api.tasks_chunks_stream import router as tasks_chunks_stream_router
    app.include_router(tasks_chunks_stream_router)
    from dlw.api.tasks import router as tasks_router
    app.include_router(tasks_router)
```

**INSERT** these 4 lines BETWEEN `app.include_router(tasks_chunks_stream_router)` and `from dlw.api.tasks import router as tasks_router`. Do NOT remove or retype any existing line; only add these 4:

```python
    # SP5h: tasks-source-alloc stream router included BEFORE tasks_router
    # for defensive consistency. Distinct depth from any /{task_id}/* route.
    from dlw.api.tasks_source_alloc_stream import router as tasks_source_alloc_stream_router
    app.include_router(tasks_source_alloc_stream_router)
```

- [ ] **Step 3**: Sanity:

```bash
cd /d/download_weights && uv run python -c "from dlw.api.tasks_source_alloc_stream import router; [print(r.path) for r in router.routes]"
```

Expected: `/api/v1/tasks/{task_id}/source-allocation/stream`.

- [ ] **Step 4**: Commit.

```bash
git add api/openapi.yaml src/dlw/config.py src/dlw/api/tasks_source_alloc_stream.py src/dlw/main.py
git commit -q -m "UI-SP5h M1: GET /tasks/{id}/source-allocation/stream SSE endpoint + openapi + config (reuses source_allocation_for_task)"
```

---

### Task 3: Backend tests

**Files:**
- Create: `tests/api/test_task_source_alloc_stream.py`

- [ ] **Step 1**: Create `tests/api/test_task_source_alloc_stream.py`:

```python
"""Tests for GET /api/v1/tasks/{task_id}/source-allocation/stream (UI-SP5h SSE)."""
from __future__ import annotations

import asyncio
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

TASK_T1 = uuid.UUID("11111111-1111-1111-1111-111111111111")
TASK_T2 = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.task import DownloadTask
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Tenant(id=1, slug="t1", display_name="T1"))
        session.add(Tenant(id=2, slug="t2", display_name="T2"))
        await session.flush()
        session.add_all([
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
        ])
        await session.flush()
        session.add_all([
            DownloadTask(id=TASK_T1, tenant_id=1, project_id=1,
                         owner_user_id=1, storage_id=1,
                         repo_id="org/m1", revision="0" * 40,
                         path_template="hf/{model}/{revision}/{file}",
                         status="running"),
            DownloadTask(id=TASK_T2, tenant_id=2, project_id=2,
                         owner_user_id=2, storage_id=2,
                         repo_id="org/m2", revision="1" * 40,
                         path_template="hf/{model}/{revision}/{file}",
                         status="running"),
        ])
        await session.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_TASK_SOURCE_ALLOC_STREAM_INTERVAL_SECONDS", TICK)
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
async def test_source_alloc_stream_unauthenticated_401(
    client: AsyncClient,
) -> None:
    async with client.stream(
        "GET", f"/api/v1/tasks/{TASK_T1}/source-allocation/stream",
    ) as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_source_alloc_stream_cross_tenant_404(
    client: AsyncClient, auth,
) -> None:
    async with client.stream(
        "GET",
        f"/api/v1/tasks/{TASK_T2}/source-allocation/stream?max_ticks=1",
        headers=auth,
    ) as resp:
        assert resp.status_code == 404


@pytest.mark.slow
async def test_source_alloc_stream_single_snapshot(
    client: AsyncClient, auth,
) -> None:
    received = await _collect(
        client,
        f"/api/v1/tasks/{TASK_T1}/source-allocation/stream?max_ticks=1",
        auth, count=1, timeout=3.0)
    body = json.loads(received[0])
    assert body["task_id"] == str(TASK_T1)
    assert "sources_used" in body
    assert "chunk_level_routing" in body
    assert isinstance(body["sources_used"], list)
    assert isinstance(body["chunk_level_routing"], list)


@pytest.mark.slow
async def test_source_alloc_stream_multi_snapshot(
    client: AsyncClient, auth,
) -> None:
    received = await _collect(
        client,
        f"/api/v1/tasks/{TASK_T1}/source-allocation/stream?max_ticks=2",
        auth, count=2, timeout=3.0)
    assert len(received) >= 2
    for raw in received[:2]:
        body = json.loads(raw)
        assert body["task_id"] == str(TASK_T1)
        assert "sources_used" in body
        assert "chunk_level_routing" in body
```

- [ ] **Step 2**: Run:

```bash
cd /d/download_weights && uv run pytest tests/api/test_task_source_alloc_stream.py -q 2>&1 | tail -8
```

Expected: 4 passed.

- [ ] **Step 3**: Commit.

```bash
git add tests/api/test_task_source_alloc_stream.py
git commit -q -m "UI-SP5h M1: backend SSE endpoint tests (unauth-401, cross-tenant-404, single/multi snapshot)"
```

---

### M1 Full backend gate

- [ ] **Step 1**:

```bash
cd /d/download_weights && uv run pytest -q 2>&1 | tail -3
```

Expected: SP1-SP5g + new SP5h tests pass (2 known Windows-local failover flakes may appear; CI is arbiter).

---

# Milestone M2 — Frontend cutover

### Task 4: `useSourceAllocation` opts in to SSE seam

**Files:**
- Modify: `frontend/src/composables/useSourceAllocation.ts`
- Create: `frontend/tests/unit/useSourceAllocationStream.spec.ts`

- [ ] **Step 1**: Modify `frontend/src/composables/useSourceAllocation.ts` (full new file):

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

- [ ] **Step 2**: Create `frontend/tests/unit/useSourceAllocationStream.spec.ts`:

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

import { useSourceAllocation } from '@/composables/useSourceAllocation'

describe('useSourceAllocation SSE opt-in (SP5h)', () => {
  test('passes streamUrl + applyEvent + enabled + isTerminal to the seam', () => {
    captured.length = 0
    const taskId = ref('aaa')
    const enabled = ref(true)
    const terminal = ref(false)
    useSourceAllocation(taskId, enabled, terminal)
    const last = captured[captured.length - 1]
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/aaa/source-allocation/stream')
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(2_000)
    expect(last?.opts.enabled).toBe(enabled)
    expect(typeof last?.opts.isTerminal).toBe('function')
  })
  test('streamUrl is reactive to taskId ref', () => {
    captured.length = 0
    const taskId = ref('bbb')
    useSourceAllocation(taskId, ref(true), ref(false))
    const last = captured[captured.length - 1]
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/bbb/source-allocation/stream')
    taskId.value = 'ccc'
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/ccc/source-allocation/stream')
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useSourceAllocation(ref('xxx'), ref(true), ref(false))
    const apply = (captured[captured.length - 1]?.opts.applyEvent) as
      (prev: unknown, ev: { data: string }) =>
        { task_id: string; sources_used: unknown[] }
    const out = apply(undefined, {
      data: '{"task_id":"t","sources_used":[{"source_id":"s"}],"chunk_level_routing":[]}',
    })
    expect(out.task_id).toBe('t')
    expect(out.sources_used).toEqual([{ source_id: 's' }])
  })
})
```

- [ ] **Step 3**: Full frontend gate:

```bash
cd /d/download_weights/frontend && pnpm lint && pnpm exec vue-tsc --noEmit && pnpm vitest run 2>&1 | tail -8
```

Expected: lint clean, typecheck clean, all tests pass (164 + 3 = 167).

- [ ] **Step 4**: Build:

```bash
cd /d/download_weights/frontend && pnpm build 2>&1 | tail -3
```

Expected: built.

- [ ] **Step 5**: Commit.

```bash
git add frontend/src/composables/useSourceAllocation.ts frontend/tests/unit/useSourceAllocationStream.spec.ts
git commit -q -m "UI-SP5h M2: useSourceAllocation opts in to SSE (view-free; TaskDetail.vue + Sources-tab untouched)"
```

---

# Milestone M3 — Smoke + docs

### Task 5: headed Playwright smoke + operator docs

**Files:**
- Create: `.run/pw/sp5h-smoke.mjs` (gitignored)
- Modify: `docs/operator/web-ui.md`

- [ ] **Step 1**: Restart `:8011` controller with SP5h code; restart Vite (clear `node_modules/.vite` + kill stale via netstat/Stop-Process).

- [ ] **Step 2**: curl smoke:

```bash
cd /d/download_weights && JWT=$(uv run python -c "from dlw.auth.principal import issue_system_jwt; print(issue_system_jwt(secret='dev-system-jwt-change-me', user_id=1, tenant_id=1, role='tenant_admin', project_ids=[]))" 2>/dev/null | tail -1)
echo "$JWT" > .run/sp5h-token.txt
TASK_ID=$(curl -s -H "Authorization: Bearer $JWT" "http://127.0.0.1:8011/api/v1/tasks?limit=1" | python -c "import sys,json;d=json.load(sys.stdin);print(d['items'][0]['id'] if d.get('items') else 'NONE')")
curl -s -H "Authorization: Bearer $JWT" "http://127.0.0.1:8011/api/v1/tasks/$TASK_ID/source-allocation/stream?max_ticks=1" | head -c 400; echo
```

Expected: `:open\n\ndata: {"task_id":"…","sources_used":[…],"chunk_level_routing":[…]}\n\n`.

- [ ] **Step 3**: Create `.run/pw/sp5h-smoke.mjs` (Sources tab requires a click — id-stable `#tab-sources`):

```js
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'

const TOKEN = readFileSync('.run/sp5h-token.txt', 'utf8').trim()
const VITE = process.env.SP5H_VITE ?? 'http://localhost:5173'
const errors = []
const b = await chromium.launch({ headless: false })
const pg = await b.newPage()
pg.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
pg.on('pageerror', (e) => errors.push(String(e)))

const sseReqs = []
pg.on('request', (r) => {
  if (/\/api\/v1\/tasks\/[^/]+\/source-allocation\/stream(\?|$)/.test(r.url())) {
    sseReqs.push(r.url())
  }
})

await pg.goto(`${VITE}/login`)
await pg.fill('input', TOKEN)
await pg.click('button[type="submit"]')
await pg.waitForURL('**/')
const taskId = await pg.evaluate(async (token) => {
  const r = await fetch('/api/v1/tasks?limit=1', {
    headers: { Authorization: `Bearer ${token}` },
  })
  const j = await r.json()
  return j?.items?.[0]?.id ?? null
}, TOKEN)
if (!taskId) {
  console.log('SP5h smoke: no tasks exist; submit one first via `uv run dlw submit …`')
  process.exit(1)
}
await pg.goto(`${VITE}/tasks/${taskId}`)
await pg.waitForURL('**/tasks/**')
await pg.waitForSelector('.el-tabs', { timeout: 10_000 })
await pg.waitForTimeout(1500)
await pg.click('#tab-sources')
await pg.waitForTimeout(6000)
await pg.screenshot({ path: '.run/pw/sp5h-sources.png' })

await b.close()
const real = errors.filter((e) =>
  !e.includes('/health/active') && !e.includes('Failed to load resource'))
if (real.length) {
  console.log('SP5h smoke: real errors:\n' + real.join('\n'))
  process.exit(1)
}
if (sseReqs.length === 0) {
  console.log('SP5h smoke: no /tasks/<id>/source-allocation/stream SSE request observed')
  process.exit(1)
}
console.log(`SP5h smoke OK — observed ${sseReqs.length} /tasks/<id>/source-allocation/stream request(s)`)
sseReqs.forEach((u) => console.log('  -> ' + u))
```

- [ ] **Step 4**: Run:

```bash
cd /d/download_weights && node .run/pw/sp5h-smoke.mjs 2>&1 | tail -5
```

Expected: `SP5h smoke OK — observed N …`.

- [ ] **Step 5**: Append SP5h section to `docs/operator/web-ui.md` after the SP5g section:

```markdown

### UI-SP5h — Source-allocation SSE follow-on

Eighth application of the view-free SSE template, third SP2
sub-resource to graduate to SSE. The Sources tab on TaskDetail
(`useSourceAllocation`) now talks SSE via
`GET /api/v1/tasks/{task_id}/source-allocation/stream` (2 s default
tick; `DLW_TASK_SOURCE_ALLOC_STREAM_INTERVAL_SECONDS` overrides;
clamped `[0.5, 60.0]`). The view-side consumers are unchanged.

The stream reuses `source_allocation_for_task(session, task_id,
tenant_id)` (SP2-introduced), which returns a `SourceAllocation`
directly (no wrapper, no cursor). No seam change was needed — SP5f's
reactive `streaming` gate already supports `enabled`-gated consumers.
The Sources tab is NOT default-active, so the SSE opens on tab click
(the SP5f "enabled flips true → lazy-open" path). Same routing
precaution as SP5c-SP5g: `tasks_source_alloc_stream_router` is
registered after `tasks_chunks_stream_router` and BEFORE
`tasks_router` in `src/dlw/main.py`.

**Connection-cap status (monitored)**: a TaskDetail page where the
user has visited Files + Events + Sources tabs now holds 4 concurrent
SSE connections (header + 3 tab streams), still under the HTTP/1.1
6-per-origin cap. **SP5i (the last SP2 tab, Executors) would reach 5
— that SP must bundle the seam "close-on-disable" change** (bounding
concurrent streams to 2: header + active tab). This is the trigger
condition recorded in SP5g's YAGNI deferral.
```

- [ ] **Step 6**: Commit (`.run/` gitignored — do NOT add the smoke script):

```bash
git add docs/operator/web-ui.md
git commit -q -m "UI-SP5h M3: operator docs — source-allocation SSE follow-on (3rd SP2 sub-resource)"
```

---

# Final cycle (controller-driven)

1. 1 opus reviewer for whole-impl review (≤500 words).
2. Fix any HIGH; record MEDIUM/LOW in PR body.
3. `git push -u origin feat/ui-sp5h-source-alloc-sse`.
4. `gh pr create` against `main`.
5. Background poller waits for CI all-green.
6. `gh pr merge <N> --squash --delete-branch`, `git checkout main && git pull --ff-only`.
7. Update `reference_l17728_modelpull.md` (record SP5h merge, bump `main`).
8. Update `feedback_subagent_driven_dev.md` if any new learning (likely minimal — strictly-simpler repeat; main note = SP5i must bundle close-on-disable).

---

## Self-Review

- **Spec coverage**: every section maps to a task. ✓
- **Placeholder scan**: none.
- **Type consistency**: `SourceAllocation` reused (backend schema + frontend type). `source_allocation_for_task` signature matches. ✓
- **Naming**: env `DLW_TASK_SOURCE_ALLOC_STREAM_INTERVAL_SECONDS`, setting `task_source_alloc_stream_interval_seconds`, router `tasks_source_alloc_stream_router` — consistent with SP5g.
- **Test coverage**: 4 backend + 3 frontend + 1 smoke = mirror SP5g.
- **No seam change / no FileSubTask seed**: SP5h is even simpler than SP5g (service returns the schema directly; empty allocation is valid so no child rows needed).
- **Route precedence**: `tasks_source_alloc_stream_router` between `tasks_chunks_stream_router` and `tasks_router`.
- **Smoke**: Sources tab not default-active → `#tab-sources` click; stable-id selector (SP5f #42).
