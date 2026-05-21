# UI-SP5g — Subtask-Chunks SSE Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** Add `GET /api/v1/tasks/{task_id}/subtask-chunks/stream` + opt `useSubtaskChunks` in to the SP5 seam. 7th application of the view-free SSE template; 2nd SP2 sub-resource composable to graduate from polling to SSE. NO seam change (SP5f already evolved the seam to support `enabled`-gated consumers).

**Architecture:** Backend mirrors `tasks_events_stream.py` (SP5f) minus the cursor — `chunks_for_task` returns a plain list wrapped as `SubtaskChunkReport(items=…)`. Pre-stream tenant gate (5-line `tenant_filtered(select(DownloadTask.id)…)` copied from `tasks_stream.py`) → 404 cross-tenant before `:open`. Frontend `useSubtaskChunks` opts in with `computed(() => \`/api/v1/tasks/${taskId.value}/subtask-chunks/stream\`)`. `TaskDetail.vue` + Files-tab components UNCHANGED. Files tab is default-active, so the chunks SSE opens on TaskDetail landing — exercising the seam's "enabled === true at mount" path (same as the original 5 consumers), NOT SP5f's "enabled flips true" path.

---

## Conventions (apply to every task — same as SP5f)

- **Branch:** `feat/ui-sp5g-chunks-sse` (off `main` @ `1201b7a`, already created).
- **Bash cwd persists**. Always `cd /d/download_weights && git …` for git; `cd /d/download_weights/frontend && pnpm …` for frontend.
- **Tenant gate**: copy the 5-line `tenant_filtered(select(DownloadTask.id).where(DownloadTask.id == task_id), DownloadTask, principal)` pattern from `tasks_stream.py` / `tasks_events_stream.py` — pre-stream 404. Do NOT import `tasks.py:_task_in_tenant`.
- **`?max_ticks=N`**: same testability hatch as SP5-SP5f.
- **Route placement**: register `tasks_chunks_stream_router` BEFORE `tasks_router` (after `tasks_events_stream_router`) in `main.py` for defensive consistency. The new path `/{task_id}/subtask-chunks/stream` has distinct depth from any sibling — no actual collision.
- **No dead keepalive block** (SP5e learning #38).
- **Test fixture FK ordering** (SP5f learning #43): seed Tenant→Project/User/StorageBackend with an explicit `flush()` BEFORE inserting `DownloadTask`, then add `FileSubTask` rows, then `commit()`.
- **`DownloadTask` columns** (SP5f learning B1): `repo_id`, `revision`, `path_template` (NOT `model`/`revision_sha`). `FileSubTask` columns: verify against `src/dlw/db/models/task.py` at execution time.

---

## File Structure

**Backend (create):**
- `src/dlw/api/tasks_chunks_stream.py` — `GET /api/v1/tasks/{task_id}/subtask-chunks/stream` route.
- `tests/api/test_task_chunks_stream.py` — 4 tests.

**Backend (modify):**
- `api/openapi.yaml` — add `/tasks/{taskId}/subtask-chunks/stream` path block (DONE in spec commit — verify present before M1).
- `src/dlw/main.py` — register `tasks_chunks_stream_router` after `tasks_events_stream_router`, before `tasks_router`.
- `src/dlw/config.py` — add `task_chunks_stream_interval_seconds` (DONE in spec commit — verify present).

**Frontend (modify):**
- `frontend/src/composables/useSubtaskChunks.ts` — opt-in: add reactive `streamUrl` + `applyEvent`.

**Frontend (create):**
- `frontend/tests/unit/useSubtaskChunksStream.spec.ts` — new spec.

**Docs (modify):** `docs/operator/web-ui.md` (append SP5g section).

---

# Milestone M1 — Backend

### Task 1: Verify openapi + config (already added in spec commit)

- [ ] **Step 1**: Confirm `api/openapi.yaml` has the `/tasks/{taskId}/subtask-chunks/stream` block and `src/dlw/config.py` has `task_chunks_stream_interval_seconds`. (Both were added alongside the spec commit `0234dc2`. If missing, add per spec §3.3 / §3.4.)

```bash
cd /d/download_weights && grep -n "subtask-chunks/stream" api/openapi.yaml && grep -n "task_chunks_stream_interval_seconds" src/dlw/config.py
```

Expected: both grep hits present.

### Task 2: SSE endpoint

**Files:**
- Create: `src/dlw/api/tasks_chunks_stream.py`
- Modify: `src/dlw/main.py` (register router after `tasks_events_stream_router`, before `tasks_router`)

- [ ] **Step 1**: Create `src/dlw/api/tasks_chunks_stream.py`:

```python
"""GET /api/v1/tasks/{task_id}/subtask-chunks/stream — SSE chunk-progress live stream (UI-SP5g).

Hand-rolled text/event-stream; reuses SP2's chunks_for_task service
(plain list, no cursor). Mirrors tasks_events_stream.py minus pagination.
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
from dlw.schemas.task_detail import SubtaskChunkReport
from dlw.services import task_detail as _td

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "task_chunks_stream_interval_seconds", 2.0))
    return max(0.5, min(60.0, raw))


@router.get("/{task_id}/subtask-chunks/stream")
async def stream_subtask_chunks(
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
                    items = await _td.chunks_for_task(
                        s, task_id, principal.tenant_id)
                payload = SubtaskChunkReport(items=items)
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

- [ ] **Step 2**: Modify `src/dlw/main.py` — find the existing block:

```python
    from dlw.api.tasks_events_stream import router as tasks_events_stream_router
    app.include_router(tasks_events_stream_router)
    from dlw.api.tasks import router as tasks_router
    app.include_router(tasks_router)
```

**REPLACE** with (insert the chunks-stream pair between events-stream and tasks; do NOT remove the existing lines, ADD the new pair before `tasks_router`):

```python
    from dlw.api.tasks_events_stream import router as tasks_events_stream_router
    app.include_router(tasks_events_stream_router)
    # SP5g: tasks-chunks stream router included BEFORE tasks_router for
    # defensive consistency. Distinct depth from any /{task_id}/* route.
    from dlw.api.tasks_chunks_stream import router as tasks_chunks_stream_router
    app.include_router(tasks_chunks_stream_router)
    from dlw.api.tasks import router as tasks_router
    app.include_router(tasks_router)
```

- [ ] **Step 3**: Sanity: import + route smoke.

```bash
cd /d/download_weights && uv run python -c "from dlw.api.tasks_chunks_stream import router; [print(r.path) for r in router.routes]"
```

Expected: `/api/v1/tasks/{task_id}/subtask-chunks/stream`.

- [ ] **Step 4**: Commit.

```bash
git add api/openapi.yaml src/dlw/config.py src/dlw/api/tasks_chunks_stream.py src/dlw/main.py
git commit -q -m "UI-SP5g M1: GET /tasks/{id}/subtask-chunks/stream SSE endpoint + openapi + config (reuses chunks_for_task)"
```

---

### Task 3: Backend tests

**Files:**
- Create: `tests/api/test_task_chunks_stream.py`

- [ ] **Step 1**: First inspect `FileSubTask` required columns:

```bash
cd /d/download_weights && grep -nE "mapped_column|Mapped\[" src/dlw/db/models/task.py | sed -n '1,60p'
```

Note the non-nullable columns on `FileSubTask` (e.g. `task_id`, `tenant_id`, `filename`, `file_size`, `status`, `chunk_*`) and use real values in the seed. Adapt the seed below if the actual columns differ.

- [ ] **Step 2**: Create `tests/api/test_task_chunks_stream.py`:

```python
"""Tests for GET /api/v1/tasks/{task_id}/subtask-chunks/stream (UI-SP5g SSE)."""
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
    from dlw.db.models.task import DownloadTask, FileSubTask
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
        # FK ordering (SP5f learning #43): flush parents before children.
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
        await session.flush()
        # 2 subtasks on TASK_T1 (tenant 1). Adapt columns to the real
        # FileSubTask schema (verified in Task 3 Step 1).
        session.add_all([
            FileSubTask(task_id=TASK_T1, tenant_id=1,
                        filename="a.bin", file_size=1000,
                        status="downloading"),
            FileSubTask(task_id=TASK_T1, tenant_id=1,
                        filename="b.bin", file_size=2000,
                        status="pending"),
        ])
        await session.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_TASK_CHUNKS_STREAM_INTERVAL_SECONDS", TICK)
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
async def test_chunks_stream_unauthenticated_401(client: AsyncClient) -> None:
    async with client.stream(
        "GET", f"/api/v1/tasks/{TASK_T1}/subtask-chunks/stream",
    ) as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_chunks_stream_cross_tenant_404(
    client: AsyncClient, auth,
) -> None:
    async with client.stream(
        "GET",
        f"/api/v1/tasks/{TASK_T2}/subtask-chunks/stream?max_ticks=1",
        headers=auth,
    ) as resp:
        assert resp.status_code == 404


@pytest.mark.slow
async def test_chunks_stream_single_snapshot(
    client: AsyncClient, auth,
) -> None:
    received = await _collect(
        client,
        f"/api/v1/tasks/{TASK_T1}/subtask-chunks/stream?max_ticks=1",
        auth, count=1, timeout=3.0)
    body = json.loads(received[0])
    assert "items" in body
    assert isinstance(body["items"], list)
    assert len(body["items"]) >= 1


@pytest.mark.slow
async def test_chunks_stream_multi_snapshot(
    client: AsyncClient, auth,
) -> None:
    received = await _collect(
        client,
        f"/api/v1/tasks/{TASK_T1}/subtask-chunks/stream?max_ticks=2",
        auth, count=2, timeout=3.0)
    assert len(received) >= 2
    for raw in received[:2]:
        body = json.loads(raw)
        assert "items" in body
        assert isinstance(body["items"], list)
```

- [ ] **Step 3**: Run the new tests. If the `FileSubTask` seed fails on a missing/renamed column, adjust to the real schema (from Step 1) and re-run.

```bash
cd /d/download_weights && uv run pytest tests/api/test_task_chunks_stream.py -q 2>&1 | tail -10
```

Expected: 4 passed.

- [ ] **Step 4**: Commit.

```bash
git add tests/api/test_task_chunks_stream.py
git commit -q -m "UI-SP5g M1: backend SSE endpoint tests (unauth-401, cross-tenant-404, single/multi snapshot)"
```

---

### M1 Full backend gate

- [ ] **Step 1**: Run full backend pytest:

```bash
cd /d/download_weights && uv run pytest -q 2>&1 | tail -3
```

Expected: SP1-SP5f tests + new SP5g tests pass. The 2 known Windows-local `test_failover_drill.py` flakes may appear; CI is the arbiter.

---

# Milestone M2 — Frontend cutover

### Task 4: `useSubtaskChunks` opts in to SSE seam

**Files:**
- Modify: `frontend/src/composables/useSubtaskChunks.ts`
- Create: `frontend/tests/unit/useSubtaskChunksStream.spec.ts`

- [ ] **Step 1**: Modify `frontend/src/composables/useSubtaskChunks.ts` (full new file):

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

- [ ] **Step 2**: Create `frontend/tests/unit/useSubtaskChunksStream.spec.ts`:

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

import { useSubtaskChunks } from '@/composables/useSubtaskChunks'

describe('useSubtaskChunks SSE opt-in (SP5g)', () => {
  test('passes streamUrl + applyEvent + enabled + isTerminal to the seam', () => {
    captured.length = 0
    const taskId = ref('aaa')
    const enabled = ref(true)
    const terminal = ref(false)
    useSubtaskChunks(taskId, enabled, terminal)
    const last = captured[captured.length - 1]
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/aaa/subtask-chunks/stream')
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(1_500)
    expect(last?.opts.enabled).toBe(enabled)
    expect(typeof last?.opts.isTerminal).toBe('function')
  })
  test('streamUrl is reactive to taskId ref', () => {
    captured.length = 0
    const taskId = ref('bbb')
    useSubtaskChunks(taskId, ref(true), ref(false))
    const last = captured[captured.length - 1]
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/bbb/subtask-chunks/stream')
    taskId.value = 'ccc'
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/ccc/subtask-chunks/stream')
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useSubtaskChunks(ref('xxx'), ref(true), ref(false))
    const apply = (captured[captured.length - 1]?.opts.applyEvent) as
      (prev: unknown, ev: { data: string }) => { items: unknown[] }
    const out = apply(undefined, { data: '{"items":[{"filename":"a"}]}' })
    expect(out.items).toEqual([{ filename: 'a' }])
  })
})
```

- [ ] **Step 3**: Run full frontend gate:

```bash
cd /d/download_weights/frontend && pnpm lint && pnpm exec vue-tsc --noEmit && pnpm vitest run 2>&1 | tail -10
```

Expected: lint clean, typecheck clean, all tests pass (161 + 3 new = 164).

- [ ] **Step 4**: Build proof:

```bash
cd /d/download_weights/frontend && pnpm build 2>&1 | tail -3
```

Expected: built successfully.

- [ ] **Step 5**: Commit.

```bash
git add frontend/src/composables/useSubtaskChunks.ts frontend/tests/unit/useSubtaskChunksStream.spec.ts
git commit -q -m "UI-SP5g M2: useSubtaskChunks opts in to SSE (view-free; TaskDetail.vue + Files-tab untouched)"
```

---

# Milestone M3 — Smoke + docs

### Task 5: headed Playwright smoke + operator docs

**Files:**
- Create: `.run/pw/sp5g-smoke.mjs` (gitignored `.run/`)
- Modify: `docs/operator/web-ui.md` (append SP5g section)

- [ ] **Step 1**: Restart `:8011` controller with SP5g code; restart Vite proxying to it (clear `node_modules/.vite` + kill stale Vite via `netstat`/`Stop-Process` if needed — SP5f hit a stale-cache `Failed to fetch dynamically imported module` error).

- [ ] **Step 2**: Smoke the endpoint with curl (any tenant-1 task UUID):

```bash
cd /d/download_weights && JWT=$(uv run python -c "from dlw.auth.principal import issue_system_jwt; print(issue_system_jwt(secret='dev-system-jwt-change-me', user_id=1, tenant_id=1, role='tenant_admin', project_ids=[]))" 2>/dev/null | tail -1)
echo "$JWT" > .run/sp5g-token.txt
TASK_ID=$(curl -s -H "Authorization: Bearer $JWT" "http://127.0.0.1:8011/api/v1/tasks?limit=1" | python -c "import sys,json;d=json.load(sys.stdin);print(d['items'][0]['id'] if d.get('items') else 'NONE')")
echo "task=$TASK_ID"
curl -s -H "Authorization: Bearer $JWT" "http://127.0.0.1:8011/api/v1/tasks/$TASK_ID/subtask-chunks/stream?max_ticks=1" | head -c 400; echo
```

Expected: `:open\n\ndata: {"items":[…]}\n\n`.

- [ ] **Step 3**: Create `.run/pw/sp5g-smoke.mjs` (Files tab is default-active — no tab click needed; SSE opens on landing):

```js
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'

const TOKEN = readFileSync('.run/sp5g-token.txt', 'utf8').trim()
const VITE = process.env.SP5G_VITE ?? 'http://localhost:5173'
const errors = []
const b = await chromium.launch({ headless: false })
const pg = await b.newPage()
pg.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
pg.on('pageerror', (e) => errors.push(String(e)))

const sseReqs = []
pg.on('request', (r) => {
  if (/\/api\/v1\/tasks\/[^/]+\/subtask-chunks\/stream(\?|$)/.test(r.url())) {
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
  console.log('SP5g smoke: no tasks exist; submit one first via `uv run dlw submit …`')
  process.exit(1)
}
await pg.goto(`${VITE}/tasks/${taskId}`)
await pg.waitForURL('**/tasks/**')
// Files tab is default-active — chunks SSE opens without a tab click.
await pg.waitForTimeout(5000)
await pg.screenshot({ path: '.run/pw/sp5g-files.png' })

await b.close()
const real = errors.filter((e) =>
  !e.includes('/health/active') && !e.includes('Failed to load resource'))
if (real.length) {
  console.log('SP5g smoke: real errors:\n' + real.join('\n'))
  process.exit(1)
}
if (sseReqs.length === 0) {
  console.log('SP5g smoke: no /tasks/<id>/subtask-chunks/stream SSE request observed')
  process.exit(1)
}
console.log(`SP5g smoke OK — observed ${sseReqs.length} /tasks/<id>/subtask-chunks/stream request(s)`)
sseReqs.forEach((u) => console.log('  -> ' + u))
```

- [ ] **Step 4**: Run the smoke:

```bash
cd /d/download_weights && node .run/pw/sp5g-smoke.mjs 2>&1 | tail -5
```

Expected: `SP5g smoke OK — observed N /tasks/<id>/subtask-chunks/stream request(s)`.

- [ ] **Step 5**: Append SP5g section to `docs/operator/web-ui.md` after the SP5f section (end of file):

```markdown

### UI-SP5g — Subtask-chunks SSE follow-on

Seventh application of the view-free SSE template, and the second SP2
sub-resource composable to graduate from polling to SSE. The Files
tab on TaskDetail (`useSubtaskChunks`) now talks SSE via
`GET /api/v1/tasks/{task_id}/subtask-chunks/stream` (2 s default tick;
`DLW_TASK_CHUNKS_STREAM_INTERVAL_SECONDS` overrides; clamped
`[0.5, 60.0]`). The view-side consumers are unchanged.

The stream reuses `chunks_for_task(session, task_id, tenant_id)`
(SP2-introduced; a plain list, no cursor) wrapped as
`SubtaskChunkReport`. No seam change was needed — SP5f's reactive
`streaming` gate already supports `enabled`-gated consumers. Because
the Files tab is **default-active** on TaskDetail, the chunks SSE
opens on landing (the "enabled === true at mount" seam path, same as
the original 5 always-on consumers), unlike SP5f's Events tab which
required a click. Same routing precaution as SP5c-SP5f:
`tasks_chunks_stream_router` is registered after
`tasks_events_stream_router` and BEFORE `tasks_router` in
`src/dlw/main.py`.

**Connection-cap status (monitored)**: a TaskDetail page where the
user has visited both the Files and Events tabs now holds 3
concurrent SSE connections (header + events + chunks), still well
under the HTTP/1.1 6-per-origin cap. The remaining 2 SP2 tabs
(Sources, Executors) are SP5h/SP5i candidates; when the 4th tab
stream is added the worst case reaches 5, at which point a seam
"close-on-disable" change (bounding concurrent streams to 2: header +
active tab) becomes worthwhile. Deferred until then.
```

- [ ] **Step 6**: Commit (`.run/` is gitignored — do NOT add `sp5g-smoke.mjs`):

```bash
git add docs/operator/web-ui.md
git commit -q -m "UI-SP5g M3: operator docs — subtask-chunks SSE follow-on (2nd SP2 sub-resource)"
```

---

# Final cycle (controller-driven)

After all tasks complete:

1. Dispatch 1 opus reviewer for whole-impl review (HIGH/MEDIUM/LOW; ≤600 words).
2. Fix any HIGH; record MEDIUM/LOW in PR body.
3. `git push -u origin feat/ui-sp5g-chunks-sse`.
4. `gh pr create` against `main` with the standard summary template.
5. Background poller waits for CI all-green.
6. `gh pr merge <N> --squash --delete-branch`, then `git checkout main && git pull --ff-only`.
7. Update `reference_l17728_modelpull.md` (record SP5g merge, bump `main` commit).
8. Update `feedback_subagent_driven_dev.md` with any new learning points (likely: the YAGNI-deferral of close-on-disable; SP5g being a strictly-simpler template repeat than SP5f).

---

## Self-Review

- **Spec coverage**: every section maps to a task. ✓
- **Placeholder scan**: the only deliberate "adapt to real schema" note is the `FileSubTask` seed in Task 3, with an explicit verification Step 1 — not a placeholder, a guarded instruction.
- **Type consistency**: `SubtaskChunkReport` reused (backend schema + frontend type). `chunks_for_task` signature matches existing one-shot endpoint usage. ✓
- **Naming**: env `DLW_TASK_CHUNKS_STREAM_INTERVAL_SECONDS`, setting `task_chunks_stream_interval_seconds`, router `tasks_chunks_stream_router` — consistent with SP5f.
- **Test coverage**: 4 backend + 3 frontend + 1 smoke = mirror SP5f.
- **No seam change**: explicitly relied upon; the seam already supports this (SP5f). The Files tab being default-active means even the "enabled flips true" path isn't exercised — strictly simpler than SP5f.
- **Route precedence**: `tasks_chunks_stream_router` between `tasks_events_stream_router` and `tasks_router`.
- **FK ordering + DownloadTask columns**: SP5f learnings #43 + B1 applied in the test bootstrap.
