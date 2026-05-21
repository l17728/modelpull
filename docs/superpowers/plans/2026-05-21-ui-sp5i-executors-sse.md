# UI-SP5i — Participating-Executors SSE + Seam Close-on-Disable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** Add `GET /api/v1/tasks/{task_id}/participating-executors/stream` + opt `useParticipatingExecutors` in + **evolve the `useLiveResource` seam to close-on-disable** (abort the SSE when `streaming` flips false, reopen when true). 9th application; last SP2 sub-resource; bounds TaskDetail concurrent SSE to 2 (header + active tab).

**Architecture:** Backend mirrors `tasks_chunks_stream.py` (SP5g). The seam centerpiece replaces the permanent `started` latch with an open/close lifecycle keyed on `streaming.value`, using an `if (ac === controller)` identity guard to distinguish self-abort from giveup (streamSse resolves on both — `sse.ts:94,97`). `TaskDetail.vue` + Executors-tab components UNCHANGED; all 8 existing SSE consumers behave identically (always-on never flips false; the 3 gated consumers gain close-on-disable).

---

## Conventions (same as SP5h)

- **Branch:** `feat/ui-sp5i-executors-sse` (off `main` @ `6da4ebc`, already created).
- **Bash cwd persists**. `cd /d/download_weights && git …`; `cd /d/download_weights/frontend && pnpm …`.
- **Tenant gate**: copy the 5-line `tenant_filtered(select(DownloadTask.id).where(DownloadTask.id == task_id), DownloadTask, principal)` from `tasks_source_alloc_stream.py`.
- **`?max_ticks=N`**: same hatch.
- **Route placement**: register `tasks_executors_stream_router` BEFORE `tasks_router` (after `tasks_source_alloc_stream_router`).
- **No dead keepalive** (SP5e #38). **FK ordering** (SP5f #43): flush parents before DownloadTask (SP5i needs NO FileSubTask rows). **DownloadTask cols** (SP5f B1): `repo_id`/`revision`/`path_template`.
- **Seam regression discipline** (SP5f #40): all 8 existing consumer specs + both seam specs must pass unchanged/extended; the existing-consumer specs are the regression-proof.

---

## File Structure

**Backend (create):** `src/dlw/api/tasks_executors_stream.py`, `tests/api/test_task_executors_stream.py`.
**Backend (modify):** `api/openapi.yaml` (DONE in spec commit), `src/dlw/main.py` (router include), `src/dlw/config.py` (DONE in spec commit).
**Frontend (modify):** `frontend/src/composables/useLiveResource.ts` (**seam close-on-disable**), `frontend/src/composables/useParticipatingExecutors.ts` (opt-in), `frontend/tests/unit/useLiveResourceEnabledSse.spec.ts` (extend to 5 cases).
**Frontend (create):** `frontend/tests/unit/useParticipatingExecutorsStream.spec.ts`.
**Docs (modify):** `docs/operator/web-ui.md`.

---

# Milestone M1 — Backend

### Task 1: Verify openapi + config (added in spec commit)

- [ ] **Step 1**:

```bash
cd /d/download_weights && grep -n "participating-executors/stream" api/openapi.yaml && grep -n "task_executors_stream_interval_seconds" src/dlw/config.py
```

Expected: both present.

### Task 2: SSE endpoint

**Files:** Create `src/dlw/api/tasks_executors_stream.py`; modify `src/dlw/main.py`.

- [ ] **Step 1**: Create `src/dlw/api/tasks_executors_stream.py`:

```python
"""GET /api/v1/tasks/{task_id}/participating-executors/stream — SSE live stream (UI-SP5i).

Hand-rolled text/event-stream; reuses SP2's executors_for_task service
(list), wrapped as ParticipatingExecutors. Mirrors tasks_chunks_stream.py.
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
from dlw.schemas.task_detail import ParticipatingExecutors
from dlw.services import task_detail as _td

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "task_executors_stream_interval_seconds", 2.0))
    return max(0.5, min(60.0, raw))


@router.get("/{task_id}/participating-executors/stream")
async def stream_participating_executors(
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
                    items = await _td.executors_for_task(
                        s, task_id, principal.tenant_id)
                payload = ParticipatingExecutors(items=items)
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
    from dlw.api.tasks_source_alloc_stream import router as tasks_source_alloc_stream_router
    app.include_router(tasks_source_alloc_stream_router)
    from dlw.api.tasks import router as tasks_router
    app.include_router(tasks_router)
```

**INSERT** these 4 lines BETWEEN `app.include_router(tasks_source_alloc_stream_router)` and `from dlw.api.tasks import router as tasks_router`. Do NOT remove or retype any existing line; only add these 4:

```python
    # SP5i: tasks-executors stream router included BEFORE tasks_router for
    # defensive consistency. Distinct depth from any /{task_id}/* route.
    from dlw.api.tasks_executors_stream import router as tasks_executors_stream_router
    app.include_router(tasks_executors_stream_router)
```

- [ ] **Step 3**: Sanity:

```bash
cd /d/download_weights && uv run python -c "from dlw.api.tasks_executors_stream import router; [print(r.path) for r in router.routes]"
```

Expected: `/api/v1/tasks/{task_id}/participating-executors/stream`.

- [ ] **Step 4**: Commit.

```bash
git add api/openapi.yaml src/dlw/config.py src/dlw/api/tasks_executors_stream.py src/dlw/main.py
git commit -q -m "UI-SP5i M1: GET /tasks/{id}/participating-executors/stream SSE endpoint + openapi + config (reuses executors_for_task)"
```

---

### Task 3: Backend tests

**Files:** Create `tests/api/test_task_executors_stream.py`.

- [ ] **Step 1**: Create `tests/api/test_task_executors_stream.py`:

```python
"""Tests for GET /api/v1/tasks/{task_id}/participating-executors/stream (UI-SP5i SSE)."""
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
    monkeypatch.setenv("DLW_TASK_EXECUTORS_STREAM_INTERVAL_SECONDS", TICK)
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
async def test_executors_stream_unauthenticated_401(
    client: AsyncClient,
) -> None:
    async with client.stream(
        "GET", f"/api/v1/tasks/{TASK_T1}/participating-executors/stream",
    ) as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_executors_stream_cross_tenant_404(
    client: AsyncClient, auth,
) -> None:
    async with client.stream(
        "GET",
        f"/api/v1/tasks/{TASK_T2}/participating-executors/stream?max_ticks=1",
        headers=auth,
    ) as resp:
        assert resp.status_code == 404


@pytest.mark.slow
async def test_executors_stream_single_snapshot(
    client: AsyncClient, auth,
) -> None:
    received = await _collect(
        client,
        f"/api/v1/tasks/{TASK_T1}/participating-executors/stream?max_ticks=1",
        auth, count=1, timeout=3.0)
    body = json.loads(received[0])
    assert "items" in body
    assert isinstance(body["items"], list)


@pytest.mark.slow
async def test_executors_stream_multi_snapshot(
    client: AsyncClient, auth,
) -> None:
    received = await _collect(
        client,
        f"/api/v1/tasks/{TASK_T1}/participating-executors/stream?max_ticks=2",
        auth, count=2, timeout=3.0)
    assert len(received) >= 2
    for raw in received[:2]:
        body = json.loads(raw)
        assert "items" in body
        assert isinstance(body["items"], list)
```

- [ ] **Step 2**: Run + commit.

```bash
cd /d/download_weights && uv run pytest tests/api/test_task_executors_stream.py -q 2>&1 | tail -8
git add tests/api/test_task_executors_stream.py
git commit -q -m "UI-SP5i M1: backend SSE endpoint tests (unauth-401, cross-tenant-404, single/multi snapshot)"
```

Expected: 4 passed.

### M1 Full backend gate

- [ ] **Step 1**: `cd /d/download_weights && uv run pytest -q 2>&1 | tail -3` — all pass (2 known Windows-local failover flakes may appear; CI arbiter).

---

# Milestone M2 — Seam close-on-disable + frontend cutover

### Task 4: Seam close-on-disable rewrite

**Files:** Modify `frontend/src/composables/useLiveResource.ts`; extend `frontend/tests/unit/useLiveResourceEnabledSse.spec.ts`.

- [ ] **Step 1**: Replace the SSE block in `frontend/src/composables/useLiveResource.ts` (the `if (opts.streamUrl && opts.applyEvent) { … }` block) with the open/close lifecycle. The `computed` `streaming`, the `useQuery` call, and `pollingFallback` ref are UNCHANGED — only the SSE block changes. Full new SSE block:

```ts
  if (opts.streamUrl && opts.applyEvent) {
    const qc = useQueryClient()
    const auth = useAuthStore()
    const apply = opts.applyEvent
    // SP5i: open/close lifecycle (was a permanent `started` latch). The
    // stream closes when `streaming` flips false (e.g. a tab-gated consumer
    // deactivates) and reopens when it flips true again — bounding the
    // concurrent SSE count to (always-on streams) + (1 per active gated
    // tab). `ac` is non-null iff a stream is currently live.
    let ac: AbortController | null = null
    let gaveUp = false

    const openStream = () => {
      if (ac) return
      if (gaveUp) return
      if (!streaming.value) return
      if (q.data.value === undefined) return
      const controller = new AbortController()
      ac = controller
      const url = toValue(opts.streamUrl as MaybeRefOrGetter<string>)
      void streamSse({
        url, token: auth.accessToken, signal: controller.signal,
        onEvent: (ev) => {
          const prev = qc.getQueryData<T>(key)
          const next = apply(prev, ev)
          qc.setQueryData(key, next)
        },
        onUnauthorized: () => {
          auth.logout()
        },
      }).then(() => {
        // streamSse RESOLVES on BOTH abort and giveup (sse.ts:94,97).
        // Identity guard: if `ac` no longer references THIS controller, we
        // aborted it ourselves (closeStream / reopen) — do nothing. Else it
        // gave up after 3 consecutive failures → fall back to polling.
        if (ac === controller) {
          ac = null
          gaveUp = true
          pollingFallback.value = true
          void q.refetch()
        }
      }).catch(() => {
        // 401 path — onUnauthorized already invoked.
        if (ac === controller) ac = null
      })
    }

    const closeStream = () => {
      if (ac) { ac.abort(); ac = null }
    }

    watch(
      [streaming, () => q.data.value] as const,
      ([isOn, data]) => {
        if (isOn && data !== undefined) openStream()
        else if (!isOn) closeStream()
      },
      { immediate: true },
    )
    onScopeDispose(closeStream)
  }
```

Ensure the imports at the top still include `computed, onScopeDispose, ref, toValue, watch, type MaybeRefOrGetter, type Ref` (the SP5f version already imports these; `WatchStopHandle` is no longer needed — remove it from the import if present and unused, or leave it harmlessly. Run lint to confirm no unused-import error).

- [ ] **Step 2**: Extend `frontend/tests/unit/useLiveResourceEnabledSse.spec.ts` to 5 cases. Full new file:

```ts
import { describe, expect, test, vi } from 'vitest'
import { ref, nextTick, defineComponent, type Ref } from 'vue'
import { mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { createPinia, setActivePinia } from 'pinia'

const { streamSseMock, signals } = vi.hoisted(() => ({
  streamSseMock: vi.fn((_opts: { url: string; signal: AbortSignal }) =>
    new Promise<void>(() => {})),
  signals: [] as AbortSignal[],
}))
vi.mock('@/api/sse', async () => {
  const actual = await vi.importActual<typeof import('@/api/sse')>('@/api/sse')
  return {
    ...actual,
    streamSse: (o: { url: string; signal: AbortSignal }) => {
      signals.push(o.signal)
      return streamSseMock(o)
    },
  }
})

import { useLiveResource } from '@/composables/useLiveResource'

function mountWith(enabled: Ref<boolean>) {
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

async function waitForStream(target: number) {
  for (let i = 0; i < 30 && streamSseMock.mock.calls.length < target; i++) {
    await new Promise((r) => setTimeout(r, 20))
  }
}

describe('useLiveResource seam — close-on-disable lifecycle (SP5i)', () => {
  test('enabled=false at mount: streamSse NOT called', async () => {
    streamSseMock.mockClear(); signals.length = 0
    const enabled = ref(false)
    const w = mountWith(enabled)
    await nextTick()
    await new Promise((r) => setTimeout(r, 50))
    expect(streamSseMock).not.toHaveBeenCalled()
    w.unmount()
  })
  test('enabled flips false→true: streamSse called once', async () => {
    streamSseMock.mockClear(); signals.length = 0
    const enabled = ref(false)
    const w = mountWith(enabled)
    await nextTick()
    enabled.value = true
    await waitForStream(1)
    expect(streamSseMock).toHaveBeenCalledTimes(1)
    w.unmount()
  })
  test('enabled=true at mount: streamSse called once (always-on path)', async () => {
    streamSseMock.mockClear(); signals.length = 0
    const enabled = ref(true)
    const w = mountWith(enabled)
    await waitForStream(1)
    expect(streamSseMock).toHaveBeenCalledTimes(1)
    w.unmount()
  })
  test('enabled true→false: the open stream is aborted', async () => {
    streamSseMock.mockClear(); signals.length = 0
    const enabled = ref(true)
    const w = mountWith(enabled)
    await waitForStream(1)
    expect(signals[0]?.aborted).toBe(false)
    enabled.value = false
    await nextTick()
    await new Promise((r) => setTimeout(r, 20))
    expect(signals[0]?.aborted).toBe(true)
    w.unmount()
  })
  test('enabled true→false→true: streamSse called twice (open, close, reopen)', async () => {
    streamSseMock.mockClear(); signals.length = 0
    const enabled = ref(true)
    const w = mountWith(enabled)
    await waitForStream(1)
    enabled.value = false
    await nextTick()
    await new Promise((r) => setTimeout(r, 20))
    enabled.value = true
    await waitForStream(2)
    expect(streamSseMock).toHaveBeenCalledTimes(2)
    expect(signals[0]?.aborted).toBe(true)   // first stream was closed
    expect(signals[1]?.aborted).toBe(false)  // reopened stream is live
    w.unmount()
  })
})
```

- [ ] **Step 3**: Run the seam tests + the 8 consumer specs:

```bash
cd /d/download_weights/frontend && pnpm vitest run tests/unit/useLiveResource tests/unit/useTaskEventsStream tests/unit/useSubtaskChunksStream tests/unit/useSourceAllocationStream 2>&1 | tail -12
```

Expected: all pass (seam 5 + 2 pre-existing seam + 3 consumer specs). If the close-on-disable test fails because `q.data.value` resets to undefined when `enabled` flips false (vue-query may clear data on disable), adjust the watcher condition or the test — but the seam's `else if (!isOn) closeStream()` branch does NOT depend on data, so abort fires on `isOn=false` regardless.

- [ ] **Step 4**: Commit.

```bash
git add frontend/src/composables/useLiveResource.ts frontend/tests/unit/useLiveResourceEnabledSse.spec.ts
git commit -q -m "UI-SP5i M2: seam close-on-disable (abort SSE when streaming flips false, reopen when true; identity-guard for abort-vs-giveup). 5-case regression test."
```

---

### Task 5: `useParticipatingExecutors` opts in

**Files:** Modify `frontend/src/composables/useParticipatingExecutors.ts`; create `frontend/tests/unit/useParticipatingExecutorsStream.spec.ts`.

- [ ] **Step 1**: Modify `frontend/src/composables/useParticipatingExecutors.ts` (full new file):

```ts
import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { ParticipatingExecutors } from '@/api/types'

export function useParticipatingExecutors(
  taskId: Ref<string>, enabled: Ref<boolean>, terminal: Ref<boolean>,
) {
  const streamUrl = computed(
    () => `/api/v1/tasks/${taskId.value}/participating-executors/stream`)
  return useLiveResource<ParticipatingExecutors>(
    ['task-executors', taskId],
    async () => (await client.get<ParticipatingExecutors>(
      `/api/v1/tasks/${taskId.value}/participating-executors`)).data,
    {
      baseIntervalMs: 2_000,
      enabled,
      isTerminal: () => terminal.value,
      streamUrl,
      applyEvent: (_prev, ev) => JSON.parse(ev.data) as ParticipatingExecutors,
    },
  )
}
```

- [ ] **Step 2**: Create `frontend/tests/unit/useParticipatingExecutorsStream.spec.ts`:

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

import { useParticipatingExecutors } from '@/composables/useParticipatingExecutors'

describe('useParticipatingExecutors SSE opt-in (SP5i)', () => {
  test('passes streamUrl + applyEvent + enabled + isTerminal to the seam', () => {
    captured.length = 0
    const taskId = ref('aaa')
    const enabled = ref(true)
    const terminal = ref(false)
    useParticipatingExecutors(taskId, enabled, terminal)
    const last = captured[captured.length - 1]
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/aaa/participating-executors/stream')
    expect(typeof last?.opts.applyEvent).toBe('function')
    expect(last?.opts.baseIntervalMs).toBe(2_000)
    expect(last?.opts.enabled).toBe(enabled)
    expect(typeof last?.opts.isTerminal).toBe('function')
  })
  test('streamUrl is reactive to taskId ref', () => {
    captured.length = 0
    const taskId = ref('bbb')
    useParticipatingExecutors(taskId, ref(true), ref(false))
    const last = captured[captured.length - 1]
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/bbb/participating-executors/stream')
    taskId.value = 'ccc'
    expect((last?.opts.streamUrl as { value: string }).value)
      .toBe('/api/v1/tasks/ccc/participating-executors/stream')
  })
  test('applyEvent parses ev.data as JSON', () => {
    captured.length = 0
    useParticipatingExecutors(ref('xxx'), ref(true), ref(false))
    const apply = (captured[captured.length - 1]?.opts.applyEvent) as
      (prev: unknown, ev: { data: string }) => { items: unknown[] }
    const out = apply(undefined, { data: '{"items":[{"executor_id":"e1"}]}' })
    expect(out.items).toEqual([{ executor_id: 'e1' }])
  })
})
```

- [ ] **Step 3**: Full frontend gate:

```bash
cd /d/download_weights/frontend && pnpm lint && pnpm exec vue-tsc --noEmit && pnpm vitest run 2>&1 | tail -8
```

Expected: lint clean, typecheck clean, all tests pass (167 + 3 new composable + 2 new seam cases = ~172).

- [ ] **Step 4**: Build:

```bash
cd /d/download_weights/frontend && pnpm build 2>&1 | tail -3
```

- [ ] **Step 5**: Commit.

```bash
git add frontend/src/composables/useParticipatingExecutors.ts frontend/tests/unit/useParticipatingExecutorsStream.spec.ts
git commit -q -m "UI-SP5i M2: useParticipatingExecutors opts in to SSE (view-free; TaskDetail.vue + Executors-tab untouched)"
```

---

# Milestone M3 — Smoke + docs

### Task 6: headed Playwright smoke + operator docs

**Files:** Create `.run/pw/sp5i-smoke.mjs` (gitignored); modify `docs/operator/web-ui.md`.

- [ ] **Step 1**: Restart `:8011` controller with SP5i code; restart Vite (clear `node_modules/.vite`, kill stale via netstat/Stop-Process).

- [ ] **Step 2**: curl smoke:

```bash
cd /d/download_weights && JWT=$(uv run python -c "from dlw.auth.principal import issue_system_jwt; print(issue_system_jwt(secret='dev-system-jwt-change-me', user_id=1, tenant_id=1, role='tenant_admin', project_ids=[]))" 2>/dev/null | tail -1)
echo "$JWT" > .run/sp5i-token.txt
TASK_ID=$(curl -s -H "Authorization: Bearer $JWT" "http://127.0.0.1:8011/api/v1/tasks?limit=1" | python -c "import sys,json;d=json.load(sys.stdin);print(d['items'][0]['id'] if d.get('items') else 'NONE')")
curl -s -H "Authorization: Bearer $JWT" "http://127.0.0.1:8011/api/v1/tasks/$TASK_ID/participating-executors/stream?max_ticks=1" | head -c 400; echo
```

Expected: `:open\n\ndata: {"items":[…]}\n\n`.

- [ ] **Step 3**: Create `.run/pw/sp5i-smoke.mjs` (Executors tab requires a click — `#tab-executors`; also exercise close-on-disable by toggling back to Files then Executors):

```js
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'

const TOKEN = readFileSync('.run/sp5i-token.txt', 'utf8').trim()
const VITE = process.env.SP5I_VITE ?? 'http://localhost:5173'
const errors = []
const b = await chromium.launch({ headless: false })
const pg = await b.newPage()
pg.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
pg.on('pageerror', (e) => errors.push(String(e)))

const sseReqs = []
pg.on('request', (r) => {
  if (/\/api\/v1\/tasks\/[^/]+\/participating-executors\/stream(\?|$)/.test(r.url())) {
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
  console.log('SP5i smoke: no tasks exist; submit one first via `uv run dlw submit …`')
  process.exit(1)
}
await pg.goto(`${VITE}/tasks/${taskId}`)
await pg.waitForURL('**/tasks/**')
await pg.waitForSelector('.el-tabs', { timeout: 10_000 })
await pg.waitForTimeout(1500)
await pg.click('#tab-executors')
await pg.waitForTimeout(3000)
const afterFirst = sseReqs.length
// close-on-disable cycle: leave Executors (close), return (reopen)
await pg.click('#tab-files')
await pg.waitForTimeout(1500)
await pg.click('#tab-executors')
await pg.waitForTimeout(3000)
await pg.screenshot({ path: '.run/pw/sp5i-executors.png' })

await b.close()
const real = errors.filter((e) =>
  !e.includes('/health/active') && !e.includes('Failed to load resource'))
if (real.length) {
  console.log('SP5i smoke: real errors:\n' + real.join('\n'))
  process.exit(1)
}
if (sseReqs.length === 0) {
  console.log('SP5i smoke: no /participating-executors/stream SSE request observed')
  process.exit(1)
}
console.log(`SP5i smoke OK — ${afterFirst} request(s) after first Executors visit, ${sseReqs.length} total after revisit (a 2nd request proves close-on-disable reopened)`)
sseReqs.forEach((u) => console.log('  -> ' + u))
```

- [ ] **Step 4**: Run:

```bash
cd /d/download_weights && node .run/pw/sp5i-smoke.mjs 2>&1 | tail -6
```

Expected: `SP5i smoke OK — 1 request(s) after first Executors visit, 2 total after revisit …`.

- [ ] **Step 5**: Append SP5i section to `docs/operator/web-ui.md` after the SP5h section:

```markdown

### UI-SP5i — Participating-executors SSE + seam close-on-disable (SP5* SSE conversion complete)

Ninth and final application of the view-free SSE template — the last
SP2 sub-resource. The Executors tab on TaskDetail
(`useParticipatingExecutors`) now talks SSE via
`GET /api/v1/tasks/{task_id}/participating-executors/stream` (2 s tick;
`DLW_TASK_EXECUTORS_STREAM_INTERVAL_SECONDS`; clamped `[0.5, 60.0]`),
reusing `executors_for_task` wrapped as `ParticipatingExecutors`.

**Seam close-on-disable**: SP5i replaced `useLiveResource`'s permanent
`started` latch with an open/close lifecycle keyed on the reactive
`streaming` gate. When a tab-gated consumer deactivates (`streaming`
flips false), its SSE is aborted; reactivating reopens it. This bounds
the concurrent SSE count on TaskDetail to **2** (the always-on header
stream + the one active tab's stream), regardless of how many tabs the
user has visited — resolving the connection-cap concern tracked since
SP5g. `streamSse` resolves on both abort and giveup, so the seam uses
an `if (ac === controller)` identity guard to tell "we aborted it"
(reopen later) from "it gave up after 3 failures" (permanent polling
fallback). The 5 always-on consumers (SP5/SP5b/SP5c/SP5d/SP5e) are
unaffected — their `streaming` never flips false, so they open once
and stay open exactly as before; the regression is proven by the
unchanged consumer specs plus a 5-case seam lifecycle test.

With SP5i merged, **the SP5* SSE conversion is complete**: every
TaskDetail tab (chunks/sources/executors/events) and every global
consumer (task-detail header, tasks-list, executors, audit, quota)
streams via SSE through the single `useLiveResource` seam, with
concurrency bounded and polling as the automatic fallback.
```

- [ ] **Step 6**: Commit (`.run/` gitignored):

```bash
git add docs/operator/web-ui.md
git commit -q -m "UI-SP5i M3: operator docs — executors SSE + close-on-disable (SP5* SSE conversion complete)"
```

---

# Final cycle (controller-driven)

1. 1 opus reviewer (seam-change focus; ≤600 words).
2. Fix any HIGH; record MEDIUM/LOW in PR body.
3. `git push -u origin feat/ui-sp5i-executors-sse`.
4. `gh pr create` against `main`.
5. Poller waits CI all-green.
6. `gh pr merge <N> --squash --delete-branch`, `git checkout main && git pull --ff-only`.
7. Update `reference_l17728_modelpull.md` (SP5i merge, bump `main`, note SP5* SSE conversion COMPLETE).
8. Update `feedback_subagent_driven_dev.md` (close-on-disable seam: abort/giveup identity-guard; YAGNI trigger fired exactly as planned; SP5* family done).

---

## Self-Review

- **Spec coverage**: every section maps to a task. ✓
- **Placeholder scan**: none.
- **Type consistency**: `ParticipatingExecutors` reused; `executors_for_task` returns the list wrapped. Seam types: `ac: AbortController | null`, identity guard. ✓
- **Naming**: env `DLW_TASK_EXECUTORS_STREAM_INTERVAL_SECONDS`, setting `task_executors_stream_interval_seconds`, router `tasks_executors_stream_router`. Consistent.
- **Seam regression-proof**: 5-case lifecycle test + all 8 consumer specs unchanged. The abort/giveup identity guard is the one subtle correctness point — explicitly tested (case e asserts 2 calls + signal states).
- **Behavior preservation**: always-on consumers analyzed (§2.4) — `streaming` never false → open-once-stay-open. Gated consumers gain close-on-disable.
- **Route precedence**: `tasks_executors_stream_router` between source-alloc-stream and tasks.
- **Smoke**: `#tab-executors` (stable id, SP5f #42); close-on-disable observed via 2nd request on revisit.
