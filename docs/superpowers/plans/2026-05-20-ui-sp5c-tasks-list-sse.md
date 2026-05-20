# UI-SP5c — Tasks-List SSE Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** Add `GET /api/v1/tasks/stream` + opt `useTaskList` in to the SP5 seam. Third demonstration of the view-free SSE template (after SP5 `useTaskDetail` and SP5b `useExecutors`).

**Architecture:** Mirror SP5b. Backend = hand-rolled `text/event-stream` via `StreamingResponse`; reuse SP1's `list_tasks` aggregation logic (`tenant_filtered(select(DownloadTask))` + `total = count()`); new file `src/dlw/api/tasks_list_stream.py`. Frontend = `useTaskList` opts in via `streamUrl: '/api/v1/tasks/stream'` + `applyEvent` (plain string streamUrl since the composable takes no params). `TaskList.vue` and `Dashboard.vue` (both consume `useTaskList`) UNCHANGED.

---

## Conventions (apply to every task — same as SP5b)

- **Branch:** `feat/ui-sp5c-tasks-list-sse` (off `main` @ `af48a0a`).
- **Bash cwd persists**. Always `cd /d/download_weights && git …` for git; `cd /d/download_weights/frontend && pnpm …` for frontend.
- **Tenant gate**: inline `tenant_filtered(select(DownloadTask)…)` (same as `tasks.py:115`); no parent gate needed (resource IS the list).
- **`?max_ticks=N`**: same SP5/SP5b testability hatch.

---

## File Structure

**Backend (create):**
- `src/dlw/api/tasks_list_stream.py` — `GET /api/v1/tasks/stream` route module.
- `tests/api/test_tasks_list_stream.py` — 4 tests.

**Backend (modify):**
- `api/openapi.yaml` — add `/tasks/stream` path block.
- `src/dlw/main.py` — register the new router.
- `src/dlw/config.py` — add `tasks_list_stream_interval_seconds` setting.

**Frontend (modify):**
- `frontend/src/composables/useTaskList.ts` — opt in.

**Frontend (create):**
- `frontend/tests/unit/useTaskListStream.spec.ts` — 1 new spec.

**Docs (modify):** `docs/operator/web-ui.md` (append SP5c section).

---

# Milestone M1 — Backend

### Task 1: OpenAPI + config

- [ ] **Step 1**: In `api/openapi.yaml`, find `/tasks/{taskId}/stream:` (SP5-added). Insert `/tasks/stream:` block **before** it (alphabetical-ish: `/tasks/stream` < `/tasks/{taskId}/...`). Concretely: look for the section header `# ========== Tasks ==========` (`api/openapi.yaml:167`) — the existing tasks paths follow. Insert immediately before the existing `/tasks/{taskId}/stream:` block (added in SP5):

```yaml

  /tasks/stream:
    get:
      tags: [tasks]
      summary: Live tenant-scoped tasks-list SSE stream (UI-SP5c)
      operationId: streamTaskList
      responses:
        '200':
          description: SSE stream of TaskList snapshots (text/event-stream)
          content:
            text/event-stream:
              schema:
                type: string
                description: |
                  Each event is `data: <TaskList JSON>\n\n` ({items, total}).
                  Stream terminates only on client disconnect or controller
                  shutdown. Keep-alive comment lines (`:keepalive`) may appear.
```

(If the implementer finds it easier, insertion immediately AFTER the existing `/tasks/{taskId}/stream` block is also acceptable — the order of path blocks within the file is not semantically significant.)

- [ ] **Step 2**: In `src/dlw/config.py`, add 1 setting next to the other `*_stream_interval_seconds` fields:

```python
    tasks_list_stream_interval_seconds: float = Field(default=5.0)
```

- [ ] **Step 3**: Validate.

```bash
cd /d/download_weights
npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error  # exit 0
npx --yes @apidevtools/swagger-cli validate api/openapi.yaml  # valid
```

- [ ] **Step 4**: Commit.

```bash
cd /d/download_weights && git add api/openapi.yaml src/dlw/config.py && git commit -q -m "UI-SP5c M1: openapi /tasks/stream path + tasks_list_stream_interval_seconds setting"
```

---

### Task 2: SSE route + 4 tests

- [ ] **Step 1**: Write the failing test. Create `tests/api/test_tasks_list_stream.py`:

```python
"""Tests for GET /api/v1/tasks/stream (UI-SP5c SSE)."""
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
        session.add(Project(id=2, tenant_id=2, name="other"))
        session.add(User(id=1, tenant_id=1, oidc_subject="dev",
                         email="d@l", role="tenant_admin"))
        # Pre-review BLOCKER 2 fix: tenant 2 needs its own User row so the
        # cross-tenant POST as user_id=2 doesn't FK-violate users.id.
        session.add(User(id=2, tenant_id=2, oidc_subject="dev2",
                         email="d2@l", role="tenant_admin"))
        session.add(StorageBackend(id=1, tenant_id=1, name="default",
                                   backend_type="s3", config_encrypted=b""))
        session.add(StorageBackend(id=2, tenant_id=2, name="other",
                                   backend_type="s3", config_encrypted=b""))
        await session.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_TASKS_LIST_STREAM_INTERVAL_SECONDS", TICK)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _patch_hf(monkeypatch: pytest.MonkeyPatch):
    from dlw.services.hf_metadata import RepoFile

    async def fake(*args, **kwargs):
        return [RepoFile(path="config.json", size=4096, sha256=None)]
    monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake)


@pytest.fixture
def auth() -> dict[str, str]:
    return principal_headers(secret=SECRET, role="tenant_admin")


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test", timeout=10.0) as c:
        yield c


async def _seed_tasks(client, *, tenant_admin_headers, n: int, prefix: str,
                      storage_id: int = 1):
    out: list[str] = []
    for i in range(n):
        r = await client.post("/api/v1/tasks", json={
            "repo_id": f"o/{prefix}-{i}", "revision": "0" * 40,
            "storage_id": storage_id,
        }, headers=tenant_admin_headers)
        assert r.status_code == 201, r.text
        out.append(r.json()["id"])
    return out


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
async def test_tasks_list_stream_unauthenticated_401(
    client: AsyncClient,
) -> None:
    async with client.stream("GET", "/api/v1/tasks/stream") as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_tasks_list_stream_tenant_isolation(
    client: AsyncClient, auth,
) -> None:
    t1_ids = await _seed_tasks(client, tenant_admin_headers=auth,
                                n=2, prefix="t1")
    other = principal_headers(secret=SECRET, role="tenant_admin",
                              user_id=2, tenant_id=2)
    # Pre-review IMPORTANT 1 fix: use tenant-2's storage so the seeded task
    # doesn't leak across tenants (current backend doesn't validate storage
    # ownership but the test should be semantically correct).
    await _seed_tasks(client, tenant_admin_headers=other,
                       n=1, prefix="t2", storage_id=2)
    received = await _collect(
        client, "/api/v1/tasks/stream?max_ticks=1", auth,
        count=1, timeout=3.0)
    body = json.loads(received[0])
    ids = {it["id"] for it in body["items"]}
    assert set(t1_ids) <= ids
    # No t2 task in the tenant-1 snapshot.
    assert body["total"] == len(t1_ids)


@pytest.mark.slow
async def test_tasks_list_stream_multi_snapshot(
    client: AsyncClient, auth,
) -> None:
    received = await _collect(
        client, "/api/v1/tasks/stream?max_ticks=2", auth,
        count=2, timeout=3.0)
    assert len(received) >= 2
    for raw in received[:2]:
        body = json.loads(raw)
        assert "items" in body and "total" in body
        assert isinstance(body["items"], list)
        assert isinstance(body["total"], int)


@pytest.mark.slow
async def test_tasks_list_stream_shape_is_slim_no_subtasks(
    client: AsyncClient, auth,
) -> None:
    await _seed_tasks(client, tenant_admin_headers=auth,
                       n=1, prefix="slim")
    received = await _collect(
        client, "/api/v1/tasks/stream?max_ticks=1", auth,
        count=1, timeout=3.0)
    body = json.loads(received[0])
    assert body["items"], "expected at least one task in the snapshot"
    for item in body["items"]:
        assert "subtasks" not in item, "list shape must stay slim (no subtasks)"
        assert "id" in item and "status" in item and "repo_id" in item
```

- [ ] **Step 2**: Run to verify it fails.

`uv run pytest tests/api/test_tasks_list_stream.py -v` → 4 FAIL (route 404).

- [ ] **Step 3**: Create the route. `src/dlw/api/tasks_list_stream.py`:

```python
"""GET /api/v1/tasks/stream — SSE tenant-scoped tasks-list stream (UI-SP5c).

Hand-rolled text/event-stream via StreamingResponse; reuses the existing
list_tasks aggregation logic (tenant_filtered select + total). Same idiom
as tasks_stream.py / executors_stream.py.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.models.task import DownloadTask
from dlw.db.session import get_engine
from dlw.db.tenant_scope import tenant_filtered
from dlw.schemas.task import TaskList, TaskRead

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

_KEEPALIVE_EVERY_TICKS = 6  # vestigial (cf. SP5/SP5b); kept for parity


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "tasks_list_stream_interval_seconds", 5.0))
    return max(0.5, min(60.0, raw))


@router.get("/stream")
async def stream_tasks_list(
    request: Request,
    max_ticks: int | None = Query(default=None, ge=1, le=10000),
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
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
                    rows = (await s.execute(
                        tenant_filtered(
                            select(DownloadTask), DownloadTask, principal)
                        .order_by(DownloadTask.created_at.desc())
                    )).scalars().all()
                    total = await s.scalar(
                        tenant_filtered(
                            select(func.count()).select_from(DownloadTask),
                            DownloadTask, principal))
                payload = TaskList(
                    items=[TaskRead.model_validate(r) for r in rows],
                    total=int(total or 0))
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

- [ ] **Step 4**: Register router in `src/dlw/main.py` — **MUST go BEFORE `tasks_router`** (pre-review BLOCKER 1).

**Background**: SP5/SP5b registered their stream routers at the end (after all REST routers) and that was fine because their prefixes (`/api/v1/tasks` for SP5's `/{task_id}/stream`, `/api/v1/executors` for SP5b's `""`) didn't expose any route-collision risk. SP5c does: the new `/api/v1/tasks/stream` shares the `tasks_router` prefix AND is at the same level as `/{task_id}`. FastAPI iterates registered routes in include order; if `tasks_router` is registered first, `GET /api/v1/tasks/stream` is matched against `/{task_id}` first → Pydantic tries to parse `"stream"` as a UUID → 422. The fix is to register the SP5c router BEFORE `tasks_router` so its static `/stream` wins.

In `src/dlw/main.py`, find this block inside `create_app()`:

```python
    from dlw.api.tasks import router as tasks_router
    app.include_router(tasks_router)
```

Insert the SP5c router **immediately above** it:

```python
    # SP5c MUST be registered BEFORE tasks_router so the static `/stream`
    # path wins over `/{task_id}` (FastAPI iterates routers in include order).
    from dlw.api.tasks_list_stream import router as tasks_list_stream_router
    app.include_router(tasks_list_stream_router)
    from dlw.api.tasks import router as tasks_router
    app.include_router(tasks_router)
```

(I.e. the existing `from dlw.api.tasks import …` + `app.include_router(tasks_router)` lines stay; the two new lines go right above them.)

- [ ] **Step 5**: Run → PASS.

`uv run pytest tests/api/test_tasks_list_stream.py -v` → 4 PASS.

- [ ] **Step 6**: Commit.

```bash
cd /d/download_weights && git add src/dlw/api/tasks_list_stream.py src/dlw/main.py tests/api/test_tasks_list_stream.py && git commit -q -m "UI-SP5c M1: GET /tasks/stream SSE endpoint (reuses list_tasks aggregation)"
```

---

### Task 3: M1 gate

- [ ] `uv run pytest tests/ -q` → baseline (455 prior + 4 new = ~459); 0 failures.
- [ ] `npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error` → 0 errors.
- [ ] `npx --yes @apidevtools/swagger-cli validate api/openapi.yaml` → valid.
- [ ] `python tools/lint_invariants.py` → OK.
- [ ] `python tools/lint_no_direct_status_write.py` → OK.

---

# Milestone M2 — Frontend cutover

### Task 4: `useTaskList` opts in

- [ ] **Step 1**: Replace `frontend/src/composables/useTaskList.ts`:

```ts
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { TaskListResponse } from '@/api/types'

export function useTaskList() {
  return useLiveResource<TaskListResponse>(
    ['tasks'],
    async () => (await client.get<TaskListResponse>('/api/v1/tasks')).data,
    {
      baseIntervalMs: 5_000,
      staleTime: 5_000,
      streamUrl: '/api/v1/tasks/stream',
      applyEvent: (_prev, ev) => JSON.parse(ev.data) as TaskListResponse,
    },
  )
}
```

- [ ] **Step 2**: Create `frontend/tests/unit/useTaskListStream.spec.ts`:

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

import { useTaskList } from '@/composables/useTaskList'

describe('useTaskList SSE opt-in (SP5c)', () => {
  test('passes streamUrl + applyEvent to the seam', () => {
    captured.length = 0
    useTaskList()
    const last = captured[captured.length - 1]
    expect(last?.opts.streamUrl).toBe('/api/v1/tasks/stream')
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(5_000)
    expect(last?.opts.staleTime).toBe(5_000)
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useTaskList()
    const apply = (captured[captured.length - 1]?.opts.applyEvent) as
      (prev: unknown, ev: { data: string }) => { items: unknown[]; total: number }
    const out = apply(undefined, {
      data: '{"items":[{"id":"x"}],"total":1}',
    })
    expect(out.items).toEqual([{ id: 'x' }])
    expect(out.total).toBe(1)
  })
})
```

- [ ] **Step 3**: Verify existing tests still pass (view-free proof).

```bash
cd /d/download_weights/frontend
pnpm test:unit -- useTaskList useTaskListStream Dashboard TaskList
```
Expected: all PASS — existing `useTaskList.spec.ts` (if any), `Dashboard.spec.ts`, `TaskList`-using specs stay green unchanged.

- [ ] **Step 4**: Full frontend gate.

```bash
cd /d/download_weights/frontend && pnpm test:unit && pnpm typecheck && pnpm lint:fix && pnpm lint && pnpm build
```

- [ ] **Step 5**: Commit.

```bash
cd /d/download_weights && git add frontend/src/composables/useTaskList.ts frontend/tests/unit/useTaskListStream.spec.ts && git commit -q -m "UI-SP5c M2: useTaskList opts in to SSE (view-free; TaskList.vue + Dashboard.vue unchanged)"
```

---

# Milestone M3 — Smoke + docs

### Task 5: Headed Playwright smoke + docs

- [ ] **Step 1**: Restart the ephemeral `:8011` controller with SP5c code (per the SP5/SP5b recipe). Restart Vite with `DLW_API_PROXY=http://localhost:8011`. Mint a fresh tenant JWT to `.run/sp5c-token.txt`.

- [ ] **Step 2**: Direct live check.

```bash
TOK=$(cat .run/sp5c-token.txt)
curl -s -m 1 -H "Authorization: Bearer $TOK" \
  "http://localhost:8011/api/v1/tasks/stream?max_ticks=1" | head -c 300
```
Expected: `:open\n\ndata: {"items":[...],"total":N}\n\n`.

- [ ] **Step 3**: Create `.run/pw/sp5c-smoke.mjs`:

```js
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'

const TOKEN = readFileSync('.run/sp5c-token.txt', 'utf8').trim()
const VITE = process.env.SP5C_VITE ?? 'http://localhost:5173'
const errors = []
const b = await chromium.launch({ headless: false })
const pg = await b.newPage()
pg.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
pg.on('pageerror', (e) => errors.push(String(e)))

const sseReqs = []
pg.on('request', (r) => {
  // Match exactly the tasks-list stream, not the per-task stream.
  if (/\/api\/v1\/tasks\/stream(\?|$)/.test(r.url())) sseReqs.push(r.url())
})

await pg.goto(`${VITE}/login`)
await pg.fill('input', TOKEN)
await pg.click('button[type="submit"]')
await pg.waitForURL('**/')
await pg.goto(`${VITE}/tasks`)
await pg.waitForSelector('table, .empty-state', { timeout: 10_000 })
await pg.screenshot({ path: '.run/pw/sp5c-tasks.png' })
await pg.waitForTimeout(4000)
await pg.screenshot({ path: '.run/pw/sp5c-after-stream.png' })

await b.close()
const real = errors.filter((e) =>
  !e.includes('/health/active') && !e.includes('Failed to load resource'))
if (real.length) {
  console.log('SP5c smoke: real errors:\n' + real.join('\n'))
  process.exit(1)
}
if (sseReqs.length === 0) {
  console.log('SP5c smoke: no /tasks/stream SSE request observed')
  process.exit(1)
}
console.log(`SP5c smoke OK — observed ${sseReqs.length} /tasks/stream request(s)`)
sseReqs.forEach((u) => console.log('  -> ' + u))
```

- [ ] **Step 4**: Run.

```bash
SP5C_VITE=http://localhost:5173 node .run/pw/sp5c-smoke.mjs
```

Expected: `SP5c smoke OK — observed ≥1 /tasks/stream request(s)`.

- [ ] **Step 5**: Append to `docs/operator/web-ui.md` (under the SP5b section):

```markdown

### UI-SP5c — Tasks-list SSE follow-on

Third application of the view-free SSE template (after SP5 task-detail, SP5b
executors). The Tasks landing page (`useTaskList`, consumed by both
`TaskList.vue` and the Dashboard "recent tasks" widget) now talks SSE via
`GET /api/v1/tasks/stream` (5 s default tick; `DLW_TASKS_LIST_STREAM_INTERVAL_SECONDS`
overrides; clamped `[0.5, 60.0]`). The page and composable consumers are
unchanged; SP1's existing tests pass unmodified.

Tenant filtering reuses the existing `list_tasks` aggregation
(`tenant_filtered(select(DownloadTask))`) — same RBAC and slim-shape
(`{items: TaskRead[], total: int}`, no subtasks) as the `GET /api/v1/tasks`
endpoint.
```

- [ ] **Step 6**: Commit.

```bash
cd /d/download_weights && git add docs/operator/web-ui.md && git commit -q -m "UI-SP5c M3: operator docs for the tasks-list SSE follow-on"
```

---

## Self-Review

**1. Spec coverage:** §3 backend endpoint (reuse list_tasks aggregation) + RBAC reuse + OpenAPI + config + 4 tests → Tasks 1-2. ✓ §4 frontend opt-in + new spec + view-free proof → Task 4. ✓ §5 M1/M2/M3 with gates → Tasks 3, 5. ✓

**2. Placeholder scan:** No "TBD"; every step is complete code.

**3. Type consistency:**
- Backend reuses `TaskList` (`schemas/task.py`) with `items: list[TaskRead]` and `total: int`. Frontend `TaskListResponse` (`api/types.ts`) matches. ✓
- `streamUrl: '/api/v1/tasks/stream'` is a plain string (assignable to `string | Ref<string>` per the SP5 seam). ✓
- `applyEvent: (_prev, ev) => JSON.parse(ev.data) as TaskListResponse` — same shape. ✓
- 5 s default + clamp `[0.5, 60.0]` matches `useTaskList`'s `baseIntervalMs: 5_000`. ✓

**4. View-free proof chain:**
- `frontend/src/pages/TaskList.vue` — NOT in any modify list.
- `frontend/src/pages/Dashboard.vue` — NOT in any modify list (also consumes `useTaskList`).
- The only files modified are `useTaskList.ts` (additive opt-in; signature/return unchanged) and the new test file.
