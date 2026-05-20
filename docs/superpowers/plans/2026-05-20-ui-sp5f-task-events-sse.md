# UI-SP5f — Task Events SSE Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** Add `GET /api/v1/tasks/{task_id}/events/stream` + opt `useTaskEvents` in to the SP5 seam. 6th application of the view-free SSE template; first SP2 sub-resource composable to graduate from polling to SSE.

**Architecture:** Backend mirrors `audit_stream.py` (page-1 stream over a cursor-paginated source); reuses SP2's `events_for_task` service; pre-stream tenant gate (5-line `tenant_filtered(select(DownloadTask.id)…)` block copied verbatim from `tasks_stream.py`) → 404 cross-tenant before `:open`. Frontend `useTaskEvents` opts in with a `computed(() => \`/api/v1/tasks/${taskId.value}/events/stream\`)` reactive `streamUrl`. `TaskDetail.vue` page and SP2 event-tab component UNCHANGED; `fetchOlderEvents` "Load older" function unchanged. **`useLiveResource` seam evolved**: `streaming` becomes a `computed` (was a `const` evaluated once at call-time) so that an `enabled: Ref<boolean>` starting `false` can later flip true and open the SSE — required because `useTaskEvents` is the first SSE consumer whose `enabled` starts false (Events tab inactive at TaskDetail mount).

---

## Conventions (apply to every task — same as SP5e)

- **Branch:** `feat/ui-sp5f-task-events-sse` (off `main` @ `e82be75`, already created).
- **Bash cwd persists**. Always `cd /d/download_weights && git …` for git; `cd /d/download_weights/frontend && pnpm …` for frontend.
- **Tenant gate**: copy the 5-line `tenant_filtered(select(DownloadTask.id).where(DownloadTask.id == task_id), DownloadTask, principal)` pattern from `tasks_stream.py:62-66` — pre-stream 404. Do NOT import `tasks.py:_task_in_tenant` (private helper; inline copy is the established SP5* style).
- **`?max_ticks=N`**: same SP5/SP5b/SP5c/SP5d/SP5e testability hatch.
- **Route placement**: register `tasks_events_stream_router` BEFORE the existing `tasks_router` in `main.py` for defensive consistency with SP5c/SP5d/SP5e (the new path `/{task_id}/events/stream` has distinct depth from `/{task_id}/events` — no actual collision risk — but the pattern is now established).
- **No dead keepalive block** (SP5e learning point #38). The `_KEEPALIVE_EVERY_TICKS` reset-then-increment in prior SP5* files is unreachable; SP5f omits it entirely (matching SP5e).

---

## File Structure

**Backend (create):**
- `src/dlw/api/tasks_events_stream.py` — `GET /api/v1/tasks/{task_id}/events/stream` route.
- `tests/api/test_task_events_stream.py` — 4 tests.

**Backend (modify):**
- `api/openapi.yaml` — add `/tasks/{taskId}/events/stream` path block at line 531 (right after the `/tasks/{taskId}/events` GET block closes, before `/tasks/stream`).
- `src/dlw/main.py` — register router BEFORE `tasks_router`. Use the SP5c-block include pattern: place new include immediately after the existing `tasks_list_stream_router` include (line 298-299) so the include order is `tasks_list_stream → tasks_events_stream → tasks`.
- `src/dlw/config.py` — add `task_events_stream_interval_seconds` setting.

**Frontend (modify):**
- `frontend/src/composables/useLiveResource.ts` — **seam fix**: convert `streaming` from a `const` to a `computed`; watch BOTH `q.data.value` and `streaming.value` before opening SSE (so `enabled: Ref<false>` at mount can later flip true and lazy-open the stream). Required by SP5f's `useTaskEvents` (1st SSE consumer with `enabled` starting false). Existing 5 SP5* consumers unaffected because their `streaming.value` is `true` from start.
- `frontend/src/composables/useTaskEvents.ts` — opt-in: add reactive `streamUrl` + `applyEvent`.

**Frontend (create):**
- `frontend/tests/unit/useLiveResourceEnabledSse.spec.ts` — **seam regression test**: assert that an SSE consumer with `enabled: ref(false)` initially does NOT call streamSse; when `enabled` flips true and useQuery data arrives, streamSse IS called.
- `frontend/tests/unit/useTaskEventsStream.spec.ts` — new composable spec (mirror `useAuditLogStream.spec.ts`).

**Docs (modify):** `docs/operator/web-ui.md` (append SP5f section).

---

# Milestone M1 — Backend

### Task 1: OpenAPI + config

**Files:**
- Modify: `api/openapi.yaml:531` (after `/tasks/{taskId}/events` block, before `/tasks/stream`)
- Modify: `src/dlw/config.py:52` (after `quota_stream_interval_seconds`)

- [ ] **Step 1**: In `api/openapi.yaml`, find the existing `/tasks/{taskId}/events:` block (anchor: line ~509, ends at the `next_cursor` property close ~line 530). Insert `/tasks/{taskId}/events/stream:` block on the blank line immediately before `/tasks/stream:` (anchor: `/tasks/stream:` is the next path block after `/tasks/{taskId}/events`):

```yaml

  /tasks/{taskId}/events/stream:
    parameters:
      - $ref: '#/components/parameters/TaskId'
    get:
      tags: [tasks]
      summary: Live task-events SSE stream (UI-SP5f)
      operationId: streamTaskEvents
      responses:
        '200':
          description: SSE stream of TaskEventsResponse snapshots
          content:
            text/event-stream:
              schema:
                type: string
                description: |
                  Each event is `data: <TaskEventsResponse JSON>\n\n`
                  ({items, next_cursor}). Live feed always pushes
                  page 1 (newest first); the client's "Load older"
                  button still uses the one-shot
                  /tasks/{taskId}/events?cursor=… endpoint. Stream
                  terminates only on client disconnect or shutdown.
        '401': {$ref: '#/components/responses/Unauthenticated'}
        '404':
          description: Task not found (or not in caller's tenant)
```

- [ ] **Step 2**: In `src/dlw/config.py`, after the `quota_stream_interval_seconds` field, add:

```python
    # UI-SP5f — SSE tick rate for /tasks/{id}/events/stream (clamped at runtime).
    task_events_stream_interval_seconds: float = Field(default=5.0)
```

- [ ] **Step 3**: Commit.

```bash
git add api/openapi.yaml src/dlw/config.py
git commit -q -m "UI-SP5f M1: openapi /tasks/{id}/events/stream path + task_events_stream_interval_seconds setting"
```

---

### Task 2: SSE endpoint

**Files:**
- Create: `src/dlw/api/tasks_events_stream.py`
- Modify: `src/dlw/main.py` (register router AFTER `tasks_list_stream_router` and BEFORE `tasks_router`)

- [ ] **Step 1**: Create `src/dlw/api/tasks_events_stream.py`:

```python
"""GET /api/v1/tasks/{task_id}/events/stream — SSE task-events live page-1 stream (UI-SP5f).

Hand-rolled text/event-stream; reuses SP2's events_for_task service.
Stream always passes cursor=None (live = page 1); the client's "Load older"
button keeps using the one-shot /tasks/{task_id}/events?cursor=… endpoint.
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
from dlw.schemas.task_detail import TaskEventsResponse
from dlw.services import task_detail as _td

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "task_events_stream_interval_seconds", 5.0))
    return max(0.5, min(60.0, raw))


@router.get("/{task_id}/events/stream")
async def stream_task_events(
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
                    items, next_cursor = await _td.events_for_task(
                        s, task_id, principal.tenant_id,
                        limit=50, cursor=None)
                payload = TaskEventsResponse(
                    items=items, next_cursor=next_cursor)
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

- [ ] **Step 2**: Modify `src/dlw/main.py` — find the existing block at lines 298-301:

```python
    # SP5c MUST be registered BEFORE tasks_router so the static `/stream`
    # path wins over `/{task_id}` (FastAPI iterates routers in include order).
    from dlw.api.tasks_list_stream import router as tasks_list_stream_router
    app.include_router(tasks_list_stream_router)
    from dlw.api.tasks import router as tasks_router
    app.include_router(tasks_router)
```

**INSERT** the SP5f include between `tasks_list_stream_router` and `tasks_router` (so the final order is `tasks_list_stream → tasks_events_stream → tasks`). Replace the existing block with:

```python
    # SP5c MUST be registered BEFORE tasks_router so the static `/stream`
    # path wins over `/{task_id}` (FastAPI iterates routers in include order).
    from dlw.api.tasks_list_stream import router as tasks_list_stream_router
    app.include_router(tasks_list_stream_router)
    # SP5f: tasks-events stream router included BEFORE tasks_router for
    # defensive consistency with SP5c/SP5d/SP5e lesson. The new path
    # `/{task_id}/events/stream` has distinct depth from any existing
    # `/{task_id}/*` route, so no actual collision — pattern only.
    from dlw.api.tasks_events_stream import router as tasks_events_stream_router
    app.include_router(tasks_events_stream_router)
    from dlw.api.tasks import router as tasks_router
    app.include_router(tasks_router)
```

- [ ] **Step 3**: Sanity: import + route smoke.

```bash
cd /d/download_weights && uv run python -c "from dlw.api.tasks_events_stream import router; [print(r.path) for r in router.routes]"
```

Expected: one route with path `/api/v1/tasks/{task_id}/events/stream`.

- [ ] **Step 4**: Commit.

```bash
git add src/dlw/api/tasks_events_stream.py src/dlw/main.py
git commit -q -m "UI-SP5f M1: GET /tasks/{id}/events/stream SSE endpoint (reuses events_for_task)"
```

---

### Task 3: Backend tests

**Files:**
- Create: `tests/api/test_task_events_stream.py`

- [ ] **Step 1**: Create `tests/api/test_task_events_stream.py`:

```python
"""Tests for GET /api/v1/tasks/{task_id}/events/stream (UI-SP5f SSE)."""
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
    monkeypatch.setenv("DLW_TASK_EVENTS_STREAM_INTERVAL_SECONDS", TICK)
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


async def _seed_audit(engine, *, tenant_id: int, task_id: uuid.UUID,
                       n: int, action: str = "task.note"):
    from dlw.db.models.audit import AuditLog
    base = dt.datetime(2026, 5, 20, 12, 0, 0, tzinfo=dt.UTC)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        for i in range(n):
            s.add(AuditLog(
                occurred_at=base + dt.timedelta(seconds=i),
                tenant_id=tenant_id, actor_user_id=1, action=action,
                resource_type="task", resource_id=str(task_id),
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
async def test_task_events_stream_unauthenticated_401(
    client: AsyncClient,
) -> None:
    async with client.stream(
        "GET", f"/api/v1/tasks/{TASK_T1}/events/stream",
    ) as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_task_events_stream_cross_tenant_404(
    client: AsyncClient, auth,
) -> None:
    # Tenant 1 JWT, but TASK_T2 belongs to tenant 2 — pre-stream 404.
    async with client.stream(
        "GET",
        f"/api/v1/tasks/{TASK_T2}/events/stream?max_ticks=1",
        headers=auth,
    ) as resp:
        assert resp.status_code == 404


@pytest.mark.slow
async def test_task_events_stream_single_snapshot(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed_audit(engine, tenant_id=1, task_id=TASK_T1, n=3,
                       action="task.created")
    received = await _collect(
        client,
        f"/api/v1/tasks/{TASK_T1}/events/stream?max_ticks=1",
        auth, count=1, timeout=3.0)
    body = json.loads(received[0])
    assert "items" in body
    assert "next_cursor" in body
    assert len(body["items"]) >= 1
    # TaskEvent schema fields: ts, type, message, details (resource_type
    # is on the AuditLog model but events_for_task drops it in the mapping).
    for it in body["items"]:
        assert {"ts", "type", "message", "details"} <= set(it.keys())


@pytest.mark.slow
async def test_task_events_stream_multi_snapshot(
    client: AsyncClient, auth, engine,
) -> None:
    await _seed_audit(engine, tenant_id=1, task_id=TASK_T1, n=2,
                       action="task.note")
    received = await _collect(
        client,
        f"/api/v1/tasks/{TASK_T1}/events/stream?max_ticks=2",
        auth, count=2, timeout=3.0)
    assert len(received) >= 2
    for raw in received[:2]:
        body = json.loads(raw)
        assert "items" in body
        assert "next_cursor" in body
        assert isinstance(body["items"], list)
```

- [ ] **Step 2**: Run the new tests:

```bash
cd /d/download_weights && uv run pytest tests/api/test_task_events_stream.py -q 2>&1 | tail -8
```

Expected: 4 passed.

- [ ] **Step 3**: Commit.

```bash
git add tests/api/test_task_events_stream.py
git commit -q -m "UI-SP5f M1: backend SSE endpoint tests (unauth-401, cross-tenant-404, single/multi snapshot)"
```

---

### M1 Full backend gate

- [ ] **Step 1**: Run full backend pytest:

```bash
cd /d/download_weights && uv run pytest -q 2>&1 | tail -3
```

Expected: SP1-SP5e tests + new SP5f tests all pass. The 2 known Windows-local `test_failover_drill.py` flakes may appear; CI is the arbiter.

---

# Milestone M2 — Frontend cutover

### Task 4: Seam fix — reactive `streaming` so `enabled` starting false can later open SSE

**Files:**
- Modify: `frontend/src/composables/useLiveResource.ts`
- Create: `frontend/tests/unit/useLiveResourceEnabledSse.spec.ts`

- [ ] **Step 1**: Modify `frontend/src/composables/useLiveResource.ts`. Current shape (lines ~43-49 + 76-120) evaluates `const streaming = shouldStream(...)` once at call-time and gates the SSE branch on it. **Replace** with a reactive `computed` + watchers that lazy-open the SSE once both `streaming.value` and `q.data.value` are ready.

Full new file:

```ts
import { onScopeDispose, ref, computed, toValue, watch, type MaybeRefOrGetter, type Ref, type WatchStopHandle } from 'vue'
import { useQuery, useQueryClient, type QueryKey } from '@tanstack/vue-query'
import { shouldStream, streamSse, type SseEvent } from '@/api/sse'
import { useAuthStore } from '@/stores/auth'

const ERROR_BACKOFF_MS = 5_000
const HIDDEN_MULTIPLIER = 3

export function computeInterval(o: {
  base: number; terminal: boolean; hidden: boolean; errored: boolean
}): number | false {
  if (o.terminal) return false
  if (o.errored) return ERROR_BACKOFF_MS
  return o.hidden ? o.base * HIDDEN_MULTIPLIER : o.base
}

export interface LiveOptions<T> {
  baseIntervalMs: number
  isTerminal?: (data: T) => boolean
  staleTime?: number
  enabled?: Ref<boolean> | boolean
  /** UI-SP5: opt in to SSE. SP5f: streaming is now reactive — an
   * enabled Ref that starts false will lazy-open the SSE on first
   * enabled === true (after useQuery has produced data). */
  streamUrl?: string | Ref<string>
  applyEvent?: (prev: T | undefined, ev: SseEvent) => T
}

export function useLiveResource<T>(
  key: QueryKey,
  fetcher: () => Promise<T>,
  opts: LiveOptions<T>,
) {
  // SP5f: reactive streaming gate. Was a `const` (evaluated once at
  // call-time) in SP5-SP5e because no consumer passed an `enabled`
  // Ref that started false. `useTaskEvents` (SP5f) is the first; the
  // computed makes streamUrl lazy-open when enabled flips true.
  const streaming = computed(() => shouldStream({
    streamUrl: opts.streamUrl,
    applyEvent: opts.applyEvent as
      ((prev: unknown, ev: SseEvent) => unknown) | undefined,
    enabled: opts.enabled,
  }))

  const pollingFallback = ref(false)

  const q = useQuery<T>({
    queryKey: key,
    queryFn: fetcher,
    enabled: opts.enabled,
    staleTime: opts.staleTime ?? 0,
    refetchInterval: (query) => {
      const data = query.state.data as T | undefined
      const errored = query.state.status === 'error'
      const terminal = data !== undefined && !!opts.isTerminal?.(data)
      const hidden = typeof document !== 'undefined'
        && document.visibilityState === 'hidden'
      if (streaming.value && !pollingFallback.value) {
        return false
      }
      return computeInterval({
        base: opts.baseIntervalMs, terminal, hidden, errored,
      })
    },
  })

  if (opts.streamUrl && opts.applyEvent) {
    const qc = useQueryClient()
    const ac = new AbortController()
    const auth = useAuthStore()
    const apply = opts.applyEvent
    let started = false
    let stopDataWatch: WatchStopHandle | undefined
    let stopStreamingWatch: WatchStopHandle | undefined

    const tryStart = () => {
      if (started) return
      if (!streaming.value) return
      if (q.data.value === undefined) return
      started = true
      stopDataWatch?.()
      stopStreamingWatch?.()
      const url = toValue(opts.streamUrl as MaybeRefOrGetter<string>)
      void streamSse({
        url, token: auth.accessToken, signal: ac.signal,
        onEvent: (ev) => {
          const prev = qc.getQueryData<T>(key)
          const next = apply(prev, ev)
          qc.setQueryData(key, next)
        },
        onUnauthorized: () => {
          auth.logout()
        },
      }).then(() => {
        // streamSse resolved without abort → it gave up (3 consecutive
        // failures). Fall back to polling.
        pollingFallback.value = true
        void q.refetch()
      }).catch(() => {
        // 401 path — onUnauthorized already invoked.
      })
    }

    stopDataWatch = watch(() => q.data.value, tryStart, { immediate: true })
    stopStreamingWatch = watch(streaming, tryStart)
    onScopeDispose(() => { ac.abort() })
  }

  return q
}
```

- [ ] **Step 2**: Create `frontend/tests/unit/useLiveResourceEnabledSse.spec.ts`:

```ts
import { describe, expect, test, vi } from 'vitest'
import { ref, nextTick, defineComponent } from 'vue'
import { mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia, setActivePinia } from 'pinia'

const { streamSseMock } = vi.hoisted(() => ({
  streamSseMock: vi.fn(() => new Promise(() => {})),
}))
vi.mock('@/api/sse', async () => {
  const actual = await vi.importActual<typeof import('@/api/sse')>('@/api/sse')
  return { ...actual, streamSse: streamSseMock }
})

import { useLiveResource } from '@/composables/useLiveResource'

function mountWith(enabled: ReturnType<typeof ref<boolean>>) {
  setActivePinia(createPinia())
  const Comp = defineComponent({
    setup() {
      const q = useLiveResource<{ v: number }>(
        ['k'],
        async () => ({ v: 1 }),
        {
          baseIntervalMs: 5_000,
          enabled,
          streamUrl: '/api/v1/stream',
          applyEvent: (_p, ev) => JSON.parse(ev.data),
        },
      )
      return { q }
    },
    template: '<div>{{ q.data?.v }}</div>',
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return mount(Comp, {
    global: { plugins: [[VueQueryPlugin, { queryClient: qc }]] },
  })
}

describe('useLiveResource seam — enabled flips true (SP5f regression)', () => {
  test('enabled=false at mount: streamSse NOT called', async () => {
    streamSseMock.mockClear()
    const enabled = ref(false)
    const w = mountWith(enabled)
    await nextTick()
    await new Promise((r) => setTimeout(r, 50))
    expect(streamSseMock).not.toHaveBeenCalled()
    w.unmount()
  })
  test('enabled flips false→true: streamSse called once data arrives', async () => {
    streamSseMock.mockClear()
    const enabled = ref(false)
    const w = mountWith(enabled)
    await nextTick()
    enabled.value = true
    // Allow useQuery to fetch + the watcher to fire.
    for (let i = 0; i < 20 && streamSseMock.mock.calls.length === 0; i++) {
      await new Promise((r) => setTimeout(r, 20))
    }
    expect(streamSseMock).toHaveBeenCalledTimes(1)
    expect(streamSseMock.mock.calls[0]?.[0]?.url).toBe('/api/v1/stream')
    w.unmount()
  })
  test('enabled=true at mount: streamSse called once data arrives (no regression)', async () => {
    streamSseMock.mockClear()
    const enabled = ref(true)
    const w = mountWith(enabled)
    for (let i = 0; i < 20 && streamSseMock.mock.calls.length === 0; i++) {
      await new Promise((r) => setTimeout(r, 20))
    }
    expect(streamSseMock).toHaveBeenCalledTimes(1)
    w.unmount()
  })
})
```

- [ ] **Step 3**: Run the new seam test + the existing seam tests:

```bash
cd /d/download_weights/frontend && pnpm vitest run tests/unit/useLiveResource 2>&1 | tail -10
```

Expected: all `useLiveResource*` tests pass (the new one + any pre-existing).

- [ ] **Step 4**: Commit.

```bash
git add frontend/src/composables/useLiveResource.ts frontend/tests/unit/useLiveResourceEnabledSse.spec.ts
git commit -q -m "UI-SP5f M2: seam — make streaming reactive (lazy SSE on enabled flip true); regression test for SP5-SP5e + new SP5f path"
```

---

### Task 5: `useTaskEvents` opts in to SSE seam

**Files:**
- Modify: `frontend/src/composables/useTaskEvents.ts`
- Create: `frontend/tests/unit/useTaskEventsStream.spec.ts`

- [ ] **Step 1**: Modify `frontend/src/composables/useTaskEvents.ts` (full new file):

```ts
import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { TaskEventsResponse } from '@/api/types'

export function useTaskEvents(
  taskId: Ref<string>, enabled: Ref<boolean>, terminal: Ref<boolean>,
) {
  const streamUrl = computed(
    () => `/api/v1/tasks/${taskId.value}/events/stream`)
  return useLiveResource<TaskEventsResponse>(
    ['task-events', taskId],
    async () => (await client.get<TaskEventsResponse>(
      `/api/v1/tasks/${taskId.value}/events?limit=50`)).data,
    {
      baseIntervalMs: 5_000,
      enabled,
      isTerminal: () => terminal.value,
      streamUrl,
      applyEvent: (_prev, ev) =>
        JSON.parse(ev.data) as TaskEventsResponse,
    },
  )
}

/** One-shot "load older" page (not live; appended in the page). */
export async function fetchOlderEvents(
  taskId: string, cursor: string,
): Promise<TaskEventsResponse> {
  return (await client.get<TaskEventsResponse>(
    `/api/v1/tasks/${taskId}/events?limit=50&cursor=${encodeURIComponent(cursor)}`,
  )).data
}
```

- [ ] **Step 2**: Create `frontend/tests/unit/useTaskEventsStream.spec.ts`:

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

import { useTaskEvents } from '@/composables/useTaskEvents'

describe('useTaskEvents SSE opt-in (SP5f)', () => {
  test('passes streamUrl + applyEvent + enabled + isTerminal to the seam', () => {
    captured.length = 0
    const taskId = ref('aaa')
    const enabled = ref(true)
    const terminal = ref(false)
    useTaskEvents(taskId, enabled, terminal)
    const last = captured[captured.length - 1]
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/aaa/events/stream')
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(5_000)
    expect(last?.opts.enabled).toBe(enabled)
    expect(typeof last?.opts.isTerminal).toBe('function')
  })
  test('streamUrl is reactive to taskId ref', () => {
    captured.length = 0
    const taskId = ref('bbb')
    useTaskEvents(taskId, ref(true), ref(false))
    const last = captured[captured.length - 1]
    const url0 = (last?.opts.streamUrl as { value: string }).value
    expect(url0).toBe('/api/v1/tasks/bbb/events/stream')
    taskId.value = 'ccc'
    const url1 = (last?.opts.streamUrl as { value: string }).value
    expect(url1).toBe('/api/v1/tasks/ccc/events/stream')
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useTaskEvents(ref('xxx'), ref(true), ref(false))
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

- [ ] **Step 3**: Run full frontend gate:

```bash
cd /d/download_weights/frontend && pnpm lint && pnpm exec vue-tsc --noEmit && pnpm vitest run 2>&1 | tail -10
```

Expected: lint clean, typecheck clean, all tests pass (155 + 3 new = 158).

- [ ] **Step 4**: Build proof:

```bash
cd /d/download_weights/frontend && pnpm build 2>&1 | tail -3
```

Expected: built successfully.

- [ ] **Step 5**: Commit.

```bash
git add frontend/src/composables/useTaskEvents.ts frontend/tests/unit/useTaskEventsStream.spec.ts
git commit -q -m "UI-SP5f M2: useTaskEvents opts in to SSE (view-free; TaskDetail.vue + SP2 event-tab untouched)"
```

---

# Milestone M3 — Smoke + docs

### Task 6: headed Playwright smoke + operator docs

**Files:**
- Create: `.run/pw/sp5f-smoke.mjs` (in gitignored `.run/`)
- Modify: `docs/operator/web-ui.md` (append SP5f section)

- [ ] **Step 1**: Restart `:8011` ephemeral controller with SP5f code; restart Vite proxying to it (kill stale Vite via `netstat -ano | grep :5173` if needed).

- [ ] **Step 2**: Smoke the endpoint with curl. Note: needs a valid task UUID owned by tenant 1 — pick any from `dlw list` or query `SELECT id FROM download_tasks WHERE tenant_id=1 LIMIT 1;`. If no task exists, create one with `uv run dlw submit org/model -r 0000000000000000000000000000000000000000 -s 1`.

```bash
cd /d/download_weights && JWT=$(cat .run/sp5e-token.txt 2>/dev/null || uv run python -c "from dlw.auth.principal import issue_system_jwt; print(issue_system_jwt(secret='dev-system-jwt-change-me', user_id=1, tenant_id=1, role='tenant_admin', project_ids=[]))" 2>/dev/null | tail -1)
TASK_ID=$(curl -s -H "Authorization: Bearer $JWT" "http://127.0.0.1:8011/api/v1/tasks?limit=1" | jq -r '.items[0].id')
echo "smoking task $TASK_ID"
curl -s -H "Authorization: Bearer $JWT" "http://127.0.0.1:8011/api/v1/tasks/$TASK_ID/events/stream?max_ticks=1" | head -c 500
```

Expected: `:open\n\ndata: {"items":[…],"next_cursor":null|"…"}\n\n`.

- [ ] **Step 3**: Save token + create `.run/pw/sp5f-smoke.mjs` (mirror `sp5d-smoke.mjs` adapted to `/tasks/<id>` route + click Events tab):

```js
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'

const TOKEN = readFileSync('.run/sp5e-token.txt', 'utf8').trim()
const VITE = process.env.SP5F_VITE ?? 'http://localhost:5173'
const errors = []
const b = await chromium.launch({ headless: false })
const pg = await b.newPage()
pg.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
pg.on('pageerror', (e) => errors.push(String(e)))

const sseReqs = []
pg.on('request', (r) => {
  if (/\/api\/v1\/tasks\/[^/]+\/events\/stream(\?|$)/.test(r.url())) {
    sseReqs.push(r.url())
  }
})

await pg.goto(`${VITE}/login`)
await pg.fill('input', TOKEN)
await pg.click('button[type="submit"]')
await pg.waitForURL('**/')
await pg.goto(`${VITE}/tasks`)
await pg.waitForSelector('table tr, .empty-state', { timeout: 10_000 })
// Click first task row → navigate to /tasks/<id>
const firstLink = await pg.$('table a')
if (!firstLink) {
  console.log('SP5f smoke: no tasks exist; submit one first via `uv run dlw submit …`')
  process.exit(1)
}
await firstLink.click()
await pg.waitForURL('**/tasks/**')
// Switch to Events tab — heuristic: the tab labels are "Files | Sources | Executors | Events"
const eventsTab = await pg.$('text=Events')
if (eventsTab) await eventsTab.click()
await pg.waitForTimeout(5000)
await pg.screenshot({ path: '.run/pw/sp5f-events.png' })

await b.close()
const real = errors.filter((e) =>
  !e.includes('/health/active') && !e.includes('Failed to load resource'))
if (real.length) {
  console.log('SP5f smoke: real errors:\n' + real.join('\n'))
  process.exit(1)
}
if (sseReqs.length === 0) {
  console.log('SP5f smoke: no /tasks/<id>/events/stream SSE request observed')
  process.exit(1)
}
console.log(`SP5f smoke OK — observed ${sseReqs.length} /tasks/<id>/events/stream request(s)`)
sseReqs.forEach((u) => console.log('  -> ' + u))
```

- [ ] **Step 4**: Run the smoke:

```bash
cd /d/download_weights && node .run/pw/sp5f-smoke.mjs 2>&1 | tail -5
```

Expected: `SP5f smoke OK — observed N /tasks/<id>/events/stream request(s)`.

- [ ] **Step 5**: Append SP5f section to `docs/operator/web-ui.md` after the SP5e section (end of file):

```markdown

### UI-SP5f — Task Events SSE follow-on

Sixth application of the view-free SSE template, and the first SP2
sub-resource composable to graduate from polling to SSE. The Events
tab on TaskDetail (`useTaskEvents`, consumed by the events panel in
`TaskDetail.vue`) now talks SSE via
`GET /api/v1/tasks/{task_id}/events/stream` (5 s default tick;
`DLW_TASK_EVENTS_STREAM_INTERVAL_SECONDS` overrides; clamped
`[0.5, 60.0]`). The view-side consumers are unchanged; the SP2
"Load older" cursor pagination (`fetchOlderEvents`) is untouched.

The stream sends page 1 only — it reuses
`events_for_task(session, task_id, tenant_id, limit=50, cursor=None)`
(SP2-introduced; already a service, no extraction needed) and wraps
the result as `TaskEventsResponse`. Older pages still come from the
one-shot `GET /api/v1/tasks/{task_id}/events?cursor=…` endpoint.

The stream URL is **reactive to the `taskId` ref**: `useTaskEvents`
derives `streamUrl` from a `computed` over the `taskId` prop, so
navigating between tasks both re-fetches and re-streams under the new
URL. The `enabled` tab-gate is preserved by the seam — the SSE only
opens when the Events tab is the active tab. Same routing precaution
as SP5c/SP5d/SP5e: `tasks_events_stream_router` is registered after
`tasks_list_stream_router` and BEFORE `tasks_router` in
`src/dlw/main.py`.

**Browser connection-cap note**: a TaskDetail page with the Events tab
active will hold two concurrent SSE connections to the controller
origin (the SP5 task-detail stream `/tasks/{id}/stream` for the header
aggregator + the new SP5f events stream `/tasks/{id}/events/stream`).
This is well within the per-origin HTTP/2 cap (≈100); HTTP/1.1 (e.g.
the Vite dev proxy) has a 6-stream cap, which is unlikely to be
exhausted in practice given tab-gated lifecycle. No mitigation
implemented; documented for awareness.
```

- [ ] **Step 6**: Commit (`.run/` is gitignored — do NOT add `sp5f-smoke.mjs`):

```bash
git add docs/operator/web-ui.md
git commit -q -m "UI-SP5f M3: operator docs — task-events SSE follow-on (1st SP2 sub-resource SSE)"
```

---

# Final cycle (controller-driven, not part of the per-task plan)

After all tasks complete:

1. Dispatch 1 opus reviewer for whole-impl review (HIGH/MEDIUM/LOW; ≤600 words).
2. Fix any HIGH; record MEDIUM/LOW in PR body.
3. `git push -u origin feat/ui-sp5f-task-events-sse`.
4. `gh pr create` against `main` with the standard summary template.
5. Background poller waits for CI all-green.
6. `gh pr merge <N> --squash --delete-branch`, then `git checkout main && git pull --ff-only`.
7. Update `reference_l17728_modelpull.md` (record SP5f merge, bump `main` commit).
8. Update `feedback_subagent_driven_dev.md` with any new learning points (likely: connection-cap concern observation, first SP2 sub-resource graduating to SSE).

---

## Self-Review

- **Spec coverage**: every section of the spec maps to a task. ✓
- **Placeholder scan**: none.
- **Type consistency**: `TaskEventsResponse` reused throughout (backend schema + frontend type). `_td.events_for_task` signature matches the existing one-shot endpoint. ✓
- **Naming**: env var `DLW_TASK_EVENTS_STREAM_INTERVAL_SECONDS`, setting `task_events_stream_interval_seconds`, function `_clamped_interval`, router `tasks_events_stream_router` — all consistent with SP5d/SP5e pattern.
- **Test coverage**: 4 backend tests + 3 frontend tests + 1 smoke = mirror SP5d (the closest prior-art).
- **Route precedence**: `tasks_events_stream_router` includes between `tasks_list_stream_router` and `tasks_router` — preserves SP5c's ordering (which is the reason SP5c works) while extending the pattern.
- **Pre-stream 404**: exact copy of SP5's `tasks_stream.py` pattern — 5-line `tenant_filtered` block. No reuse of `tasks.py:_task_in_tenant` (private; deliberately not imported).
- **`.run/` gitignore**: M3 explicitly notes do NOT commit the Playwright script.
