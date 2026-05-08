# Phase 1 Week 3: UI Scaffold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A 3-page Vue 3 SPA (Login + TaskList + TaskDetail) running on `pnpm dev :5173` against the existing controller `:8000`. End of plan: a user pastes a Bearer token, sees a list of download tasks, clicks one, and watches its status + subtasks refresh every second until terminal.

**Architecture:** Standalone `frontend/` directory at repo root, Vite-bundled Vue 3 SPA. HTTP polling via `@tanstack/vue-query` (no WebSocket). Auth is single-shared-Bearer pasted into a Login form, persisted to `localStorage`, attached via axios interceptor. Backend gains a small schema split (`TaskRead` slim list + `TaskDetail` with subtasks) requiring a new ORM relationship.

**Tech Stack:** Vue 3 + Vite 5 + TypeScript 5 (strict) + Pinia 2 + `@tanstack/vue-query` 5 + axios + Element Plus 2 + vue-i18n 9 + Vitest + Playwright + pnpm 9. Backend additions: `selectinload` (already in SQLAlchemy 2.x) — no new Python deps.

**Scope:** 3 pages, polling-only realtime, zh-CN locale only, no charts. Companion full design in `docs/v2.0/10-frontend-wireframes.md`; Phase-1 subset spec in `docs/superpowers/specs/2026-05-08-week-3-ui-scaffold-design.md`.

**Pre-flight:** Phase 1 W1 (PR #1), W2 (PR #2), W3 Executor (PR #3) all merged to `main`. `feat/phase-1-week-3-ui-scaffold` branch exists (already created during spec commit `7762c1d`).

**Out-of-scope (deferred — explicit list to keep this plan honest):**
- WebSocket `/ws/v1` snapshot+delta+seq → Phase 2 plan
- TaskCreate page (form + speed-probe) → Phase 2 plan
- ExecutorList page → Phase 2 plan
- Dashboard / Quota / Audit / AI Copilot → Phase 3-4 plans
- OIDC + multi-user → Phase 3 plan
- `en-US` locale → Phase 2
- ECharts → Phase 2
- Dark mode → Phase 2
- `openapi-typescript` codegen → Phase 2 (3 hand-written interfaces are faster now)
- Production nginx + `Dockerfile.frontend` → Phase 2 (alpha demo runs `pnpm dev`)
- Playwright E2E (file + CI integration) → Phase 1 polish or Phase 2; spec §8.2 sketches the smoke shape but no test file or `@playwright/test` dep ships in this plan (defer browser-install drag until needed)

---

## File Structure

After this plan:

```
modelpull/
├── frontend/                                 # NEW
│   ├── package.json
│   ├── pnpm-lock.yaml
│   ├── pnpm-workspace.yaml                   # (single-pkg, but reserves space for monorepo later)
│   ├── tsconfig.json
│   ├── tsconfig.node.json
│   ├── vite.config.ts
│   ├── .eslintrc.cjs
│   ├── .prettierrc
│   ├── .gitignore
│   ├── index.html
│   ├── public/
│   │   └── favicon.svg
│   ├── src/
│   │   ├── main.ts                           # createApp + plugins
│   │   ├── App.vue                           # AppLayout > RouterView
│   │   ├── env.d.ts                          # Vite env types
│   │   ├── router/index.ts                   # 3 routes + guard
│   │   ├── api/
│   │   │   ├── client.ts                     # axios + interceptors
│   │   │   └── types.ts                      # TaskRead / TaskDetail / SubTaskRead
│   │   ├── stores/auth.ts                    # accessToken (localStorage-synced)
│   │   ├── composables/
│   │   │   ├── useTaskList.ts                # vue-query GET /tasks
│   │   │   └── useTaskDetail.ts              # vue-query GET /tasks/:id (terminal-aware)
│   │   ├── pages/
│   │   │   ├── Login.vue
│   │   │   ├── TaskList.vue
│   │   │   └── TaskDetail.vue
│   │   ├── components/
│   │   │   ├── AppLayout.vue
│   │   │   ├── StatusBadge.vue
│   │   │   └── EmptyState.vue
│   │   ├── locale/zh-CN.json
│   │   └── styles/main.scss
│   ├── tests/
│   │   ├── unit/
│   │   │   ├── auth.spec.ts
│   │   │   ├── client.spec.ts
│   │   │   ├── StatusBadge.spec.ts
│   │   │   └── useTaskDetail.spec.ts
│   │   └── e2e/
│   │       └── smoke.spec.ts                 # Playwright (locally only)
│   └── vitest.config.ts
├── src/dlw/
│   ├── db/models/task.py                     # MODIFY — add subtasks relationship + task back-ref
│   ├── schemas/task.py                       # MODIFY — add TaskDetail
│   └── api/tasks.py                          # MODIFY — get_task returns TaskDetail with selectinload
├── tests/
│   ├── services/test_task_service.py         # MODIFY — assert relationship populated
│   └── api/test_tasks.py                     # MODIFY — assert subtasks shape
├── .github/workflows/ci.yml                  # MODIFY — +frontend-lint, +frontend-build
└── README.md                                 # MODIFY — +Week 3 UI demo block
```

**Why `frontend/` at repo root**: clean ecosystem boundary (npm vs uv toolchains), separate CI jobs, matches `docs/v2.0/10-frontend-wireframes.md §1`.

---

## Pre-flight checks

- [ ] PR #1, PR #2, PR #3 all merged to `main` (verify: `git log main --oneline | grep "Merge PR"`)
- [ ] On branch `feat/phase-1-week-3-ui-scaffold`, spec committed (`git log --oneline -1` shows `7762c1d` or descendant)
- [ ] Local PG running on `localhost:5433` (`pg_isready -h localhost -p 5433`)
- [ ] `dlw` database exists, migrations applied (`uv run alembic upgrade head` is no-op)
- [ ] Existing pytest suite green (`uv run pytest -x` finishes with 0 failures)
- [ ] `pnpm` 9.x installed (`pnpm --version` ≥ 9.0; install via `npm install -g pnpm@9` if missing)
- [ ] Node 20.x installed (`node --version` matches `v20.*`)

---

## Milestone 1 — Backend schema split

After M1, backend exposes `subtasks` array on `GET /api/v1/tasks/{id}`. List endpoint stays slim. Verified by pytest.

---

### Task 1: Add `DownloadTask.subtasks` ORM relationship

**Why this exists:** The spec calls for `selectinload(DownloadTask.subtasks)` in the endpoint, but the current ORM model has no `relationship()` between `DownloadTask` and `FileSubTask` — only column-level FK. We add the relationship so the eager-load works and the test can assert it.

**Files:**
- Modify: `src/dlw/db/models/task.py`
- Modify: `tests/services/test_task_service.py` (add new test only)

- [ ] **Step 1: Write failing test (append to `tests/services/test_task_service.py`)**

```python
# Append at end of tests/services/test_task_service.py


@pytest.mark.slow
async def test_create_task_populates_subtasks_relationship(
    db_session: AsyncSession,
) -> None:
    """After create_task + flush, task.subtasks is a list of 2 FileSubTask rows.

    Locks the relationship so api/tasks.get_task can use
    selectinload(DownloadTask.subtasks).
    """
    from sqlalchemy.orm import selectinload
    from sqlalchemy import select

    from dlw.db.models.task import DownloadTask
    from dlw.schemas.task import TaskCreate
    from dlw.services.task_service import create_task

    body = TaskCreate(
        repo_id="o/relationship-probe",
        revision="a" * 40,
        storage_id=1,
    )
    task = await create_task(
        db_session, body,
        owner_user_id=1, tenant_id=1, project_id=1,
    )
    await db_session.commit()

    # Re-fetch with eager-load
    refreshed = (await db_session.execute(
        select(DownloadTask)
          .where(DownloadTask.id == task.id)
          .options(selectinload(DownloadTask.subtasks))
    )).scalar_one()

    assert len(refreshed.subtasks) == 2
    filenames = {s.filename for s in refreshed.subtasks}
    assert filenames == {"config.json", "model.safetensors"}
```

Note: this test depends on the existing `_bootstrap` fixture in `tests/services/test_task_service.py` (or its module conftest) seeding `tenant_id=1`, `project_id=1`, `user_id=1`, `storage_id=1`. If the existing test file lacks such bootstrap, copy the bootstrap pattern from `tests/api/test_tasks.py` lines 15-36 into a module-scoped fixture.

- [ ] **Step 2: Run the test to confirm it fails**

```bash
uv run pytest tests/services/test_task_service.py::test_create_task_populates_subtasks_relationship -v
```

Expected: FAIL with `AttributeError: 'DownloadTask' object has no attribute 'subtasks'`.

- [ ] **Step 3: Add the relationship to `src/dlw/db/models/task.py`**

At the top of the file, add `relationship` to the SQLAlchemy imports, then add a relationship attribute to `DownloadTask` and a back-ref attribute to `FileSubTask`.

In the imports block (around line 11):

```python
from sqlalchemy.orm import Mapped, mapped_column, relationship
```

In `DownloadTask` (after the `trace_id` column, line 56):

```python
    # ORM relationship — Phase 1 Week 3 UI scaffold consumes via
    # selectinload(DownloadTask.subtasks) in api/tasks.get_task.
    subtasks: Mapped[list["FileSubTask"]] = relationship(
        "FileSubTask",
        back_populates="task",
        cascade="all, delete-orphan",
        lazy="select",
    )
```

In `FileSubTask` (after the `completed_at` column, end of class, line 91):

```python
    task: Mapped["DownloadTask"] = relationship(
        "DownloadTask",
        back_populates="subtasks",
        lazy="select",
    )
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
uv run pytest tests/services/test_task_service.py::test_create_task_populates_subtasks_relationship -v
```

Expected: PASS.

- [ ] **Step 5: Run the full backend suite to confirm nothing broke**

```bash
uv run pytest -x
```

Expected: all existing tests still pass (no regressions from the relationship addition).

- [ ] **Step 6: Commit**

```bash
git add src/dlw/db/models/task.py tests/services/test_task_service.py
git commit -m "feat(db): add DownloadTask.subtasks ORM relationship + back-ref"
```

---

### Task 2: TaskDetail schema + `get_task` endpoint with `selectinload`

**Files:**
- Modify: `src/dlw/schemas/task.py`
- Modify: `src/dlw/api/tasks.py`
- Modify: `tests/api/test_tasks.py` (add 2 tests)

- [ ] **Step 1: Write failing tests (append to `tests/api/test_tasks.py`)**

```python
# Append at end of tests/api/test_tasks.py


@pytest.mark.slow
async def test_get_task_by_id_includes_subtasks_array(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """GET /tasks/{id} returns subtasks: [...] (Phase 1 W3 UI scaffold contract)."""
    create = await client.post("/api/v1/tasks", json={
        "repo_id": "o/with-subtasks",
        "revision": "3" * 40,
        "storage_id": 1,
    }, headers=auth)
    task_id = create.json()["id"]

    r = await client.get(f"/api/v1/tasks/{task_id}", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "subtasks" in body
    assert isinstance(body["subtasks"], list)
    assert len(body["subtasks"]) == 2
    filenames = {s["filename"] for s in body["subtasks"]}
    assert filenames == {"config.json", "model.safetensors"}
    # Each subtask carries the SubTaskRead shape
    for s in body["subtasks"]:
        assert {"id", "task_id", "filename", "status"} <= set(s.keys())


@pytest.mark.slow
async def test_get_tasks_list_omits_subtasks_field(
    client: AsyncClient, auth: dict[str, str]
) -> None:
    """List endpoint stays slim — TaskRead has no subtasks (avoids N+1)."""
    await client.post("/api/v1/tasks", json={
        "repo_id": "o/list-no-subtasks",
        "revision": "4" * 40,
        "storage_id": 1,
    }, headers=auth)
    r = await client.get("/api/v1/tasks", headers=auth)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) > 0
    for item in items:
        assert "subtasks" not in item
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/api/test_tasks.py::test_get_task_by_id_includes_subtasks_array tests/api/test_tasks.py::test_get_tasks_list_omits_subtasks_field -v
```

Expected: both FAIL — first with `KeyError: 'subtasks'`, second passes already (current TaskRead has no subtasks).

- [ ] **Step 3: Add `TaskDetail` to `src/dlw/schemas/task.py`**

Replace the entire file with:

```python
"""Task request/response DTOs."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from dlw.schemas.subtask import SubTaskRead


class TaskCreate(BaseModel):
    """POST /api/v1/tasks request body."""
    repo_id: str = Field(min_length=1, max_length=256, examples=["deepseek-ai/DeepSeek-V3"])
    revision: str = Field(min_length=1, max_length=64, examples=["abc123def4567890" * 2 + "abc12345"])
    storage_id: int = Field(gt=0)
    path_template: str = Field(default="{tenant}/{repo_id}/{revision}", max_length=512)
    priority: int = Field(default=1, ge=0, le=10)


class TaskRead(BaseModel):
    """Slim list-item shape — no subtasks (avoids N+1 across many tasks)."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repo_id: str
    revision: str
    status: str
    priority: int
    created_at: datetime
    completed_at: datetime | None
    error_message: str | None


class TaskDetail(TaskRead):
    """GET /api/v1/tasks/{id} response — adds subtasks list."""
    subtasks: list[SubTaskRead] = []


class TaskList(BaseModel):
    """GET /api/v1/tasks response body."""
    items: list[TaskRead]
    total: int
```

- [ ] **Step 4: Update `src/dlw/api/tasks.py` `get_task` to return `TaskDetail` with eager-load**

Replace the existing `get_task` function (lines 63-68) and add the import. Full new file:

```python
"""Tasks API: POST / GET list / GET by id.

Week 2: tenant_id=1, project_id=1, owner_user_id=1 hardcoded. Multi-tenancy
scoping via JWT claims comes in Phase 3.
Week 3 UI scaffold: GET /{id} returns TaskDetail (with subtasks).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from dlw.auth.bearer import require_bearer
from dlw.db.models.task import DownloadTask
from dlw.db.session import get_engine
from dlw.schemas.task import TaskCreate, TaskDetail, TaskList, TaskRead
from dlw.services.task_service import create_task

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

_TENANT_ID = 1
_PROJECT_ID = 1
_OWNER_USER_ID = 1


async def _session():
    """Per-request session backed by Phase 1's lru_cached singleton engine.

    Do NOT call engine.dispose() here — would race with concurrent requests
    sharing the same pool (same root cause as Phase 1 P1-A health.py fix).
    Lifespan disposes the engine once at app shutdown.
    """
    engine = get_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_bearer)])
async def post_task(body: TaskCreate, session: AsyncSession = Depends(_session)) -> TaskRead:
    task = await create_task(
        session, body,
        owner_user_id=_OWNER_USER_ID, tenant_id=_TENANT_ID, project_id=_PROJECT_ID,
    )
    await session.commit()
    return TaskRead.model_validate(task)


@router.get("", dependencies=[Depends(require_bearer)])
async def list_tasks(session: AsyncSession = Depends(_session)) -> TaskList:
    rows = (await session.execute(
        select(DownloadTask).where(DownloadTask.tenant_id == _TENANT_ID)
        .order_by(DownloadTask.created_at.desc())
    )).scalars().all()
    total = await session.scalar(
        select(func.count()).select_from(DownloadTask)
        .where(DownloadTask.tenant_id == _TENANT_ID)
    )
    return TaskList(items=[TaskRead.model_validate(r) for r in rows], total=int(total or 0))


@router.get("/{task_id}", dependencies=[Depends(require_bearer)])
async def get_task(task_id: uuid.UUID, session: AsyncSession = Depends(_session)) -> TaskDetail:
    row = (await session.execute(
        select(DownloadTask)
          .where(DownloadTask.id == task_id, DownloadTask.tenant_id == _TENANT_ID)
          .options(selectinload(DownloadTask.subtasks))
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return TaskDetail.model_validate(row)
```

- [ ] **Step 5: Run the new tests to confirm they pass**

```bash
uv run pytest tests/api/test_tasks.py::test_get_task_by_id_includes_subtasks_array tests/api/test_tasks.py::test_get_tasks_list_omits_subtasks_field -v
```

Expected: both PASS.

- [ ] **Step 6: Run the full backend suite — milestone gate**

```bash
uv run pytest
```

Expected: every existing test still passes + the two new tests pass. No regressions.

- [ ] **Step 7: Commit**

```bash
git add src/dlw/schemas/task.py src/dlw/api/tasks.py tests/api/test_tasks.py
git commit -m "feat(api): GET /tasks/{id} returns TaskDetail with subtasks (UI scaffold prep)"
```

### Milestone 1 verification

Run end-to-end against your local controller (separate from pytest):

```bash
# Terminal 1 — controller
uv run uvicorn dlw.main:app --port 8000

# Terminal 2 — probe
TOKEN=$DLW_BEARER_TOKEN
curl -s -X POST http://localhost:8000/api/v1/tasks \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"repo_id":"deepseek-ai/DeepSeek-V3","revision":"abc123def4567890abc123def4567890abc12345","storage_id":1}' | jq -r '.id' > /tmp/tid
TID=$(cat /tmp/tid)
curl -s "http://localhost:8000/api/v1/tasks/$TID" -H "Authorization: Bearer $TOKEN" | jq '.subtasks | length'
```

Expected output: `2`

---

## Milestone 2 — Frontend scaffolding boots

After M2, `pnpm dev` opens `http://localhost:5173/` and (because no token in localStorage) router redirects to `/login` showing an empty Element Plus form. No data calls yet — auth + pages come in M3.

---

### Task 3: `frontend/` tooling files (package.json + tsconfig + vite + lint)

**Files (all new):**
- Create: `frontend/.gitignore`
- Create: `frontend/package.json`
- Create: `frontend/pnpm-workspace.yaml`
- Create: `frontend/tsconfig.json`
- Create: `frontend/tsconfig.node.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/vitest.config.ts`
- Create: `frontend/.eslintrc.cjs`
- Create: `frontend/.prettierrc`
- Create: `frontend/src/env.d.ts`

- [ ] **Step 1: Create `frontend/.gitignore`**

```
node_modules
dist
.env.local
.env.*.local
coverage
*.log
.vite
```

- [ ] **Step 2: Create `frontend/package.json`**

```json
{
  "name": "@modelpull/frontend",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vue-tsc --noEmit && vite build",
    "preview": "vite preview",
    "typecheck": "vue-tsc --noEmit",
    "lint": "eslint . --ext .ts,.vue --max-warnings=0",
    "lint:fix": "eslint . --ext .ts,.vue --fix",
    "format": "prettier --write 'src/**/*.{ts,vue,scss,json}'",
    "test:unit": "vitest"
  },
  "dependencies": {
    "@tanstack/vue-query": "^5.59.0",
    "axios": "^1.7.7",
    "element-plus": "^2.8.4",
    "pinia": "^2.2.4",
    "vue": "^3.5.10",
    "vue-i18n": "^9.14.0",
    "vue-router": "^4.4.5"
  },
  "devDependencies": {
    "@types/node": "^20.16.10",
    "@vitejs/plugin-vue": "^5.1.4",
    "@vue/test-utils": "^2.4.6",
    "@vue/tsconfig": "^0.5.1",
    "eslint": "^8.57.1",
    "eslint-plugin-vue": "^9.28.0",
    "happy-dom": "^15.7.4",
    "prettier": "^3.3.3",
    "sass": "^1.79.4",
    "typescript": "^5.6.2",
    "vite": "^5.4.8",
    "vitest": "^2.1.2",
    "vue-tsc": "^2.1.6"
  },
  "engines": {
    "node": ">=20",
    "pnpm": ">=9"
  },
  "packageManager": "pnpm@9.12.0"
}
```

- [ ] **Step 3: Create `frontend/pnpm-workspace.yaml`**

```yaml
packages:
  - .
```

(Single-package workspace; reserves the layout for adding sub-packages without restructuring.)

- [ ] **Step 4: Create `frontend/tsconfig.json`**

```json
{
  "extends": "@vue/tsconfig/tsconfig.dom.json",
  "compilerOptions": {
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "noFallthroughCasesInSwitch": true,
    "noImplicitOverride": true,
    "exactOptionalPropertyTypes": false,
    "moduleResolution": "Bundler",
    "baseUrl": ".",
    "paths": { "@/*": ["src/*"] },
    "types": ["vite/client", "node"]
  },
  "include": ["src/**/*", "src/**/*.vue", "tests/**/*"],
  "references": [{ "path": "./tsconfig.node.json" }]
}
```

- [ ] **Step 5: Create `frontend/tsconfig.node.json`**

```json
{
  "extends": "@vue/tsconfig/tsconfig.json",
  "compilerOptions": {
    "composite": true,
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "types": ["node"]
  },
  "include": ["vite.config.ts", "vitest.config.ts"]
}
```

- [ ] **Step 6: Create `frontend/vite.config.ts`**

```typescript
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: false },
      '/health': { target: 'http://localhost:8000', changeOrigin: false },
    },
  },
})
```

- [ ] **Step 7: Create `frontend/vitest.config.ts`**

```typescript
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    environment: 'happy-dom',
    globals: true,
    include: ['tests/unit/**/*.spec.ts'],
  },
})
```

- [ ] **Step 8: Create `frontend/.eslintrc.cjs`**

```javascript
module.exports = {
  root: true,
  env: { browser: true, node: true, es2022: true },
  extends: [
    'eslint:recommended',
    'plugin:vue/vue3-recommended',
  ],
  parser: 'vue-eslint-parser',
  parserOptions: {
    parser: '@typescript-eslint/parser',
    sourceType: 'module',
    ecmaVersion: 2022,
  },
  rules: {
    'vue/multi-word-component-names': 'off',
    'no-unused-vars': 'off',
    'vue/no-unused-vars': 'warn',
  },
  ignorePatterns: ['dist/', 'node_modules/', 'coverage/'],
}
```

(We skip `@typescript-eslint` plugin in Phase 1 — `vue-tsc --noEmit` already catches type errors and adding the plugin is one more dep + config layer for thin gain. Phase 2 can add it.)

- [ ] **Step 9: Create `frontend/.prettierrc`**

```json
{
  "semi": false,
  "singleQuote": true,
  "trailingComma": "all",
  "printWidth": 100,
  "vueIndentScriptAndStyle": false
}
```

- [ ] **Step 10: Create `frontend/src/env.d.ts`**

```typescript
/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE: string
}
interface ImportMeta {
  readonly env: ImportMetaEnv
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}
```

- [ ] **Step 11: Install dependencies — generates `pnpm-lock.yaml`**

```bash
cd frontend
pnpm install
```

Expected: `pnpm-lock.yaml` is created; no errors. (May print "WARN deprecated" for transitives — acceptable.)

- [ ] **Step 12: Commit**

```bash
cd ..
git add frontend/.gitignore frontend/package.json frontend/pnpm-workspace.yaml \
        frontend/tsconfig.json frontend/tsconfig.node.json \
        frontend/vite.config.ts frontend/vitest.config.ts \
        frontend/.eslintrc.cjs frontend/.prettierrc \
        frontend/src/env.d.ts frontend/pnpm-lock.yaml
git commit -m "feat(frontend): tooling — pnpm + Vite + TS strict + ESLint + Prettier"
```

---

### Task 4: SPA shell — index.html + main.ts + App.vue + AppLayout

**Files (all new):**
- Create: `frontend/index.html`
- Create: `frontend/public/favicon.svg`
- Create: `frontend/src/main.ts`
- Create: `frontend/src/App.vue`
- Create: `frontend/src/components/AppLayout.vue`
- Create: `frontend/src/components/EmptyState.vue`
- Create: `frontend/src/locale/zh-CN.json`
- Create: `frontend/src/styles/main.scss`

- [ ] **Step 1: Create `frontend/index.html`**

```html
<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>modelpull</title>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.ts"></script>
  </body>
</html>
```

- [ ] **Step 2: Create `frontend/public/favicon.svg`**

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#1890ff"/>
  <text x="32" y="42" text-anchor="middle" font-family="Arial, sans-serif"
        font-size="32" font-weight="bold" fill="white">m</text>
</svg>
```

- [ ] **Step 3: Create `frontend/src/locale/zh-CN.json`**

```json
{
  "app": {
    "title": "modelpull",
    "logout": "退出登录"
  },
  "login": {
    "heading": "登录 modelpull",
    "tokenLabel": "Bearer Token",
    "tokenPlaceholder": "粘贴 DLW_BEARER_TOKEN 的值",
    "submit": "登录",
    "tokenRequired": "请输入 token"
  },
  "tasks": {
    "listHeading": "任务列表",
    "empty": "暂无任务，使用 curl POST /api/v1/tasks 创建一个",
    "columns": {
      "id": "ID",
      "repo": "仓库",
      "revision": "Revision",
      "status": "状态",
      "createdAt": "创建时间",
      "actions": "操作"
    },
    "view": "查看",
    "detailHeading": "任务详情",
    "subtasksHeading": "子任务",
    "polling": "实时刷新中…",
    "completed": "已停止刷新（终态）",
    "back": "返回列表",
    "notFound": "任务不存在或已删除",
    "subtaskColumns": {
      "filename": "文件名",
      "size": "大小",
      "sha256": "SHA256",
      "status": "状态"
    }
  },
  "status": {
    "pending": "排队中",
    "queued": "排队中",
    "scheduling": "调度中",
    "downloading": "下载中",
    "succeeded": "成功",
    "failed": "失败",
    "cancelled": "已取消",
    "assigned": "已分派",
    "in_progress": "下载中"
  },
  "errors": {
    "invalid_token": "Token 无效或已失效，请重新登录",
    "service_unavailable": "服务暂不可用，正在重试…",
    "network": "网络错误，请检查连接"
  }
}
```

- [ ] **Step 4: Create `frontend/src/styles/main.scss`**

```scss
*,
*::before,
*::after {
  box-sizing: border-box;
}

html,
body,
#app {
  height: 100%;
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
    'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
}

body {
  background-color: #f5f7fa;
  color: #303133;
}

a {
  color: var(--el-color-primary);
  text-decoration: none;
}

.page-container {
  padding: 24px;
  max-width: 1280px;
  margin: 0 auto;
}
```

- [ ] **Step 5: Create `frontend/src/components/EmptyState.vue`**

```vue
<script setup lang="ts">
defineProps<{
  message: string
  description?: string
}>()
</script>

<template>
  <div class="empty-state">
    <div class="icon">📭</div>
    <div class="message">{{ message }}</div>
    <div v-if="description" class="description">{{ description }}</div>
    <div class="action">
      <slot />
    </div>
  </div>
</template>

<style lang="scss" scoped>
.empty-state {
  text-align: center;
  padding: 64px 24px;
  color: #909399;

  .icon {
    font-size: 48px;
    margin-bottom: 16px;
  }
  .message {
    font-size: 16px;
    color: #606266;
    margin-bottom: 8px;
  }
  .description {
    font-size: 13px;
  }
  .action {
    margin-top: 16px;
  }
}
</style>
```

- [ ] **Step 6: Create `frontend/src/components/AppLayout.vue`**

```vue
<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const { t } = useI18n()
const router = useRouter()
const authStore = useAuthStore()

function logout() {
  authStore.logout()
  router.push('/login')
}
</script>

<template>
  <el-container class="app-layout">
    <el-header class="app-header">
      <div class="brand">
        <img src="/favicon.svg" alt="logo" class="logo" />
        <span class="title">{{ t('app.title') }}</span>
      </div>
      <el-button
        v-if="authStore.isAuthenticated"
        link
        type="primary"
        @click="logout"
      >
        {{ t('app.logout') }}
      </el-button>
    </el-header>
    <el-main>
      <slot />
    </el-main>
  </el-container>
</template>

<style lang="scss" scoped>
.app-layout {
  min-height: 100vh;
}
.app-header {
  background: white;
  border-bottom: 1px solid #ebeef5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;

  .brand {
    display: flex;
    align-items: center;
    gap: 8px;

    .logo {
      width: 28px;
      height: 28px;
    }
    .title {
      font-size: 16px;
      font-weight: 600;
    }
  }
}
</style>
```

- [ ] **Step 7: Create `frontend/src/App.vue`**

```vue
<script setup lang="ts">
import AppLayout from '@/components/AppLayout.vue'
</script>

<template>
  <AppLayout>
    <RouterView />
  </AppLayout>
</template>
```

- [ ] **Step 8: Create `frontend/src/main.ts`**

```typescript
import 'element-plus/dist/index.css'
import './styles/main.scss'

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { VueQueryPlugin } from '@tanstack/vue-query'
import { createI18n } from 'vue-i18n'
import ElementPlus from 'element-plus'

import App from './App.vue'
import router from './router'
import zhCN from './locale/zh-CN.json'

const i18n = createI18n({
  legacy: false,
  locale: 'zh-CN',
  fallbackLocale: 'zh-CN',
  messages: { 'zh-CN': zhCN },
})

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(i18n)
app.use(ElementPlus)
app.use(VueQueryPlugin, {
  queryClientConfig: {
    defaultOptions: {
      queries: {
        retry: 3,
        refetchOnWindowFocus: true,
        staleTime: 5_000,
      },
    },
  },
})
app.mount('#app')
```

- [ ] **Step 9: Commit (router and stores come in Task 5)**

This task ends with a project that won't run yet — the `import router from './router'` line in `main.ts` references a file Task 5 creates. Commit as a checkpoint, the tooling/shell pieces are coherent on their own:

```bash
git add frontend/index.html frontend/public/favicon.svg \
        frontend/src/main.ts frontend/src/App.vue \
        frontend/src/components/AppLayout.vue frontend/src/components/EmptyState.vue \
        frontend/src/locale/zh-CN.json frontend/src/styles/main.scss
git commit -m "feat(frontend): app shell — main.ts + App.vue + AppLayout + i18n"
```

---

### Task 5: Auth store + router with guard

**Files (all new):**
- Create: `frontend/src/stores/auth.ts`
- Create: `frontend/src/router/index.ts`
- Create: `frontend/tests/unit/auth.spec.ts`

- [ ] **Step 1: Write failing test `frontend/tests/unit/auth.spec.ts`**

```typescript
import { beforeEach, describe, expect, test } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useAuthStore } from '@/stores/auth'

describe('useAuthStore', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  test('starts unauthenticated when localStorage is empty', () => {
    const auth = useAuthStore()
    expect(auth.accessToken).toBeNull()
    expect(auth.isAuthenticated).toBe(false)
  })

  test('hydrates accessToken from localStorage on creation', () => {
    localStorage.setItem('dlw_token', 'persisted-token')
    const auth = useAuthStore()
    expect(auth.accessToken).toBe('persisted-token')
    expect(auth.isAuthenticated).toBe(true)
  })

  test('login persists token + sets accessToken', () => {
    const auth = useAuthStore()
    auth.login('new-token')
    expect(auth.accessToken).toBe('new-token')
    expect(localStorage.getItem('dlw_token')).toBe('new-token')
    expect(auth.isAuthenticated).toBe(true)
  })

  test('logout clears localStorage + resets accessToken', () => {
    localStorage.setItem('dlw_token', 'live-token')
    const auth = useAuthStore()
    auth.logout()
    expect(auth.accessToken).toBeNull()
    expect(localStorage.getItem('dlw_token')).toBeNull()
    expect(auth.isAuthenticated).toBe(false)
  })
})
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
cd frontend
pnpm test:unit -- --run tests/unit/auth.spec.ts
```

Expected: FAIL with module-not-found for `@/stores/auth`.

- [ ] **Step 3: Create `frontend/src/stores/auth.ts`**

```typescript
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

const STORAGE_KEY = 'dlw_token'

export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref<string | null>(localStorage.getItem(STORAGE_KEY))

  function login(token: string) {
    localStorage.setItem(STORAGE_KEY, token)
    accessToken.value = token
  }

  function logout() {
    localStorage.removeItem(STORAGE_KEY)
    accessToken.value = null
  }

  const isAuthenticated = computed(() => accessToken.value !== null)

  return { accessToken, isAuthenticated, login, logout }
})
```

- [ ] **Step 4: Run test to confirm it passes**

```bash
pnpm test:unit -- --run tests/unit/auth.spec.ts
```

Expected: PASS, 4 tests.

- [ ] **Step 5: Create `frontend/src/router/index.ts`**

The router uses lazy-loaded route components and a global `beforeEach` guard.

```typescript
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/pages/Login.vue'),
    meta: { public: true },
  },
  {
    path: '/',
    name: 'taskList',
    component: () => import('@/pages/TaskList.vue'),
  },
  {
    path: '/tasks/:id',
    name: 'taskDetail',
    component: () => import('@/pages/TaskDetail.vue'),
    props: true,
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  if (to.meta.public) return true
  const auth = useAuthStore()
  if (!auth.isAuthenticated) {
    return { path: '/login' }
  }
  return true
})

export default router
```

Note: `Login.vue`, `TaskList.vue`, `TaskDetail.vue` are placeholders here — they will be created in Tasks 7, 8, 10. To keep `pnpm dev` from breaking, create three minimal stub pages now:

- [ ] **Step 6: Create stub pages so `pnpm dev` boots**

`frontend/src/pages/Login.vue` (will be replaced in Task 7):

```vue
<template>
  <div>login (stub)</div>
</template>
```

`frontend/src/pages/TaskList.vue` (will be replaced in Task 8):

```vue
<template>
  <div>task list (stub)</div>
</template>
```

`frontend/src/pages/TaskDetail.vue` (will be replaced in Task 10):

```vue
<template>
  <div>task detail (stub)</div>
</template>
```

- [ ] **Step 7: Run typecheck + lint**

```bash
pnpm typecheck && pnpm lint
```

Expected: 0 errors.

- [ ] **Step 8: Run dev server smoke (manual, do not commit until working)**

```bash
pnpm dev
```

Open `http://localhost:5173/`. Expected: redirected to `http://localhost:5173/login` and the screen shows "login (stub)" inside the AppLayout header. Stop with Ctrl+C.

- [ ] **Step 9: Commit**

```bash
cd ..
git add frontend/src/stores/auth.ts frontend/src/router/index.ts \
        frontend/src/pages/Login.vue frontend/src/pages/TaskList.vue frontend/src/pages/TaskDetail.vue \
        frontend/tests/unit/auth.spec.ts
git commit -m "feat(frontend): auth store + router with guard + page stubs"
```

---

### Task 6: API client (axios + interceptors) + types

**Files (all new):**
- Create: `frontend/src/api/types.ts`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/tests/unit/client.spec.ts`

- [ ] **Step 1: Write failing test `frontend/tests/unit/client.spec.ts`**

```typescript
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import MockAdapter from 'axios-mock-adapter'

import { client } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { router } from '@/router'

describe('api client', () => {
  let mock: MockAdapter

  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    mock = new MockAdapter(client)
  })

  afterEach(() => {
    mock.restore()
  })

  test('attaches Authorization header from auth store', async () => {
    useAuthStore().login('alpha')
    mock.onGet('/api/v1/tasks').reply((cfg) => {
      expect(cfg.headers?.Authorization).toBe('Bearer alpha')
      return [200, { items: [], total: 0 }]
    })
    await client.get('/api/v1/tasks')
  })

  test('on 401 → logout + push /login?reason=invalid_token', async () => {
    useAuthStore().login('bad')
    const push = vi.spyOn(router, 'push').mockImplementation(async () => {})
    mock.onGet('/api/v1/tasks').reply(401, { detail: 'invalid' })

    await expect(client.get('/api/v1/tasks')).rejects.toBeDefined()
    expect(useAuthStore().accessToken).toBeNull()
    expect(push).toHaveBeenCalledWith({
      path: '/login',
      query: { reason: 'invalid_token' },
    })
  })
})
```

This test imports `axios-mock-adapter` and a named export `router` (the test needs `router.push` as a real reference to spy on). Two implications: add the dep, and re-export `router` from `@/router`.

- [ ] **Step 2: Add `axios-mock-adapter` to devDependencies**

```bash
cd frontend
pnpm add -D axios-mock-adapter
```

- [ ] **Step 3: Re-export router as a named export — modify `frontend/src/router/index.ts`**

Edit the last two lines (`export default router`) to:

```typescript
export { router }
export default router
```

- [ ] **Step 4: Run test to confirm it fails**

```bash
pnpm test:unit -- --run tests/unit/client.spec.ts
```

Expected: FAIL — module-not-found for `@/api/client`.

- [ ] **Step 5: Create `frontend/src/api/types.ts`**

```typescript
// Hand-written DTOs — mirror src/dlw/schemas/{task,subtask}.py.
// Phase 2 plan: replace with openapi-typescript codegen.

export type TaskStatus =
  | 'pending'
  | 'queued'
  | 'scheduling'
  | 'downloading'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export interface SubTaskRead {
  id: string
  task_id: string
  filename: string
  file_size: number | null
  expected_sha256: string | null
  status: string
}

export interface TaskRead {
  id: string
  repo_id: string
  revision: string
  status: TaskStatus
  priority: number
  created_at: string
  completed_at: string | null
  error_message: string | null
}

export interface TaskDetail extends TaskRead {
  subtasks: SubTaskRead[]
}

export interface TaskListResponse {
  items: TaskRead[]
  total: number
}

export const TERMINAL_STATUSES: ReadonlySet<TaskStatus> = new Set([
  'succeeded',
  'failed',
  'cancelled',
])
```

- [ ] **Step 6: Create `frontend/src/api/client.ts`**

```typescript
import axios from 'axios'

import { router } from '@/router'
import { useAuthStore } from '@/stores/auth'

export const client = axios.create({
  baseURL: import.meta.env.VITE_API_BASE ?? '',
  timeout: 10_000,
})

client.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.accessToken) {
    config.headers.Authorization = `Bearer ${auth.accessToken}`
  }
  return config
})

client.interceptors.response.use(
  (response) => response,
  (error) => {
    const status: number | undefined = error?.response?.status
    if (status === 401) {
      const auth = useAuthStore()
      auth.logout()
      router.push({ path: '/login', query: { reason: 'invalid_token' } })
    }
    return Promise.reject(error)
  },
)
```

- [ ] **Step 7: Run test to confirm it passes**

```bash
pnpm test:unit -- --run tests/unit/client.spec.ts
```

Expected: 2 tests PASS.

- [ ] **Step 8: Commit**

```bash
cd ..
git add frontend/package.json frontend/pnpm-lock.yaml \
        frontend/src/api/types.ts frontend/src/api/client.ts \
        frontend/src/router/index.ts \
        frontend/tests/unit/client.spec.ts
git commit -m "feat(frontend): axios client with auth + 401 logout interceptors"
```

---

### Milestone 2 verification

```bash
cd frontend
pnpm typecheck && pnpm lint && pnpm test:unit -- --run && pnpm build
```

Expected: all green; `dist/` produced.

Manual smoke:

```bash
pnpm dev
```

Open `http://localhost:5173/`. Expected: redirected to `/login`, AppLayout header shows the modelpull logo + title + (no logout button — no token). Stop with Ctrl+C.

---

## Milestone 3 — Auth + Task list

After M3, a user can paste a token, get pushed to `/`, and see a table of tasks polled every 5s. Click a row navigates to `/tasks/:id` (stub at this point).

---

### Task 7: Login page

**Files:**
- Replace: `frontend/src/pages/Login.vue` (currently a stub)

- [ ] **Step 1: Replace `frontend/src/pages/Login.vue`**

```vue
<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'

import { useAuthStore } from '@/stores/auth'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const formRef = ref<FormInstance>()
const form = reactive({ token: '' })
const rules: FormRules = {
  token: [{ required: true, message: () => t('login.tokenRequired'), trigger: 'submit' }],
}

onMounted(() => {
  if (route.query.reason === 'invalid_token') {
    ElMessage.error(t('errors.invalid_token'))
  }
  if (authStore.isAuthenticated) {
    router.replace('/')
  }
})

async function onSubmit() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  authStore.login(form.token.trim())
  router.push('/')
}
</script>

<template>
  <div class="login-page">
    <el-card class="login-card">
      <template #header>
        <h2>{{ t('login.heading') }}</h2>
      </template>
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="onSubmit"
      >
        <el-form-item :label="t('login.tokenLabel')" prop="token">
          <el-input
            v-model="form.token"
            type="password"
            show-password
            :placeholder="t('login.tokenPlaceholder')"
            autocomplete="off"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" native-type="submit" @click="onSubmit">
            {{ t('login.submit') }}
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<style lang="scss" scoped>
.login-page {
  display: flex;
  justify-content: center;
  padding-top: 96px;

  .login-card {
    width: 420px;

    h2 {
      margin: 0;
      font-size: 18px;
    }
  }
}
</style>
```

- [ ] **Step 2: Run typecheck**

```bash
cd frontend
pnpm typecheck
```

Expected: 0 errors.

- [ ] **Step 3: Manual smoke — login flow with a wrong then correct token**

Terminal 1:

```bash
uv run uvicorn dlw.main:app --port 8000
```

Terminal 2:

```bash
cd frontend
echo "VITE_API_BASE=http://localhost:8000" > .env.local
pnpm dev
```

Browser at `http://localhost:5173/login`:

1. Submit empty → form validation: "请输入 token"
2. Submit any non-empty token (e.g. "fake") → router pushes `/`. Page is the TaskList stub. (No 401 yet because TaskList hasn't been wired to fetch.)
3. Click `退出登录` button → returns to `/login`, no `?reason`.
4. Visit `http://localhost:5173/login?reason=invalid_token` → red error toast appears at top.

Stop both processes with Ctrl+C.

- [ ] **Step 4: Commit**

```bash
cd ..
git add frontend/src/pages/Login.vue
git commit -m "feat(frontend): Login page with token paste + ?reason error toast"
```

---

### Task 8: TaskList page + StatusBadge + useTaskList composable

**Files:**
- Create: `frontend/src/components/StatusBadge.vue`
- Create: `frontend/src/composables/useTaskList.ts`
- Replace: `frontend/src/pages/TaskList.vue`
- Create: `frontend/tests/unit/StatusBadge.spec.ts`

- [ ] **Step 1: Write failing test `frontend/tests/unit/StatusBadge.spec.ts`**

```typescript
import { describe, expect, test } from 'vitest'
import { createI18n } from 'vue-i18n'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'

import StatusBadge from '@/components/StatusBadge.vue'
import zhCN from '@/locale/zh-CN.json'

const i18n = createI18n({
  legacy: false,
  locale: 'zh-CN',
  messages: { 'zh-CN': zhCN },
})

const cases: ReadonlyArray<[string, 'info' | 'warning' | 'primary' | 'success' | 'danger']> = [
  ['pending', 'info'],
  ['queued', 'info'],
  ['scheduling', 'warning'],
  ['downloading', 'primary'],
  ['succeeded', 'success'],
  ['failed', 'danger'],
  ['cancelled', 'info'],
  ['unknown', 'info'],
]

describe('StatusBadge', () => {
  test.each(cases)('status %s → tag type %s', (status, expectedType) => {
    const wrapper = mount(StatusBadge, {
      props: { status },
      global: { plugins: [ElementPlus, i18n] },
    })
    const tag = wrapper.findComponent({ name: 'ElTag' })
    expect(tag.exists()).toBe(true)
    expect(tag.props('type')).toBe(expectedType)
  })
})
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
cd frontend
pnpm test:unit -- --run tests/unit/StatusBadge.spec.ts
```

Expected: FAIL — module-not-found for `@/components/StatusBadge.vue`.

- [ ] **Step 3: Create `frontend/src/components/StatusBadge.vue`**

```vue
<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{ status: string }>()
const { t, te } = useI18n()

type ElTagType = 'info' | 'warning' | 'primary' | 'success' | 'danger'

const TYPE_MAP: Readonly<Record<string, ElTagType>> = {
  pending: 'info',
  queued: 'info',
  cancelled: 'info',
  scheduling: 'warning',
  downloading: 'primary',
  in_progress: 'primary',
  assigned: 'primary',
  succeeded: 'success',
  failed: 'danger',
}

const tagType = computed<ElTagType>(() => TYPE_MAP[props.status] ?? 'info')

const label = computed(() => {
  const key = `status.${props.status}`
  return te(key) ? t(key) : props.status
})
</script>

<template>
  <el-tag :type="tagType" disable-transitions>{{ label }}</el-tag>
</template>
```

- [ ] **Step 4: Run test to confirm it passes**

```bash
pnpm test:unit -- --run tests/unit/StatusBadge.spec.ts
```

Expected: 8 cases PASS.

- [ ] **Step 5: Create `frontend/src/composables/useTaskList.ts`**

```typescript
import { useQuery } from '@tanstack/vue-query'

import { client } from '@/api/client'
import type { TaskListResponse } from '@/api/types'

export function useTaskList() {
  return useQuery({
    queryKey: ['tasks'] as const,
    queryFn: async () => {
      const r = await client.get<TaskListResponse>('/api/v1/tasks')
      return r.data
    },
    refetchInterval: 5_000,
    staleTime: 5_000,
  })
}
```

- [ ] **Step 6: Replace `frontend/src/pages/TaskList.vue`**

```vue
<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'

import StatusBadge from '@/components/StatusBadge.vue'
import EmptyState from '@/components/EmptyState.vue'
import { useTaskList } from '@/composables/useTaskList'

const { t } = useI18n()
const router = useRouter()
const { data, isLoading, isError } = useTaskList()

const items = computed(() => data.value?.items ?? [])

function open(id: string) {
  router.push({ name: 'taskDetail', params: { id } })
}

function shortId(id: string) {
  return id.slice(0, 8)
}

function shortRevision(rev: string) {
  return rev.slice(0, 8)
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleString('zh-CN')
}
</script>

<template>
  <div class="page-container">
    <h2>{{ t('tasks.listHeading') }}</h2>

    <el-table
      v-if="!isLoading && !isError && items.length > 0"
      :data="items"
      stripe
      style="width: 100%"
      @row-click="(row) => open(row.id)"
    >
      <el-table-column :label="t('tasks.columns.id')" width="120">
        <template #default="{ row }">{{ shortId(row.id) }}…</template>
      </el-table-column>
      <el-table-column prop="repo_id" :label="t('tasks.columns.repo')" min-width="240" />
      <el-table-column :label="t('tasks.columns.revision')" width="120">
        <template #default="{ row }">{{ shortRevision(row.revision) }}…</template>
      </el-table-column>
      <el-table-column :label="t('tasks.columns.status')" width="120">
        <template #default="{ row }"><StatusBadge :status="row.status" /></template>
      </el-table-column>
      <el-table-column :label="t('tasks.columns.createdAt')" width="200">
        <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
      </el-table-column>
      <el-table-column :label="t('tasks.columns.actions')" width="100">
        <template #default="{ row }">
          <el-button link type="primary" @click.stop="open(row.id)">
            {{ t('tasks.view') }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-skeleton v-else-if="isLoading" :rows="4" animated />

    <EmptyState
      v-else-if="!isError && items.length === 0"
      :message="t('tasks.empty')"
    />

    <el-alert
      v-else-if="isError"
      type="error"
      :title="t('errors.service_unavailable')"
      :closable="false"
    />
  </div>
</template>
```

- [ ] **Step 7: Run typecheck + the full unit suite**

```bash
pnpm typecheck && pnpm test:unit -- --run
```

Expected: 0 type errors; all unit tests pass.

- [ ] **Step 8: Manual smoke — full M3 flow against running controller**

Terminal 1 (controller):

```bash
uv run uvicorn dlw.main:app --port 8000
```

Terminal 2 (seed at least one task):

```bash
TOKEN=$DLW_BEARER_TOKEN
for i in 1 2 3; do
  curl -s -X POST http://localhost:8000/api/v1/tasks \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"repo_id\":\"o/demo-$i\",\"revision\":\"$(printf '%040d' $i)\",\"storage_id\":1}"
  echo
done
```

Terminal 3 (frontend):

```bash
cd frontend
pnpm dev
```

Browser at `http://localhost:5173/login`:

1. Paste the same `$DLW_BEARER_TOKEN` value → push to `/`.
2. See a table of 3 tasks with status badges.
3. Wait 5s → table refetches (you can confirm via DevTools Network panel).
4. Logout → kicked back to `/login`.

Stop all processes.

- [ ] **Step 9: Commit**

```bash
cd ..
git add frontend/src/components/StatusBadge.vue \
        frontend/src/composables/useTaskList.ts \
        frontend/src/pages/TaskList.vue \
        frontend/tests/unit/StatusBadge.spec.ts
git commit -m "feat(frontend): TaskList page + StatusBadge + 5s polling"
```

---

### Milestone 3 verification

`pnpm typecheck && pnpm lint && pnpm test:unit -- --run && pnpm build` all green.

Manual: paste token → list of tasks visible, polled every 5s.

---

## Milestone 4 — TaskDetail + polling + CI + PR

After M4, the full flow is shippable: detail page polls 1s while non-terminal, stops on succeeded/failed; CI has frontend lint + build jobs; README documents the demo.

---

### Task 9: useTaskDetail composable + tests

**Files:**
- Create: `frontend/src/composables/useTaskDetail.ts`
- Create: `frontend/tests/unit/useTaskDetail.spec.ts`

- [ ] **Step 1: Write failing test `frontend/tests/unit/useTaskDetail.spec.ts`**

```typescript
import { describe, expect, test } from 'vitest'

import { computeRefetchInterval } from '@/composables/useTaskDetail'
import type { TaskDetail } from '@/api/types'

function fakeTask(status: TaskDetail['status']): TaskDetail {
  return {
    id: 'x',
    repo_id: 'o/r',
    revision: 'a',
    status,
    priority: 1,
    created_at: '2026-01-01T00:00:00Z',
    completed_at: null,
    error_message: null,
    subtasks: [],
  }
}

describe('computeRefetchInterval', () => {
  test('non-terminal status → 1000ms', () => {
    expect(computeRefetchInterval(fakeTask('downloading'))).toBe(1000)
    expect(computeRefetchInterval(fakeTask('queued'))).toBe(1000)
    expect(computeRefetchInterval(fakeTask('scheduling'))).toBe(1000)
    expect(computeRefetchInterval(fakeTask('pending'))).toBe(1000)
  })

  test('terminal status → false (stop polling)', () => {
    expect(computeRefetchInterval(fakeTask('succeeded'))).toBe(false)
    expect(computeRefetchInterval(fakeTask('failed'))).toBe(false)
    expect(computeRefetchInterval(fakeTask('cancelled'))).toBe(false)
  })

  test('undefined data (first fetch in flight) → 1000ms', () => {
    expect(computeRefetchInterval(undefined)).toBe(1000)
  })
})
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
cd frontend
pnpm test:unit -- --run tests/unit/useTaskDetail.spec.ts
```

Expected: FAIL — `@/composables/useTaskDetail` not found.

- [ ] **Step 3: Create `frontend/src/composables/useTaskDetail.ts`**

```typescript
import { useQuery } from '@tanstack/vue-query'
import type { Ref } from 'vue'

import { client } from '@/api/client'
import { TERMINAL_STATUSES, type TaskDetail } from '@/api/types'

const POLL_INTERVAL_MS = 1_000

/** Pure helper — exported so tests don't need vue-query plumbing. */
export function computeRefetchInterval(data: TaskDetail | undefined): number | false {
  if (!data) return POLL_INTERVAL_MS
  return TERMINAL_STATUSES.has(data.status) ? false : POLL_INTERVAL_MS
}

export function useTaskDetail(taskId: Ref<string>) {
  return useQuery({
    queryKey: ['task', taskId] as const,
    queryFn: async () => {
      const r = await client.get<TaskDetail>(`/api/v1/tasks/${taskId.value}`)
      return r.data
    },
    refetchInterval: (query) => computeRefetchInterval(query.state.data),
    staleTime: 0,
  })
}
```

- [ ] **Step 4: Run test to confirm it passes**

```bash
pnpm test:unit -- --run tests/unit/useTaskDetail.spec.ts
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd ..
git add frontend/src/composables/useTaskDetail.ts frontend/tests/unit/useTaskDetail.spec.ts
git commit -m "feat(frontend): useTaskDetail composable with terminal-aware polling"
```

---

### Task 10: TaskDetail page

**Files:**
- Replace: `frontend/src/pages/TaskDetail.vue`

- [ ] **Step 1: Replace `frontend/src/pages/TaskDetail.vue`**

```vue
<script setup lang="ts">
import { computed, toRef } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'

import StatusBadge from '@/components/StatusBadge.vue'
import EmptyState from '@/components/EmptyState.vue'
import { useTaskDetail } from '@/composables/useTaskDetail'
import { TERMINAL_STATUSES } from '@/api/types'

const props = defineProps<{ id: string }>()

const { t } = useI18n()
const router = useRouter()
const taskIdRef = toRef(() => props.id)
const { data, isLoading, isError, error } = useTaskDetail(taskIdRef)

const isPolling = computed(() => {
  if (!data.value) return true
  return !TERMINAL_STATUSES.has(data.value.status)
})

const is404 = computed(() => {
  return (error.value as { response?: { status?: number } } | null)?.response?.status === 404
})

function formatDate(iso: string | null) {
  return iso ? new Date(iso).toLocaleString('zh-CN') : '—'
}

function formatBytes(n: number | null) {
  if (n === null || n === undefined) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

function shortSha(s: string | null) {
  return s ? `${s.slice(0, 12)}…` : '—'
}

function back() {
  router.push('/')
}
</script>

<template>
  <div class="page-container">
    <el-page-header @back="back">
      <template #content>{{ t('tasks.detailHeading') }}</template>
    </el-page-header>

    <el-skeleton v-if="isLoading" :rows="6" animated style="margin-top: 24px" />

    <EmptyState
      v-else-if="is404"
      :message="t('tasks.notFound')"
    >
      <el-button @click="back">{{ t('tasks.back') }}</el-button>
    </EmptyState>

    <el-alert
      v-else-if="isError"
      type="error"
      :title="t('errors.service_unavailable')"
      :closable="false"
      style="margin-top: 24px"
    />

    <template v-else-if="data">
      <el-card class="summary" style="margin-top: 24px">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="ID">{{ data.id }}</el-descriptions-item>
          <el-descriptions-item :label="t('tasks.columns.repo')">{{ data.repo_id }}</el-descriptions-item>
          <el-descriptions-item :label="t('tasks.columns.revision')">{{ data.revision }}</el-descriptions-item>
          <el-descriptions-item :label="t('tasks.columns.status')">
            <StatusBadge :status="data.status" />
          </el-descriptions-item>
          <el-descriptions-item :label="t('tasks.columns.createdAt')">
            {{ formatDate(data.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="completed_at">
            {{ formatDate(data.completed_at) }}
          </el-descriptions-item>
          <el-descriptions-item v-if="data.error_message" label="error" :span="2">
            <pre class="error">{{ data.error_message }}</pre>
          </el-descriptions-item>
        </el-descriptions>

        <div class="polling-indicator" :class="{ active: isPolling }">
          <span class="dot" />
          {{ isPolling ? t('tasks.polling') : t('tasks.completed') }}
        </div>
      </el-card>

      <h3 style="margin-top: 24px">{{ t('tasks.subtasksHeading') }}</h3>
      <el-table :data="data.subtasks" stripe style="width: 100%">
        <el-table-column :label="t('tasks.subtaskColumns.filename')" prop="filename" min-width="240" />
        <el-table-column :label="t('tasks.subtaskColumns.size')" width="140">
          <template #default="{ row }">{{ formatBytes(row.file_size) }}</template>
        </el-table-column>
        <el-table-column :label="t('tasks.subtaskColumns.sha256')" width="180">
          <template #default="{ row }">{{ shortSha(row.expected_sha256) }}</template>
        </el-table-column>
        <el-table-column :label="t('tasks.subtaskColumns.status')" width="140">
          <template #default="{ row }"><StatusBadge :status="row.status" /></template>
        </el-table-column>
      </el-table>
    </template>
  </div>
</template>

<style lang="scss" scoped>
.summary {
  .polling-indicator {
    margin-top: 12px;
    color: #909399;
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 6px;

    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #c0c4cc;
    }
    &.active .dot {
      background: var(--el-color-primary);
      animation: pulse 1.4s ease-in-out infinite;
    }
  }

  .error {
    margin: 0;
    white-space: pre-wrap;
    color: var(--el-color-danger);
    font-size: 12px;
  }
}

@keyframes pulse {
  0%, 100% { opacity: 0.4; }
  50%      { opacity: 1; }
}
</style>
```

- [ ] **Step 2: Run typecheck**

```bash
cd frontend
pnpm typecheck
```

Expected: 0 errors.

- [ ] **Step 3: Manual smoke — full happy path**

Terminal 1 (controller):

```bash
uv run uvicorn dlw.main:app --port 8000
```

Terminal 2 (seed task + drive a subtask through the protocol manually):

```bash
TOKEN=$DLW_BEARER_TOKEN
TID=$(curl -s -X POST http://localhost:8000/api/v1/tasks \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"repo_id":"o/detail-demo","revision":"abcdef0123456789abcdef0123456789abcdef01","storage_id":1}' \
  | python -c "import json,sys;print(json.load(sys.stdin)['id'])")
echo "task: $TID"
```

Terminal 3 (frontend):

```bash
cd frontend
pnpm dev
```

Browser:

1. Paste token → task list shows the seeded task.
2. Click the row → `/tasks/$TID`. See summary card + 2 subtasks (`config.json`, `model.safetensors`), both `pending`. Polling indicator pulsing.
3. Visit a non-existent UUID like `/tasks/00000000-0000-0000-0000-000000000000` → `EmptyState` with "返回列表" button.
4. (Optional) Run an executor in another terminal:

   ```bash
   uv run dlw-executor
   ```

   Watch the detail page subtask states transition pending → assigned → succeeded; once the parent task hits `succeeded`, the polling indicator says "已停止刷新".

Stop all processes.

- [ ] **Step 4: Commit**

```bash
cd ..
git add frontend/src/pages/TaskDetail.vue
git commit -m "feat(frontend): TaskDetail page — summary + subtasks table + 1s polling"
```

---

### Task 11: CI jobs — frontend-lint + frontend-build

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Add the two new jobs to `.github/workflows/ci.yml`**

Insert these two jobs at the end of the `jobs:` block, before any aggregator job (or after the `pytest` job):

```yaml
  # ============================================================
  # Frontend: lint + typecheck + unit tests (Phase 1 W3 UI scaffold)
  # ============================================================
  frontend-lint:
    name: Frontend lint
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: frontend
    steps:
      - uses: actions/checkout@v4

      - uses: pnpm/action-setup@v4
        with:
          version: 9

      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: pnpm
          cache-dependency-path: frontend/pnpm-lock.yaml

      - name: Install
        run: pnpm install --frozen-lockfile

      - name: ESLint
        run: pnpm lint

      - name: Typecheck
        run: pnpm typecheck

      - name: Unit tests
        run: pnpm test:unit -- --run

  # ============================================================
  # Frontend: production build (smoke — fail CI if vite build fails)
  # ============================================================
  frontend-build:
    name: Frontend build
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: frontend
    steps:
      - uses: actions/checkout@v4

      - uses: pnpm/action-setup@v4
        with:
          version: 9

      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: pnpm
          cache-dependency-path: frontend/pnpm-lock.yaml

      - name: Install
        run: pnpm install --frozen-lockfile

      - name: Build
        run: pnpm build
```

- [ ] **Step 2: If `ci.yml` has a `ci-status` aggregator job (`needs:` listing all required jobs), append `frontend-lint` and `frontend-build` to its `needs:` list**

Open `.github/workflows/ci.yml`, locate the aggregator (likely named `ci-status` or `all-checks`). Append the two new job names to its `needs:` array. If no aggregator exists, skip — branch protection rules can list them directly.

- [ ] **Step 3: Validate the YAML locally**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
```

Expected: no exception.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add frontend-lint + frontend-build jobs"
```

---

### Task 12: README "Week 3 UI demo" block + push + PR

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Open `README.md` and locate the existing "Week 3 demo" block (added by PR #3 commit `0903ab4`)**

Find the closing of that block (likely a markdown horizontal rule `---` or another `## ` heading) and add a new section right after.

- [ ] **Step 2: Insert the new "Week 3 UI demo" block**

```markdown
## Week 3 UI demo

A 3-page Vue 3 SPA driven by `pnpm dev` against the running controller.

```bash
# Terminal 1 — controller
docker compose -f docker-compose.dev.yml up -d postgres
uv run alembic upgrade head
uv run uvicorn dlw.main:app --port 8000

# Terminal 2 — seed a task so the list isn't empty
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Authorization: Bearer $DLW_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"repo_id":"deepseek-ai/DeepSeek-V3","revision":"abc123def4567890abc123def4567890abc12345","storage_id":1}'

# Terminal 3 — frontend
cd frontend
pnpm install
echo "VITE_API_BASE=http://localhost:8000" > .env.local
pnpm dev    # http://localhost:5173
```

Open `http://localhost:5173/`, paste the value of `$DLW_BEARER_TOKEN`, see the
seeded task in the list, click into it. The detail page polls every second
until the task hits a terminal state. Pair with `dlw-executor` in another
terminal to watch subtasks transition from `pending` → `assigned` → `succeeded`.

```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): add Week 3 UI demo block — pnpm dev against controller"
```

- [ ] **Step 4: Push the branch**

```bash
git push -u origin feat/phase-1-week-3-ui-scaffold
```

- [ ] **Step 5: Open the PR**

```bash
gh pr create \
  --title "Phase 1 Week 3 — UI Scaffold (3-page Vue 3 SPA)" \
  --body "$(cat <<'EOF'
## Summary

- 3-page Vue 3 SPA in \`frontend/\` (Login + TaskList + TaskDetail)
- HTTP polling for realtime (5s on list, 1s on detail with terminal-state stop)
- Backend gains \`TaskDetail\` schema + \`DownloadTask.subtasks\` ORM relationship
- New CI jobs: \`frontend-lint\` (ESLint + vue-tsc + Vitest) and \`frontend-build\` (vite build smoke)
- README documents the dev-time 3-terminal demo workflow

Closes the UI half of Phase 1 §1.6 Week 3 (Executor half shipped in PR #3).

## Test plan

- [x] Backend: existing pytest suite still green; 2 new tests cover \`subtasks\` field shape
- [x] Frontend unit: \`pnpm test:unit\` green — auth store, axios interceptor (401 path), StatusBadge mapping, useTaskDetail terminal-stop logic
- [x] Frontend build: \`pnpm build\` green
- [x] Manual smoke: paste token → list → detail → polling stops on terminal status
- [x] 401 path: wrong token → bounced to \`/login?reason=invalid_token\` with red toast

## Out of scope (deferred)

WebSocket, TaskCreate/ExecutorList/Dashboard pages, OIDC, en-US, ECharts, dark mode, openapi-typescript codegen, production nginx — see \`docs/superpowers/specs/2026-05-08-week-3-ui-scaffold-design.md\` §1.2.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 6: Wait for CI and address failures**

Open the PR URL printed by `gh pr create`. CI runs:

- `openapi`, `helm`, `shellcheck`, `markdown`, `yamllint`, `security`, `json`, `invariant_lint`, `pytest`, `frontend-lint`, `frontend-build`

Expected first run: all green. If anything fails, fix in a new commit on the same branch and push (do **not** force-push; do **not** amend).

---

### Milestone 4 verification

- [ ] PR opened and CI fully green (11 checks).
- [ ] Manual final smoke against the running controller:
  1. `pnpm dev` + `uv run uvicorn ...` + seed a task via curl
  2. Login → list → detail → watch polling indicator pulsing
  3. Run `uv run dlw-executor` separately → watch subtask states change → terminal status flips polling indicator off
  4. Logout button works
  5. Wrong token → red toast on `/login`

---

## Definition of Done

- [ ] All 12 tasks committed on `feat/phase-1-week-3-ui-scaffold`
- [ ] PR opened, all CI checks green (≥11 jobs)
- [ ] `pnpm typecheck && pnpm lint && pnpm test:unit -- --run && pnpm build` all 0 errors locally
- [ ] Existing pytest suite still green (no backend regression from M1 schema split)
- [ ] Manual smoke: paste token → see task list → click → see detail with subtasks + polling
- [ ] README "Week 3 UI demo" block resolves to working commands
- [ ] No new files outside `frontend/`, `src/dlw/{db/models,schemas,api}/task*.py`, `tests/{services,api}/test_tasks.py`, `.github/workflows/ci.yml`, `README.md`, `docs/superpowers/{specs,plans}/`

---

## Plan Revisions Log

(Empty on first draft. Populated by the pre-execution multi-agent reviewer pass.)

| Tag | Severity | Issue | Fix applied |
|-----|----------|-------|-------------|
| _(none yet)_ | | | |

---

## References

- Spec: `docs/superpowers/specs/2026-05-08-week-3-ui-scaffold-design.md`
- Companion full design: `docs/v2.0/10-frontend-wireframes.md`
- Phase 1 scope: `docs/v2.0/08-mvp-roadmap.md` §1
- Precedent plan: `docs/superpowers/plans/2026-05-09-phase-1-week-3-executor-process.md`
- Existing tests: `tests/api/test_tasks.py`, `tests/services/test_task_service.py`
- Modified backend files: `src/dlw/db/models/task.py`, `src/dlw/schemas/task.py`, `src/dlw/api/tasks.py`
