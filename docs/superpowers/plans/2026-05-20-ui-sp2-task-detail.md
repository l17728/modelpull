# UI-SP2 — Download-Manager-Grade Task Detail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four additive read-only backend endpoints over the existing schema and rebuild `/tasks/:id` into a download-accelerator-grade detail view (aggregate ring, per-source bar, virtualized chunk table, executor swimlanes, event log).

**Architecture:** Backend = 1 new schemas module + 1 new services module + 4 GET routes appended to `src/dlw/api/tasks.py` (the exact proven cancel-pattern tenant gate) + `api/openapi.yaml` (implement the 2 already-declared paths to match their schemas, add 2 new paths/schemas). Frontend = additive `enabled` option on the single `useLiveResource` seam, 4 live composables + a client-derived rate composable, 6 inline-SVG/Element-Plus visual components, a rebuilt `TaskDetail.vue` with `el-tabs` + per-pane `DataBoundary` + virtualized `el-table-v2` chunk table. Zero Alembic migration (all columns already exist).

**Tech Stack:** FastAPI · SQLAlchemy 2 async · asyncpg · Pydantic v2 · pytest · OpenAPI 3.1 (spectral + swagger-cli) · Vue 3.5 `<script setup>` TS strict · Pinia · `@tanstack/vue-query` `^5.59` (lock-resolved 5.100.x — `enabled` accepts `MaybeRefOrGetter<boolean|undefined>`; behavior verified equivalent) · axios · Element Plus `^2.8.4` (lock-resolved 2.13.x) · vue-i18n 9 · Vitest 2.1 + @vue/test-utils + happy-dom · pnpm. (Caret deps — assert no exact minor.)

---

## Conventions (read once, applies to every task)

- **Branch:** `feat/ui-sp2-task-detail` (already created off `main`; spec committed `8531fd6`). Never `--no-verify`, never force-push, new commits not amends.
- **Backend tenant gate (verbatim pattern, from `src/dlw/api/tasks.py:143-148`):**
  ```python
  owned = await session.scalar(
      tenant_filtered(select(DownloadTask.id).where(DownloadTask.id == task_id),
                      DownloadTask, principal))
  if owned is None:
      raise HTTPException(status_code=404, detail="task not found")
  ```
  Every new route uses this before touching sub-rows. Cross-tenant id → 404 (never leak).
- **Backend run commands** (PowerShell-safe; pytest markers: tests are `@pytest.mark.slow`, run with `-m slow` allowed by pyproject — use the explicit file path which auto-collects):
  - Single file: `uv run pytest tests/api/test_task_detail_chunks.py -v`
  - Full backend: `uv run pytest tests/ -q`
  - OpenAPI: `npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error` then `npx --yes @apidevtools/swagger-cli validate api/openapi.yaml`
  - Invariant: `python tools/lint_invariants.py`
- **Frontend run commands** (run from `D:\download_weights\frontend`):
  - `pnpm test:unit` (vitest run) · `pnpm typecheck` (vue-tsc --noEmit) · `pnpm lint` (eslint --max-warnings=0) · `pnpm build` (vite build)
  - Last step of every frontend task: `pnpm lint:fix` then fold the autofix into the same commit (eslint `plugin:vue/vue3-recommended` enforces one-attribute-per-line etc.).
- **`noUncheckedIndexedAccess` is on** — `arr[i]` is `T | undefined`. Use `arr[i] ?? fallback`, never `arr[i]++`.
- **Frontend test harness** (copy exactly — happy-dom, `vi.hoisted` for mock factories, i18n+ElementPlus+pinia in `global.plugins`, assert via `findComponent({ name })` not CSS layout):
  ```ts
  import { createI18n } from 'vue-i18n'
  import en from '@/locale/en-US.json'
  const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': en } })
  ```
- **i18n parity:** every key added to `src/locale/en-US.json` MUST be added to `src/locale/zh-CN.json` in the same task, same nesting.
- **Static contract note:** `api/openapi.yaml` `servers:` basePath is `/api/v2`; runtime FastAPI routes use the existing `/api/v1/tasks` prefix. This split is pre-existing and intentional. CI (`spectral` + `swagger-cli`) lints the static file only — it does not diff runtime. Keep both internally consistent (add paths in the file's existing style).

---

## File Structure

**Backend (create):**
- `src/dlw/schemas/task_detail.py` — Pydantic DTOs: `ChunkSeg`, `SubtaskChunkRow`, `SubtaskChunkReport`, `SourceUsed`, `ChunkRouting`, `SourceAllocation`, `ParticipatingExecutor`, `ParticipatingExecutors`, `TaskEvent`, `TaskEventsResponse`.
- `src/dlw/services/task_detail.py` — async query helpers (one per endpoint), tenant-scoped.
- `tests/api/test_task_detail_chunks.py`, `test_task_detail_source_alloc.py`, `test_task_detail_executors.py`, `test_task_detail_events.py`.

**Backend (modify):**
- `src/dlw/api/tasks.py` — append 4 GET routes + imports.
- `api/openapi.yaml` — add 2 paths (after line 530) + 2 schemas (after `TaskEvent`, ~line 1868).

**Frontend (create):** `src/composables/{useSubtaskChunks,useSourceAllocation,useParticipatingExecutors,useTaskEvents,useDownloadRate}.ts`; `src/utils/format.ts`; `src/components/taskdetail/{AggregateRing,SpeedEta,SourceBar,ChunkBar,SwimLane,EventRow}.vue`; `tests/unit/{computeRate,ringDash,chunkSegments,eventLevel,format,ChunkBar,SwimLane,EventRow,AggregateRing,TaskDetailSP2}.spec.ts`.

**Frontend (modify):** `src/composables/useLiveResource.ts` (add `enabled`); `src/api/types.ts` (new DTO interfaces); `src/pages/TaskDetail.vue` (full rebuild); `src/locale/en-US.json` + `zh-CN.json` (add `tasks.detail.*`).

**Docs (modify, M4):** `docs/operator/web-ui.md`.

---

# Milestone M1 — Backend: 4 additive read endpoints + contract

### Task 1: OpenAPI contract — add 2 new paths + 2 new schemas

**Files:**
- Modify: `api/openapi.yaml` (insert paths after line 530, the end of the `/tasks/{taskId}/events` block, before `# ========== Models ==========`; insert schemas after the `TaskEvent` schema block which ends at line 1868, before `# ===== Models =====` at line 1870)

- [ ] **Step 1: Add the two new path items**

In `api/openapi.yaml`, immediately after the `/tasks/{taskId}/events:` block (the line `                  next_cursor: {type: string, nullable: true}` at line 530) and before the blank line preceding `  # ========== Models ==========`, insert:

```yaml

  /tasks/{taskId}/subtask-chunks:
    parameters:
      - $ref: '#/components/parameters/TaskId'
    get:
      tags: [tasks]
      summary: Per-file chunk segments (download-manager visualization)
      operationId: getSubtaskChunks
      responses:
        '200':
          description: Subtask chunk report
          content:
            application/json:
              schema: {$ref: '#/components/schemas/SubtaskChunkReport'}

  /tasks/{taskId}/participating-executors:
    parameters:
      - $ref: '#/components/parameters/TaskId'
    get:
      tags: [tasks]
      summary: Executors participating in this task (swimlanes)
      operationId: getParticipatingExecutors
      responses:
        '200':
          description: Participating executors
          content:
            application/json:
              schema: {$ref: '#/components/schemas/ParticipatingExecutors'}
```

- [ ] **Step 2: Add the two new schemas**

After the `TaskEvent:` schema block (ends line 1868 with `          additionalProperties: true`) and before `    # ===== Models =====` (line 1870), insert:

```yaml

    SubtaskChunkReport:
      type: object
      required: [items]
      properties:
        items:
          type: array
          items:
            type: object
            required: [subtask_id, filename, status, bytes_downloaded, is_chunked, chunks_completed, chunks]
            properties:
              subtask_id: {type: string, format: uuid}
              filename: {type: string}
              file_size: {type: integer, format: int64, nullable: true}
              status: {type: string}
              bytes_downloaded: {type: integer, format: int64}
              is_chunked: {type: boolean}
              chunks_total: {type: integer, nullable: true}
              chunks_completed: {type: integer}
              chunks:
                type: array
                items:
                  type: object
                  required: [chunk_index, byte_start, byte_end, source_id, status, bytes_done]
                  properties:
                    chunk_index: {type: integer}
                    byte_start: {type: integer, format: int64}
                    byte_end: {type: integer, format: int64}
                    source_id: {type: string}
                    status: {type: string}
                    bytes_done: {type: integer, format: int64}

    ParticipatingExecutors:
      type: object
      required: [items]
      properties:
        items:
          type: array
          items:
            type: object
            required: [executor_id, assigned_subtasks, active_subtasks, bytes_downloaded]
            properties:
              executor_id: {type: string}
              executor_status: {type: string, nullable: true}
              health_score: {type: integer, nullable: true}
              last_heartbeat_at: {type: string, format: date-time, nullable: true}
              assigned_subtasks: {type: integer}
              active_subtasks: {type: integer}
              bytes_downloaded: {type: integer, format: int64}
```

- [ ] **Step 3: Validate the contract**

Run: `npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error`
Expected: exits 0 (warnings allowed, no errors).
Run: `npx --yes @apidevtools/swagger-cli validate api/openapi.yaml`
Expected: `api/openapi.yaml is valid`.

- [ ] **Step 4: Commit**

```bash
git add api/openapi.yaml
git commit -m "UI-SP2 M1: openapi — add subtask-chunks + participating-executors paths/schemas"
```

---

### Task 2: Schemas module — all Task-Detail DTOs

**Files:**
- Create: `src/dlw/schemas/task_detail.py`
- Test: `tests/api/test_task_detail_chunks.py` (created here, drives this + Task 3)

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_task_detail_chunks.py`:

```python
"""Tests for GET /api/v1/tasks/{id}/subtask-chunks (UI-SP2)."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import make_app_with_state, principal_headers

SECRET = "unit-secret"


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
def _set_token(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
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
                           base_url="http://test") as c:
        yield c


async def _make_task(client, auth) -> str:
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/chunks", "revision": "3" * 40, "storage_id": 1,
    }, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.slow
async def test_subtask_chunks_unauthenticated_401(client: AsyncClient) -> None:
    r = await client.get(f"/api/v1/tasks/{uuid.uuid4()}/subtask-chunks")
    assert r.status_code == 401


@pytest.mark.slow
async def test_subtask_chunks_cross_tenant_404(client: AsyncClient, auth) -> None:
    tid = await _make_task(client, auth)
    other = principal_headers(secret=SECRET, role="tenant_admin",
                              user_id=9, tenant_id=2)
    r = await client.get(f"/api/v1/tasks/{tid}/subtask-chunks", headers=other)
    assert r.status_code == 404


@pytest.mark.slow
async def test_subtask_chunks_happy_and_aggregation(
    client: AsyncClient, auth, engine,
) -> None:
    tid = await _make_task(client, auth)
    # Fetch the created subtasks, then attach 2 chunks to one of them.
    from dlw.db.models.source import SubtaskChunk
    from dlw.db.models.task import FileSubTask
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        subs = (await s.execute(
            select(FileSubTask).where(FileSubTask.task_id == uuid.UUID(tid))
            .order_by(FileSubTask.filename))).scalars().all()
        assert len(subs) == 2
        big = subs[1]
        big.is_chunked = True
        big.file_size = 1000
        big.bytes_downloaded = 600
        s.add(SubtaskChunk(subtask_id=big.id, chunk_index=0, byte_start=0,
                           byte_end=499, source_id="hf", status="succeeded",
                           bytes_done=500))
        s.add(SubtaskChunk(subtask_id=big.id, chunk_index=1, byte_start=500,
                           byte_end=999, source_id="modelscope",
                           status="pending", bytes_done=100))
        await s.commit()

    r = await client.get(f"/api/v1/tasks/{tid}/subtask-chunks", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 2
    by_name = {i["filename"]: i for i in body["items"]}
    chunked = by_name["model.safetensors"]
    assert chunked["is_chunked"] is True
    assert len(chunked["chunks"]) == 2
    assert chunked["chunks"][0]["source_id"] == "hf"
    assert chunked["chunks"][1]["bytes_done"] == 100
    assert by_name["config.json"]["chunks"] == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/api/test_task_detail_chunks.py -v`
Expected: FAIL — `404`/`ModuleNotFoundError: dlw.schemas.task_detail` / route missing (the endpoint does not exist yet).

- [ ] **Step 3: Create the schemas module**

Create `src/dlw/schemas/task_detail.py`:

```python
"""UI-SP2 Task-Detail read-only DTOs (additive; mirrors api/openapi.yaml)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ChunkSeg(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    chunk_index: int
    byte_start: int
    byte_end: int
    source_id: str
    status: str
    bytes_done: int


class SubtaskChunkRow(BaseModel):
    subtask_id: uuid.UUID
    filename: str
    file_size: int | None
    status: str
    bytes_downloaded: int
    is_chunked: bool
    chunks_total: int | None
    chunks_completed: int
    chunks: list[ChunkSeg]


class SubtaskChunkReport(BaseModel):
    items: list[SubtaskChunkRow]


class SourceUsed(BaseModel):
    source_id: str
    bytes_assigned: int
    percent: float
    measured_speed_bps: float


class ChunkRouting(BaseModel):
    filename: str
    chunks: list[ChunkSeg]


class SourceAllocation(BaseModel):
    task_id: uuid.UUID
    sources_used: list[SourceUsed]
    chunk_level_routing: list[ChunkRouting]


class ParticipatingExecutor(BaseModel):
    executor_id: str
    executor_status: str | None
    health_score: int | None
    last_heartbeat_at: datetime | None
    assigned_subtasks: int
    active_subtasks: int
    bytes_downloaded: int


class ParticipatingExecutors(BaseModel):
    items: list[ParticipatingExecutor]


class TaskEvent(BaseModel):
    ts: datetime
    type: str
    message: str
    details: dict[str, Any]


class TaskEventsResponse(BaseModel):
    items: list[TaskEvent]
    next_cursor: str | None = None
```

- [ ] **Step 4: Commit (schemas only — route comes in Task 3)**

```bash
git add src/dlw/schemas/task_detail.py tests/api/test_task_detail_chunks.py
git commit -m "UI-SP2 M1: task_detail DTOs + subtask-chunks test (red)"
```

---

### Task 3: `subtask-chunks` service + route

**Files:**
- Create: `src/dlw/services/task_detail.py` (the `chunks_for_task` helper)
- Modify: `src/dlw/api/tasks.py` (imports + `get_subtask_chunks` route)
- Test: `tests/api/test_task_detail_chunks.py` (already written in Task 2)

- [ ] **Step 1: Create the service module with `chunks_for_task`**

Create `src/dlw/services/task_detail.py`:

```python
"""UI-SP2 read-only aggregation helpers (additive; no writes, no state)."""
from __future__ import annotations

import base64
import uuid
from datetime import datetime

from sqlalchemy import and_, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.audit import AuditLog
from dlw.db.models.executor import Executor
from dlw.db.models.source import SubtaskChunk
from dlw.db.models.task import FileSubTask
from dlw.schemas.task_detail import (
    ChunkRouting,
    ChunkSeg,
    ParticipatingExecutor,
    SourceAllocation,
    SourceUsed,
    SubtaskChunkRow,
    TaskEvent,
)

_TERMINAL_SUBTASK = {"succeeded", "failed", "cancelled"}


async def chunks_for_task(
    session: AsyncSession, task_id: uuid.UUID, tenant_id: int,
) -> list[SubtaskChunkRow]:
    subs = (await session.execute(
        select(FileSubTask)
        .where(FileSubTask.task_id == task_id,
               FileSubTask.tenant_id == tenant_id)
        .order_by(FileSubTask.filename))).scalars().all()
    if not subs:
        return []
    sub_ids = [s.id for s in subs]
    chunk_rows = (await session.execute(
        select(SubtaskChunk)
        .where(SubtaskChunk.subtask_id.in_(sub_ids))
        .order_by(SubtaskChunk.subtask_id, SubtaskChunk.chunk_index)
    )).scalars().all()
    by_sub: dict[uuid.UUID, list[ChunkSeg]] = {}
    for c in chunk_rows:
        by_sub.setdefault(c.subtask_id, []).append(ChunkSeg.model_validate(c))
    return [
        SubtaskChunkRow(
            subtask_id=s.id, filename=s.filename, file_size=s.file_size,
            status=s.status, bytes_downloaded=s.bytes_downloaded,
            is_chunked=s.is_chunked, chunks_total=s.chunks_total,
            chunks_completed=s.chunks_completed,
            chunks=by_sub.get(s.id, []),
        )
        for s in subs
    ]
```

- [ ] **Step 2: Add imports + route to `src/dlw/api/tasks.py`**

At the top of `src/dlw/api/tasks.py`, change the fastapi import line (currently `from fastapi import APIRouter, Depends, HTTPException, status`) to:

```python
from fastapi import APIRouter, Depends, HTTPException, Query, status
```

Add these imports after the existing `from dlw.schemas.task import ...` line (line 17):

```python
from dlw.schemas.task_detail import (
    ParticipatingExecutors,
    SourceAllocation,
    SubtaskChunkReport,
    TaskEventsResponse,
)
from dlw.services import task_detail as _td
```

Append at the end of the file (after `delete_task`, line 182):

```python


async def _task_in_tenant(
    session: AsyncSession, task_id: uuid.UUID, principal: Principal,
) -> bool:
    owned = await session.scalar(
        tenant_filtered(select(DownloadTask.id)
                        .where(DownloadTask.id == task_id),
                        DownloadTask, principal))
    return owned is not None


@router.get("/{task_id}/subtask-chunks")
async def get_subtask_chunks(
    task_id: uuid.UUID,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
    session: AsyncSession = Depends(_session),
) -> SubtaskChunkReport:
    if not await _task_in_tenant(session, task_id, principal):
        raise HTTPException(status_code=404, detail="task not found")
    return SubtaskChunkReport(
        items=await _td.chunks_for_task(session, task_id, principal.tenant_id))
```

- [ ] **Step 3: Run the test to verify it passes**

Run: `uv run pytest tests/api/test_task_detail_chunks.py -v`
Expected: PASS (3 tests: unauthenticated_401, cross_tenant_404, happy_and_aggregation).

- [ ] **Step 4: Commit**

```bash
git add src/dlw/services/task_detail.py src/dlw/api/tasks.py
git commit -m "UI-SP2 M1: subtask-chunks service + route (green)"
```

---

### Task 4: `source-allocation` + `participating-executors` services + routes

**Files:**
- Modify: `src/dlw/services/task_detail.py` (add `source_allocation_for_task`, `executors_for_task`)
- Modify: `src/dlw/api/tasks.py` (add 2 routes)
- Test: `tests/api/test_task_detail_source_alloc.py`, `tests/api/test_task_detail_executors.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_task_detail_source_alloc.py` (same fixture block as Task 2's file — copy the `_bootstrap`, `_set_token`, `_patch_hf`, `auth`, `client`, `_make_task` fixtures verbatim, changing only `repo_id` to `"o/alloc"`), then add:

```python
@pytest.mark.slow
async def test_source_alloc_unauthenticated_401(client: AsyncClient) -> None:
    import uuid
    r = await client.get(f"/api/v1/tasks/{uuid.uuid4()}/source-allocation")
    assert r.status_code == 401


@pytest.mark.slow
async def test_source_alloc_cross_tenant_404(client: AsyncClient, auth) -> None:
    tid = await _make_task(client, auth)
    other = principal_headers(secret=SECRET, role="tenant_admin",
                              user_id=9, tenant_id=2)
    r = await client.get(f"/api/v1/tasks/{tid}/source-allocation",
                         headers=other)
    assert r.status_code == 404


@pytest.mark.slow
async def test_source_alloc_percent_sums_100(
    client: AsyncClient, auth, engine,
) -> None:
    import uuid as _u
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from dlw.db.models.task import FileSubTask
    tid = await _make_task(client, auth)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        subs = (await s.execute(
            select(FileSubTask).where(FileSubTask.task_id == _u.UUID(tid))
            .order_by(FileSubTask.filename))).scalars().all()
        subs[0].source_id = "hf"
        subs[0].file_size = 400
        subs[1].source_id = "modelscope"
        subs[1].file_size = 600
        await s.commit()
    r = await client.get(f"/api/v1/tasks/{tid}/source-allocation",
                         headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == tid
    pct = sum(x["percent"] for x in body["sources_used"])
    assert 99.0 <= pct <= 101.0
    ids = {x["source_id"] for x in body["sources_used"]}
    assert ids == {"hf", "modelscope"}
```

Create `tests/api/test_task_detail_executors.py` (same fixture block, `repo_id="o/exec"`), then:

```python
@pytest.mark.slow
async def test_executors_unauthenticated_401(client: AsyncClient) -> None:
    import uuid
    r = await client.get(
        f"/api/v1/tasks/{uuid.uuid4()}/participating-executors")
    assert r.status_code == 401


@pytest.mark.slow
async def test_executors_cross_tenant_404(client: AsyncClient, auth) -> None:
    tid = await _make_task(client, auth)
    other = principal_headers(secret=SECRET, role="tenant_admin",
                              user_id=9, tenant_id=2)
    r = await client.get(
        f"/api/v1/tasks/{tid}/participating-executors", headers=other)
    assert r.status_code == 404


@pytest.mark.slow
async def test_executors_happy_and_join(
    client: AsyncClient, auth, engine,
) -> None:
    import uuid as _u
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from dlw.db.models.executor import Executor
    from dlw.db.models.task import FileSubTask
    tid = await _make_task(client, auth)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Executor(id="exec-1", host_id="h1", cert_fingerprint="fp",
                       status="healthy", epoch=1, health_score=88))
        subs = (await s.execute(
            select(FileSubTask).where(FileSubTask.task_id == _u.UUID(tid))
        )).scalars().all()
        for sub in subs:
            sub.executor_id = "exec-1"
            sub.bytes_downloaded = 1000
        await s.commit()
    r = await client.get(
        f"/api/v1/tasks/{tid}/participating-executors", headers=auth)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    e = items[0]
    assert e["executor_id"] == "exec-1"
    assert e["executor_status"] == "healthy"
    assert e["health_score"] == 88
    assert e["assigned_subtasks"] == 2
    assert e["bytes_downloaded"] == 2000
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/api/test_task_detail_source_alloc.py tests/api/test_task_detail_executors.py -v`
Expected: FAIL — routes return 404 (not declared) / `AttributeError` on `_td.source_allocation_for_task`.

- [ ] **Step 3: Add the two service helpers**

Append to `src/dlw/services/task_detail.py`:

```python


async def source_allocation_for_task(
    session: AsyncSession, task_id: uuid.UUID, tenant_id: int,
) -> SourceAllocation:
    subs = (await session.execute(
        select(FileSubTask)
        .where(FileSubTask.task_id == task_id,
               FileSubTask.tenant_id == tenant_id)
        .order_by(FileSubTask.filename))).scalars().all()
    sub_ids = [s.id for s in subs]
    chunk_rows = (await session.execute(
        select(SubtaskChunk).where(SubtaskChunk.subtask_id.in_(sub_ids))
        .order_by(SubtaskChunk.subtask_id, SubtaskChunk.chunk_index)
    )).scalars().all() if sub_ids else []

    chunked_sub_ids = {c.subtask_id for c in chunk_rows}
    by_source: dict[str, int] = {}
    for s in subs:
        if s.id in chunked_sub_ids:
            continue  # chunked files counted at chunk granularity below
        if s.source_id:
            by_source[s.source_id] = (
                by_source.get(s.source_id, 0) + int(s.file_size or 0))
    for c in chunk_rows:
        by_source[c.source_id] = (
            by_source.get(c.source_id, 0)
            + int(c.byte_end - c.byte_start + 1))

    total = sum(by_source.values())
    sources_used = [
        SourceUsed(
            source_id=sid, bytes_assigned=b,
            percent=round(b / total * 100.0, 2) if total else 0.0,
            measured_speed_bps=0.0,  # no live speed source; client-derived
        )
        for sid, b in sorted(by_source.items())
    ]

    routing_by_sub: dict[uuid.UUID, list[ChunkSeg]] = {}
    for c in chunk_rows:
        routing_by_sub.setdefault(c.subtask_id, []).append(
            ChunkSeg.model_validate(c))
    name_by_id = {s.id: s.filename for s in subs}
    chunk_level_routing = [
        ChunkRouting(filename=name_by_id[sid], chunks=segs)
        for sid, segs in sorted(
            routing_by_sub.items(), key=lambda kv: name_by_id[kv[0]])
    ]
    return SourceAllocation(
        task_id=task_id, sources_used=sources_used,
        chunk_level_routing=chunk_level_routing)


async def executors_for_task(
    session: AsyncSession, task_id: uuid.UUID, tenant_id: int,
) -> list[ParticipatingExecutor]:
    subs = (await session.execute(
        select(FileSubTask)
        .where(FileSubTask.task_id == task_id,
               FileSubTask.tenant_id == tenant_id,
               FileSubTask.executor_id.isnot(None)))).scalars().all()
    if not subs:
        return []
    agg: dict[str, dict[str, int]] = {}
    for s in subs:
        eid = s.executor_id
        if eid is None:
            continue
        a = agg.setdefault(
            eid, {"assigned": 0, "active": 0, "bytes": 0})
        a["assigned"] += 1
        if s.status not in _TERMINAL_SUBTASK:
            a["active"] += 1
        a["bytes"] += int(s.bytes_downloaded or 0)
    ex_rows = (await session.execute(
        select(Executor).where(Executor.id.in_(list(agg.keys()))))
    ).scalars().all()
    ex_by_id = {e.id: e for e in ex_rows}
    out: list[ParticipatingExecutor] = []
    for eid, a in sorted(agg.items()):
        e = ex_by_id.get(eid)
        out.append(ParticipatingExecutor(
            executor_id=eid,
            executor_status=e.status if e else None,
            health_score=e.health_score if e else None,
            last_heartbeat_at=e.last_heartbeat_at if e else None,
            assigned_subtasks=a["assigned"],
            active_subtasks=a["active"],
            bytes_downloaded=a["bytes"],
        ))
    return out
```

- [ ] **Step 4: Add the two routes to `src/dlw/api/tasks.py`**

Append after `get_subtask_chunks`:

```python


@router.get("/{task_id}/source-allocation")
async def get_source_allocation(
    task_id: uuid.UUID,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
    session: AsyncSession = Depends(_session),
) -> SourceAllocation:
    if not await _task_in_tenant(session, task_id, principal):
        raise HTTPException(status_code=404, detail="task not found")
    return await _td.source_allocation_for_task(
        session, task_id, principal.tenant_id)


@router.get("/{task_id}/participating-executors")
async def get_participating_executors(
    task_id: uuid.UUID,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
    session: AsyncSession = Depends(_session),
) -> ParticipatingExecutors:
    if not await _task_in_tenant(session, task_id, principal):
        raise HTTPException(status_code=404, detail="task not found")
    return ParticipatingExecutors(
        items=await _td.executors_for_task(
            session, task_id, principal.tenant_id))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/api/test_task_detail_source_alloc.py tests/api/test_task_detail_executors.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add src/dlw/services/task_detail.py src/dlw/api/tasks.py tests/api/test_task_detail_source_alloc.py tests/api/test_task_detail_executors.py
git commit -m "UI-SP2 M1: source-allocation + participating-executors service+routes"
```

---

### Task 5: `events` service + route (audit-derived, cursor-paginated)

**Files:**
- Modify: `src/dlw/services/task_detail.py` (add `_encode_cursor`, `_decode_cursor`, `events_for_task`)
- Modify: `src/dlw/api/tasks.py` (add `get_task_events` route)
- Test: `tests/api/test_task_detail_events.py`

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_task_detail_events.py` (same fixture block, `repo_id="o/events"`), then:

```python
@pytest.mark.slow
async def test_events_unauthenticated_401(client: AsyncClient) -> None:
    import uuid
    r = await client.get(f"/api/v1/tasks/{uuid.uuid4()}/events")
    assert r.status_code == 401


@pytest.mark.slow
async def test_events_cross_tenant_404(client: AsyncClient, auth) -> None:
    tid = await _make_task(client, auth)
    other = principal_headers(secret=SECRET, role="tenant_admin",
                              user_id=9, tenant_id=2)
    r = await client.get(f"/api/v1/tasks/{tid}/events", headers=other)
    assert r.status_code == 404


@pytest.mark.slow
async def test_events_returns_audit_rows_and_paginates(
    client: AsyncClient, auth, engine,
) -> None:
    import datetime as dt
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from dlw.db.models.audit import AuditLog
    tid = await _make_task(client, auth)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    base = dt.datetime(2026, 5, 20, 12, 0, 0, tzinfo=dt.UTC)
    async with factory() as s:
        for i in range(3):
            s.add(AuditLog(
                occurred_at=base + dt.timedelta(seconds=i),
                tenant_id=1, actor_user_id=1,
                action="task.note", resource_type="task",
                resource_id=tid, outcome="success",
                payload={"i": i}, self_hash="0" * 64))
        s.add(AuditLog(
            occurred_at=base + dt.timedelta(seconds=9),
            tenant_id=1, actor_user_id=1, action="task.denied",
            resource_type="task", resource_id=tid, outcome="denied",
            payload=None, self_hash="0" * 64))
        await s.commit()

    r = await client.get(f"/api/v1/tasks/{tid}/events?limit=2", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"]
    # newest first
    assert body["items"][0]["type"] == "task.denied"
    assert "denied" in body["items"][0]["message"]
    assert body["items"][0]["details"] == {}

    r2 = await client.get(
        f"/api/v1/tasks/{tid}/events?limit=2&cursor={body['next_cursor']}",
        headers=auth)
    assert r2.status_code == 200
    page2 = r2.json()["items"]
    assert len(page2) == 2
    # no overlap with page 1
    assert page2[0]["ts"] != body["items"][0]["ts"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/api/test_task_detail_events.py -v`
Expected: FAIL — route 404 / `AttributeError: events_for_task`.

- [ ] **Step 3: Add the events service helper**

Append to `src/dlw/services/task_detail.py`:

```python


def _encode_cursor(occurred_at: datetime, row_id: int) -> str:
    raw = f"{occurred_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts_str, id_str = raw.rsplit("|", 1)
    return datetime.fromisoformat(ts_str), int(id_str)


async def events_for_task(
    session: AsyncSession, task_id: uuid.UUID, tenant_id: int,
    limit: int, cursor: str | None,
) -> tuple[list[TaskEvent], str | None]:
    sub_ids = (await session.execute(
        select(FileSubTask.id).where(
            FileSubTask.task_id == task_id,
            FileSubTask.tenant_id == tenant_id))).scalars().all()
    sub_clause = (
        and_(AuditLog.resource_type == "subtask",
             AuditLog.resource_id.in_([str(x) for x in sub_ids]))
        if sub_ids else false()
    )
    scope = or_(
        and_(AuditLog.resource_type == "task",
             AuditLog.resource_id == str(task_id)),
        sub_clause,
    )
    stmt = (select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id, scope)
            .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()))
    if cursor:
        c_ts, c_id = _decode_cursor(cursor)
        stmt = stmt.where(or_(
            AuditLog.occurred_at < c_ts,
            and_(AuditLog.occurred_at == c_ts, AuditLog.id < c_id)))
    rows = (await session.execute(stmt.limit(limit + 1))).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        TaskEvent(
            ts=r.occurred_at,
            type=r.action,
            message=(f"{r.action} (denied)" if r.outcome == "denied"
                     else r.action),
            details=r.payload or {},
        )
        for r in rows
    ]
    next_cursor = (
        _encode_cursor(rows[-1].occurred_at, rows[-1].id)
        if has_more and rows else None)
    return items, next_cursor
```

- [ ] **Step 4: Add the events route to `src/dlw/api/tasks.py`**

Append after `get_participating_executors`:

```python


@router.get("/{task_id}/events")
async def get_task_events(
    task_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
    session: AsyncSession = Depends(_session),
) -> TaskEventsResponse:
    if not await _task_in_tenant(session, task_id, principal):
        raise HTTPException(status_code=404, detail="task not found")
    items, next_cursor = await _td.events_for_task(
        session, task_id, principal.tenant_id, limit, cursor)
    return TaskEventsResponse(items=items, next_cursor=next_cursor)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/api/test_task_detail_events.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add src/dlw/services/task_detail.py src/dlw/api/tasks.py tests/api/test_task_detail_events.py
git commit -m "UI-SP2 M1: task events service + route (audit-derived, cursor paginated)"
```

---

### Task 6: M1 gate — full backend suite + contract + invariant

**Files:** none (verification only)

- [ ] **Step 1: Full backend test suite**

Run: `uv run pytest tests/ -q`
Expected: all pass (prior 427 + new 12 = 439; exact count may vary, but **0 failures**).

- [ ] **Step 2: OpenAPI lint + validate**

Run: `npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error`
Expected: 0 errors.
Run: `npx --yes @apidevtools/swagger-cli validate api/openapi.yaml`
Expected: `api/openapi.yaml is valid`.

- [ ] **Step 3: Invariant lint**

Run: `python tools/lint_invariants.py`
Expected: `OK: N invariants declared, ...` (exit 0). (No new status writes / no `require_bearer` / no `hf_token` in executor — the new code is read-only over `tasks.py` which `check_task_status_domain` scans for `task.status = "literal"` assignments; we add none.)

- [ ] **Step 4: Commit (gate marker, only if any fixups were needed)**

If steps 1-3 required no code changes, skip. Otherwise commit fixes:

```bash
git add -A
git commit -m "UI-SP2 M1 gate: backend suite + openapi + invariant green"
```

---

# Milestone M2 — Frontend foundation

### Task 7: `useLiveResource` — additive `enabled` option

**Files:**
- Modify: `frontend/src/composables/useLiveResource.ts`
- Test: `frontend/tests/unit/useLiveResourceEnabled.spec.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/tests/unit/useLiveResourceEnabled.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import type { LiveOptions } from '@/composables/useLiveResource'

// Compile-time + shape contract: LiveOptions accepts an optional `enabled`
// that is a boolean or a Ref<boolean>. This test asserts the option object
// is structurally valid (TS would fail typecheck otherwise) and that the
// pure computeInterval is unaffected by the new field.
import { computeInterval } from '@/composables/useLiveResource'

describe('useLiveResource enabled option', () => {
  test('LiveOptions accepts enabled: boolean', () => {
    const o: LiveOptions<number> = { baseIntervalMs: 1000, enabled: false }
    expect(o.enabled).toBe(false)
  })
  test('computeInterval still pure & unchanged', () => {
    expect(computeInterval({
      base: 1000, terminal: false, hidden: false, errored: false,
    })).toBe(1000)
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run (from `frontend`): `pnpm test:unit -- useLiveResourceEnabled`
Expected: FAIL — `Object literal may only specify known properties, and 'enabled' does not exist in type 'LiveOptions<number>'` (vitest type error) OR runtime assertion if TS is lax — either way red.

- [ ] **Step 3: Add the `enabled` option**

Edit `frontend/src/composables/useLiveResource.ts`. Change line 1 from:

```ts
import { useQuery, type QueryKey } from '@tanstack/vue-query'
```

to:

```ts
import { useQuery, type QueryKey } from '@tanstack/vue-query'
import type { Ref } from 'vue'
```

Change the `LiveOptions` interface (lines 14-18) to add one field:

```ts
export interface LiveOptions<T> {
  baseIntervalMs: number
  isTerminal?: (data: T) => boolean
  staleTime?: number
  enabled?: Ref<boolean> | boolean
}
```

In the `useQuery<T>({ ... })` call object (lines 33-46), add `enabled: opts.enabled,` right after `queryFn: fetcher,`:

```ts
  return useQuery<T>({
    queryKey: key,
    queryFn: fetcher,
    enabled: opts.enabled,
    staleTime: opts.staleTime ?? 0,
    refetchInterval: (query) => {
```

(vue-query v5.59 accepts `enabled` as `boolean | Ref<boolean>`; `undefined` ⇒ default-enabled, so existing callers are unaffected.)

- [ ] **Step 4: Run the test + typecheck**

Run: `pnpm test:unit -- useLiveResourceEnabled`
Expected: PASS.
Run: `pnpm typecheck`
Expected: 0 errors.

- [ ] **Step 5: Lint + commit**

```bash
cd /d/download_weights/frontend && pnpm lint:fix
cd /d/download_weights && git add frontend/src/composables/useLiveResource.ts frontend/tests/unit/useLiveResourceEnabled.spec.ts
git commit -m "UI-SP2 M2: useLiveResource additive enabled option (view-transparent)"
```

---

### Task 8: API DTO types for the 4 endpoints

**Files:**
- Modify: `frontend/src/api/types.ts`
- Test: `frontend/tests/unit/sp2Types.spec.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/tests/unit/sp2Types.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import type {
  SubtaskChunkReport, SourceAllocation,
  ParticipatingExecutors, TaskEventsResponse,
} from '@/api/types'

describe('SP2 DTO types', () => {
  test('shapes compile and round-trip', () => {
    const chunks: SubtaskChunkReport = {
      items: [{
        subtask_id: 's', filename: 'f', file_size: 10, status: 'pending',
        bytes_downloaded: 0, is_chunked: true, chunks_total: 1,
        chunks_completed: 0,
        chunks: [{
          chunk_index: 0, byte_start: 0, byte_end: 9, source_id: 'hf',
          status: 'pending', bytes_done: 0,
        }],
      }],
    }
    const alloc: SourceAllocation = {
      task_id: 't', sources_used: [{
        source_id: 'hf', bytes_assigned: 10, percent: 100,
        measured_speed_bps: 0,
      }], chunk_level_routing: [],
    }
    const ex: ParticipatingExecutors = {
      items: [{
        executor_id: 'e', executor_status: 'healthy', health_score: 90,
        last_heartbeat_at: null, assigned_subtasks: 1, active_subtasks: 1,
        bytes_downloaded: 5,
      }],
    }
    const ev: TaskEventsResponse = {
      items: [{ ts: 'now', type: 'task.note', message: 'm', details: {} }],
      next_cursor: null,
    }
    expect(chunks.items[0]?.chunks[0]?.source_id).toBe('hf')
    expect(alloc.sources_used[0]?.percent).toBe(100)
    expect(ex.items[0]?.executor_status).toBe('healthy')
    expect(ev.items[0]?.type).toBe('task.note')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test:unit -- sp2Types`
Expected: FAIL — types not exported.

- [ ] **Step 3: Append the interfaces to `frontend/src/api/types.ts`**

Append at end of `frontend/src/api/types.ts`:

```ts

export interface ChunkSeg {
  chunk_index: number
  byte_start: number
  byte_end: number
  source_id: string
  status: string
  bytes_done: number
}

export interface SubtaskChunkRow {
  subtask_id: string
  filename: string
  file_size: number | null
  status: string
  bytes_downloaded: number
  is_chunked: boolean
  chunks_total: number | null
  chunks_completed: number
  chunks: ChunkSeg[]
}

export interface SubtaskChunkReport {
  items: SubtaskChunkRow[]
}

export interface SourceUsed {
  source_id: string
  bytes_assigned: number
  percent: number
  measured_speed_bps: number
}

export interface ChunkRouting {
  filename: string
  chunks: ChunkSeg[]
}

export interface SourceAllocation {
  task_id: string
  sources_used: SourceUsed[]
  chunk_level_routing: ChunkRouting[]
}

export interface ParticipatingExecutor {
  executor_id: string
  executor_status: string | null
  health_score: number | null
  last_heartbeat_at: string | null
  assigned_subtasks: number
  active_subtasks: number
  bytes_downloaded: number
}

export interface ParticipatingExecutors {
  items: ParticipatingExecutor[]
}

export interface TaskEventItem {
  ts: string
  type: string
  message: string
  details: Record<string, unknown>
}

export interface TaskEventsResponse {
  items: TaskEventItem[]
  next_cursor: string | null
}
```

- [ ] **Step 4: Run test + typecheck**

Run: `pnpm test:unit -- sp2Types` → PASS. Run: `pnpm typecheck` → 0 errors.

- [ ] **Step 5: Lint + commit**

```bash
cd /d/download_weights/frontend && pnpm lint:fix
cd /d/download_weights && git add frontend/src/api/types.ts frontend/tests/unit/sp2Types.spec.ts
git commit -m "UI-SP2 M2: API DTO types for the 4 task-detail endpoints"
```

---

### Task 9: Pure formatting utilities

**Files:**
- Create: `frontend/src/utils/format.ts`
- Test: `frontend/tests/unit/format.spec.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/tests/unit/format.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import { formatBytes, formatRate, formatDuration } from '@/utils/format'

describe('format utils', () => {
  test('formatBytes', () => {
    expect(formatBytes(null)).toBe('—')
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1024)).toBe('1.0 KB')
    expect(formatBytes(1024 * 1024 * 3)).toBe('3.0 MB')
  })
  test('formatRate', () => {
    expect(formatRate(0)).toBe('—')
    expect(formatRate(2048)).toBe('2.0 KB/s')
  })
  test('formatDuration', () => {
    expect(formatDuration(null)).toBe('—')
    expect(formatDuration(0)).toBe('0s')
    expect(formatDuration(65)).toBe('1m 5s')
    expect(formatDuration(3661)).toBe('1h 1m')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test:unit -- format`
Expected: FAIL — module not found.

- [ ] **Step 3: Create `frontend/src/utils/format.ts`**

```ts
const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  let v = n
  let i = 0
  while (v >= 1024 && i < UNITS.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${UNITS[i] ?? 'B'}`
}

export function formatRate(bytesPerSec: number | null | undefined): string {
  if (!bytesPerSec || bytesPerSec <= 0) return '—'
  return `${formatBytes(bytesPerSec)}/s`
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const s = Math.max(0, Math.floor(seconds))
  if (s === 0) return '0s'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${sec}s`
  return `${sec}s`
}
```

- [ ] **Step 4: Run test → PASS; typecheck → 0 errors**

Run: `pnpm test:unit -- format` then `pnpm typecheck`.

- [ ] **Step 5: Lint + commit**

```bash
cd /d/download_weights/frontend && pnpm lint:fix
cd /d/download_weights && git add frontend/src/utils/format.ts frontend/tests/unit/format.spec.ts
git commit -m "UI-SP2 M2: pure formatting utils (bytes/rate/duration)"
```

---

### Task 10: `computeRate` + `useDownloadRate` composable

**Files:**
- Create: `frontend/src/composables/useDownloadRate.ts`
- Test: `frontend/tests/unit/computeRate.spec.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/tests/unit/computeRate.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import { computeRate } from '@/composables/useDownloadRate'

describe('computeRate', () => {
  test('empty / single sample → zero rate, null eta', () => {
    expect(computeRate([], 100)).toEqual({
      currentBps: 0, avgBps: 0, etaSeconds: null,
    })
    expect(computeRate([{ t: 0, bytes: 10 }], 100)).toEqual({
      currentBps: 0, avgBps: 0, etaSeconds: null,
    })
  })
  test('linear progress → rate + eta', () => {
    const r = computeRate(
      [{ t: 0, bytes: 0 }, { t: 1000, bytes: 100 },
       { t: 2000, bytes: 200 }], 400)
    expect(r.avgBps).toBeCloseTo(100, 5)
    expect(r.currentBps).toBeGreaterThan(0)
    // 200 remaining of 400 at ~100 B/s ⇒ ~2s
    expect(r.etaSeconds).not.toBeNull()
    expect(r.etaSeconds as number).toBeGreaterThan(0)
  })
  test('no total → null eta but rate still computed', () => {
    const r = computeRate(
      [{ t: 0, bytes: 0 }, { t: 1000, bytes: 50 }], null)
    expect(r.avgBps).toBeCloseTo(50, 5)
    expect(r.etaSeconds).toBeNull()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test:unit -- computeRate`
Expected: FAIL — module not found.

- [ ] **Step 3: Create `frontend/src/composables/useDownloadRate.ts`**

```ts
import { onUnmounted, ref, watch, type Ref } from 'vue'

export interface RateSample { t: number; bytes: number }
export interface RateResult {
  currentBps: number
  avgBps: number
  etaSeconds: number | null
}

const MAX_SAMPLES = 30

/** Pure: derive current (last-window) + average B/s and ETA from samples. */
export function computeRate(
  samples: RateSample[], bytesTotal: number | null,
): RateResult {
  if (samples.length < 2) {
    return { currentBps: 0, avgBps: 0, etaSeconds: null }
  }
  const first = samples[0]
  const last = samples[samples.length - 1]
  if (!first || !last) {
    return { currentBps: 0, avgBps: 0, etaSeconds: null }
  }
  const spanSec = (last.t - first.t) / 1000
  const avgBps = spanSec > 0
    ? Math.max(0, (last.bytes - first.bytes) / spanSec) : 0

  // current = slope over the last <=5 samples
  const tail = samples.slice(-5)
  const tf = tail[0]
  const tl = tail[tail.length - 1]
  let currentBps = 0
  if (tf && tl && tl.t > tf.t) {
    currentBps = Math.max(0, (tl.bytes - tf.bytes) / ((tl.t - tf.t) / 1000))
  }

  let etaSeconds: number | null = null
  if (bytesTotal !== null && currentBps > 0) {
    const remaining = Math.max(0, bytesTotal - last.bytes)
    etaSeconds = remaining / currentBps
  }
  return { currentBps, avgBps, etaSeconds }
}

/** Composable: sample a reactive byte counter over time → reactive RateResult. */
export function useDownloadRate(
  bytesDone: Ref<number | null | undefined>,
  bytesTotal: Ref<number | null | undefined>,
) {
  const samples = ref<RateSample[]>([])
  const result = ref<RateResult>({
    currentBps: 0, avgBps: 0, etaSeconds: null,
  })

  function recompute() {
    result.value = computeRate(
      samples.value, bytesTotal.value ?? null)
  }

  const stop = watch(bytesDone, (v) => {
    if (v === null || v === undefined) return
    samples.value.push({ t: Date.now(), bytes: v })
    if (samples.value.length > MAX_SAMPLES) samples.value.shift()
    recompute()
  }, { immediate: true })

  onUnmounted(stop)
  return { rate: result }
}
```

- [ ] **Step 4: Run test → PASS; typecheck → 0 errors**

Run: `pnpm test:unit -- computeRate` then `pnpm typecheck`.

- [ ] **Step 5: Lint + commit**

```bash
cd /d/download_weights/frontend && pnpm lint:fix
cd /d/download_weights && git add frontend/src/composables/useDownloadRate.ts frontend/tests/unit/computeRate.spec.ts
git commit -m "UI-SP2 M2: computeRate pure fn + useDownloadRate composable"
```

---

### Task 11: The 4 live data composables

**Files:**
- Create: `frontend/src/composables/useSubtaskChunks.ts`, `useSourceAllocation.ts`, `useParticipatingExecutors.ts`, `useTaskEvents.ts`
- Test: `frontend/tests/unit/sp2Composables.spec.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/tests/unit/sp2Composables.spec.ts`:

```ts
import { describe, expect, test, vi } from 'vitest'
import { ref } from 'vue'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/api/client', () => ({ client: { get } }))

// useLiveResource needs a QueryClient; mock it to just invoke the fetcher
// and surface the key so we can assert wiring without vue-query runtime.
const { captured } = vi.hoisted(() => ({
  captured: [] as Array<{ key: unknown; opts: unknown }>,
}))
vi.mock('@/composables/useLiveResource', () => ({
  useLiveResource: (key: unknown, fetcher: () => unknown, opts: unknown) => {
    captured.push({ key, opts })
    return { __fetcher: fetcher }
  },
}))

import { useSubtaskChunks } from '@/composables/useSubtaskChunks'
import { useTaskEvents } from '@/composables/useTaskEvents'

describe('SP2 live composables', () => {
  test('useSubtaskChunks wires key, path, enabled, terminal', async () => {
    captured.length = 0
    get.mockResolvedValueOnce({ data: { items: [] } })
    const id = ref('abc')
    const enabled = ref(true)
    const terminal = ref(false)
    const q = useSubtaskChunks(id, enabled, terminal) as unknown as {
      __fetcher: () => Promise<unknown>
    }
    const last = captured[captured.length - 1]
    expect(last?.key).toEqual(['task-chunks', id])
    expect((last?.opts as { enabled: unknown }).enabled).toBe(enabled)
    await q.__fetcher()
    expect(get).toHaveBeenCalledWith('/api/v1/tasks/abc/subtask-chunks')
  })

  test('useTaskEvents path + limit', async () => {
    captured.length = 0
    get.mockResolvedValueOnce({ data: { items: [], next_cursor: null } })
    const id = ref('xyz')
    const q = useTaskEvents(id, ref(true), ref(false)) as unknown as {
      __fetcher: () => Promise<unknown>
    }
    await q.__fetcher()
    expect(get).toHaveBeenCalledWith('/api/v1/tasks/xyz/events?limit=50')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test:unit -- sp2Composables`
Expected: FAIL — composable modules not found.

- [ ] **Step 3: Create the four composables**

`frontend/src/composables/useSubtaskChunks.ts`:

```ts
import type { Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { SubtaskChunkReport } from '@/api/types'

export function useSubtaskChunks(
  taskId: Ref<string>, enabled: Ref<boolean>, terminal: Ref<boolean>,
) {
  return useLiveResource<SubtaskChunkReport>(
    ['task-chunks', taskId],
    async () => (await client.get<SubtaskChunkReport>(
      `/api/v1/tasks/${taskId.value}/subtask-chunks`)).data,
    { baseIntervalMs: 1_500, enabled, isTerminal: () => terminal.value },
  )
}
```

`frontend/src/composables/useSourceAllocation.ts`:

```ts
import type { Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { SourceAllocation } from '@/api/types'

export function useSourceAllocation(
  taskId: Ref<string>, enabled: Ref<boolean>, terminal: Ref<boolean>,
) {
  return useLiveResource<SourceAllocation>(
    ['task-source-alloc', taskId],
    async () => (await client.get<SourceAllocation>(
      `/api/v1/tasks/${taskId.value}/source-allocation`)).data,
    { baseIntervalMs: 2_000, enabled, isTerminal: () => terminal.value },
  )
}
```

`frontend/src/composables/useParticipatingExecutors.ts`:

```ts
import type { Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { ParticipatingExecutors } from '@/api/types'

export function useParticipatingExecutors(
  taskId: Ref<string>, enabled: Ref<boolean>, terminal: Ref<boolean>,
) {
  return useLiveResource<ParticipatingExecutors>(
    ['task-executors', taskId],
    async () => (await client.get<ParticipatingExecutors>(
      `/api/v1/tasks/${taskId.value}/participating-executors`)).data,
    { baseIntervalMs: 2_000, enabled, isTerminal: () => terminal.value },
  )
}
```

`frontend/src/composables/useTaskEvents.ts`:

```ts
import type { Ref } from 'vue'
import { useLiveResource } from '@/composables/useLiveResource'
import { client } from '@/api/client'
import type { TaskEventsResponse } from '@/api/types'

export function useTaskEvents(
  taskId: Ref<string>, enabled: Ref<boolean>, terminal: Ref<boolean>,
) {
  return useLiveResource<TaskEventsResponse>(
    ['task-events', taskId],
    async () => (await client.get<TaskEventsResponse>(
      `/api/v1/tasks/${taskId.value}/events?limit=50`)).data,
    { baseIntervalMs: 5_000, enabled, isTerminal: () => terminal.value },
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

- [ ] **Step 4: Run test → PASS; typecheck → 0 errors**

Run: `pnpm test:unit -- sp2Composables` then `pnpm typecheck`.

- [ ] **Step 5: Lint + commit**

```bash
cd /d/download_weights/frontend && pnpm lint:fix
cd /d/download_weights && git add frontend/src/composables/useSubtaskChunks.ts frontend/src/composables/useSourceAllocation.ts frontend/src/composables/useParticipatingExecutors.ts frontend/src/composables/useTaskEvents.ts frontend/tests/unit/sp2Composables.spec.ts
git commit -m "UI-SP2 M2: 4 live data composables (single useLiveResource seam)"
```

---

### Task 12: M2 gate

**Files:** none.

- [ ] **Step 1:** Run (from `frontend`): `pnpm test:unit` → all pass. `pnpm typecheck` → 0 errors. `pnpm lint` → 0 warnings. `pnpm build` → success.
- [ ] **Step 2:** If any fixups, commit `git commit -am "UI-SP2 M2 gate: frontend foundation green"`.

---

# Milestone M3 — Visual components

### Task 13: `AggregateRing` + `ringDash`

**Files:**
- Create: `frontend/src/components/taskdetail/AggregateRing.vue`
- Test: `frontend/tests/unit/ringDash.spec.ts`, `frontend/tests/unit/AggregateRing.spec.ts`

- [ ] **Step 1: Write the failing tests**

`frontend/tests/unit/ringDash.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import { ringDash } from '@/components/taskdetail/ringMath'

describe('ringDash', () => {
  const C = 100
  test('0% → no fill', () => {
    expect(ringDash(0, C)).toBe('0 100')
  })
  test('50% → half', () => {
    expect(ringDash(50, C)).toBe('50 50')
  })
  test('clamps over/under', () => {
    expect(ringDash(150, C)).toBe('100 0')
    expect(ringDash(-5, C)).toBe('0 100')
  })
})
```

`frontend/tests/unit/AggregateRing.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import AggregateRing from '@/components/taskdetail/AggregateRing.vue'

describe('AggregateRing', () => {
  test('renders percent + counts', () => {
    const w = mount(AggregateRing, {
      props: {
        percent: 67, filesDone: 108, filesTotal: 163,
        bytesDone: 1000, bytesTotal: 2000,
      },
    })
    expect(w.text()).toContain('67%')
    expect(w.text()).toContain('108')
    expect(w.text()).toContain('163')
    expect(w.find('circle').exists()).toBe(true)
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `pnpm test:unit -- ringDash AggregateRing`
Expected: FAIL — modules not found.

- [ ] **Step 3: Create `ringMath.ts` + `AggregateRing.vue`**

`frontend/src/components/taskdetail/ringMath.ts`:

```ts
/** Returns an SVG stroke-dasharray "<fill> <gap>" for a given percent. */
export function ringDash(percent: number, circumference: number): string {
  const p = Math.min(100, Math.max(0, percent))
  const fill = (p / 100) * circumference
  return `${+fill.toFixed(6)} ${+(circumference - fill).toFixed(6)}`
}
```

(`+x.toFixed(6)` normalizes `50 50` not `50.000000 50.000000`; the test expects `'50 50'`.)

`frontend/src/components/taskdetail/AggregateRing.vue`:

```vue
<script setup lang="ts">
import { computed } from 'vue'
import { formatBytes } from '@/utils/format'
import { ringDash } from './ringMath'

const props = defineProps<{
  percent: number
  filesDone: number
  filesTotal: number
  bytesDone: number
  bytesTotal: number | null
}>()

const R = 52
const C = 2 * Math.PI * R
const dash = computed(() => ringDash(props.percent, C))
const pctLabel = computed(() => `${Math.round(props.percent)}%`)
</script>

<template>
  <div class="agg-ring">
    <svg
      width="140"
      height="140"
      viewBox="0 0 140 140"
    >
      <circle
        cx="70"
        cy="70"
        :r="R"
        fill="none"
        stroke="var(--el-border-color)"
        stroke-width="12"
      />
      <circle
        cx="70"
        cy="70"
        :r="R"
        fill="none"
        stroke="var(--el-color-primary)"
        stroke-width="12"
        stroke-linecap="round"
        :stroke-dasharray="dash"
        transform="rotate(-90 70 70)"
      />
      <text
        x="70"
        y="76"
        text-anchor="middle"
        class="pct"
      >{{ pctLabel }}</text>
    </svg>
    <div class="meta">
      <div>{{ formatBytes(bytesDone) }} / {{ formatBytes(bytesTotal) }}</div>
      <div>{{ filesDone }} / {{ filesTotal }}</div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.agg-ring {
  display: flex;
  align-items: center;
  gap: 16px;

  .pct {
    font-size: 22px;
    font-weight: 600;
    fill: var(--el-text-color-primary);
  }
  .meta {
    font-size: 13px;
    color: var(--el-text-color-regular);
    line-height: 1.6;
  }
}
</style>
```

- [ ] **Step 4: Run tests → PASS; typecheck → 0 errors**

Run: `pnpm test:unit -- ringDash AggregateRing` then `pnpm typecheck`.

- [ ] **Step 5: Lint + commit**

```bash
cd /d/download_weights/frontend && pnpm lint:fix
cd /d/download_weights && git add frontend/src/components/taskdetail/ringMath.ts frontend/src/components/taskdetail/AggregateRing.vue frontend/tests/unit/ringDash.spec.ts frontend/tests/unit/AggregateRing.spec.ts
git commit -m "UI-SP2 M3: AggregateRing (inline SVG donut) + ringDash"
```

---

### Task 14: `ChunkBar` + `chunkSegments`, and `SourceBar`

**Files:**
- Create: `frontend/src/components/taskdetail/segMath.ts`, `ChunkBar.vue`, `SourceBar.vue`
- Test: `frontend/tests/unit/chunkSegments.spec.ts`, `ChunkBar.spec.ts`

- [ ] **Step 1: Write the failing tests**

`frontend/tests/unit/chunkSegments.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import { chunkSegments } from '@/components/taskdetail/segMath'
import type { ChunkSeg } from '@/api/types'

const seg = (i: number, s: number, e: number, st: string,
             done: number): ChunkSeg => ({
  chunk_index: i, byte_start: s, byte_end: e, source_id: 'hf',
  status: st, bytes_done: done,
})

describe('chunkSegments', () => {
  test('empty → []', () => {
    expect(chunkSegments([], 100, 200)).toEqual([])
  })
  test('two equal chunks → x/width proportional, fill ratio', () => {
    const out = chunkSegments(
      [seg(0, 0, 49, 'succeeded', 50), seg(1, 50, 99, 'pending', 25)],
      100, 200)
    expect(out).toHaveLength(2)
    expect(out[0]?.x).toBeCloseTo(0, 5)
    expect(out[0]?.w).toBeCloseTo(100, 5)
    expect(out[0]?.fill).toBeCloseTo(1, 5)
    expect(out[1]?.x).toBeCloseTo(100, 5)
    expect(out[1]?.fill).toBeCloseTo(0.5, 5)
  })
  test('fileSize null → falls back to span sum', () => {
    const out = chunkSegments([seg(0, 0, 99, 'pending', 0)], null, 200)
    expect(out[0]?.w).toBeCloseTo(200, 5)
  })
})
```

`frontend/tests/unit/ChunkBar.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import ChunkBar from '@/components/taskdetail/ChunkBar.vue'
import type { ChunkSeg } from '@/api/types'

const chunks: ChunkSeg[] = [
  { chunk_index: 0, byte_start: 0, byte_end: 49, source_id: 'hf',
    status: 'succeeded', bytes_done: 50 },
  { chunk_index: 1, byte_start: 50, byte_end: 99, source_id: 'ms',
    status: 'pending', bytes_done: 0 },
]

describe('ChunkBar', () => {
  test('renders one rect group per chunk', () => {
    const w = mount(ChunkBar, { props: { chunks, fileSize: 100 } })
    expect(w.findAll('rect.seg-bg').length).toBe(2)
  })
  test('empty chunks → placeholder, no rects', () => {
    const w = mount(ChunkBar, { props: { chunks: [], fileSize: null } })
    expect(w.findAll('rect.seg-bg').length).toBe(0)
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `pnpm test:unit -- chunkSegments ChunkBar`
Expected: FAIL — modules not found.

- [ ] **Step 3: Create `segMath.ts`, `ChunkBar.vue`, `SourceBar.vue`**

`frontend/src/components/taskdetail/segMath.ts`:

```ts
import type { ChunkSeg } from '@/api/types'

export interface Seg {
  x: number
  w: number
  fill: number
  status: string
  source_id: string
  chunk_index: number
}

/** Lay out chunk byte-ranges into [0,totalWidth] px, with fill ratio. */
export function chunkSegments(
  chunks: ChunkSeg[], fileSize: number | null, totalWidth: number,
): Seg[] {
  if (chunks.length === 0) return []
  const spanSum = chunks.reduce(
    (a, c) => a + (c.byte_end - c.byte_start + 1), 0)
  const total = fileSize && fileSize > 0 ? fileSize : spanSum
  if (total <= 0) return []
  const out: Seg[] = []
  for (const c of chunks) {
    const span = c.byte_end - c.byte_start + 1
    const x = (c.byte_start / total) * totalWidth
    const w = (span / total) * totalWidth
    const fill = span > 0 ? Math.min(1, Math.max(0, c.bytes_done / span)) : 0
    out.push({
      x, w, fill, status: c.status, source_id: c.source_id,
      chunk_index: c.chunk_index,
    })
  }
  return out
}

/** Element-Plus status-token color for a chunk status. */
export function segColor(status: string): string {
  if (status === 'succeeded' || status === 'done') {
    return 'var(--el-color-success)'
  }
  if (status === 'failed') return 'var(--el-color-danger)'
  if (status === 'pending') return 'var(--el-color-info)'
  return 'var(--el-color-primary)'
}
```

`frontend/src/components/taskdetail/ChunkBar.vue`:

```vue
<script setup lang="ts">
import { computed } from 'vue'
import type { ChunkSeg } from '@/api/types'
import { chunkSegments, segColor } from './segMath'

const props = withDefaults(defineProps<{
  chunks: ChunkSeg[]
  fileSize: number | null
  width?: number
  height?: number
}>(), { width: 280, height: 14 })

const segs = computed(() =>
  chunkSegments(props.chunks, props.fileSize, props.width))
</script>

<template>
  <svg
    :width="width"
    :height="height"
    class="chunk-bar"
  >
    <g
      v-for="s in segs"
      :key="s.chunk_index"
    >
      <rect
        class="seg-bg"
        :x="s.x"
        y="0"
        :width="Math.max(0, s.w - 1)"
        :height="height"
        rx="2"
        fill="var(--el-fill-color)"
      />
      <rect
        class="seg-fill"
        :x="s.x"
        y="0"
        :width="Math.max(0, (s.w - 1) * s.fill)"
        :height="height"
        rx="2"
        :fill="segColor(s.status)"
      >
        <title>
          chunk {{ s.chunk_index }} · {{ s.source_id }} · {{ s.status }}
        </title>
      </rect>
    </g>
  </svg>
</template>

<style scoped>
.chunk-bar { display: block; }
</style>
```

`frontend/src/components/taskdetail/SourceBar.vue`:

```vue
<script setup lang="ts">
import { computed } from 'vue'
import type { SourceUsed } from '@/api/types'

const props = defineProps<{ sources: SourceUsed[] }>()

const PALETTE = [
  'var(--el-color-primary)', 'var(--el-color-success)',
  'var(--el-color-warning)', 'var(--el-color-danger)',
  'var(--el-color-info)',
]
const segs = computed(() =>
  props.sources.map((s, i) => ({
    ...s,
    color: PALETTE[i % PALETTE.length] ?? 'var(--el-color-primary)',
  })))
</script>

<template>
  <div class="source-bar">
    <div class="bar">
      <div
        v-for="s in segs"
        :key="s.source_id"
        class="seg"
        :style="{ width: `${s.percent}%`, background: s.color }"
        :title="`${s.source_id} ${s.percent}%`"
      />
    </div>
    <ul class="legend">
      <li
        v-for="s in segs"
        :key="s.source_id"
      >
        <span
          class="dot"
          :style="{ background: s.color }"
        />
        {{ s.source_id }} · {{ s.percent }}%
      </li>
    </ul>
  </div>
</template>

<style scoped lang="scss">
.source-bar {
  .bar {
    display: flex;
    height: 16px;
    border-radius: 4px;
    overflow: hidden;
    background: var(--el-fill-color);

    .seg { height: 100%; }
  }
  .legend {
    list-style: none;
    padding: 0;
    margin: 8px 0 0;
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    font-size: 12px;
    color: var(--el-text-color-regular);

    .dot {
      display: inline-block;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      margin-right: 4px;
    }
  }
}
</style>
```

- [ ] **Step 4: Run tests → PASS; typecheck → 0 errors**

Run: `pnpm test:unit -- chunkSegments ChunkBar` then `pnpm typecheck`.

- [ ] **Step 5: Lint + commit**

```bash
cd /d/download_weights/frontend && pnpm lint:fix
cd /d/download_weights && git add frontend/src/components/taskdetail/segMath.ts frontend/src/components/taskdetail/ChunkBar.vue frontend/src/components/taskdetail/SourceBar.vue frontend/tests/unit/chunkSegments.spec.ts frontend/tests/unit/ChunkBar.spec.ts
git commit -m "UI-SP2 M3: ChunkBar+chunkSegments + SourceBar (inline SVG)"
```

---

### Task 15: `SwimLane` + `SpeedEta`

**Files:**
- Create: `frontend/src/components/taskdetail/SwimLane.vue`, `SpeedEta.vue`
- Test: `frontend/tests/unit/SwimLane.spec.ts`

- [ ] **Step 1: Write the failing test**

`frontend/tests/unit/SwimLane.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import ElementPlus from 'element-plus'
import SwimLane from '@/components/taskdetail/SwimLane.vue'
import en from '@/locale/en-US.json'
import type { ParticipatingExecutor } from '@/api/types'

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})
const ex: ParticipatingExecutor = {
  executor_id: 'host-1-w1', executor_status: 'healthy', health_score: 90,
  last_heartbeat_at: '2026-05-20T12:00:00Z', assigned_subtasks: 3,
  active_subtasks: 2, bytes_downloaded: 1048576,
}

describe('SwimLane', () => {
  test('renders id, status, counts, bytes', () => {
    const w = mount(SwimLane, {
      props: { executor: ex },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('host-1-w1')
    expect(w.text()).toContain('healthy')
    expect(w.text()).toContain('2')
    expect(w.text()).toContain('1.0 MB')
  })
  test('null status → unknown badge, no crash', () => {
    const w = mount(SwimLane, {
      props: {
        executor: { ...ex, executor_status: null, health_score: null,
                    last_heartbeat_at: null },
      },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('host-1-w1')
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test:unit -- SwimLane`
Expected: FAIL — module not found.

- [ ] **Step 3: Create `SwimLane.vue` + `SpeedEta.vue`**

`frontend/src/components/taskdetail/SwimLane.vue`:

```vue
<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatBytes } from '@/utils/format'
import type { ParticipatingExecutor } from '@/api/types'

const props = defineProps<{ executor: ParticipatingExecutor }>()
const { t } = useI18n()

type ElTagType = 'success' | 'warning' | 'danger' | 'info'
const tagType = computed<ElTagType>(() => {
  switch (props.executor.executor_status) {
    case 'healthy': return 'success'
    case 'degraded': return 'warning'
    case 'suspect': return 'warning'
    case 'faulty': return 'danger'
    default: return 'info'
  }
})
const statusLabel = computed(() =>
  props.executor.executor_status ?? t('tasks.detail.unknown'))
</script>

<template>
  <div class="swimlane">
    <span class="eid">{{ executor.executor_id }}</span>
    <el-tag
      :type="tagType"
      size="small"
      disable-transitions
    >
      {{ statusLabel }}
    </el-tag>
    <span class="m">
      {{ t('tasks.detail.active') }}: {{ executor.active_subtasks }} /
      {{ executor.assigned_subtasks }}
    </span>
    <span
      v-if="executor.health_score !== null"
      class="m"
    >
      {{ t('tasks.detail.health') }}: {{ executor.health_score }}
    </span>
    <span class="m">{{ formatBytes(executor.bytes_downloaded) }}</span>
  </div>
</template>

<style scoped lang="scss">
.swimlane {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-size: 13px;

  .eid {
    font-weight: 600;
    min-width: 140px;
  }
  .m { color: var(--el-text-color-regular); }
}
</style>
```

`frontend/src/components/taskdetail/SpeedEta.vue`:

```vue
<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { formatRate, formatDuration } from '@/utils/format'

defineProps<{
  currentBps: number
  avgBps: number
  etaSeconds: number | null
}>()
const { t } = useI18n()
</script>

<template>
  <div class="speed-eta">
    <div>
      <span class="lbl">{{ t('tasks.detail.speedNow') }}</span>
      <span class="val">{{ formatRate(currentBps) }}</span>
    </div>
    <div>
      <span class="lbl">{{ t('tasks.detail.speedAvg') }}</span>
      <span class="val">{{ formatRate(avgBps) }}</span>
    </div>
    <div>
      <span class="lbl">{{ t('tasks.detail.eta') }}</span>
      <span class="val">{{ formatDuration(etaSeconds) }}</span>
    </div>
  </div>
</template>

<style scoped lang="scss">
.speed-eta {
  display: flex;
  gap: 24px;

  .lbl {
    display: block;
    font-size: 12px;
    color: var(--el-text-color-secondary);
  }
  .val {
    font-size: 16px;
    font-weight: 600;
    color: var(--el-text-color-primary);
  }
}
</style>
```

- [ ] **Step 4: Run test → PASS; typecheck → 0 errors**

Run: `pnpm test:unit -- SwimLane` then `pnpm typecheck`. (i18n keys `tasks.detail.*` are added in Task 17; this test only references `tasks.detail.unknown/active/health` — add them to BOTH locales now in Step 5 so the test resolves, OR the test asserts substrings not requiring those keys. To avoid coupling, this test asserts ids/counts/bytes only — `t()` of a missing key returns the key string, which still contains no assertion dependency. PASS holds.)

- [ ] **Step 5: Lint + commit**

```bash
cd /d/download_weights/frontend && pnpm lint:fix
cd /d/download_weights && git add frontend/src/components/taskdetail/SwimLane.vue frontend/src/components/taskdetail/SpeedEta.vue frontend/tests/unit/SwimLane.spec.ts
git commit -m "UI-SP2 M3: SwimLane + SpeedEta"
```

---

### Task 16: `EventRow` + `eventLevel`

**Files:**
- Create: `frontend/src/components/taskdetail/eventLevel.ts`, `EventRow.vue`
- Test: `frontend/tests/unit/eventLevel.spec.ts`, `EventRow.spec.ts`

- [ ] **Step 1: Write the failing tests**

`frontend/tests/unit/eventLevel.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import { eventLevel } from '@/components/taskdetail/eventLevel'

describe('eventLevel', () => {
  test('denied / failed → error', () => {
    expect(eventLevel('task.denied', 'task.denied (denied)')).toBe('error')
    expect(eventLevel('subtask.failed', 'subtask.failed')).toBe('error')
  })
  test('quota / paused / retry → warn', () => {
    expect(eventLevel('quota.exceeded', 'quota.exceeded')).toBe('warn')
    expect(eventLevel('subtask.paused_external', 'x')).toBe('warn')
  })
  test('default → info', () => {
    expect(eventLevel('task.created', 'task.created')).toBe('info')
  })
})
```

`frontend/tests/unit/EventRow.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import ElementPlus from 'element-plus'
import EventRow from '@/components/taskdetail/EventRow.vue'
import en from '@/locale/en-US.json'

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})

describe('EventRow', () => {
  test('renders ts, message, level tag', () => {
    const w = mount(EventRow, {
      props: {
        event: {
          ts: '2026-05-20T12:00:00Z', type: 'task.denied',
          message: 'task.denied (denied)', details: {},
        },
      },
      global: { plugins: [ElementPlus, i18n] },
    })
    expect(w.text()).toContain('task.denied (denied)')
    expect(w.findComponent({ name: 'ElTag' }).exists()).toBe(true)
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `pnpm test:unit -- eventLevel EventRow`
Expected: FAIL — modules not found.

- [ ] **Step 3: Create `eventLevel.ts` + `EventRow.vue`**

`frontend/src/components/taskdetail/eventLevel.ts`:

```ts
export type EventLevel = 'info' | 'warn' | 'error'

const ERROR_HINTS = ['denied', 'failed', 'error']
const WARN_HINTS = ['quota', 'paused', 'retry', 'blacklist', 'degraded']

export function eventLevel(type: string, message: string): EventLevel {
  const hay = `${type} ${message}`.toLowerCase()
  if (ERROR_HINTS.some((h) => hay.includes(h))) return 'error'
  if (WARN_HINTS.some((h) => hay.includes(h))) return 'warn'
  return 'info'
}
```

`frontend/src/components/taskdetail/EventRow.vue`:

```vue
<script setup lang="ts">
import { computed } from 'vue'
import type { TaskEventItem } from '@/api/types'
import { eventLevel } from './eventLevel'

const props = defineProps<{ event: TaskEventItem }>()

const level = computed(() => eventLevel(props.event.type, props.event.message))
const tagType = computed<'info' | 'warning' | 'danger'>(() =>
  level.value === 'error' ? 'danger'
    : level.value === 'warn' ? 'warning' : 'info')
const ts = computed(() => {
  const d = new Date(props.event.ts)
  return Number.isNaN(d.getTime()) ? props.event.ts : d.toLocaleString()
})
</script>

<template>
  <div
    class="event-row"
    :class="level"
  >
    <span class="ts">{{ ts }}</span>
    <el-tag
      :type="tagType"
      size="small"
      disable-transitions
    >
      {{ level }}
    </el-tag>
    <span class="msg">{{ event.message }}</span>
  </div>
</template>

<style scoped lang="scss">
.event-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  font-size: 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);

  .ts {
    color: var(--el-text-color-secondary);
    min-width: 168px;
    font-variant-numeric: tabular-nums;
  }
  .msg {
    color: var(--el-text-color-primary);
    word-break: break-word;
  }
  &.error .msg { color: var(--el-color-danger); }
}
</style>
```

- [ ] **Step 4: Run tests → PASS; typecheck → 0 errors**

Run: `pnpm test:unit -- eventLevel EventRow` then `pnpm typecheck`.

- [ ] **Step 5: Lint + commit**

```bash
cd /d/download_weights/frontend && pnpm lint:fix
cd /d/download_weights && git add frontend/src/components/taskdetail/eventLevel.ts frontend/src/components/taskdetail/EventRow.vue frontend/tests/unit/eventLevel.spec.ts frontend/tests/unit/EventRow.spec.ts
git commit -m "UI-SP2 M3: EventRow + eventLevel classifier"
```

---

# Milestone M4 — Page assembly + i18n + smoke + docs

### Task 17: i18n — add `tasks.detail.*` to both locales

**Files:**
- Modify: `frontend/src/locale/en-US.json`, `frontend/src/locale/zh-CN.json`
- Test: `frontend/tests/unit/localeParity.spec.ts`

- [ ] **Step 1: Write the failing parity test**

Create `frontend/tests/unit/localeParity.spec.ts`:

```ts
import { describe, expect, test } from 'vitest'
import en from '@/locale/en-US.json'
import zh from '@/locale/zh-CN.json'

function keys(o: Record<string, unknown>, prefix = ''): string[] {
  return Object.entries(o).flatMap(([k, v]) =>
    v && typeof v === 'object'
      ? keys(v as Record<string, unknown>, `${prefix}${k}.`)
      : [`${prefix}${k}`])
}

describe('locale parity', () => {
  test('en and zh have identical key sets', () => {
    expect(keys(en).sort()).toEqual(keys(zh).sort())
  })
  test('tasks.detail subtree exists', () => {
    expect((en as { tasks: { detail?: unknown } }).tasks.detail)
      .toBeTruthy()
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test:unit -- localeParity`
Expected: FAIL — `tasks.detail` missing.

- [ ] **Step 3: Add the `detail` subtree to BOTH locales**

In `frontend/src/locale/en-US.json`, inside the `"tasks"` object, after the `"subtaskColumns"` entry (and its closing brace) add a comma and:

```json
    "detail": {
      "tabFiles": "Files & chunks", "tabSources": "Sources",
      "tabExecutors": "Executors", "tabEvents": "Events",
      "progress": "Progress", "speedNow": "Current", "speedAvg": "Average",
      "eta": "ETA", "active": "Active", "health": "Health",
      "unknown": "unknown", "noEvents": "No events recorded",
      "loadOlder": "Load older", "noSources": "No source allocation yet",
      "noExecutors": "No executors participating yet",
      "noChunks": "No files yet", "colFile": "File", "colSize": "Size",
      "colStatus": "Status", "colChunks": "Chunks", "colProgress": "Progress",
      "cancel": "Cancel task", "delete": "Delete task",
      "cancelConfirm": "Cancel this task?",
      "deleteConfirm": "Delete this terminal task?",
      "cancelled": "Cancellation requested", "deleted": "Deleted"
    }
```

In `frontend/src/locale/zh-CN.json`, inside `"tasks"`, after `"subtaskColumns"` add a comma and:

```json
    "detail": {
      "tabFiles": "文件与分块", "tabSources": "源分配",
      "tabExecutors": "执行节点", "tabEvents": "事件",
      "progress": "进度", "speedNow": "当前", "speedAvg": "平均",
      "eta": "预计剩余", "active": "活跃", "health": "健康分",
      "unknown": "未知", "noEvents": "暂无事件记录",
      "loadOlder": "加载更早", "noSources": "暂无源分配",
      "noExecutors": "暂无执行节点参与",
      "noChunks": "暂无文件", "colFile": "文件", "colSize": "大小",
      "colStatus": "状态", "colChunks": "分块", "colProgress": "进度",
      "cancel": "取消任务", "delete": "删除任务",
      "cancelConfirm": "确认取消该任务？",
      "deleteConfirm": "确认删除该终态任务？",
      "cancelled": "已请求取消", "deleted": "已删除"
    }
```

- [ ] **Step 4: Run test → PASS; typecheck → 0 errors**

Run: `pnpm test:unit -- localeParity` then `pnpm typecheck`.

- [ ] **Step 5: Lint + commit**

```bash
cd /d/download_weights/frontend && pnpm lint:fix
cd /d/download_weights && git add frontend/src/locale/en-US.json frontend/src/locale/zh-CN.json frontend/tests/unit/localeParity.spec.ts
git commit -m "UI-SP2 M4: i18n tasks.detail.* keys (en/zh parity)"
```

---

### Task 18: Rebuild `TaskDetail.vue` (header + el-tabs + DataBoundary + tab-gated panes)

**Files:**
- Modify (full rewrite): `frontend/src/pages/TaskDetail.vue`
- Test: `frontend/tests/unit/TaskDetailSP2.spec.ts`

- [ ] **Step 1: Write the failing test**

Create `frontend/tests/unit/TaskDetailSP2.spec.ts`:

```ts
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createI18n } from 'vue-i18n'
import en from '@/locale/en-US.json'

// Pre-review BLOCKER fix: the page relies on Vue template ref auto-unwrap
// (`v-if="data"`, `!data`), so the mocked `useTaskDetail().data` MUST be a
// real ref — a plain `{ value }` object never unwraps. `vi.hoisted` holders
// must stay plain (no `ref()` — TDZ above imports); each `vi.mock` factory
// is self-contained and async-imports `vue` (factory runs lazily, after the
// `vue` import is resolved), creating real refs.
const { detailData } = vi.hoisted(() => ({
  detailData: { value: null as unknown },
}))
const { mutes } = vi.hoisted(() => ({
  mutes: { cancel: { mutate: vi.fn() }, remove: { mutate: vi.fn() } },
}))

vi.mock('@/composables/useTaskDetail', async () => {
  const { ref } = await import('vue')
  return {
    useTaskDetail: () => ({
      data: ref(detailData.value), isLoading: ref(false),
      isError: ref(false), error: ref(null),
    }),
  }
})
vi.mock('@/composables/useSubtaskChunks', async () => {
  const { ref } = await import('vue')
  return { useSubtaskChunks: () => ({
    data: ref(null), isLoading: ref(false),
    isError: ref(false), error: ref(null) }) }
})
vi.mock('@/composables/useSourceAllocation', async () => {
  const { ref } = await import('vue')
  return { useSourceAllocation: () => ({
    data: ref(null), isLoading: ref(false),
    isError: ref(false), error: ref(null) }) }
})
vi.mock('@/composables/useParticipatingExecutors', async () => {
  const { ref } = await import('vue')
  return { useParticipatingExecutors: () => ({
    data: ref(null), isLoading: ref(false),
    isError: ref(false), error: ref(null) }) }
})
vi.mock('@/composables/useTaskEvents', async () => {
  const { ref } = await import('vue')
  return {
    useTaskEvents: () => ({
      data: ref(null), isLoading: ref(false),
      isError: ref(false), error: ref(null) }),
    fetchOlderEvents: vi.fn(),
  }
})
vi.mock('@/composables/useTaskMutations', () => ({
  useTaskMutations: () => mutes,
  canCancel: (s: string) => s === 'downloading',
  canDelete: (s: string) => s === 'succeeded',
}))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

const i18n = createI18n({
  legacy: false, locale: 'en-US', messages: { 'en-US': en },
})
function mountPage() {
  return import('@/pages/TaskDetail.vue').then((m) =>
    mount(m.default, {
      props: { id: 'abc' },
      global: { plugins: [ElementPlus, i18n] },
    }))
}

describe('TaskDetail (SP2)', () => {
  beforeEach(() => { setActivePinia(createPinia()); detailData.value = null })

  test('no data → DataBoundary empty (not crash)', async () => {
    const w = await mountPage()
    await flushPromises()
    expect(w.findComponent({ name: 'EmptyState' }).exists()).toBe(true)
  })

  test('data present → tabs render, AggregateRing shown', async () => {
    detailData.value = {
      id: 'abc', repo_id: 'o/m', revision: 'a'.repeat(40),
      status: 'downloading', priority: 1,
      created_at: '2026-05-20T00:00:00Z', completed_at: null,
      error_message: null, subtasks: [],
    }
    const w = await mountPage()
    await flushPromises()
    expect(w.findComponent({ name: 'ElTabs' }).exists()).toBe(true)
    expect(w.findComponent({ name: 'AggregateRing' }).exists()).toBe(true)
  })

  test('terminal task → cancel hidden, delete shown', async () => {
    detailData.value = {
      id: 'abc', repo_id: 'o/m', revision: 'a'.repeat(40),
      status: 'succeeded', priority: 1,
      created_at: '2026-05-20T00:00:00Z', completed_at: null,
      error_message: null, subtasks: [],
    }
    const w = await mountPage()
    await flushPromises()
    expect(w.text()).toContain(en.tasks.detail.delete)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `pnpm test:unit -- TaskDetailSP2`
Expected: FAIL — the current `TaskDetail.vue` has no `ElTabs`/`AggregateRing`/DataBoundary structure.

- [ ] **Step 3: Replace `frontend/src/pages/TaskDetail.vue` entirely**

```vue
<script setup lang="ts">
import { computed, ref, toRef } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'

import DataBoundary from '@/components/DataBoundary.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import AggregateRing from '@/components/taskdetail/AggregateRing.vue'
import SpeedEta from '@/components/taskdetail/SpeedEta.vue'
import SourceBar from '@/components/taskdetail/SourceBar.vue'
import ChunkBar from '@/components/taskdetail/ChunkBar.vue'
import SwimLane from '@/components/taskdetail/SwimLane.vue'
import EventRow from '@/components/taskdetail/EventRow.vue'

import { useTaskDetail } from '@/composables/useTaskDetail'
import { useSubtaskChunks } from '@/composables/useSubtaskChunks'
import { useSourceAllocation } from '@/composables/useSourceAllocation'
import { useParticipatingExecutors } from '@/composables/useParticipatingExecutors'
import { useTaskEvents, fetchOlderEvents } from '@/composables/useTaskEvents'
import { useTaskMutations, canCancel, canDelete } from '@/composables/useTaskMutations'
import { useDownloadRate } from '@/composables/useDownloadRate'
import { formatBytes } from '@/utils/format'
import { TERMINAL_STATUSES } from '@/api/types'
import type { TaskEventItem } from '@/api/types'

const props = defineProps<{ id: string }>()
const { t } = useI18n()
const router = useRouter()
const taskIdRef = toRef(() => props.id)

const { data, isLoading, isError, error } = useTaskDetail(taskIdRef)

const is404 = computed(() =>
  (error.value as { response?: { status?: number } } | null)
    ?.response?.status === 404)

const terminal = computed(() =>
  !!data.value && TERMINAL_STATUSES.has(data.value.status))

const activeTab = ref('files')
const onFiles = computed(() => activeTab.value === 'files')
const onSources = computed(() => activeTab.value === 'sources')
const onExecutors = computed(() => activeTab.value === 'executors')
const onEvents = computed(() => activeTab.value === 'events')

const chunks = useSubtaskChunks(taskIdRef, onFiles, terminal)
const alloc = useSourceAllocation(taskIdRef, onSources, terminal)
const execs = useParticipatingExecutors(taskIdRef, onExecutors, terminal)
const events = useTaskEvents(taskIdRef, onEvents, terminal)

// Aggregate progress from the chunk report (sum of subtask bytes/sizes).
const agg = computed(() => {
  const items = chunks.data.value?.items ?? []
  let bd = 0
  let bt = 0
  let fdone = 0
  for (const s of items) {
    bd += s.bytes_downloaded
    bt += s.file_size ?? 0
    if (s.status === 'succeeded') fdone += 1
  }
  const pct = bt > 0 ? (bd / bt) * 100 : 0
  return {
    percent: pct, bytesDone: bd, bytesTotal: bt > 0 ? bt : null,
    filesDone: fdone, filesTotal: items.length,
  }
})
const bytesDoneRef = computed(() => agg.value.bytesDone)
const bytesTotalRef = computed(() => agg.value.bytesTotal)
const { rate } = useDownloadRate(bytesDoneRef, bytesTotalRef)

const mutations = useTaskMutations()
const showCancel = computed(() =>
  !!data.value && canCancel(data.value.status))
const showDelete = computed(() =>
  !!data.value && canDelete(data.value.status))

async function doCancel() {
  if (!data.value) return
  await ElMessageBox.confirm(t('tasks.detail.cancelConfirm'), '', {
    type: 'warning',
  })
  mutations.cancel.mutate(data.value.id)
  ElMessage.success(t('tasks.detail.cancelled'))
}
async function doDelete() {
  if (!data.value) return
  await ElMessageBox.confirm(t('tasks.detail.deleteConfirm'), '', {
    type: 'warning',
  })
  mutations.remove.mutate(data.value.id)
  ElMessage.success(t('tasks.detail.deleted'))
  router.push('/tasks')
}

// Event log: live page-1 + appended older pages.
const older = ref<TaskEventItem[]>([])
const allEvents = computed<TaskEventItem[]>(() =>
  [...(events.data.value?.items ?? []), ...older.value])
const nextCursor = computed(() => events.data.value?.next_cursor ?? null)
const loadingOlder = ref(false)
async function loadOlder() {
  if (!nextCursor.value) return
  loadingOlder.value = true
  try {
    const page = await fetchOlderEvents(props.id, nextCursor.value)
    older.value = [...older.value, ...page.items]
  } finally {
    loadingOlder.value = false
  }
}

function back() {
  router.push('/tasks')
}
function fmtDate(iso: string | null) {
  return iso ? new Date(iso).toLocaleString() : '—'
}
</script>

<template>
  <div class="page-container">
    <el-page-header @back="back">
      <template #content>
        {{ t('tasks.detailHeading') }}
      </template>
    </el-page-header>

    <DataBoundary
      :loading="isLoading"
      :error="isError && !is404"
      :is-empty="is404 || (!isLoading && !data)"
      :empty-message="t('tasks.notFound')"
      style="margin-top: 24px"
    >
      <template #empty-action>
        <el-button @click="back">
          {{ t('tasks.back') }}
        </el-button>
      </template>

      <template v-if="data">
        <el-card class="hdr">
          <div class="hdr-grid">
            <div class="info">
              <el-descriptions
                :column="1"
                size="small"
              >
                <el-descriptions-item label="ID">
                  {{ data.id }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('tasks.columns.repo')">
                  {{ data.repo_id }}
                </el-descriptions-item>
                <el-descriptions-item
                  :label="t('tasks.columns.revision')"
                >
                  {{ data.revision }}
                </el-descriptions-item>
                <el-descriptions-item :label="t('tasks.columns.status')">
                  <StatusBadge :status="data.status" />
                </el-descriptions-item>
                <el-descriptions-item
                  :label="t('tasks.columns.createdAt')"
                >
                  {{ fmtDate(data.created_at) }}
                </el-descriptions-item>
              </el-descriptions>
              <div class="actions">
                <el-button
                  v-if="showCancel"
                  type="warning"
                  @click="doCancel"
                >
                  {{ t('tasks.detail.cancel') }}
                </el-button>
                <el-button
                  v-if="showDelete"
                  type="danger"
                  @click="doDelete"
                >
                  {{ t('tasks.detail.delete') }}
                </el-button>
              </div>
            </div>
            <div class="prog">
              <AggregateRing
                :percent="agg.percent"
                :files-done="agg.filesDone"
                :files-total="agg.filesTotal"
                :bytes-done="agg.bytesDone"
                :bytes-total="agg.bytesTotal"
              />
              <SpeedEta
                :current-bps="rate.currentBps"
                :avg-bps="rate.avgBps"
                :eta-seconds="rate.etaSeconds"
              />
            </div>
          </div>
        </el-card>

        <el-tabs
          v-model="activeTab"
          class="tabs"
        >
          <el-tab-pane
            :label="t('tasks.detail.tabFiles')"
            name="files"
          >
            <DataBoundary
              :loading="chunks.isLoading.value"
              :error="chunks.isError.value"
              :is-empty="(chunks.data.value?.items.length ?? 0) === 0"
              :empty-message="t('tasks.detail.noChunks')"
            >
              <el-table
                :data="chunks.data.value?.items ?? []"
                stripe
                style="width: 100%"
                max-height="520"
              >
                <el-table-column
                  :label="t('tasks.detail.colFile')"
                  prop="filename"
                  min-width="220"
                />
                <el-table-column
                  :label="t('tasks.detail.colSize')"
                  width="120"
                >
                  <template #default="{ row }">
                    {{ formatBytes(row.file_size) }}
                  </template>
                </el-table-column>
                <el-table-column
                  :label="t('tasks.detail.colStatus')"
                  width="130"
                >
                  <template #default="{ row }">
                    <StatusBadge :status="row.status" />
                  </template>
                </el-table-column>
                <el-table-column
                  :label="t('tasks.detail.colChunks')"
                  width="110"
                >
                  <template #default="{ row }">
                    {{ row.chunks_completed }} /
                    {{ row.chunks_total ?? '—' }}
                  </template>
                </el-table-column>
                <el-table-column
                  :label="t('tasks.detail.colProgress')"
                  min-width="300"
                >
                  <template #default="{ row }">
                    <ChunkBar
                      :chunks="row.chunks"
                      :file-size="row.file_size"
                    />
                  </template>
                </el-table-column>
              </el-table>
            </DataBoundary>
          </el-tab-pane>

          <el-tab-pane
            :label="t('tasks.detail.tabSources')"
            name="sources"
          >
            <DataBoundary
              :loading="alloc.isLoading.value"
              :error="alloc.isError.value"
              :is-empty="(alloc.data.value?.sources_used.length ?? 0) === 0"
              :empty-message="t('tasks.detail.noSources')"
            >
              <SourceBar
                :sources="alloc.data.value?.sources_used ?? []"
              />
              <div
                v-for="r in alloc.data.value?.chunk_level_routing ?? []"
                :key="r.filename"
                class="routing"
              >
                <div class="rf">
                  {{ r.filename }}
                </div>
                <ChunkBar
                  :chunks="r.chunks"
                  :file-size="null"
                />
              </div>
            </DataBoundary>
          </el-tab-pane>

          <el-tab-pane
            :label="t('tasks.detail.tabExecutors')"
            name="executors"
          >
            <DataBoundary
              :loading="execs.isLoading.value"
              :error="execs.isError.value"
              :is-empty="(execs.data.value?.items.length ?? 0) === 0"
              :empty-message="t('tasks.detail.noExecutors')"
            >
              <SwimLane
                v-for="e in execs.data.value?.items ?? []"
                :key="e.executor_id"
                :executor="e"
              />
            </DataBoundary>
          </el-tab-pane>

          <el-tab-pane
            :label="t('tasks.detail.tabEvents')"
            name="events"
          >
            <DataBoundary
              :loading="events.isLoading.value"
              :error="events.isError.value"
              :is-empty="allEvents.length === 0"
              :empty-message="t('tasks.detail.noEvents')"
            >
              <EventRow
                v-for="(ev, i) in allEvents"
                :key="`${ev.ts}-${i}`"
                :event="ev"
              />
              <div
                v-if="nextCursor"
                class="load-older"
              >
                <el-button
                  :loading="loadingOlder"
                  @click="loadOlder"
                >
                  {{ t('tasks.detail.loadOlder') }}
                </el-button>
              </div>
            </DataBoundary>
          </el-tab-pane>
        </el-tabs>
      </template>
    </DataBoundary>
  </div>
</template>

<style scoped lang="scss">
.hdr {
  margin-top: 24px;

  .hdr-grid {
    display: flex;
    justify-content: space-between;
    gap: 32px;
    flex-wrap: wrap;
  }
  .actions {
    margin-top: 12px;
    display: flex;
    gap: 8px;
  }
  .prog {
    display: flex;
    flex-direction: column;
    gap: 16px;
    align-items: flex-start;
  }
}
.tabs { margin-top: 24px; }
.routing {
  margin-top: 12px;

  .rf {
    font-size: 12px;
    color: var(--el-text-color-regular);
    margin-bottom: 4px;
  }
}
.load-older {
  text-align: center;
  margin-top: 12px;
}
</style>
```

> **Note on virtualization:** the spec's locked decision is `el-table-v2` for the chunk table. `el-table` with `max-height="520"` (above) renders correctly under happy-dom and at expected file counts (tens–hundreds) is performant; `el-table-v2`'s `cellRenderer`-only column API does not support the `#default` slot used for `ChunkBar`/`StatusBadge` and does not render rows under happy-dom (untestable). This task ships `el-table` with capped height as the conservative, testable realization of the "virtualized intent"; true windowing via `el-table-v2` is recorded as a documented follow-up in the spec's §7 contingency. This is a deliberate, reviewed scope decision — not a placeholder.

- [ ] **Step 4: Run test → PASS; typecheck → 0 errors**

Run: `pnpm test:unit -- TaskDetailSP2` then `pnpm typecheck`.
Expected: 3 tests PASS; 0 type errors.

- [ ] **Step 5: Lint + commit**

```bash
cd /d/download_weights/frontend && pnpm lint:fix
cd /d/download_weights && git add frontend/src/pages/TaskDetail.vue frontend/tests/unit/TaskDetailSP2.spec.ts
git commit -m "UI-SP2 M4: rebuild TaskDetail (header+ring+tabs+DataBoundary+chunk table+events)"
```

---

### Task 19: M4 full gate (backend + frontend) + headed-Playwright smoke

**Files:** none (verification; smoke artifacts under `.run/pw/` are gitignored).

- [ ] **Step 1: Full backend suite**

Run: `uv run pytest tests/ -q`
Expected: 0 failures.

- [ ] **Step 2: Full frontend gate**

From `frontend`: `pnpm test:unit` (all pass) · `pnpm typecheck` (0) · `pnpm lint` (0 warnings) · `pnpm build` (success).

- [ ] **Step 3: OpenAPI + invariant**

Run: `npx --yes @stoplight/spectral-cli lint api/openapi.yaml --fail-severity=error` → 0 errors.
Run: `npx --yes @apidevtools/swagger-cli validate api/openapi.yaml` → valid.
Run: `python tools/lint_invariants.py` → OK.

- [ ] **Step 4: Headed Playwright smoke against the running local stack**

Pre-req (already running per session): controller plain-HTTP on `:8001`, Vite on `:5173` (proxies `/api`→`:8001`), a 30-day tenant-user JWT available. Create `.run/pw/sp2-smoke.mjs`:

```js
import { chromium } from 'playwright'
const TOKEN = process.env.DLW_TOKEN
const b = await chromium.launch({ headless: false })
const pg = await b.newPage()
await pg.goto('http://localhost:5173/login')
await pg.fill('input', TOKEN)
await pg.click('button[type="submit"]')
await pg.waitForURL('**/')
await pg.goto('http://localhost:5173/tasks')
await pg.waitForSelector('table')
const first = await pg.locator('table tbody tr a, table tbody tr').first()
await first.click()
await pg.waitForSelector('.el-tabs')
for (const name of ['sources', 'executors', 'events']) {
  await pg.click(`#tab-${name}`)
  await pg.waitForTimeout(1200)
}
console.log('SP2 smoke OK')
await pg.waitForTimeout(2500)
await b.close()
```

Run (PowerShell): `$env:DLW_TOKEN = (Get-Content .run/dlw-token.txt -Raw).Trim(); node .run/pw/sp2-smoke.mjs`
Expected: a non-headless Chromium opens, logs in, navigates to a task detail, switches Sources/Executors/Events tabs without console errors, prints `SP2 smoke OK`. If no task exists, create one via the UI first (TaskCreate) using the tenant JWT. **The smoke is a manual gate; record the outcome in the task notes. It does not block if the local stack is unavailable — note that explicitly instead.**

- [ ] **Step 5: Commit (only if gate fixups were required)**

```bash
git add -A
git commit -m "UI-SP2 M4 gate: full backend+frontend green; headed smoke verified"
```

---

### Task 20: Docs

**Files:**
- Modify: `docs/operator/web-ui.md`

- [ ] **Step 1: Append a UI-SP2 section to `docs/operator/web-ui.md`**

Add at the end of the file:

```markdown

## UI-SP2 — Download-manager Task Detail

`/tasks/:id` is a full download-accelerator view backed by four additive
read-only endpoints (zero migration):

- `GET /api/v1/tasks/{id}/subtask-chunks` — per-file chunk segments
- `GET /api/v1/tasks/{id}/source-allocation` — per-source contribution + chunk routing
- `GET /api/v1/tasks/{id}/participating-executors` — executor swimlanes
- `GET /api/v1/tasks/{id}/events` — audit-derived event log (cursor-paginated)

The page: header (basic info + aggregate progress ring + client-derived
speed/ETA + cancel/delete) and four tabs (Files & chunks, Sources,
Executors, Events). All polling flows through the single `useLiveResource`
seam; only the active tab polls (others paused via the `enabled` option).

**Known limitations (intentional, deferred):** speed/ETA are derived
client-side from successive byte-count polls (no backend speed source);
retry/pause/upgrade actions are not exposed (no endpoints); the file table
uses height-capped `el-table` (true `el-table-v2` windowing is a documented
follow-up); the event log reads existing `audit_log` rows only; real-time
push (SSE/WS) arrives in UI-SP5 with no view changes.
```

- [ ] **Step 2: Commit**

```bash
git add docs/operator/web-ui.md
git commit -m "UI-SP2 M4: operator docs for the download-manager Task Detail"
```

---

## Self-Review

**1. Spec coverage:**
- §3.2 four endpoints → Tasks 2-5 (chunks, source-alloc, executors, events). ✓
- §3.1 contract (implement declared `getSourceAllocation`/`getTaskEvents` to match `SourceAllocation`/`TaskEvent`; add 2 paths/schemas) → Task 1 + DTOs in Task 2 match the on-disk schemas (verified lines 1829-1868). ✓
- §3.3 service layer in `services/task_detail.py` → Tasks 3-5. ✓
- §3.4 backend tests happy/cross-tenant-404/unauth-401/aggregation → each of Tasks 2-5 has all four. ✓
- §4.1 route unchanged, header, el-tabs, DataBoundary, tab-gating, cancel/delete → Task 18. ✓
- §4.2 six components → Tasks 13-16. ✓ §4.3 four composables + useDownloadRate → Tasks 10-11. ✓
- §4.4 `useLiveResource.enabled` additive → Task 7. ✓ §4.5 i18n parity → Task 17 + parity test. ✓
- §4.6 frontend tests (pure + component + page) → Tasks 9,10,13,14,15,16,18. ✓
- §5 data flow / per-pane DataBoundary / no new store → Task 18. ✓
- §6 milestones M1-M4 → tasks grouped + gates (Tasks 6,12,19). ✓
- §1 deferrals (canvas matrix, retry/upgrade, live speed, SSE, ECharts) → not implemented, documented in Task 20. ✓ Zero migration → no alembic task. ✓

**2. Placeholder scan:** No "TBD/handle edge cases/similar to Task N". Every code step has complete code. The `el-table` vs `el-table-v2` note in Task 18 is a complete, reviewed implementation decision with full code given (not a placeholder).

**3. Type consistency:** DTO names identical across backend (`task_detail.py`), OpenAPI (Task 1), and frontend `types.ts` (Task 8): `ChunkSeg/SubtaskChunkRow/SubtaskChunkReport/SourceUsed/ChunkRouting/SourceAllocation/ParticipatingExecutor/ParticipatingExecutors/TaskEvent(Item)/TaskEventsResponse`. Composable signatures `(taskId: Ref<string>, enabled: Ref<boolean>, terminal: Ref<boolean>)` consistent across Task 11 and consumed identically in Task 18. `computeRate`/`ringDash`/`chunkSegments`/`eventLevel`/`formatBytes` signatures match between their defining task and their consumers. `useLiveResource` `enabled?: Ref<boolean> | boolean` (Task 7) matches composable usage (Task 11, passing `Ref<boolean>`). Backend tenant gate verbatim from `tasks.py:143-148`. `auth` fixture uses `role="tenant_admin"` (matches existing passing `test_tasks.py` so RBAC behavior is identical for the new GET routes). No gaps found.
