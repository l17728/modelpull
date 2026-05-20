# UI-SP5 — Realtime SSE Swap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 1 additive SSE endpoint (`GET /api/v1/tasks/{id}/stream`) + extend `useLiveResource` with `streamUrl` / `applyEvent` options + opt in ONE consumer (`useTaskDetail`) — delivering the locked "single-seam SSE swap, view-free" promise from SP1/SP2/SP3.

**Architecture:** Backend = hand-rolled `text/event-stream` via FastAPI `StreamingResponse` (same idiom as `src/dlw/api/hf_proxy.py:99-110`); fresh DB session per 1 Hz tick; tenant-gated via the proven cancel-pattern. Frontend = new `api/sse.ts` (pure `parseSseChunk` + `streamSse` fetch+ReadableStream with exponential backoff) + additive `LiveOptions<T>` fields (`streamUrl`, `applyEvent`); `useTaskDetail` is the ONLY consumer opting in. **9 other composables stay polling = truly view-free.**

**Tech Stack:** FastAPI · SQLAlchemy 2 async · asyncpg · Pydantic v2 · pytest (httpx `AsyncClient.stream()` for SSE tests) · OpenAPI 3.1 (spectral + swagger-cli) · Vue 3.5 `<script setup>` TS strict · `@tanstack/vue-query` v5.x (`queryClient.setQueryData` for SSE-driven cache writes) · `fetch` + native `ReadableStream` + `TextDecoder` · Vitest 2.1 + happy-dom (pure-fn tests only; the streamer is smoke-tested headed). **Zero new runtime deps.**

---

## Conventions (apply to every task — same as SP3)

- **Branch:** `feat/ui-sp5-realtime-sse` (created off `main` @ `a32f165`; spec committed `06c718a`).
- **Bash cwd persists**. `cd /d/download_weights && git …` for git; `cd /d/download_weights/frontend && pnpm …` for frontend.
- **Backend tenant gate**: the proven cancel-pattern (`tenant_filtered(select(DownloadTask.id).where(id==task_id), DownloadTask, principal)` → `None` → 404). Re-checked at the start of each tick (cheap defense against revoke-during-stream).
- **Run commands**: `uv run pytest tests/api/test_task_stream.py -v`; `uv run pytest tests/ -q`; spectral + swagger-cli; `python tools/lint_invariants.py`; `python tools/lint_no_direct_status_write.py`.
- **Frontend run**: `pnpm test:unit`, `pnpm typecheck`, `pnpm lint:fix && pnpm lint`, `pnpm build`.
- **`noUncheckedIndexedAccess`**: guard `arr[i]` with `?? fallback` / optional chaining / length checks.
- **i18n**: SP5 introduces **no new UI text** — locale files untouched; parity test stays green for free.

---

## File Structure

**Backend (create):**
- `src/dlw/api/tasks_stream.py` — `GET /api/v1/tasks/{task_id}/stream` route module.
- `tests/api/test_task_stream.py` — 4 tests.

**Backend (modify):**
- `api/openapi.yaml` — add the `/tasks/{taskId}/stream` GET path block.
- `src/dlw/main.py` — register the new router (1 lazy-import + 1 `include_router`).
- `src/dlw/config.py` — add 1 setting `task_stream_interval_seconds` (env `DLW_TASK_STREAM_INTERVAL`, default `1.0`, clamped `[0.1, 10.0]`).

**Frontend (create):** `frontend/src/api/sse.ts`; tests `frontend/tests/unit/parseSseChunk.spec.ts`, `frontend/tests/unit/streamGate.spec.ts`.

**Frontend (modify):**
- `frontend/src/composables/useLiveResource.ts` — additive `streamUrl` / `applyEvent` fields on `LiveOptions<T>`; the stream wiring (`useQueryClient().setQueryData(key, applyEvent(...))`) using `fetch`-based `streamSse` from `@/api/sse`.
- `frontend/src/composables/useTaskDetail.ts` — opt in with `streamUrl` + `applyEvent`.

**Docs (modify, M3):** `docs/operator/web-ui.md`.

---

# Milestone M1 — Backend: 1 additive SSE endpoint

### Task 1: OpenAPI contract + config setting

**Files:**
- Modify: `api/openapi.yaml` (add path after the existing `/tasks/{taskId}/events` block)
- Modify: `src/dlw/config.py` (add 1 setting)

- [ ] **Step 1: Add the path block to `api/openapi.yaml`**

Find the existing `/tasks/{taskId}/events:` block (around line 509 — the SP2-declared/UI-SP2-implemented endpoint). Immediately after its response (the line `next_cursor: type string, nullable: true …` from UI-SP3 Task 1 — or its surrounding `}` if SP3 changed the shape), and before the SP2-added `/tasks/{taskId}/subtask-chunks:` block, insert:

```yaml

  /tasks/{taskId}/stream:
    parameters:
      - $ref: '#/components/parameters/TaskId'
    get:
      tags: [tasks]
      summary: Live TaskDetail SSE stream (UI-SP5)
      operationId: streamTaskDetail
      responses:
        '200':
          description: SSE stream of TaskDetail snapshots (text/event-stream)
          content:
            text/event-stream:
              schema:
                type: string
                description: |
                  Each event is `data: <TaskDetail JSON>\n\n`. Stream terminates
                  on terminal task status, client disconnect, or controller
                  shutdown. Keep-alive comment lines (`:keepalive`) may appear.
```

- [ ] **Step 2: Validate the contract**

Run: `npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error`
Expected: 0 errors. Run: `npx --yes @apidevtools/swagger-cli validate api/openapi.yaml`
Expected: `api/openapi.yaml is valid`.

- [ ] **Step 3: Add the setting**

Read `src/dlw/config.py` first to find the right block. The settings class is a Pydantic `BaseSettings`. Add a new field alongside the existing ones (look for a similar `*_seconds` float — e.g. `hf_proxy_timeout_seconds` — and place the new field next to it for cohesion):

```python
    task_stream_interval_seconds: float = 1.0  # UI-SP5: SSE tick rate
```

The `BaseSettings` `env_prefix` (likely `DLW_`) already maps `DLW_TASK_STREAM_INTERVAL_SECONDS` automatically — **verify** by reading the existing field convention. If the env mapping is custom (e.g. an explicit `Field(..., alias="DLW_TASK_STREAM_INTERVAL")`), follow that pattern. Pin clamping in code, not in the schema:

In the route handler (Task 2) the value is clamped to `[0.1, 10.0]` via `max(0.1, min(10.0, settings.task_stream_interval_seconds))`. **No clamping in config** (keeps the setting trivial).

- [ ] **Step 4: Commit**

```bash
cd /d/download_weights && git add api/openapi.yaml src/dlw/config.py && git commit -q -m "UI-SP5 M1: openapi /tasks/{id}/stream path + task_stream_interval_seconds setting"
```

---

### Task 2: SSE route + service tick + 4 tests

**Files:**
- Create: `src/dlw/api/tasks_stream.py`, `tests/api/test_task_stream.py`
- Modify: `src/dlw/main.py` (register router — same idiom UI-SP3 pinned: lazy import inside `create_app()`, append after the last existing `include_router`)

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_task_stream.py`:

```python
"""Tests for GET /api/v1/tasks/{id}/stream (UI-SP5 SSE)."""
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
TICK = "0.1"  # 100 ms — keeps tests <2 s while still proving multi-tick


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
    monkeypatch.setenv("DLW_TASK_STREAM_INTERVAL_SECONDS", TICK)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _patch_hf(monkeypatch: pytest.MonkeyPatch):
    from dlw.services.hf_metadata import RepoFile

    async def fake(*args, **kwargs):
        return [
            RepoFile(path="config.json", size=4096, sha256=None),
            RepoFile(path="model.safetensors", size=64 * 1024, sha256="a" * 64),
        ]
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


async def _make_task(client, auth) -> str:
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/stream", "revision": "3" * 40, "storage_id": 1,
    }, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _collect_events(client, url, headers, *, count, timeout=4.0):
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
async def test_stream_unauthenticated_401(client: AsyncClient) -> None:
    async with client.stream(
        "GET", f"/api/v1/tasks/{uuid.uuid4()}/stream",
    ) as resp:
        assert resp.status_code == 401


@pytest.mark.slow
async def test_stream_cross_tenant_404(client: AsyncClient, auth) -> None:
    tid = await _make_task(client, auth)
    other = principal_headers(secret=SECRET, role="tenant_admin",
                              user_id=9, tenant_id=2)
    async with client.stream(
        "GET", f"/api/v1/tasks/{tid}/stream", headers=other,
    ) as resp:
        assert resp.status_code == 404


@pytest.mark.slow
async def test_stream_emits_multiple_snapshots(
    client: AsyncClient, auth,
) -> None:
    tid = await _make_task(client, auth)
    received = await _collect_events(
        client, f"/api/v1/tasks/{tid}/stream", auth, count=2, timeout=3.0)
    assert len(received) >= 2
    for raw in received[:2]:
        payload = json.loads(raw)
        assert payload["id"] == tid
        assert "status" in payload
        assert "subtasks" in payload


@pytest.mark.slow
async def test_stream_terminates_on_terminal_status(
    client: AsyncClient, auth, engine,
) -> None:
    from dlw.db.models.task import DownloadTask
    tid = await _make_task(client, auth)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        task = await s.get(DownloadTask, uuid.UUID(tid))
        assert task is not None
        task.status = "succeeded"  # noqa: direct test seeding
        await s.commit()
    received: list[str] = []
    async with asyncio.timeout(3.0):
        async with client.stream(
            "GET", f"/api/v1/tasks/{tid}/stream", headers=auth,
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    received.append(line)
    assert len(received) == 1
    assert "succeeded" in received[0]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/api/test_task_stream.py -v`
Expected: FAIL — route 404 (not implemented).

- [ ] **Step 3: Create the route module**

Create `src/dlw/api/tasks_stream.py`:

```python
"""GET /api/v1/tasks/{id}/stream — SSE TaskDetail stream (UI-SP5).

Hand-rolled text/event-stream via StreamingResponse (same idiom as hf_proxy.py).
1 Hz default tick rate; configurable via DLW_TASK_STREAM_INTERVAL_SECONDS
(clamped [0.1, 10.0] in code). Stream terminates on terminal task status,
client disconnect, or controller shutdown.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.models.task import DownloadTask
from dlw.db.session import get_engine
from dlw.db.tenant_scope import tenant_filtered
from dlw.schemas.task import TaskDetail

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

_TERMINAL = {"succeeded", "failed", "cancelled"}
_KEEPALIVE_EVERY_TICKS = 15  # at 1 Hz default, every 15s


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "task_stream_interval_seconds", 1.0))
    return max(0.1, min(10.0, raw))


async def _load_detail(
    session_maker, task_id: uuid.UUID, tenant_id: int,
) -> TaskDetail | None:
    async with session_maker() as s:
        row = (await s.execute(
            select(DownloadTask).where(
                DownloadTask.id == task_id,
                DownloadTask.tenant_id == tenant_id)
            .options(selectinload(DownloadTask.subtasks))
        )).scalar_one_or_none()
        return TaskDetail.model_validate(row) if row is not None else None


@router.get("/{task_id}/stream")
async def stream_task_detail(
    task_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
) -> StreamingResponse:
    # Tenant gate — proven cancel-pattern. 404 cross-tenant; never leak.
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
        # Pre-review IMPORTANT fix: flush an immediate comment line so the
        # response headers + first byte ship together. Defeats httpx 0.27.x
        # ASGITransport buffering (which would otherwise hold the response
        # until generator close) AND any production reverse-proxy buffering.
        # SSE parser ignores comment lines; tests' "data:" filter also skips.
        yield b":open\n\n"
        ticks_since_data = 0
        try:
            while True:
                if await request.is_disconnected():
                    return
                detail = await _load_detail(
                    session_maker, task_id, principal.tenant_id)
                if detail is None:
                    # Task deleted mid-stream.
                    return
                yield (f"data: {detail.model_dump_json()}"
                       "\n\n").encode("utf-8")
                ticks_since_data = 0
                if detail.status in _TERMINAL:
                    return
                # Sleep in small slices so cancellation propagates quickly.
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
            # Client disconnect or lifespan shutdown.
            return

    return StreamingResponse(
        _body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        },
    )
```

- [ ] **Step 4: Register the router in `src/dlw/main.py`**

The byte-faithful idiom (verified in SP3 — inside `create_app()`, lazy import). Append immediately after the last existing `include_router` call (the SP3 `executors_read_router`):

```python
    from dlw.api.tasks_stream import router as tasks_stream_router
    app.include_router(tasks_stream_router)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/api/test_task_stream.py -v`
Expected: 4 tests PASS in <10 s total.

- [ ] **Step 6: Commit**

```bash
cd /d/download_weights && git add src/dlw/api/tasks_stream.py src/dlw/main.py tests/api/test_task_stream.py && git commit -q -m "UI-SP5 M1: GET /tasks/{id}/stream SSE endpoint (hand-rolled, 1 Hz tick, terminal-stop)"
```

---

### Task 3: M1 gate

**Files:** none.

- [ ] **Step 1: Full backend suite**

Run: `uv run pytest tests/ -q`
Expected: prior baseline (record `uv run pytest tests/ --collect-only -q | tail -1` BEFORE Task 1 if uncertain) + 4 new tests; 0 failures.

- [ ] **Step 2: OpenAPI + invariant + status-write lint**

```bash
npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error
npx --yes @apidevtools/swagger-cli validate api/openapi.yaml
python tools/lint_invariants.py
python tools/lint_no_direct_status_write.py
```
All exit 0.

- [ ] **Step 3: Commit (only if fixups needed)**

```bash
cd /d/download_weights && git status -s
# If any fixups: git add -A && git commit -q -m "UI-SP5 M1 gate: backend suite + openapi + invariant green"
```

---

# Milestone M2 — Frontend foundation: pure SSE parser + streamer

### Task 4: `parseSseChunk` pure function + spec

**Files:**
- Create: `frontend/src/api/sse.ts`, `frontend/tests/unit/parseSseChunk.spec.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/tests/unit/parseSseChunk.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import { parseSseChunk } from '@/api/sse'

describe('parseSseChunk', () => {
  test('empty → no events, empty remainder', () => {
    expect(parseSseChunk('')).toEqual({ events: [], remainder: '' })
  })
  test('single event', () => {
    const { events, remainder } = parseSseChunk('data: hello\n\n')
    expect(remainder).toBe('')
    expect(events).toHaveLength(1)
    expect(events[0]?.event).toBe('message')
    expect(events[0]?.data).toBe('hello')
  })
  test('two events in one buffer', () => {
    const { events, remainder } = parseSseChunk(
      'data: a\n\ndata: b\n\n')
    expect(remainder).toBe('')
    expect(events.map((e) => e.data)).toEqual(['a', 'b'])
  })
  test('event split across two buffers → remainder carries', () => {
    const a = parseSseChunk('data: hel')
    expect(a.events).toEqual([])
    expect(a.remainder).toBe('data: hel')
    const b = parseSseChunk(a.remainder + 'lo\n\n')
    expect(b.events).toHaveLength(1)
    expect(b.events[0]?.data).toBe('hello')
  })
  test('comment lines ignored', () => {
    const { events } = parseSseChunk(
      ':keepalive\ndata: payload\n\n')
    expect(events).toHaveLength(1)
    expect(events[0]?.data).toBe('payload')
  })
  test('custom event field', () => {
    const { events } = parseSseChunk(
      'event: progress\ndata: 42\n\n')
    expect(events[0]?.event).toBe('progress')
    expect(events[0]?.data).toBe('42')
  })
  test('multi-line data field joined with newline', () => {
    const { events } = parseSseChunk(
      'data: line1\ndata: line2\n\n')
    expect(events[0]?.data).toBe('line1\nline2')
  })
  test('CRLF line endings also accepted', () => {
    const { events } = parseSseChunk(
      'data: x\r\n\r\n')
    expect(events[0]?.data).toBe('x')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

`cd /d/download_weights/frontend && pnpm test:unit -- parseSseChunk`
Expected: FAIL — module not found.

- [ ] **Step 3: Create `frontend/src/api/sse.ts`** (parser only for this task; streamer in Task 5)

```ts
export interface SseEvent {
  event: string
  data: string
  id?: string
}

/**
 * Pure SSE wire-format parser. The caller accumulates raw text chunks from a
 * ReadableStream and feeds them in; this function returns any complete events
 * plus the trailing partial-block remainder to prepend to the next chunk.
 */
export function parseSseChunk(
  buf: string,
): { events: SseEvent[]; remainder: string } {
  const events: SseEvent[] = []
  const parts = buf.split(/\r?\n\r?\n/)
  const remainder = parts.pop() ?? ''
  for (const block of parts) {
    let event = 'message'
    let data = ''
    let id: string | undefined
    // Pre-review IMPORTANT fix: track presence of any `data:` field, not just
    // truthy strings, so legitimate payloads like "0" / "false" / "" aren't
    // silently dropped (cf. SSE spec — empty data is a valid event).
    let hasData = false
    for (const line of block.split(/\r?\n/)) {
      if (line === '' || line.startsWith(':')) continue
      const i = line.indexOf(':')
      const field = i === -1 ? line : line.slice(0, i)
      const value = i === -1 ? '' : line.slice(i + 1).replace(/^ /, '')
      if (field === 'event') event = value
      else if (field === 'data') {
        hasData = true
        data += (data ? '\n' : '') + value
      }
      else if (field === 'id') id = value
    }
    if (hasData) {
      events.push(id !== undefined ? { event, data, id } : { event, data })
    }
  }
  return { events, remainder }
}
```

- [ ] **Step 4: Run test → PASS; typecheck → 0**

`cd /d/download_weights/frontend && pnpm test:unit -- parseSseChunk` → 8 PASS.
`pnpm typecheck` → 0 errors.

- [ ] **Step 5: Lint + commit**

```bash
cd /d/download_weights/frontend && pnpm lint:fix
cd /d/download_weights && git add frontend/src/api/sse.ts frontend/tests/unit/parseSseChunk.spec.ts && git commit -q -m "UI-SP5 M2: parseSseChunk pure SSE wire-format parser"
```

---

### Task 5: `streamSse` (fetch + ReadableStream + backoff) + `shouldStream` gate

**Files:**
- Modify: `frontend/src/api/sse.ts` (append streamer + helper)
- Create: `frontend/tests/unit/streamGate.spec.ts`

- [ ] **Step 1: Write the failing test for the pure gate**

Create `frontend/tests/unit/streamGate.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import { ref } from 'vue'
import { shouldStream } from '@/api/sse'

describe('shouldStream', () => {
  test('no streamUrl → false', () => {
    expect(shouldStream({ streamUrl: undefined, applyEvent: () => 1,
      enabled: true })).toBe(false)
  })
  test('no applyEvent → false', () => {
    expect(shouldStream({ streamUrl: '/x', applyEvent: undefined,
      enabled: true })).toBe(false)
  })
  test('enabled: false → false', () => {
    expect(shouldStream({ streamUrl: '/x', applyEvent: () => 1,
      enabled: false })).toBe(false)
  })
  test('enabled: Ref<false> → false (unwrapped)', () => {
    expect(shouldStream({ streamUrl: '/x', applyEvent: () => 1,
      enabled: ref(false) })).toBe(false)
  })
  test('all conditions met → true', () => {
    expect(shouldStream({ streamUrl: '/x', applyEvent: () => 1,
      enabled: true })).toBe(true)
  })
  test('enabled: Ref<true> → true', () => {
    expect(shouldStream({ streamUrl: '/x', applyEvent: () => 1,
      enabled: ref(true) })).toBe(true)
  })
  test('enabled: undefined defaults to true', () => {
    expect(shouldStream({ streamUrl: '/x', applyEvent: () => 1 })).toBe(true)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

`cd /d/download_weights/frontend && pnpm test:unit -- streamGate`
Expected: FAIL — `shouldStream` not exported.

- [ ] **Step 3: Append `streamSse` + `shouldStream` to `frontend/src/api/sse.ts`**

```ts
import { isRef, type Ref } from 'vue'

export interface StreamSseOptions {
  url: string
  token: string | null
  onEvent: (ev: SseEvent) => void
  onUnauthorized: () => void
  signal: AbortSignal
  /** Test seam: override the global fetch (defaults to window.fetch). */
  fetchImpl?: typeof fetch
}

/** Pure gate: does this LiveResource configuration want streaming? */
export interface StreamGateInput {
  streamUrl?: string | Ref<string> | undefined
  applyEvent?: ((prev: unknown, ev: SseEvent) => unknown) | undefined
  enabled?: boolean | Ref<boolean> | undefined
}

export function shouldStream(o: StreamGateInput): boolean {
  if (!o.streamUrl || !o.applyEvent) return false
  if (o.enabled === false) return false
  if (isRef(o.enabled) && o.enabled.value === false) return false
  return true
}

const BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000] as const
const GIVEUP_AFTER_CONSECUTIVE_FAILURES = 3

/**
 * Fetch-based SSE client. Sends `Authorization: Bearer <token>` (browser
 * EventSource cannot set headers). On 401 → calls onUnauthorized and rejects.
 * On any other disconnect/error: exponential backoff with jitter, capped at
 * 30 s. Successful chunk receipt resets the backoff counter to 0. Aborts when
 * the AbortSignal fires.
 *
 * Returns a Promise that resolves only when consecutive failures exceed
 * GIVEUP_AFTER_CONSECUTIVE_FAILURES (signal for the consumer to fall back to
 * polling). Rejects on 401.
 */
export async function streamSse(opts: StreamSseOptions): Promise<void> {
  const fetchFn = opts.fetchImpl ?? globalThis.fetch
  let consecutiveFailures = 0
  let backoffIdx = 0
  while (!opts.signal.aborted) {
    let connected = false
    try {
      const headers: Record<string, string> = {
        Accept: 'text/event-stream',
      }
      if (opts.token) headers.Authorization = `Bearer ${opts.token}`
      const resp = await fetchFn(opts.url, {
        method: 'GET', headers, signal: opts.signal,
      })
      if (resp.status === 401) {
        opts.onUnauthorized()
        throw new Error('SSE 401')
      }
      // Pre-review IMPORTANT fix: permanent client errors (task gone /
      // forbidden) should fail-fast — burning 7+ s of backoff on a 404 is
      // pure latency before the consumer's poll-fallback kicks in.
      if (resp.status === 403 || resp.status === 404) {
        return
      }
      if (!resp.ok || !resp.body) {
        throw new Error(`SSE upstream status ${resp.status}`)
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const { events, remainder } = parseSseChunk(buf)
        buf = remainder
        for (const ev of events) {
          opts.onEvent(ev)
          if (!connected) {
            connected = true
            consecutiveFailures = 0
            backoffIdx = 0
          }
        }
      }
    } catch (err) {
      if (opts.signal.aborted) return
      if ((err as Error).message === 'SSE 401') throw err
      consecutiveFailures += 1
      if (consecutiveFailures >= GIVEUP_AFTER_CONSECUTIVE_FAILURES) return
    }
    if (opts.signal.aborted) return
    const base = BACKOFF_MS[Math.min(backoffIdx, BACKOFF_MS.length - 1)]
      ?? 30_000
    const jitter = base * (0.8 + Math.random() * 0.4)  // ±20%
    backoffIdx += 1
    await new Promise<void>((resolve) => {
      const t = setTimeout(resolve, jitter)
      opts.signal.addEventListener('abort',
        () => { clearTimeout(t); resolve() }, { once: true })
    })
  }
}
```

- [ ] **Step 4: Add a streamSse happy-path unit test (pre-review IMP 4)**

The streamer is ~70 LOC of cancellation-sensitive logic; the pure parser + gate don't exercise it. Add ONE happy-path test using a stub `fetchImpl` that returns a `ReadableStream` of two SSE events; abort after the second:

Create `frontend/tests/unit/streamSse.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import { streamSse, type SseEvent } from '@/api/sse'

describe('streamSse', () => {
  test('parses events from a ReadableStream and stops on abort', async () => {
    const ac = new AbortController()
    const got: SseEvent[] = []
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(new TextEncoder().encode('data: a\n\n'))
        c.enqueue(new TextEncoder().encode('data: b\n\n'))
        c.close()
      },
    })
    const fetchImpl = (async () => new Response(stream, { status: 200 })) as
      typeof fetch
    await streamSse({
      url: '/x', token: 't', signal: ac.signal,
      onEvent: (e) => {
        got.push(e)
        if (got.length === 2) ac.abort()
      },
      onUnauthorized: () => {},
      fetchImpl,
    })
    expect(got.map((e) => e.data)).toEqual(['a', 'b'])
  })

  test('401 → calls onUnauthorized and rejects without retry', async () => {
    const ac = new AbortController()
    let unauth = 0
    const fetchImpl = (async () => new Response('', { status: 401 })) as
      typeof fetch
    await expect(streamSse({
      url: '/x', token: 't', signal: ac.signal,
      onEvent: () => {},
      onUnauthorized: () => { unauth++ },
      fetchImpl,
    })).rejects.toThrow(/SSE 401/)
    expect(unauth).toBe(1)
  })

  test('404 → resolves without retry (permanent client error)', async () => {
    const ac = new AbortController()
    const fetchImpl = (async () => new Response('not found', { status: 404 }))
      as typeof fetch
    await streamSse({
      url: '/x', token: 't', signal: ac.signal,
      onEvent: () => {},
      onUnauthorized: () => {},
      fetchImpl,
    })
  })
})
```

Run: `cd /d/download_weights/frontend && pnpm test:unit -- streamSse`
Expected: 3 PASS.

- [ ] **Step 5: Run gate test → PASS; typecheck → 0; lint OK**

`pnpm test:unit -- streamGate` → 7 PASS.
`pnpm typecheck` → 0 errors.
`pnpm lint:fix && pnpm lint` → OK.

- [ ] **Step 6: Commit**

```bash
cd /d/download_weights && git add frontend/src/api/sse.ts frontend/tests/unit/streamGate.spec.ts frontend/tests/unit/streamSse.spec.ts && git commit -q -m "UI-SP5 M2: streamSse fetch+ReadableStream client + shouldStream pure gate + happy-path tests"
```

---

### Task 6: M2 gate

**Files:** none.

- [ ] `cd /d/download_weights/frontend && pnpm test:unit && pnpm typecheck && pnpm lint && pnpm build` — all green.

---

# Milestone M3 — Cutover + smoke + docs

### Task 7: `useLiveResource` accepts `streamUrl` + `applyEvent`

**Files:**
- Modify: `frontend/src/composables/useLiveResource.ts`

- [ ] **Step 1: Augment `LiveOptions<T>` and wire the streamer**

Read `frontend/src/composables/useLiveResource.ts` first (post-SP2 has `enabled?: Ref<boolean> | boolean`). Replace the file with:

```ts
import { onScopeDispose, ref, toValue, watch, type MaybeRefOrGetter, type Ref, type WatchStopHandle } from 'vue'
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
  /** UI-SP5: opt in to SSE. When set with applyEvent, the composable opens a
   * stream after the first useQuery success and writes events into the cache
   * via setQueryData. Polling stays disabled while streaming is healthy and
   * resumes automatically if the stream gives up. */
  streamUrl?: string | Ref<string>
  applyEvent?: (prev: T | undefined, ev: SseEvent) => T
}

/**
 * Single realtime seam. Today: adaptive polling on vue-query, with an
 * additive opt-in SSE swap (UI-SP5). UI-SP4 (AI-Copilot) and future
 * transports plug in here without view changes.
 *
 * vue-query v5 does NOT accept a getter for `queryKey` — it must be a
 * QueryKey (array). Reactivity comes from putting refs *inside* the array.
 */
export function useLiveResource<T>(
  key: QueryKey,
  fetcher: () => Promise<T>,
  opts: LiveOptions<T>,
) {
  const streaming = shouldStream({
    streamUrl: opts.streamUrl, applyEvent: opts.applyEvent,
    enabled: opts.enabled,
  })

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
      if (streaming) {
        // Streaming owns the refresh cadence; only resume polling when the
        // stream gives up (see watcher below sets `pollingFallback.value`).
        if (!pollingFallback.value) return false
      }
      return computeInterval({
        base: opts.baseIntervalMs, terminal, hidden, errored,
      })
    },
  })

  const qc = useQueryClient()
  // Pre-review fix (IMPORTANT 1): use a ref for clarity; the callback re-reads
  // the closure variable per refetchInterval-eval, but matching Vue idioms
  // makes intent obvious and future-proofs anyone wanting to watch() this.
  const pollingFallback = ref(false)

  if (streaming && opts.applyEvent) {
    const ac = new AbortController()
    const auth = useAuthStore()
    const apply = opts.applyEvent
    // Pre-review BLOCKER fix: Vue 3.5's `watch({ immediate: true })` invokes
    // the handler SYNCHRONOUSLY inside the watch() call — before `stopWatch`
    // is assigned the returned stop handle. If vue-query serves a cached
    // snapshot synchronously (e.g. HMR / route revisit / future initialData),
    // calling `stopWatch()` from inside the handler would hit TDZ
    // (ReferenceError). Fix: `let stopWatch` + optional-call + a `started`
    // flag so the kick-off runs exactly once even if the watcher fires
    // synchronously on registration.
    let stopWatch: WatchStopHandle | undefined
    let started = false
    stopWatch = watch(
      () => q.data.value,
      (snapshot) => {
        if (snapshot === undefined || started) return
        started = true
        stopWatch?.()  // safe — undefined on synchronous immediate fire
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
          q.refetch()
        }).catch(() => {
          // 401 path — onUnauthorized already invoked.
        })
      },
      { immediate: true },
    )
    onScopeDispose(() => { ac.abort() })
  }

  return q
}
```

- [ ] **Step 2: Verify the existing useLiveResource tests still pass**

`cd /d/download_weights/frontend && pnpm test:unit -- useLiveResource`
Expected: existing 4 tests (computeInterval + useLiveResourceEnabled) PASS unchanged.

- [ ] **Step 3: Typecheck + lint**

`pnpm typecheck` → 0 errors. `pnpm lint:fix && pnpm lint` → OK.

- [ ] **Step 4: Commit**

```bash
cd /d/download_weights && git add frontend/src/composables/useLiveResource.ts && git commit -q -m "UI-SP5 M3: useLiveResource opt-in SSE (streamUrl+applyEvent; view-transparent)"
```

---

### Task 8: `useTaskDetail` opts in

**Files:**
- Modify: `frontend/src/composables/useTaskDetail.ts`

- [ ] **Step 1: Replace file content**

```ts
import { computed, type Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import { TERMINAL_STATUSES, type TaskDetail } from '@/api/types'

export function useTaskDetail(taskId: Ref<string>) {
  const streamUrl = computed(() => `/api/v1/tasks/${taskId.value}/stream`)
  return useLiveResource<TaskDetail>(
    ['task', taskId],
    async () => (await client.get<TaskDetail>(
      `/api/v1/tasks/${taskId.value}`)).data,
    {
      baseIntervalMs: 1_000,
      isTerminal: (d) => TERMINAL_STATUSES.has(d.status),
      streamUrl,
      applyEvent: (_prev, ev) => JSON.parse(ev.data) as TaskDetail,
    },
  )
}
```

- [ ] **Step 2: Verify the existing useTaskDetail tests still pass (return shape unchanged)**

`cd /d/download_weights/frontend && pnpm test:unit -- useTaskDetail TaskDetailSP2`
Expected: existing tests (the SP1 `useTaskDetail.spec.ts` + the SP2 `TaskDetailSP2.spec.ts`) PASS unchanged. **This is the view-free proof.**

- [ ] **Step 3: Typecheck + lint**

`pnpm typecheck` → 0 errors. `pnpm lint:fix && pnpm lint` → OK.

- [ ] **Step 4: Commit**

```bash
cd /d/download_weights && git add frontend/src/composables/useTaskDetail.ts && git commit -q -m "UI-SP5 M3: useTaskDetail opts in to SSE (view-free; all consumers unchanged)"
```

---

### Task 9: M3 full gate + headed Playwright smoke + docs

**Files:**
- Modify: `docs/operator/web-ui.md`

- [ ] **Step 1: Full backend suite**

`uv run pytest tests/ -q` → (M1 baseline + 4) PASS; 0 failures.

- [ ] **Step 2: Full frontend gate**

`cd /d/download_weights/frontend && pnpm test:unit && pnpm typecheck && pnpm lint && pnpm build` — all green.

- [ ] **Step 3: OpenAPI + invariant + status-write lint**

```bash
cd /d/download_weights
npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error
npx --yes @apidevtools/swagger-cli validate api/openapi.yaml
python tools/lint_invariants.py
python tools/lint_no_direct_status_write.py
```
All exit 0.

- [ ] **Step 4: Headed Playwright smoke**

Per SP3-codified recipe: ensure the ephemeral `:8011` controller is running with CURRENT code (kill the existing PID via `taskkill //F //PID` if needed, then relaunch via the SP3 recipe). Ensure Vite is up with `DLW_API_PROXY=http://localhost:8011` (find the actual port via `tail -5 /tmp/vite-sp5.log` — Vite may have rolled to :5174/:5175 if :5173 is occupied). Mint a fresh tenant JWT, save to `.run/sp5-token.txt`.

Create `.run/pw/sp5-smoke.mjs`:

```js
import { chromium } from 'playwright'
import { readFileSync } from 'node:fs'

const TOKEN = readFileSync('.run/sp5-token.txt', 'utf8').trim()
const TASK_ID = process.env.SP5_TASK_ID
const VITE = process.env.SP5_VITE ?? 'http://localhost:5173'
const errors = []
const b = await chromium.launch({ headless: false })
const pg = await b.newPage()
pg.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
pg.on('pageerror', (e) => errors.push(String(e)))

await pg.goto(`${VITE}/login`)
await pg.fill('input', TOKEN)
await pg.click('button[type="submit"]')
await pg.waitForURL('**/')
await pg.goto(`${VITE}/tasks/${TASK_ID}`)
await pg.waitForSelector('.el-tabs', { timeout: 10_000 })
await pg.screenshot({ path: '.run/pw/sp5-detail.png' })

// Watch the network for SSE traffic
const sseReqs = []
pg.on('request', (r) => {
  if (r.url().endsWith('/stream')) sseReqs.push(r.url())
})
// Reload the page so we observe the SSE request initiate
await pg.reload()
await pg.waitForSelector('.el-tabs', { timeout: 10_000 })
await pg.waitForTimeout(3000)
await pg.screenshot({ path: '.run/pw/sp5-after-stream.png' })

await b.close()
// Filter expected /health/active 503 noise (ephemeral controller).
const real = errors.filter((e) =>
  !e.includes('/health/active') && !e.includes('Failed to load resource'))
if (real.length) {
  console.log('SP5 smoke: real console/page errors:\n' + real.join('\n'))
  process.exit(1)
}
if (sseReqs.length === 0) {
  console.log('SP5 smoke: no SSE request observed — stream not opened')
  process.exit(1)
}
console.log(`SP5 smoke OK — observed ${sseReqs.length} SSE request(s)`)
```

Run (PowerShell): pick a task id from the existing dev DB (`$TID = (curl ... /api/v1/tasks | jq -r '.items[0].id')`) and `$env:SP5_TASK_ID=$TID; $env:SP5_VITE='http://localhost:5174'; node .run/pw/sp5-smoke.mjs`.

Expected: prints `SP5 smoke OK — observed ≥1 SSE request(s)`. The screenshots show the Task Detail page rendering normally. **This is the view-free verification: SP1/SP2/SP3 pages render identically; only the network tab shows the new `/stream` connection.**

Record the outcome. If the local stack is unavailable, note it explicitly — the smoke is a manual gate that does not block CI.

- [ ] **Step 5: Append docs**

Add to `docs/operator/web-ui.md`:

```markdown

## UI-SP5 — Realtime SSE swap (delivered)

`useLiveResource` now supports an opt-in SSE transport. The locked promise from
SP1/SP2/SP3 — *"SP5 swaps internals to SSE/WS with zero view changes"* — is
honored: every view, page, and other composable is unchanged.

- **Backend**: `GET /api/v1/tasks/{id}/stream` — hand-rolled
  `text/event-stream`, 1 Hz default tick (env
  `DLW_TASK_STREAM_INTERVAL_SECONDS` overrides; clamped `[0.1, 10.0]`).
  Tenant-scoped via the proven cancel-pattern (404 cross-tenant). Terminates on
  terminal task status, client disconnect, or controller shutdown.
- **Frontend**: `frontend/src/api/sse.ts` (pure `parseSseChunk` + `streamSse`
  fetch+ReadableStream with exponential backoff, Bearer-via-header). The
  `useLiveResource` composable gains optional `streamUrl` + `applyEvent`
  fields; when both are set, the composable opens an SSE connection after the
  first snapshot and writes events into the vue-query cache. On 3 consecutive
  failures the stream gives up and polling resumes automatically.
- **One consumer opts in**: `useTaskDetail`. Every other composable
  (`useTaskList`, `useQuota`, `useSubtaskChunks`, `useSourceAllocation`,
  `useParticipatingExecutors`, `useTaskEvents`, `useExecutors`, `useAuditLog`,
  `useSystemHealth`) stays on polling.

**Known deferrals** (intentional): WebSocket transport; SSE for the 4 SP2
sub-resource composables (would require a multi-resource stream that breaks
view-free — defer to SP5b if telemetry justifies); SSE for the
low-frequency SP3 composables (push value is negligible at 5–30 s cadences).
UI-SP4 (AI-Copilot) remains the v2.1 follow-up.
```

- [ ] **Step 6: Commit**

```bash
cd /d/download_weights && git add docs/operator/web-ui.md && git commit -q -m "UI-SP5 M3: operator docs for the realtime SSE swap"
```

---

## Self-Review

**1. Spec coverage:**
- §2 in-scope: 1 SSE endpoint (Tasks 1-2), `parseSseChunk` + `streamSse` + `shouldStream` (Tasks 4-5), `useLiveResource` opt-in (Task 7), `useTaskDetail` cutover (Task 8), headed smoke + docs (Task 9). ✓
- §3 inherited locked decisions: useLiveResource return shape preserved (Task 7 keeps `useQuery<T>` as the return); additive backend (Task 2 new file, Task 1 openapi additive); zero new runtime dep (only stdlib `asyncio` / `httpx` already a dep; frontend uses native `fetch`+`ReadableStream`+`TextDecoder`); CI gates only (Task 3/9 gates); no migration; tenant gate verbatim from cancel-pattern. ✓
- §4 backend: route in new file (`tasks_stream.py`), 4 tests covering unauth/cross-tenant/happy/terminal-stop, env-configurable tick rate. ✓
- §5 frontend: pure parser + streamer with backoff, additive `LiveOptions` fields, ONE consumer opted in. ✓
- §7 risks/contingency: cancellation propagates via small-slice sleep + `finally`; 401 path; backoff gives up at 3 failures, polling resumes; no fake-timer test needed (streamer is smoke-tested). ✓

**2. Placeholder scan:** none. Every step has complete code or exact commands.

**3. Type consistency:**
- `SseEvent` shape `{ event, data, id? }` is the SAME in `sse.ts`, `parseSseChunk` (returned), `streamSse` (`onEvent` param), `useLiveResource` (`applyEvent` second param), and `useTaskDetail` (`(_prev, ev) => JSON.parse(ev.data)`). ✓
- `streamUrl?: string | Ref<string>` consistent in `LiveOptions` (Task 7) and `useTaskDetail` (Task 8 — `computed(() => '/api/v1/...')` typed as `ComputedRef<string>` which is assignable to `Ref<string>`). ✓
- `applyEvent?: (prev: T | undefined, ev: SseEvent) => T` — consistent return-shape; `useTaskDetail` returns `TaskDetail`; `useLiveResource` calls `setQueryData(key, next)` where `next: T`. ✓
- `shouldStream` gate (Task 5) is invoked once in `useLiveResource` Task 7 to compute `streaming: boolean`; the gate's three inputs match the LiveOptions fields. ✓

**4. Verification chain locked:**
- Backend: `client.stream("GET", ".../stream")` + `aiter_lines` (httpx). The terminal-stop test directly mutates `DownloadTask.status` via test session — this is `tests/`, NOT in the `check_task_status_domain` scan list, so `lint_invariants` stays green.
- Frontend pure tests (`parseSseChunk` 8 cases, `streamGate` 7 cases) run under happy-dom without DOM. Integration (`streamSse` invoker) covered by the headed Playwright smoke.
- "View-free" proof: existing `useTaskDetail.spec.ts` + `TaskDetailSP2.spec.ts` still PASS unchanged (Task 8 step 2).
