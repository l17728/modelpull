# Phase 1 Week 2: Controller Core (Task CRUD + Scheduler Loop) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A mock executor can complete the full happy-path loop end-to-end: `POST /api/v1/tasks` → controller persists task + 2 mock subtasks → executor polls → gets one subtask assignment → reports completion → next poll gets the second → reports completion → task transitions to `succeeded`.

**Architecture:** FastAPI routers grouped by resource (`tasks/`, `executors/`, `subtasks/`). Pydantic v2 DTOs for request/response (separate from ORM models). Service layer (`dlw.services.*`) holds business logic so routers stay thin. Authentication is a **Bearer token middleware** with a single env-var-configured shared secret (full OIDC PKCE in Phase 3). Scheduler is **pull-model**: executor calls `POST /executors/{id}/poll`, controller atomically claims one pending subtask via `SELECT ... FOR UPDATE SKIP LOCKED`. No WebSocket, no real HuggingFace Hub calls — subtasks are mocked as 2 placeholder files per task.

**Tech Stack:** Same as Phase 1 — Python 3.12, FastAPI, async SQLAlchemy 2.x, asyncpg, alembic, pydantic v2, structlog. Adds: `pyjwt` is **NOT** needed (Bearer token is just a shared secret string). Tests stay on local PG via existing `tests/conftest.py`.

**Scope:** Week 2 only (Days 6-10 of Phase 1 per `docs/v2.0/08-mvp-roadmap.md` §1.6). After this plan: a developer with `curl` can drive a mock executor through a full task lifecycle.

**Out-of-scope (deferred to later plans):**
- Real HuggingFace Hub API calls for file resolution → Week 4 plan
- mTLS executor authentication → Phase 2 plan
- Fence token / `executor_epoch` lifecycle (schema columns exist but logic is dormant) → Phase 2 plan
- WebSocket progress fan-out → Week 3 plan (paired with UI)
- OIDC PKCE / multi-user RBAC → Phase 3 plan
- Multi-tenancy logic — `tenant_id=1` hardcoded for all rows; tenant scoping in Phase 3
- Real S3 multipart upload → Week 4 plan
- Streaming SHA256 verification → Week 4 plan
- Adaptive optimizer / multi-source / AI Copilot → v2.1+

**Pre-flight:** Phase 1 PR #1 must be merged to `main` before starting this plan. If not yet merged, branch off `feat/phase-1-foundation` instead.

---

## Plan Revisions Log

This plan was reviewed by 2 specialized agents (completeness + distributed correctness) on 2026-05-07; the following fixes were applied to the original draft before execution. Marked W2-A through W2-J in commit messages.

| Tag | Issue | Fix applied |
|-----|-------|-------------|
| W2-A | `_session()` disposed engine per request — same antipattern as Phase 1 P1-A | Removed dispose; relies on lru_cached `get_engine()` from Phase 1 fix |
| W2-B | API tests would connect to production `dlw` DB instead of test DB | Added Pre-Task 0: session-autouse conftest fixture points app config at `test_db_name` |
| W2-C | `env` fixture in Task 6 commits id=1 rows → PK conflict on second test | Changed to module-scoped autouse so seed runs once per module |
| W2-D | Concurrency test with 2 subtasks could pass for wrong reasons | Added 1-subtask + 2-claimant test that directly proves SKIP LOCKED |
| W2-E | `complete_subtask` parent transition raceable when 2 subtasks finish at once | Added `with_for_update()` on parent task fetch |
| W2-F | `assignment_token` written but never verified by `/report` endpoint | One-line equality check added in `complete_subtask` |
| W2-G | Double-report idempotency (409) untested | Added `test_double_report_returns_409` |
| W2-H | Invariant 9 (executor ID format `host-X-worker-N`) silently violated | Added explicit deferral comment in `ExecutorJoin` schema |
| W2-I | Task 8 import lines placed mid-file in plan instructions | Moved to "imports go at top of file" annotation |
| W2-J | Alembic `sa.text("created_at DESC")` in column list — undocumented API | Changed to `postgresql_ops={"created_at": "DESC NULLS LAST"}` |

---

## File Structure

After this plan, the repo adds:

```
modelpull/
├── src/dlw/
│   ├── auth/
│   │   ├── __init__.py
│   │   └── bearer.py                          # Bearer token middleware + dependency
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── task.py                            # TaskCreate, TaskRead, TaskList
│   │   ├── subtask.py                         # SubTaskRead, SubTaskReport
│   │   └── executor.py                        # ExecutorJoin, ExecutorHeartbeat, AssignmentResponse
│   ├── services/
│   │   ├── __init__.py
│   │   ├── task_service.py                    # create_task + split_into_subtasks (mock 2 files)
│   │   ├── scheduler.py                       # claim_one_subtask (FOR UPDATE SKIP LOCKED)
│   │   └── executor_service.py                # heartbeat upsert, status transitions
│   ├── api/
│   │   ├── tasks.py                           # POST/GET /tasks, GET /tasks/{id}
│   │   ├── executors.py                       # POST /executors/join, /heartbeat, /poll
│   │   ├── subtasks.py                        # POST /subtasks/{id}/report
│   │   └── (existing health.py)
│   ├── alembic/versions/
│   │   └── <auto>_add_task_indexes.py         # CREATE INDEX for hot scheduler paths
│   └── (existing main.py, db/, config.py)
└── tests/
    ├── auth/
    │   ├── __init__.py
    │   └── test_bearer.py
    ├── api/
    │   ├── test_tasks.py                      # CRUD + auth
    │   ├── test_executors.py                  # join/heartbeat/poll
    │   └── test_subtasks.py                   # report
    ├── services/
    │   ├── __init__.py
    │   ├── test_task_service.py               # split_into_subtasks
    │   └── test_scheduler.py                  # FOR UPDATE SKIP LOCKED contention
    └── e2e/
        ├── __init__.py
        └── test_happy_path.py                 # POST task → poll → report → succeeded
```

**Why this structure:** Separation of concerns — `api/` = HTTP boundary, `schemas/` = wire contract, `services/` = business logic, `db/` = persistence. Each test file mirrors its source. The `e2e/` directory holds black-box integration tests that drive the controller via HTTP only (no internal imports).

---

## Pre-flight checks

- [ ] **Phase 1 merged** (or working off `feat/phase-1-foundation`)
- [ ] **Local PG running** (`pg_isready -h localhost -p 5433` or your override)
- [ ] **`uv sync` clean** (no missing deps)
- [ ] **All 18 Phase 1 tests pass** (`uv run pytest tests/`)
- [ ] **Branch created**: `git checkout -b feat/phase-1-week-2-controller-core` (off main if PR #1 merged, else off `feat/phase-1-foundation`)

> **Note**: Week 2 no longer needs a separate `dlw` database for tests — the Pre-Task-0 fixture below points the app at the per-session test DB.

---

## Pre-Task 0: Update conftest to point app at test DB (W2-B fix)

**Files:**
- Modify: `tests/conftest.py` (add session-autouse fixture)

**Why:** Without this, the FastAPI app's `_session()` dependency calls `get_engine()` which reads `DLW_DB_NAME` (default = `dlw`, the production database). API tests seed data into the test DB created by the `engine` fixture but the app under test queries the production DB → all API tests in Tasks 4/7/8/10 silently fail or 500 in CI. Solution: a session-autouse fixture overrides env vars + clears engine cache before any API test runs.

- [ ] **Step 1: Append to `tests/conftest.py`**

```python
@pytest_asyncio.fixture(scope="session", autouse=True)
async def _point_app_at_test_db(test_db_name: str, engine: AsyncEngine):
    """Make the FastAPI app's get_engine() / get_settings() see the test DB.

    Runs once per pytest session AFTER the engine fixture creates the test DB.
    Without this, API tests would query the production `dlw` DB which has no
    seeded fixtures.
    """
    env_overrides = {
        "DLW_DB_HOST": os.environ.get("DLW_TEST_PG_HOST", "localhost"),
        "DLW_DB_PORT": os.environ.get("DLW_TEST_PG_PORT", "5433"),
        "DLW_DB_USER": os.environ.get("DLW_TEST_PG_USER", "postgres"),
        "DLW_DB_PASSWORD": os.environ.get("DLW_TEST_PG_PASSWORD", ""),
        "DLW_DB_NAME": test_db_name,
    }
    saved = {k: os.environ.get(k) for k in env_overrides}
    os.environ.update(env_overrides)

    from dlw.config import get_settings
    from dlw.db.session import reset_engine
    get_settings.cache_clear()
    await reset_engine()

    yield

    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    await reset_engine()
```

(`AsyncEngine` should already be imported at the top of `conftest.py`; if not, add the import.)

- [ ] **Step 2: Verify Phase 1 tests still pass**

```bash
uv run pytest tests/ -v 2>&1 | tail -10
```

Expected: 18 PASS (no regression). The session-autouse fixture doesn't affect Phase 1 tests because they don't drive the FastAPI app via HTTP.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "$(cat <<'EOF'
test(infra): conftest session-autouse — point app at test DB

Without this, API tests would query the production `dlw` DB while seeding
went to test_dlw_<random>. New fixture overrides DLW_DB_NAME to test_db_name
session-wide and clears the lru_cached engine + settings.

Refs: W2-B from 2-agent plan review (2026-05-07)
Plan: docs/superpowers/plans/2026-05-08-phase-1-week-2-controller-core.md Pre-Task 0
EOF
)"
```

---

## Task 1: Bearer token authentication (TDD)

**Files:**
- Create: `src/dlw/auth/__init__.py`
- Create: `src/dlw/auth/bearer.py`
- Create: `tests/auth/__init__.py`
- Create: `tests/auth/test_bearer.py`
- Modify: `src/dlw/config.py` (add `bearer_token` setting)

- [ ] **Step 1: Add `bearer_token` to config**

In `src/dlw/config.py`, add inside the `Settings` class (after `db_name`, before `log_level`):

```python
    bearer_token: str = Field(default="dev-token-change-me")
```

Add a comment above explaining: this is a single shared secret. Multi-user OIDC comes in Phase 3.

- [ ] **Step 2: Write failing test `tests/auth/test_bearer.py`**

```python
"""Bearer token middleware tests using FastAPI TestClient."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Depends
from httpx import ASGITransport, AsyncClient

from dlw.auth.bearer import require_bearer
from dlw.config import get_settings


@pytest.fixture(autouse=True)
def _reset_settings():
    """Each test gets fresh settings cache so DLW_BEARER_TOKEN env can vary."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _app_with_protected_route() -> FastAPI:
    app = FastAPI()
    @app.get("/protected", dependencies=[Depends(require_bearer)])
    async def _():
        return {"ok": True}
    return app


@pytest.mark.slow
async def test_no_token_returns_401() -> None:
    app = _app_with_protected_route()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/protected")
    assert r.status_code == 401
    assert r.json()["detail"] == "missing bearer token"


@pytest.mark.slow
async def test_wrong_token_returns_401() -> None:
    app = _app_with_protected_route()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/protected", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid bearer token"


@pytest.mark.slow
async def test_correct_token_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLW_BEARER_TOKEN", "secret-xyz")
    get_settings.cache_clear()
    app = _app_with_protected_route()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/protected", headers={"Authorization": "Bearer secret-xyz"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


@pytest.mark.slow
async def test_malformed_authorization_returns_401() -> None:
    app = _app_with_protected_route()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # missing "Bearer " prefix
        r = await c.get("/protected", headers={"Authorization": "secret-xyz"})
    assert r.status_code == 401
```

- [ ] **Step 3: Run test — verify red**

```bash
uv run pytest tests/auth/test_bearer.py -v
```

Expected: ImportError on `dlw.auth.bearer`.

- [ ] **Step 4: Implement `src/dlw/auth/__init__.py`**

```python
```

- [ ] **Step 5: Implement `src/dlw/auth/bearer.py`**

```python
"""Bearer token authentication.

Single shared secret pulled from DLW_BEARER_TOKEN env var.
Multi-user OIDC PKCE comes in Phase 3 (see docs/v2.0/04-security-and-tenancy.md).
"""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from dlw.config import get_settings


async def require_bearer(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: 401 unless Authorization: Bearer <correct-token> present."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="malformed authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    expected = get_settings().bearer_token
    # Constant-time comparison to defeat timing attacks
    if not secrets.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
```

- [ ] **Step 6: Verify green**

```bash
uv run pytest tests/auth/test_bearer.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/dlw/auth/ src/dlw/config.py tests/auth/
git commit -m "$(cat <<'EOF'
feat(auth): bearer token middleware with constant-time compare

Single shared secret from DLW_BEARER_TOKEN env (default 'dev-token-change-me').
Multi-user OIDC PKCE deferred to Phase 3.

Refs: docs/v2.0/04-security-and-tenancy.md (§3 simplified for Week 2)
Plan: docs/superpowers/plans/2026-05-08-phase-1-week-2-controller-core.md Task 1
EOF
)"
```

---

## Task 2: Pydantic schemas (DTOs)

**Files:**
- Create: `src/dlw/schemas/__init__.py`
- Create: `src/dlw/schemas/task.py`
- Create: `src/dlw/schemas/subtask.py`
- Create: `src/dlw/schemas/executor.py`

No tests — schemas are validated at API boundary in Tasks 3-7.

- [ ] **Step 1: Write `src/dlw/schemas/__init__.py`**

```python
"""Pydantic v2 DTOs — wire contract for HTTP API.

Kept separate from ORM models (dlw.db.models) so the database schema can
evolve independently of the public API.
"""
```

- [ ] **Step 2: Write `src/dlw/schemas/task.py`**

```python
"""Task request/response DTOs."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    """POST /api/v1/tasks request body."""
    repo_id: str = Field(min_length=1, max_length=256, examples=["deepseek-ai/DeepSeek-V3"])
    revision: str = Field(min_length=1, max_length=64, examples=["abc123def4567890" * 2 + "abc12345"])
    storage_id: int = Field(gt=0)
    path_template: str = Field(default="{tenant}/{repo_id}/{revision}", max_length=512)
    priority: int = Field(default=1, ge=0, le=10)


class TaskRead(BaseModel):
    """GET /api/v1/tasks/{id} response body (also items in list)."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repo_id: str
    revision: str
    status: str
    priority: int
    created_at: datetime
    completed_at: datetime | None
    error_message: str | None


class TaskList(BaseModel):
    """GET /api/v1/tasks response body."""
    items: list[TaskRead]
    total: int
```

- [ ] **Step 3: Write `src/dlw/schemas/subtask.py`**

```python
"""SubTask request/response DTOs."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SubTaskRead(BaseModel):
    """Returned in assignment payload + GET subtask detail."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    filename: str
    file_size: int | None
    expected_sha256: str | None
    status: str


class SubTaskReport(BaseModel):
    """POST /api/v1/subtasks/{id}/report request body — executor reports outcome."""
    status: Literal["succeeded", "failed"]
    assignment_token: uuid.UUID | None = Field(
        default=None,
        description="Token from /poll's AssignmentResponse — verified against "
                    "stored value to defend against stale/forged reports (W2-F).",
    )
    actual_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    bytes_downloaded: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=2048)
```

- [ ] **Step 4: Write `src/dlw/schemas/executor.py`**

```python
"""Executor request/response DTOs."""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from dlw.schemas.subtask import SubTaskRead


class ExecutorJoin(BaseModel):
    """POST /api/v1/executors/join — first contact from new executor.

    NOTE (W2-H): Invariant 9 in `docs/v2.0/03-distributed-correctness.md`
    requires id format `^[a-z0-9-]+-worker-\\d+$`. Week 2 does NOT enforce this
    at the schema level — tests use shorter ids like 'exec-A' for brevity.
    Phase 2 will add a `@field_validator("id")` enforcing the regex once mTLS
    cert binding is in place (the cert CN should match the executor id).
    """
    id: str = Field(min_length=1, max_length=64, examples=["host-12.local-worker-1"])
    host_id: str = Field(min_length=1, max_length=64)
    cert_fingerprint: str = Field(default="placeholder-week2", max_length=128)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class ExecutorHeartbeat(BaseModel):
    """POST /api/v1/executors/{id}/heartbeat — periodic liveness ping."""
    health_score: int = Field(default=100, ge=0, le=100)
    parts_dir_bytes: int = Field(default=0, ge=0)


class ExecutorRead(BaseModel):
    """Returned by join/heartbeat to confirm registration."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    health_score: int


class AssignmentResponse(BaseModel):
    """POST /api/v1/executors/{id}/poll response — either subtask or empty."""
    assigned: bool
    subtask: SubTaskRead | None = None
    assignment_token: uuid.UUID | None = None
```

- [ ] **Step 5: Verify imports**

```bash
uv run python -c "from dlw.schemas.task import TaskCreate, TaskRead; from dlw.schemas.subtask import SubTaskReport; from dlw.schemas.executor import ExecutorJoin, AssignmentResponse; print('all schemas import OK')"
```

Expected: prints `all schemas import OK`. No ImportError.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/schemas/
git commit -m "$(cat <<'EOF'
feat(schemas): pydantic DTOs for tasks / subtasks / executors

Wire contract for the controller API. Kept separate from ORM models so
DB schema can evolve independently. Validation handled by FastAPI at the
HTTP boundary.

Plan: docs/superpowers/plans/2026-05-08-phase-1-week-2-controller-core.md Task 2
EOF
)"
```

---

## Task 3: Task service — create + split (TDD)

**Files:**
- Create: `src/dlw/services/__init__.py`
- Create: `src/dlw/services/task_service.py`
- Create: `tests/services/__init__.py`
- Create: `tests/services/test_task_service.py`

Service layer holds business logic. Routers call services; services call ORM. This task implements `create_task` (single transactional method that persists task + 2 mock subtasks).

- [ ] **Step 1: Write failing test `tests/services/test_task_service.py`**

```python
"""Tests for dlw.services.task_service.create_task."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.schemas.task import TaskCreate
from dlw.services.task_service import create_task


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def env(db_session: AsyncSession):
    """Tenant + project + user + storage fixtures (tenant_id=1 hardcoded for Week 2)."""
    tenant = Tenant(id=1, slug="default", display_name="Default")
    db_session.add(tenant)
    await db_session.flush()
    project = Project(id=1, tenant_id=1, name="default")
    db_session.add(project)
    user = User(
        id=1, tenant_id=1, oidc_subject="dev-user", email="dev@local",
        role="tenant_admin",
    )
    db_session.add(user)
    sb = StorageBackend(
        id=1, tenant_id=1, name="default", backend_type="s3", config_encrypted=b""
    )
    db_session.add(sb)
    await db_session.flush()


@pytest.mark.slow
async def test_create_task_persists_2_subtasks(db_session: AsyncSession, env) -> None:
    body = TaskCreate(
        repo_id="deepseek-ai/DeepSeek-V3",
        revision="0123456789abcdef" * 2 + "01234567",  # 40-char hex sha
        storage_id=1,
    )
    task = await create_task(db_session, body, owner_user_id=1, tenant_id=1, project_id=1)
    assert task.id is not None
    assert task.status == "pending"

    # Verify 2 mock subtasks created
    subs = (await db_session.execute(
        select(FileSubTask).where(FileSubTask.task_id == task.id)
    )).scalars().all()
    assert len(subs) == 2
    filenames = sorted(s.filename for s in subs)
    assert filenames == ["config.json", "model.safetensors"]
    assert all(s.status == "pending" for s in subs)
    assert all(s.tenant_id == 1 for s in subs)


@pytest.mark.slow
async def test_create_task_status_pending(db_session: AsyncSession, env) -> None:
    body = TaskCreate(repo_id="o/r", revision="0" * 40, storage_id=1)
    task = await create_task(db_session, body, owner_user_id=1, tenant_id=1, project_id=1)
    assert task.status == "pending"
    assert task.is_simulation is False
```

- [ ] **Step 2: Verify red**

```bash
uv run pytest tests/services/test_task_service.py -v
```

Expected: ImportError on `dlw.services.task_service`.

- [ ] **Step 3: Write `src/dlw/services/__init__.py`**

```python
"""Service layer — business logic between API routers and ORM."""
```

- [ ] **Step 4: Implement `src/dlw/services/task_service.py`**

```python
"""Task service: creation + sub-task generation.

In Week 2 we mock sub-task generation as 2 placeholder files. Real HuggingFace
Hub resolution comes in Week 4 plan.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.schemas.task import TaskCreate

# Week 2 mock: every task gets exactly these 2 subtasks
_MOCK_FILES: list[tuple[str, int | None, str | None]] = [
    # (filename, file_size, expected_sha256)
    ("config.json", 4096, None),
    ("model.safetensors", 1_073_741_824, None),  # 1 GB placeholder
]


async def create_task(
    session: AsyncSession,
    body: TaskCreate,
    *,
    owner_user_id: int,
    tenant_id: int,
    project_id: int,
) -> DownloadTask:
    """Persist a download task plus its mock subtasks atomically.

    Caller is responsible for transaction boundary (commit/rollback).
    """
    task = DownloadTask(
        tenant_id=tenant_id,
        project_id=project_id,
        owner_user_id=owner_user_id,
        repo_id=body.repo_id,
        revision=body.revision,
        storage_id=body.storage_id,
        path_template=body.path_template,
        priority=body.priority,
        status="pending",
    )
    session.add(task)
    await session.flush()  # populate task.id

    for filename, size, sha in _MOCK_FILES:
        session.add(FileSubTask(
            task_id=task.id,
            tenant_id=tenant_id,
            filename=filename,
            file_size=size,
            expected_sha256=sha,
            status="pending",
        ))
    await session.flush()
    return task
```

- [ ] **Step 5: Verify green**

```bash
uv run pytest tests/services/test_task_service.py -v
```

Expected: 2 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/services/ tests/services/
git commit -m "$(cat <<'EOF'
feat(services): task_service.create_task with mock 2-subtask split

Week 2 mock generates fixed (config.json + model.safetensors) per task.
Real HuggingFace Hub resolution comes in Week 4 plan (streaming sha256).

Plan: docs/superpowers/plans/2026-05-08-phase-1-week-2-controller-core.md Task 3
EOF
)"
```

---

## Task 4: Tasks API — POST + GET list + GET by id (TDD)

**Files:**
- Create: `src/dlw/api/tasks.py`
- Create: `tests/api/test_tasks.py`
- Modify: `src/dlw/main.py` (register tasks router)

Routes are protected by `require_bearer`. All queries hardcode `tenant_id=1` for Week 2.

- [ ] **Step 1: Write failing test `tests/api/test_tasks.py`**

```python
"""Tests for tasks API: POST/GET list/GET by id with bearer auth."""
from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from dlw.config import get_settings
from dlw.db.base import Base


_TOKEN = "test-bearer-token-12345"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine: AsyncEngine):
    """Create tables + seed default tenant/project/user/storage row.

    Re-uses the seeded fixtures so HTTP tests can drive the API end-to-end.
    """
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with engine.begin() as conn:
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
    monkeypatch.setenv("DLW_BEARER_TOKEN", _TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture
async def client():
    """ASGI client; create a fresh app each test so dependency overrides isolate."""
    from dlw.main import create_app
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.slow
async def test_post_task_unauthenticated_returns_401(client: AsyncClient) -> None:
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/r", "revision": "0" * 40, "storage_id": 1,
    })
    assert r.status_code == 401


@pytest.mark.slow
async def test_post_task_returns_201_with_id(client: AsyncClient, auth) -> None:
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "deepseek-ai/DeepSeek-V3",
        "revision": "0123456789abcdef" * 2 + "01234567",
        "storage_id": 1,
        "priority": 5,
    }, headers=auth)
    assert r.status_code == 201, r.text
    body = r.json()
    assert "id" in body
    assert body["status"] == "pending"
    assert body["repo_id"] == "deepseek-ai/DeepSeek-V3"
    assert body["priority"] == 5


@pytest.mark.slow
async def test_get_tasks_list_returns_paginated(client: AsyncClient, auth) -> None:
    # Create a couple
    for i in range(2):
        await client.post("/api/v1/tasks", json={
            "repo_id": f"o/list-{i}",
            "revision": "1" * 40,
            "storage_id": 1,
        }, headers=auth)
    r = await client.get("/api/v1/tasks", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "total" in body
    assert body["total"] >= 2


@pytest.mark.slow
async def test_get_task_by_id_returns_detail(client: AsyncClient, auth) -> None:
    create = await client.post("/api/v1/tasks", json={
        "repo_id": "o/detail", "revision": "2" * 40, "storage_id": 1,
    }, headers=auth)
    task_id = create.json()["id"]
    r = await client.get(f"/api/v1/tasks/{task_id}", headers=auth)
    assert r.status_code == 200
    assert r.json()["id"] == task_id


@pytest.mark.slow
async def test_get_unknown_task_returns_404(client: AsyncClient, auth) -> None:
    import uuid
    r = await client.get(f"/api/v1/tasks/{uuid.uuid4()}", headers=auth)
    assert r.status_code == 404


@pytest.mark.slow
async def test_post_task_validation_error_returns_422(client: AsyncClient, auth) -> None:
    # Missing required field 'storage_id'
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/r", "revision": "0" * 40,
    }, headers=auth)
    assert r.status_code == 422
```

- [ ] **Step 2: Verify red**

```bash
uv run pytest tests/api/test_tasks.py -v
```

Expected: errors related to missing `dlw.api.tasks` module / 404 on routes.

- [ ] **Step 3: Implement `src/dlw/api/tasks.py`**

```python
"""Tasks API: POST / GET list / GET by id.

Week 2: tenant_id=1, project_id=1, owner_user_id=1 hardcoded. Multi-tenancy
scoping via JWT claims comes in Phase 3.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.bearer import require_bearer
from dlw.db.models.task import DownloadTask
from dlw.db.session import get_engine
from dlw.schemas.task import TaskCreate, TaskList, TaskRead
from dlw.services.task_service import create_task

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

# Week 2 single-tenant defaults
_TENANT_ID = 1
_PROJECT_ID = 1
_OWNER_USER_ID = 1


async def _session() -> AsyncSession:
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
    # Week 2: no pagination yet, just return all for tenant_id=1
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
async def get_task(task_id: uuid.UUID, session: AsyncSession = Depends(_session)) -> TaskRead:
    task = await session.get(DownloadTask, task_id)
    if task is None or task.tenant_id != _TENANT_ID:
        raise HTTPException(status_code=404, detail="task not found")
    return TaskRead.model_validate(task)
```

- [ ] **Step 4: Register router in `src/dlw/main.py`**

In `create_app()`, add after `app.include_router(health_router)`:

```python
    from dlw.api.tasks import router as tasks_router
    app.include_router(tasks_router)
```

- [ ] **Step 5: Verify green**

```bash
uv run pytest tests/api/test_tasks.py -v
```

Expected: 6 tests PASS.

Then full regression:
```bash
uv run pytest tests/ -v 2>&1 | tail -10
```

Expected: 18 (Phase 1) + 4 (auth) + 2 (task_service) + 6 (tasks api) = 30 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/api/tasks.py src/dlw/main.py tests/api/test_tasks.py
git commit -m "$(cat <<'EOF'
feat(api): POST + GET tasks endpoints with bearer auth

- POST /api/v1/tasks → 201 with task ID + auto-generated 2 subtasks
- GET  /api/v1/tasks → list (no pagination yet — Week 5)
- GET  /api/v1/tasks/{id} → detail or 404

Single-tenant (tenant_id=1) hardcoded for Week 2; Phase 3 adds
tenant scoping via JWT claims.

Plan: docs/superpowers/plans/2026-05-08-phase-1-week-2-controller-core.md Task 4
EOF
)"
```

---

## Task 5: Executor service — join + heartbeat (TDD)

**Files:**
- Create: `src/dlw/services/executor_service.py`
- Create: `tests/services/test_executor_service.py`

- [ ] **Step 1: Write failing test `tests/services/test_executor_service.py`**

```python
"""Tests for executor_service: join + heartbeat upsert."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.schemas.executor import ExecutorHeartbeat, ExecutorJoin
from dlw.services.executor_service import join_executor, record_heartbeat


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
async def test_join_creates_executor(db_session: AsyncSession) -> None:
    body = ExecutorJoin(
        id="host-a-w1", host_id="host-a", capabilities={"nic_speed_gbps": 10},
    )
    ex = await join_executor(db_session, body)
    await db_session.commit()
    assert ex.id == "host-a-w1"
    assert ex.status == "joining"
    assert ex.health_score == 100


@pytest.mark.slow
async def test_join_idempotent(db_session: AsyncSession) -> None:
    body = ExecutorJoin(id="host-b-w1", host_id="host-b")
    await join_executor(db_session, body)
    await db_session.commit()
    # Second join with same id is allowed (e.g., executor restart) — should not raise
    again = await join_executor(db_session, body)
    await db_session.commit()
    assert again.id == "host-b-w1"


@pytest.mark.slow
async def test_heartbeat_updates_health_and_timestamp(db_session: AsyncSession) -> None:
    await join_executor(db_session, ExecutorJoin(id="host-c-w1", host_id="host-c"))
    await db_session.commit()
    before = datetime.now(UTC)
    ex = await record_heartbeat(
        db_session, "host-c-w1",
        ExecutorHeartbeat(health_score=87, parts_dir_bytes=1024),
    )
    await db_session.commit()
    assert ex.status == "healthy"  # transitions joining → healthy on first heartbeat
    assert ex.health_score == 87
    assert ex.parts_dir_bytes == 1024
    assert ex.last_heartbeat_at is not None
    assert ex.last_heartbeat_at >= before - timedelta(seconds=1)


@pytest.mark.slow
async def test_heartbeat_unknown_executor_raises(db_session: AsyncSession) -> None:
    with pytest.raises(LookupError):
        await record_heartbeat(
            db_session, "no-such-executor",
            ExecutorHeartbeat(),
        )
```

- [ ] **Step 2: Verify red**

```bash
uv run pytest tests/services/test_executor_service.py -v
```

Expected: ImportError on `dlw.services.executor_service`.

- [ ] **Step 3: Implement `src/dlw/services/executor_service.py`**

```python
"""Executor service: join (idempotent register) + heartbeat (upsert state)."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.executor import Executor
from dlw.schemas.executor import ExecutorHeartbeat, ExecutorJoin


async def join_executor(session: AsyncSession, body: ExecutorJoin) -> Executor:
    """Idempotent: if executor with this id exists, return it (no schema change).

    Phase 2 adds executor_epoch increment on rejoin (fence-token invariant).
    """
    existing = await session.get(Executor, body.id)
    if existing is not None:
        return existing
    ex = Executor(
        id=body.id,
        host_id=body.host_id,
        cert_fingerprint=body.cert_fingerprint,
        capabilities=body.capabilities,
        status="joining",
    )
    session.add(ex)
    await session.flush()
    return ex


async def record_heartbeat(
    session: AsyncSession,
    executor_id: str,
    body: ExecutorHeartbeat,
) -> Executor:
    """Update last_heartbeat_at + health_score + parts_dir_bytes.

    Transitions joining → healthy on first heartbeat.
    """
    ex = await session.get(Executor, executor_id)
    if ex is None:
        raise LookupError(f"executor {executor_id} not found (must POST /join first)")
    ex.last_heartbeat_at = datetime.now(UTC)
    ex.health_score = body.health_score
    ex.parts_dir_bytes = body.parts_dir_bytes
    if ex.status == "joining":
        ex.status = "healthy"
    ex.consecutive_heartbeat_failures = 0
    return ex
```

- [ ] **Step 4: Verify green**

```bash
uv run pytest tests/services/test_executor_service.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/services/executor_service.py tests/services/test_executor_service.py
git commit -m "$(cat <<'EOF'
feat(services): executor_service.join + record_heartbeat

join is idempotent (executor restart is a no-op); heartbeat transitions
joining → healthy and resets consecutive_heartbeat_failures.

Phase 2 will add executor_epoch increment on rejoin (fence-token invariant).

Plan: docs/superpowers/plans/2026-05-08-phase-1-week-2-controller-core.md Task 5
EOF
)"
```

---

## Task 6: Scheduler service — claim_one_subtask with FOR UPDATE SKIP LOCKED (TDD)

**Files:**
- Create: `src/dlw/services/scheduler.py`
- Create: `tests/services/test_scheduler.py`

This is the heart of the controller. Multiple executors must be able to poll concurrently without claiming the same subtask. PG's `SELECT ... FOR UPDATE SKIP LOCKED` solves this elegantly.

- [ ] **Step 1: Write failing test `tests/services/test_scheduler.py`**

```python
"""Tests for scheduler.claim_one_subtask — atomic FOR UPDATE SKIP LOCKED."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.services.scheduler import claim_one_subtask


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def env(engine):
    """Seed minimum data ONCE per module — committed rows would PK-conflict
    if this fixture were function-scoped (W2-C from review)."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="d", display_name="D"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="d"))
        s.add(User(id=1, tenant_id=1, oidc_subject="d",
                    email="d@l", role="tenant_admin"))
        s.add(StorageBackend(id=1, tenant_id=1, name="d",
                              backend_type="s3", config_encrypted=b""))
        s.add(Executor(id="exec-A", host_id="ha",
                        cert_fingerprint="fp", status="healthy"))
        await s.commit()


async def _make_pending_task(session: AsyncSession, n_subtasks: int) -> DownloadTask:
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="0" * 40,
        storage_id=1, path_template="x", status="pending",
    )
    session.add(task)
    await session.flush()
    for i in range(n_subtasks):
        session.add(FileSubTask(
            task_id=task.id, tenant_id=1,
            filename=f"file-{i}.bin", status="pending",
        ))
    await session.commit()
    return task


@pytest.mark.slow
async def test_claim_returns_subtask_when_pending_exists(
    db_session: AsyncSession, env, engine
) -> None:
    await _make_pending_task(db_session, n_subtasks=1)
    sub, token = await claim_one_subtask(db_session, executor_id="exec-A")
    await db_session.commit()
    assert sub is not None
    assert token is not None
    assert sub.status == "assigned"
    assert sub.executor_id == "exec-A"
    assert sub.assignment_token == token


@pytest.mark.slow
async def test_claim_returns_none_when_no_pending(
    db_session: AsyncSession, env
) -> None:
    sub, token = await claim_one_subtask(db_session, executor_id="exec-A")
    await db_session.commit()
    assert sub is None
    assert token is None


@pytest.mark.slow
async def test_two_concurrent_claims_get_different_subtasks(
    db_session: AsyncSession, engine
) -> None:
    """Concurrency: 2 sessions polling at once must get DIFFERENT subtasks."""
    await _make_pending_task(db_session, n_subtasks=2)

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def claim_in_own_session() -> uuid.UUID | None:
        async with factory() as s:
            sub, _ = await claim_one_subtask(s, executor_id="exec-A")
            await s.commit()
            return sub.id if sub else None

    id1, id2 = await asyncio.gather(claim_in_own_session(), claim_in_own_session())
    assert id1 is not None and id2 is not None
    assert id1 != id2


@pytest.mark.slow
async def test_one_subtask_two_claimants_only_one_wins(
    db_session: AsyncSession, engine
) -> None:
    """Critical correctness test: with EXACTLY 1 pending subtask and 2
    concurrent claimants, exactly one must succeed and one must get None.

    The 2-subtask test above passes even if SKIP LOCKED is broken (each
    claimant just picks a different row). This test directly falsifies
    the double-claim scenario: without SKIP LOCKED, both claimants would
    block on FOR UPDATE and both would eventually claim the same row.
    """
    await _make_pending_task(db_session, n_subtasks=1)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def claim_in_own_session() -> uuid.UUID | None:
        async with factory() as s:
            sub, _ = await claim_one_subtask(s, executor_id="exec-A")
            await s.commit()
            return sub.id if sub else None

    r1, r2 = await asyncio.gather(claim_in_own_session(), claim_in_own_session())
    succeeded = [r for r in (r1, r2) if r is not None]
    assert len(succeeded) == 1, f"Expected exactly 1 winner, got {succeeded}"


@pytest.mark.slow
async def test_third_claim_returns_none_when_all_assigned(
    db_session: AsyncSession, env, engine
) -> None:
    await _make_pending_task(db_session, n_subtasks=2)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # First two claims succeed
    async with factory() as s:
        sub1, _ = await claim_one_subtask(s, executor_id="exec-A")
        await s.commit()
    async with factory() as s:
        sub2, _ = await claim_one_subtask(s, executor_id="exec-A")
        await s.commit()
    assert sub1 is not None and sub2 is not None
    # Third call — nothing left
    async with factory() as s:
        sub3, _ = await claim_one_subtask(s, executor_id="exec-A")
        await s.commit()
    assert sub3 is None
```

- [ ] **Step 2: Verify red**

```bash
uv run pytest tests/services/test_scheduler.py -v
```

Expected: ImportError on `dlw.services.scheduler`.

- [ ] **Step 3: Implement `src/dlw/services/scheduler.py`**

```python
"""Scheduler: atomic claim_one_subtask using FOR UPDATE SKIP LOCKED.

Pull-model: executors call /poll → controller calls this. PostgreSQL's
SKIP LOCKED ensures two concurrent claimants never get the same row.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.task import FileSubTask


async def claim_one_subtask(
    session: AsyncSession,
    executor_id: str,
) -> tuple[FileSubTask | None, uuid.UUID | None]:
    """Atomically grab one pending subtask for this executor.

    Returns (None, None) if no pending subtasks. Caller must commit() to
    finalize the claim (the row stays locked until commit/rollback).

    Phase 2 will add: priority ordering, fairness across tenants,
    executor_epoch fence-token write.
    """
    stmt = (
        select(FileSubTask)
        .where(FileSubTask.status == "pending")
        .order_by(FileSubTask.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    sub = (await session.execute(stmt)).scalar_one_or_none()
    if sub is None:
        return None, None

    token = uuid.uuid4()
    sub.status = "assigned"
    sub.executor_id = executor_id
    sub.assignment_token = token
    return sub, token
```

- [ ] **Step 4: Verify green**

```bash
uv run pytest tests/services/test_scheduler.py -v
```

Expected: 4 tests PASS. The concurrent test (`test_two_concurrent_claims_get_different_subtasks`) is the critical one — if it fails, SKIP LOCKED isn't working.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/services/scheduler.py tests/services/test_scheduler.py
git commit -m "$(cat <<'EOF'
feat(services): scheduler.claim_one_subtask with FOR UPDATE SKIP LOCKED

Atomic claim guards against double-assignment under concurrency.
Test verifies 2 concurrent claimants get DIFFERENT subtasks.

Phase 2 will add priority ordering, fairness across tenants, and
executor_epoch fence-token writes (invariant 6 / 7).

Refs: docs/v2.0/03-distributed-correctness.md (CAS-then-enqueue invariant 6)
Plan: docs/superpowers/plans/2026-05-08-phase-1-week-2-controller-core.md Task 6
EOF
)"
```

---

## Task 7: Executors API — join + heartbeat + poll (TDD)

**Files:**
- Create: `src/dlw/api/executors.py`
- Create: `tests/api/test_executors.py`
- Modify: `src/dlw/main.py`

- [ ] **Step 1: Write failing test `tests/api/test_executors.py`**

```python
"""Tests for executors API: join / heartbeat / poll with bearer auth."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base


_TOKEN = "test-bearer-token-12345"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """Create tables + seed default tenant/project/user/storage."""
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="d", display_name="D"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="d"))
        s.add(User(id=1, tenant_id=1, oidc_subject="d",
                   email="d@l", role="tenant_admin"))
        s.add(StorageBackend(id=1, tenant_id=1, name="d",
                              backend_type="s3", config_encrypted=b""))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DLW_BEARER_TOKEN", _TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture
async def client():
    from dlw.main import create_app
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.slow
async def test_executor_join_returns_201(client, auth) -> None:
    r = await client.post("/api/v1/executors/join", json={
        "id": "exec-test-1", "host_id": "host-test",
    }, headers=auth)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "exec-test-1"
    assert body["status"] == "joining"


@pytest.mark.slow
async def test_executor_heartbeat_transitions_to_healthy(client, auth) -> None:
    await client.post("/api/v1/executors/join", json={
        "id": "exec-hb-1", "host_id": "host-hb"
    }, headers=auth)
    r = await client.post("/api/v1/executors/exec-hb-1/heartbeat",
                          json={"health_score": 95}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "healthy"
    assert r.json()["health_score"] == 95


@pytest.mark.slow
async def test_poll_returns_assigned_false_when_no_work(client, auth) -> None:
    await client.post("/api/v1/executors/join", json={
        "id": "exec-poll-empty", "host_id": "host-pe"
    }, headers=auth)
    r = await client.post("/api/v1/executors/exec-poll-empty/poll", headers=auth)
    assert r.status_code == 200
    assert r.json()["assigned"] is False
    assert r.json()["subtask"] is None


@pytest.mark.slow
async def test_poll_returns_subtask_when_work_available(client, auth) -> None:
    # Create a task so subtasks exist
    await client.post("/api/v1/tasks", json={
        "repo_id": "o/poll", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    # Register executor
    await client.post("/api/v1/executors/join", json={
        "id": "exec-poll-w", "host_id": "host-pw"
    }, headers=auth)
    r = await client.post("/api/v1/executors/exec-poll-w/poll", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assigned"] is True
    assert body["subtask"] is not None
    assert "id" in body["subtask"]
    assert body["assignment_token"] is not None


@pytest.mark.slow
async def test_unauthenticated_returns_401(client) -> None:
    r = await client.post("/api/v1/executors/join", json={
        "id": "x", "host_id": "y"
    })
    assert r.status_code == 401
```

- [ ] **Step 2: Verify red**

```bash
uv run pytest tests/api/test_executors.py -v
```

Expected: 404s (no /api/v1/executors/* routes yet) or ImportError.

- [ ] **Step 3: Implement `src/dlw/api/executors.py`**

```python
"""Executors API: join / heartbeat / poll."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session  # reuse session dep
from dlw.auth.bearer import require_bearer
from dlw.schemas.executor import (
    AssignmentResponse,
    ExecutorHeartbeat,
    ExecutorJoin,
    ExecutorRead,
)
from dlw.schemas.subtask import SubTaskRead
from dlw.services.executor_service import join_executor, record_heartbeat
from dlw.services.scheduler import claim_one_subtask

router = APIRouter(prefix="/api/v1/executors", tags=["executors"])


@router.post("/join", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_bearer)])
async def post_join(
    body: ExecutorJoin, session: AsyncSession = Depends(_session)
) -> ExecutorRead:
    ex = await join_executor(session, body)
    await session.commit()
    return ExecutorRead.model_validate(ex)


@router.post("/{executor_id}/heartbeat", dependencies=[Depends(require_bearer)])
async def post_heartbeat(
    executor_id: str,
    body: ExecutorHeartbeat,
    session: AsyncSession = Depends(_session),
) -> ExecutorRead:
    try:
        ex = await record_heartbeat(session, executor_id, body)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    await session.commit()
    return ExecutorRead.model_validate(ex)


@router.post("/{executor_id}/poll", dependencies=[Depends(require_bearer)])
async def post_poll(
    executor_id: str, session: AsyncSession = Depends(_session)
) -> AssignmentResponse:
    sub, token = await claim_one_subtask(session, executor_id)
    if sub is None:
        return AssignmentResponse(assigned=False)
    sub_read = SubTaskRead.model_validate(sub)
    await session.commit()
    return AssignmentResponse(assigned=True, subtask=sub_read, assignment_token=token)
```

- [ ] **Step 4: Register router in `src/dlw/main.py`**

In `create_app()`, after the tasks router:

```python
    from dlw.api.executors import router as executors_router
    app.include_router(executors_router)
```

- [ ] **Step 5: Verify green**

```bash
uv run pytest tests/api/test_executors.py -v
```

Expected: 5 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/api/executors.py src/dlw/main.py tests/api/test_executors.py
git commit -m "$(cat <<'EOF'
feat(api): POST executors join / heartbeat / poll endpoints

- /join         → idempotent register (201; status=joining)
- /heartbeat    → upsert state (transitions joining → healthy)
- /poll         → atomic claim of one pending subtask (FOR UPDATE SKIP LOCKED)

Plan: docs/superpowers/plans/2026-05-08-phase-1-week-2-controller-core.md Task 7
EOF
)"
```

---

## Task 8: Subtasks API — POST report (TDD)

**Files:**
- Create: `src/dlw/api/subtasks.py`
- Create: `tests/api/test_subtasks.py`
- Modify: `src/dlw/main.py`
- Modify: `src/dlw/services/scheduler.py` (add `complete_subtask` helper)

When all subtasks of a task succeed, the task itself flips to `succeeded`. If any subtask fails, the task flips to `failed`.

- [ ] **Step 1: Add `complete_subtask` to `src/dlw/services/scheduler.py`**

**IMPORTANT**: The new imports below go at the **top** of `scheduler.py` (alongside the existing imports from Task 6), not interleaved with the function body. Don't paste them as-is at the append point.

**Add these imports at the top of `src/dlw/services/scheduler.py`** (next to the existing `import uuid` line):

```python
from datetime import UTC, datetime

from dlw.db.models.task import DownloadTask
```

**Then append `complete_subtask` to the bottom of `src/dlw/services/scheduler.py`**:

```python
async def complete_subtask(
    session: AsyncSession,
    subtask_id: uuid.UUID,
    *,
    final_status: str,  # "succeeded" or "failed"
    actual_sha256: str | None,
    bytes_downloaded: int,
    error: str | None,
    assignment_token: uuid.UUID | None = None,
) -> tuple[FileSubTask, DownloadTask]:
    """Mark subtask done, then check if parent task can transition.

    Locking: parent task fetched with FOR UPDATE so that two concurrent
    completions racing on the LAST 2 subtasks cannot both observe
    "all succeeded" and both write parent.status (W2-E from review).

    Token verification: if `assignment_token` provided, must match the row's
    stored token (cheap defence — token is already in DB; W2-F from review).

    Returns (subtask, parent_task). Caller commits.
    """
    sub = await session.get(FileSubTask, subtask_id)
    if sub is None:
        raise LookupError(f"subtask {subtask_id} not found")
    if sub.status != "assigned":
        raise ValueError(f"subtask {subtask_id} is not assigned (status={sub.status})")
    if assignment_token is not None and sub.assignment_token != assignment_token:
        raise ValueError(f"subtask {subtask_id} assignment_token mismatch")

    sub.status = final_status
    sub.actual_sha256 = actual_sha256
    sub.bytes_downloaded = bytes_downloaded
    sub.last_error = error
    sub.completed_at = datetime.now(UTC)

    # Lock parent before reading siblings — prevents two concurrent
    # completions both flipping parent.status under the same observation
    parent = await session.get(
        DownloadTask, sub.task_id, with_for_update=True
    )
    siblings = (await session.execute(
        select(FileSubTask).where(FileSubTask.task_id == sub.task_id)
    )).scalars().all()

    statuses = {s.status for s in siblings}
    if "failed" in statuses:
        parent.status = "failed"
        parent.error_message = f"subtask {sub.filename} failed: {error}"
        parent.completed_at = datetime.now(UTC)
    elif statuses == {"succeeded"}:
        parent.status = "succeeded"
        parent.completed_at = datetime.now(UTC)
    # else: still pending/assigned subtasks — parent stays in current status

    return sub, parent
```

- [ ] **Step 2: Write failing test `tests/api/test_subtasks.py`**

```python
"""Tests for subtasks API: POST /report."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base


_TOKEN = "test-bearer-token-12345"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="d", display_name="D"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="d"))
        s.add(User(id=1, tenant_id=1, oidc_subject="d",
                   email="d@l", role="tenant_admin"))
        s.add(StorageBackend(id=1, tenant_id=1, name="d",
                              backend_type="s3", config_encrypted=b""))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DLW_BEARER_TOKEN", _TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


@pytest.fixture
async def client():
    from dlw.main import create_app
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _setup_assigned_subtask(client, auth, repo_id="o/sub-test") -> str:
    """Helper: create task → join executor → poll → return subtask id."""
    await client.post("/api/v1/tasks", json={
        "repo_id": repo_id, "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    await client.post("/api/v1/executors/join", json={
        "id": f"ex-{repo_id.replace('/', '-')}", "host_id": "h"
    }, headers=auth)
    r = await client.post(f"/api/v1/executors/ex-{repo_id.replace('/', '-')}/poll",
                          headers=auth)
    return r.json()["subtask"]["id"]


@pytest.mark.slow
async def test_report_succeeded_marks_subtask_done(client, auth) -> None:
    sub_id = await _setup_assigned_subtask(client, auth, "o/r1")
    r = await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "succeeded",
        "actual_sha256": "a" * 64,
        "bytes_downloaded": 1234,
    }, headers=auth)
    assert r.status_code == 200, r.text


@pytest.mark.slow
async def test_report_two_subtasks_succeed_then_task_succeeds(client, auth) -> None:
    """End-to-end: POST task creates 2 subtasks; succeed both; task becomes 'succeeded'."""
    create = await client.post("/api/v1/tasks", json={
        "repo_id": "o/full", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    task_id = create.json()["id"]
    await client.post("/api/v1/executors/join", json={
        "id": "ex-full", "host_id": "h"
    }, headers=auth)
    # Poll twice → 2 subtasks
    sub_ids = []
    for _ in range(2):
        r = await client.post("/api/v1/executors/ex-full/poll", headers=auth)
        sub_ids.append(r.json()["subtask"]["id"])
    # Report success for both
    for sid in sub_ids:
        await client.post(f"/api/v1/subtasks/{sid}/report", json={
            "status": "succeeded", "actual_sha256": "b" * 64, "bytes_downloaded": 100,
        }, headers=auth)
    # Now task should be succeeded
    r = await client.get(f"/api/v1/tasks/{task_id}", headers=auth)
    assert r.json()["status"] == "succeeded"
    assert r.json()["completed_at"] is not None


@pytest.mark.slow
async def test_report_one_failure_marks_task_failed(client, auth) -> None:
    create = await client.post("/api/v1/tasks", json={
        "repo_id": "o/fail", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    task_id = create.json()["id"]
    await client.post("/api/v1/executors/join", json={
        "id": "ex-fail", "host_id": "h"
    }, headers=auth)
    r = await client.post("/api/v1/executors/ex-fail/poll", headers=auth)
    sub_id = r.json()["subtask"]["id"]
    await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "failed", "error": "disk full",
    }, headers=auth)
    r = await client.get(f"/api/v1/tasks/{task_id}", headers=auth)
    assert r.json()["status"] == "failed"
    assert "disk full" in r.json()["error_message"]


@pytest.mark.slow
async def test_report_unknown_subtask_returns_404(client, auth) -> None:
    import uuid
    r = await client.post(f"/api/v1/subtasks/{uuid.uuid4()}/report", json={
        "status": "succeeded",
    }, headers=auth)
    assert r.status_code == 404


@pytest.mark.slow
async def test_report_unauthenticated_returns_401(client) -> None:
    import uuid
    r = await client.post(f"/api/v1/subtasks/{uuid.uuid4()}/report", json={
        "status": "succeeded",
    })
    assert r.status_code == 401


@pytest.mark.slow
async def test_double_report_returns_409(client, auth) -> None:
    """W2-G: idempotency / illegal-transition guard.

    First report succeeds; second report on the same subtask must be
    rejected with 409 (subtask already terminal — invariant 14)."""
    sub_id = await _setup_assigned_subtask(client, auth, "o/dup")
    r1 = await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "succeeded", "actual_sha256": "c" * 64, "bytes_downloaded": 100,
    }, headers=auth)
    assert r1.status_code == 200
    r2 = await client.post(f"/api/v1/subtasks/{sub_id}/report", json={
        "status": "succeeded", "actual_sha256": "c" * 64, "bytes_downloaded": 100,
    }, headers=auth)
    assert r2.status_code == 409, r2.text
```

- [ ] **Step 3: Verify red**

```bash
uv run pytest tests/api/test_subtasks.py -v
```

Expected: 404s on /api/v1/subtasks routes (no module yet).

- [ ] **Step 4: Implement `src/dlw/api/subtasks.py`**

```python
"""Subtasks API: POST /report (executor reports outcome)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.auth.bearer import require_bearer
from dlw.schemas.subtask import SubTaskReport
from dlw.services.scheduler import complete_subtask

router = APIRouter(prefix="/api/v1/subtasks", tags=["subtasks"])


@router.post("/{subtask_id}/report", dependencies=[Depends(require_bearer)])
async def post_report(
    subtask_id: uuid.UUID,
    body: SubTaskReport,
    session: AsyncSession = Depends(_session),
) -> dict[str, str]:
    try:
        sub, parent = await complete_subtask(
            session, subtask_id,
            final_status=body.status,
            actual_sha256=body.actual_sha256,
            bytes_downloaded=body.bytes_downloaded,
            error=body.error,
            assignment_token=body.assignment_token,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await session.commit()
    return {"subtask_status": sub.status, "task_status": parent.status}
```

- [ ] **Step 5: Register router in `src/dlw/main.py`**

```python
    from dlw.api.subtasks import router as subtasks_router
    app.include_router(subtasks_router)
```

- [ ] **Step 6: Verify green**

```bash
uv run pytest tests/api/test_subtasks.py -v
```

Expected: 5 tests PASS.

Then full regression:
```bash
uv run pytest tests/ 2>&1 | tail -3
```

Expected: ~50 tests pass total (18 Phase 1 + 4 auth + 2 task_service + 6 tasks + 4 exec_service + 5 scheduler + 5 executors + 6 subtasks).

- [ ] **Step 7: Commit**

```bash
git add src/dlw/api/subtasks.py src/dlw/services/scheduler.py src/dlw/main.py tests/api/test_subtasks.py
git commit -m "$(cat <<'EOF'
feat(api): POST /subtasks/{id}/report — executor outcome reporting

complete_subtask helper checks sibling status:
- any 'failed' → parent task status = 'failed'
- all 'succeeded' → parent task status = 'succeeded'
- otherwise → parent unchanged

Closes the happy-path loop: POST task → split → poll → report → task done.

Plan: docs/superpowers/plans/2026-05-08-phase-1-week-2-controller-core.md Task 8
EOF
)"
```

---

## Task 9: Add scheduler hot-path indexes (alembic migration)

**Files:**
- Create: `src/dlw/alembic/versions/<auto>_add_scheduler_indexes.py`

The scheduler does `WHERE status='pending' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED` on every poll. Without an index, this is a sequential scan that gets worse as the table grows.

- [ ] **Step 1: Generate migration**

```bash
DLW_DB_NAME=dlw uv run alembic revision -m "add scheduler hot-path indexes"
```

Note: NOT `--autogenerate` here because we're adding indexes that aren't in the model definitions. Hand-write the upgrade/downgrade.

- [ ] **Step 2: Open the generated file and replace `upgrade()` / `downgrade()`**

```python
def upgrade() -> None:
    # Hot path: scheduler.claim_one_subtask
    # SELECT FROM file_subtasks WHERE status='pending' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED
    op.create_index(
        "ix_file_subtasks_pending_created",
        "file_subtasks",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # Hot path: get_task list endpoint
    # SELECT FROM download_tasks WHERE tenant_id = ? ORDER BY created_at DESC
    # W2-J: use postgresql_ops to express DESC ordering — sa.text() in column
    # list is undocumented and may break across alembic versions.
    op.create_index(
        "ix_download_tasks_tenant_created",
        "download_tasks",
        ["tenant_id", "created_at"],
        postgresql_ops={"created_at": "DESC NULLS LAST"},
    )


def downgrade() -> None:
    op.drop_index("ix_download_tasks_tenant_created", table_name="download_tasks")
    op.drop_index("ix_file_subtasks_pending_created", table_name="file_subtasks")
```

(Make sure `import sqlalchemy as sa` is present at the top — alembic puts it there by default.)

- [ ] **Step 3: Apply the migration**

```bash
DLW_DB_NAME=dlw uv run alembic upgrade head
```

Expected: ends with `Running upgrade ... -> <hash>, add scheduler hot-path indexes`.

- [ ] **Step 4: Verify indexes exist**

```bash
"/c/Program Files/PostgreSQL/18/bin/psql.exe" -h localhost -p 5433 -U postgres -d dlw -c "\di"
```

Expected output includes both new indexes:
```
public | ix_download_tasks_tenant_created | index | postgres | download_tasks
public | ix_file_subtasks_pending_created | index | postgres | file_subtasks
```

- [ ] **Step 5: Re-run alembic round-trip test**

```bash
uv run pytest tests/db/test_alembic.py -v
```

Expected: still 2 PASS (round-trip works with the new migration).

- [ ] **Step 6: Commit**

```bash
git add src/dlw/alembic/versions/
git commit -m "$(cat <<'EOF'
feat(alembic): add scheduler hot-path partial + composite indexes

- ix_file_subtasks_pending_created (partial WHERE status='pending') —
  scheduler's FOR UPDATE SKIP LOCKED uses this; partial index keeps it
  small as completed/failed rows accumulate
- ix_download_tasks_tenant_created (tenant_id, created_at DESC) —
  GET /api/v1/tasks list uses this; composite supports the WHERE+ORDER BY

Plan: docs/superpowers/plans/2026-05-08-phase-1-week-2-controller-core.md Task 9
EOF
)"
```

---

## Task 10: End-to-end happy-path black-box test

**Files:**
- Create: `tests/e2e/__init__.py`
- Create: `tests/e2e/test_happy_path.py`

This test imports nothing internal — it drives the controller via HTTP only, mimicking how a real executor will interact.

- [ ] **Step 1: Write `tests/e2e/__init__.py`** (empty)

- [ ] **Step 2: Write `tests/e2e/test_happy_path.py`**

```python
"""E2E happy path: a mock executor completes a full task via HTTP only.

This test does NOT import dlw services or models. It drives the controller
exclusively through its public HTTP API, exactly like a real executor would.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base


_TOKEN = "e2e-token"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """Create tables + minimal seed (tenant + project + user + storage)."""
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="d", display_name="D"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="d"))
        s.add(User(id=1, tenant_id=1, oidc_subject="d",
                   email="d@l", role="tenant_admin"))
        s.add(StorageBackend(id=1, tenant_id=1, name="d",
                              backend_type="s3", config_encrypted=b""))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DLW_BEARER_TOKEN", _TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.slow
async def test_full_task_lifecycle_via_http() -> None:
    from dlw.main import create_app
    app = create_app()
    auth = {"Authorization": f"Bearer {_TOKEN}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # 1. Create task
        r = await c.post("/api/v1/tasks", json={
            "repo_id": "deepseek-ai/DeepSeek-V3",
            "revision": "abc123def4567890" * 2 + "abc12345",
            "storage_id": 1,
            "priority": 3,
        }, headers=auth)
        assert r.status_code == 201, r.text
        task_id = r.json()["id"]
        assert r.json()["status"] == "pending"

        # 2. Register a worker executor
        r = await c.post("/api/v1/executors/join", json={
            "id": "e2e-worker-1", "host_id": "e2e-host",
            "capabilities": {"nic_speed_gbps": 25},
        }, headers=auth)
        assert r.status_code == 201, r.text

        # 3. Heartbeat (executor reports liveness)
        r = await c.post("/api/v1/executors/e2e-worker-1/heartbeat",
                         json={"health_score": 100}, headers=auth)
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

        # 4. Poll twice (2 mock subtasks per task)
        sub_ids: list[str] = []
        for _ in range(2):
            r = await c.post("/api/v1/executors/e2e-worker-1/poll", headers=auth)
            assert r.status_code == 200
            assert r.json()["assigned"] is True
            sub_ids.append(r.json()["subtask"]["id"])
        assert len(set(sub_ids)) == 2  # got 2 different subtasks

        # 5. Third poll → no work
        r = await c.post("/api/v1/executors/e2e-worker-1/poll", headers=auth)
        assert r.json()["assigned"] is False

        # 6. Report success for both subtasks
        for sid in sub_ids:
            r = await c.post(f"/api/v1/subtasks/{sid}/report", json={
                "status": "succeeded",
                "actual_sha256": "f" * 64,
                "bytes_downloaded": 100_000_000,
            }, headers=auth)
            assert r.status_code == 200, r.text

        # 7. Task should now be succeeded
        r = await c.get(f"/api/v1/tasks/{task_id}", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "succeeded", body
        assert body["completed_at"] is not None
        assert body["error_message"] is None
```

- [ ] **Step 3: Run the E2E test**

```bash
uv run pytest tests/e2e/ -v
```

Expected: 1 test PASS. Will be slower than unit tests (~2-5s due to HTTP round-trips through ASGI).

- [ ] **Step 4: Final full-suite verify**

```bash
uv run pytest tests/ 2>&1 | tail -5
```

Expected: ~51 tests pass (Phase 1: 18 + Week 2 unit: ~32 + Week 2 E2E: 1).

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/
git commit -m "$(cat <<'EOF'
test(e2e): full task lifecycle via HTTP only — happy path

POST task → join executor → heartbeat → poll x2 → report x2 → task = succeeded.

This test deliberately imports nothing internal beyond the FastAPI app
factory and Bearer settings. It exercises exactly the same surface area
that a real executor process will use in Week 3.

Plan: docs/superpowers/plans/2026-05-08-phase-1-week-2-controller-core.md Task 10
EOF
)"
```

---

## Task 11: Update README + open PR

**Files:**
- Modify: `README.md` (Quickstart section — add Week 2 demo curl block)

- [ ] **Step 1: Append a Week 2 demo block to the Quickstart section**

In `README.md`, find the line `# 9. 跑 tests` block in the Quickstart section, and insert AFTER the `uv run pytest -v` line but BEFORE the closing `\`\`\`` and the "完整开发计划" link:

```bash
# === Week 2 demo: drive a mock executor via HTTP ===

# Set a bearer token (for dev only)
export DLW_BEARER_TOKEN="dev-secret"
TOKEN_HEADER="Authorization: Bearer dev-secret"

# Create a task
TASK_ID=$(curl -s -X POST http://localhost:8000/api/v1/tasks \
  -H "$TOKEN_HEADER" -H "Content-Type: application/json" \
  -d '{"repo_id":"deepseek-ai/DeepSeek-V3","revision":"0123456789abcdef0123456789abcdef01234567","storage_id":1}' \
  | python -c "import sys, json; print(json.load(sys.stdin)['id'])")
echo "Created task: $TASK_ID"

# Register a worker
curl -s -X POST http://localhost:8000/api/v1/executors/join \
  -H "$TOKEN_HEADER" -H "Content-Type: application/json" \
  -d '{"id":"demo-worker","host_id":"demo-host"}'

# Poll twice + report success
for i in 1 2; do
  SUB_ID=$(curl -s -X POST http://localhost:8000/api/v1/executors/demo-worker/poll \
    -H "$TOKEN_HEADER" \
    | python -c "import sys, json; print(json.load(sys.stdin)['subtask']['id'])")
  curl -s -X POST "http://localhost:8000/api/v1/subtasks/$SUB_ID/report" \
    -H "$TOKEN_HEADER" -H "Content-Type: application/json" \
    -d '{"status":"succeeded","actual_sha256":"'$(printf 'f%.0s' {1..64})'","bytes_downloaded":100000000}'
done

# Verify task completed
curl -s "http://localhost:8000/api/v1/tasks/$TASK_ID" -H "$TOKEN_HEADER"
# Expected: {"status":"succeeded", ...}
```

Also add this NOTE near the top of the Quickstart section (before the `bash` code block):

> **Week 2 update:** Controller now supports task CRUD + executor poll/report loop with Bearer auth. See the demo block at the bottom for a full HTTP walkthrough.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs(readme): add Week 2 curl demo — full task lifecycle via HTTP

8-line bash walkthrough that mirrors tests/e2e/test_happy_path.py:
create task → register worker → poll → report → verify succeeded.

Plan: docs/superpowers/plans/2026-05-08-phase-1-week-2-controller-core.md Task 11
EOF
)"
```

- [ ] **Step 3: Push branch + open PR**

```bash
git push -u origin feat/phase-1-week-2-controller-core
```

Then:

```bash
gh pr create --title "feat(week-2): controller core — task CRUD + scheduler + happy path" --body "$(cat <<'EOF'
## Summary

Phase 1 Week 2 — controller core. Adds the minimum API surface for a mock
executor to complete a task end-to-end via HTTP.

- POST `/api/v1/tasks` (auto-creates 2 mock subtasks)
- POST `/api/v1/executors/join` + `/heartbeat` + `/poll`
- POST `/api/v1/subtasks/{id}/report`
- Bearer token auth (single shared secret; OIDC in Phase 3)
- FOR UPDATE SKIP LOCKED scheduler claim (concurrent-safe)
- Hot-path indexes on file_subtasks(created_at WHERE status='pending') + download_tasks(tenant_id, created_at DESC)
- 1 E2E test drives the full lifecycle via HTTP only

Closes the foundation work; Week 3 adds the executor process + UI.

## Test plan

- [x] All Week 2 unit tests pass locally
- [x] E2E test drives full lifecycle via HTTP
- [ ] CI green on this PR
- [x] `alembic upgrade head` succeeds; new partial index visible in `\di`

## Out of scope (future)

- Real HuggingFace Hub file resolution → Week 4
- mTLS executor auth + executor_epoch fence-token logic → Phase 2
- WebSocket progress fan-out → Week 3 (with UI)
- OIDC PKCE / multi-tenancy → Phase 3
- Streaming SHA256 + S3 multipart upload → Week 4

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Wait for CI green**

```bash
until gh pr checks --json bucket --jq 'all(.[]; .bucket != "pending")' | grep -q true; do sleep 30; done
gh pr checks
```

Expected: all 9 jobs (10 with the aggregate) pass. If pytest fails, look at the specific test and fix iteratively (this is normal for first push).

---

## Acceptance criteria — done when ALL hold

- [ ] All 11 task commits exist on `feat/phase-1-week-2-controller-core` branch
- [ ] `uv run pytest tests/ -v` shows ~51 PASS, 0 FAIL
- [ ] `psql ... -d dlw -c "\di"` shows both new indexes
- [ ] `alembic upgrade head` is idempotent (re-running does nothing)
- [ ] `alembic downgrade -1` rolls back to Phase 1 head cleanly
- [ ] PR opened with full body + CI green on all 9 jobs
- [ ] The README curl demo executes successfully against a running controller (manual verify)

---

## What's next — Week 3 plan picks up

Week 3 (separate plan, to be written after this merges) will:

- Build the executor process (`src/dlw_executor/`) that polls, "downloads" (mocked file generation initially), reports
- Add WebSocket `/ws/tasks/{id}/progress` for real-time UI updates
- Vue 3 + Pinia + Element Plus UI scaffold (login, task list, task detail with live progress)
- Glue the three pieces into one `docker-compose dev up` experience

---

## Plan self-review (DRY / placeholders / type consistency)

**Spec coverage:**
- ✅ docs/v2.0/01-architecture.md §3-4 — task CRUD + scheduler invariants 6/7 (simplified)
- ✅ docs/v2.0/02-protocol.md — heartbeat + poll endpoints landed (simplified bodies)
- ✅ docs/v2.0/04-security-and-tenancy.md — Bearer token (single tenant) prep for OIDC
- ✅ docs/v2.0/08-mvp-roadmap.md §1.6 Week 2 — covered fully

**Placeholder scan:**
- No "TODO" / "TBD" / "implement later"
- All steps contain runnable code or exact commands
- All later tasks reference identifiers actually defined in earlier tasks

**Type consistency:**
- `tenant_id`, `project_id`, `owner_user_id` are int (BigInteger) everywhere
- `executor.id` is str everywhere; `task.id` and `subtask.id` are uuid.UUID everywhere
- `status` columns are strings with documented allowed values: pending|assigned|succeeded|failed (subtasks); pending|succeeded|failed (tasks); joining|healthy (executors)
- `assignment_token` is uuid.UUID everywhere

**Frequent commits:**
- 11 tasks → 11 commits (TDD red→green→commit per testable task)

**TDD adherence:**
- Tasks 1, 3, 5, 6, 7, 8: write failing test → run → fail → implement → run → pass → commit
- Tasks 2 (schemas), 4 (also has test but tests cover the API not just schemas), 9 (alembic), 10 (E2E only — no impl), 11 (docs/PR) are non-TDD scaffold/integration

**YAGNI:**
- No real HF API call (Week 4); no WebSocket (Week 3); no mTLS / fence (Phase 2); no OIDC (Phase 3)
- Single tenant_id=1 hardcoded; multi-tenant in Phase 3
- No pagination on GET /tasks (Week 5)
- No retry logic on subtask failure (Phase 2)
- `_session` dependency creates engine per request (acceptable for dev; Phase 2 will use app.state lifespan)
