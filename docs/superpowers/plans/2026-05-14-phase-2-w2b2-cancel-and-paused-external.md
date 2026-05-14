# Phase 2 Week 2b2 — Task Cancel + paused_external Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land roadmap §2.6 Day 4 — `POST /api/v1/tasks/{task_id}/cancel` + `cancelling` task state + cancel-aware `complete_subtask`, plus the individual-subtask half of D13 (`paused_external` + 5-min sweep recovery). Global throttle state machine (full 03 §8) deferred to Phase 3.

**Architecture:** Lazy cancel propagation — scheduler refuses new claims for cancelling tasks; in-flight subtasks finish naturally; the cancel-aware tail in `complete_subtask` transitions task to `cancelled` once siblings terminal. Both paused branches in `complete_subtask` become cancel-aware: a paused report arriving after `/cancel` force-terminates to `cancelled`, avoiding dead-lock with the sweepers. New `sweep_paused_external` runs alongside W2a/W2b1 sweepers on the existing 30-s loop, filtering by `last_paused_at` quiet window.

**Tech Stack:** FastAPI + SQLAlchemy 2.x async + alembic + pytest + stdlib `httpx.HTTPStatusError` classification. No new runtime deps; no new dev deps; no new CI jobs.

**Scope:** 10 tasks across 4 milestones. Branch `feat/phase-2-w2b2-cancel-and-paused-external` exists with the spec committed (`d9239b3`). Companion spec: `docs/superpowers/specs/2026-05-14-phase-2-w2b2-cancel-and-paused-external-design.md`.

**Pre-flight:** Phase 2 W2b1 merged into `main` at `6037e6b`. Local PG 18 running on `localhost:5433`. `uv` 0.11.9. Existing pytest baseline = 166 passed, 1 deselected.

**Out-of-scope (deferred — see spec §1.2):** Heartbeat-carried cancel signal; `source_throttle_state` table + global state machine; speed_limit downlink; per-source throttle; `verified` state rename; hard-cancel API; multi-user RBAC on /cancel.

---

## File Structure

After this plan:

```
modelpull/
├── src/dlw/
│   ├── alembic/versions/
│   │   └── <rev>_p2w2b2_last_paused_at.py        NEW
│   ├── db/models/task.py                          MODIFY (+last_paused_at column on FileSubTask)
│   ├── services/
│   │   ├── task_service.py                        MODIFY (+cancel_task)
│   │   ├── scheduler.py                           MODIFY (claim_one_subtask parent-active EXISTS; complete_subtask 3 new branches)
│   │   └── recovery.py                            MODIFY (+sweep_paused_external + module constant)
│   ├── schemas/subtask.py                         MODIFY (SubTaskReport.status Literal widens)
│   ├── api/tasks.py                               MODIFY (+POST /tasks/{id}/cancel)
│   ├── executor/runner.py                         MODIFY (+httpx.HTTPStatusError(429|503) → paused_external)
│   └── main.py                                    MODIFY (_sweep_loop_main calls sweep_paused_external)
├── tests/
│   ├── services/
│   │   ├── test_task_cancel.py                    NEW (4 cases)
│   │   ├── test_scheduler_skip_cancelling.py      NEW (1 case)
│   │   ├── test_complete_subtask_cancel_aware.py  NEW (3 cases — succeeded-cancelling, paused_external short, paused_external force-terminate)
│   │   └── test_sweep_paused_external.py          NEW (2 cases)
│   ├── api/test_cancel_endpoint.py                NEW (3 cases)
│   └── executor/test_runner_external_throttle.py  NEW (2 cases)
├── tools/lint_invariants.py                       MODIFY (+check_task_status_domain, +VALID_SUBTASK_STATUS paused_external)
├── api/openapi.yaml                               MODIFY (+cancel operation, +enum widenings)
└── docs/operator/executor-runbook.md              MODIFY (+cancel-latency note)
```

**Why this structure:** new tests cluster by service module (one new test file per concern). All production touches stay additive — no file balloons past 300 LOC.

---

## Pre-flight checks

- [ ] On branch `feat/phase-2-w2b2-cancel-and-paused-external`, spec committed (`git log --oneline -1` shows `d9239b3` or descendant).
- [ ] `main` at `6037e6b` (PR #10 merge): `git log main --oneline -1`.
- [ ] PG running on `localhost:5433` (`pg_isready -h localhost -p 5433`).
- [ ] `dlw` database at alembic head `5cfd4bb519f6` (W2a state machine — W2b1 added no migrations): `uv run alembic current`.
- [ ] Existing pytest suite green: `uv run pytest -x` → 166 passed, 1 deselected.

---

## Milestone 1 — Schema + cancel_task service + API endpoint

After M1, the migration adds `file_subtasks.last_paused_at`, `cancel_task()` exists with 4 unit tests, and `POST /api/v1/tasks/{task_id}/cancel` works end-to-end with 3 API tests covering 202/404/409.

---

### Task 1: Alembic migration + ORM attribute

**Files:**
- Create: `src/dlw/alembic/versions/<rev>_p2w2b2_last_paused_at.py`
- Modify: `src/dlw/db/models/task.py`

- [ ] **Step 1: Generate the revision**

```
uv run alembic revision -m "p2w2b2 last_paused_at"
```

Note the 12-char hex revision id printed. Open the new file.

- [ ] **Step 2: Verify `down_revision`**

Confirm:

```python
revision: str = '<new id>'
down_revision: Union[str, None] = '5cfd4bb519f6'
```

If `down_revision` is anything else, fix it before continuing.

- [ ] **Step 3: Implement `upgrade()` and `downgrade()`**

Replace the empty body:

```python
"""p2w2b2 last_paused_at

Revision ID: <new id>
Revises: 5cfd4bb519f6
Create Date: <auto>
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '<new id>'
down_revision: Union[str, None] = '5cfd4bb519f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "file_subtasks",
        sa.Column("last_paused_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("file_subtasks", "last_paused_at")
```

Replace `<new id>` with the actual revision id.

- [ ] **Step 4: Add ORM attribute to `src/dlw/db/models/task.py`**

Read the file. Find `class FileSubTask(Base):`. Within the class, find the existing W2b1 timestamps cluster — specifically the `multipart_started_at` / `assigned_at` / `last_heartbeat_seen_at` group. Insert immediately after `last_heartbeat_seen_at` (or wherever the W2b1 W1 timestamp cluster ends):

```python
    # W2b2 §3.8: written when sub flips to paused_external or paused_disk_full.
    # Used by sweep_paused_external's quiet-window threshold.
    last_paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

`datetime`, `Mapped`, `mapped_column`, `DateTime` are all already imported by the W1 model.

- [ ] **Step 5: Apply migration**

```
uv run alembic upgrade head
```

Expected: last line includes `Running upgrade 5cfd4bb519f6 -> <new id>, p2w2b2 last_paused_at`.

- [ ] **Step 6: Verify column**

```
psql -h localhost -p 5433 -U postgres -d dlw -c "\d file_subtasks" 2>&1 | grep last_paused_at
```

Expected: prints a line containing `last_paused_at | timestamp with time zone`.

- [ ] **Step 7: Verify downgrade reverses cleanly**

```
uv run alembic downgrade -1
psql -h localhost -p 5433 -U postgres -d dlw -c "\d file_subtasks" 2>&1 | grep last_paused_at
```

Expected: second command returns nothing (column gone).

Re-apply:

```
uv run alembic upgrade head
```

- [ ] **Step 8: Update `tests/db/test_alembic.py` `EXPECTED_TABLES`/`EXPECTED_COLUMNS` if relevant**

Read `tests/db/test_alembic.py`. If it has an `EXPECTED_TABLES` set (W2a-style) or a per-table column expectation that lists `file_subtasks` columns, add `"last_paused_at"` to that list. Otherwise skip.

- [ ] **Step 9: Run full pytest**

```
uv run pytest -x
```

Expected: 166 passed, 1 deselected (no new tests yet; just confirming the schema change didn't break anything).

- [ ] **Step 10: Commit**

```bash
git add src/dlw/alembic/versions/<rev>_p2w2b2_last_paused_at.py src/dlw/db/models/task.py tests/db/test_alembic.py
git commit -m "feat(db): p2w2b2 alembic — file_subtasks.last_paused_at (W2b2 M1)"
```

---

### Task 2: `cancel_task` service + 4 unit tests

**Files:**
- Modify: `src/dlw/services/task_service.py` (+cancel_task)
- Create: `tests/services/test_task_cancel.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_task_cancel.py`:

```python
"""Tests for cancel_task (Phase 2 W2b2 §3.1)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.services.task_service import cancel_task


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def env(db_session: AsyncSession):
    """Seed minimum FK rows."""
    db_session.add(Tenant(id=1, slug="d", display_name="D"))
    await db_session.flush()
    db_session.add(Project(id=1, tenant_id=1, name="d"))
    db_session.add(User(id=1, tenant_id=1, oidc_subject="d",
                        email="d@l", role="tenant_admin"))
    db_session.add(StorageBackend(id=1, tenant_id=1, name="d",
                                  backend_type="s3", config_encrypted=b""))
    await db_session.flush()


async def _seed_task(session: AsyncSession, status: str = "pending") -> DownloadTask:
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status=status,
    )
    session.add(task)
    await session.flush()
    return task


@pytest.mark.slow
async def test_cancel_task_flips_status_and_cancelled_at(
    db_session: AsyncSession, env,
) -> None:
    task = await _seed_task(db_session, status="pending")

    cancelled = await cancel_task(db_session, task.id)
    await db_session.flush()

    assert cancelled.status == "cancelling"
    assert cancelled.cancelled_at is not None


@pytest.mark.slow
async def test_cancel_task_force_terminates_paused_subtasks(
    db_session: AsyncSession, env,
) -> None:
    task = await _seed_task(db_session, status="downloading")
    db_session.add(FileSubTask(
        task_id=task.id, tenant_id=1, filename="a.bin",
        file_size=100, status="paused_disk_full",
    ))
    db_session.add(FileSubTask(
        task_id=task.id, tenant_id=1, filename="b.bin",
        file_size=200, status="paused_external",
    ))
    db_session.add(FileSubTask(
        task_id=task.id, tenant_id=1, filename="c.bin",
        file_size=300, status="assigned",
    ))
    await db_session.flush()

    await cancel_task(db_session, task.id)
    await db_session.flush()

    from sqlalchemy import select
    rows = (await db_session.execute(
        select(FileSubTask).where(FileSubTask.task_id == task.id)
        .order_by(FileSubTask.filename)
    )).scalars().all()
    statuses = {r.filename: r.status for r in rows}
    assert statuses == {
        "a.bin": "cancelled",   # was paused_disk_full
        "b.bin": "cancelled",   # was paused_external
        "c.bin": "assigned",    # in-flight; untouched
    }


@pytest.mark.slow
async def test_cancel_task_idempotent_when_already_cancelling(
    db_session: AsyncSession, env,
) -> None:
    task = await _seed_task(db_session, status="pending")
    await cancel_task(db_session, task.id)
    # Second call is idempotent (no error, returns same row).
    again = await cancel_task(db_session, task.id)
    assert again.status == "cancelling"


@pytest.mark.slow
async def test_cancel_task_raises_on_terminal_state(
    db_session: AsyncSession, env,
) -> None:
    for terminal in ("succeeded", "failed", "cancelled"):
        task = await _seed_task(db_session, status=terminal)
        with pytest.raises(ValueError):
            await cancel_task(db_session, task.id)
```

- [ ] **Step 2: Run — verify ImportError**

```
uv run pytest tests/services/test_task_cancel.py -v
```

Expected: 4 collection-time errors, `ImportError: cannot import name 'cancel_task' from 'dlw.services.task_service'`.

- [ ] **Step 3: Implement `cancel_task` in `src/dlw/services/task_service.py`**

Read the file first to see existing imports. Append at the END of the file (after `create_task` / `EmptyRepo`):

```python
async def cancel_task(session: AsyncSession, task_id: uuid.UUID) -> DownloadTask:
    """W2b2 §3.1: idempotently flip task to 'cancelling'.

    Three-step transaction:
      1. Lock task row FOR UPDATE; raise on missing or terminal state.
      2. Set status='cancelling', cancelled_at=now().
      3. Force-terminate any paused_* subtasks under this task to 'cancelled'
         (avoids dead-lock: sweepers can't recover paused subs under a
         cancelling task; complete_subtask's sibling-terminal check would
         otherwise never fire).

    Returns the locked-and-updated task. Caller commits.

    Raises:
      LookupError: task not found
      ValueError: task already in terminal state (succeeded/failed/cancelled)
    """
    from datetime import UTC, datetime
    from sqlalchemy import update

    task = await session.get(DownloadTask, task_id, with_for_update=True)
    if task is None:
        raise LookupError(f"task {task_id} not found")
    if task.status in ("succeeded", "failed", "cancelled"):
        raise ValueError(
            f"task {task_id} already in terminal state '{task.status}'"
        )
    if task.status == "cancelling":
        return task   # idempotent

    task.status = "cancelling"
    task.cancelled_at = datetime.now(UTC)

    await session.execute(
        update(FileSubTask)
        .where(FileSubTask.task_id == task_id)
        .where(FileSubTask.status.in_(("paused_disk_full", "paused_external")))
        .values(status="cancelled")
    )
    return task
```

Top-of-file imports: confirm `from dlw.db.models.task import DownloadTask, FileSubTask` is present. If only `DownloadTask` is imported, add `FileSubTask` to the import. `uuid` should already be imported by the existing `create_task`. The local `from datetime import UTC, datetime` and `from sqlalchemy import update` keep the function self-contained without polluting the module-level imports if they aren't already there — but if you see they're already present, you can use them directly.

- [ ] **Step 4: Run tests — verify all 4 pass**

```
uv run pytest tests/services/test_task_cancel.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Run full suite**

```
uv run pytest -x
```

Expected: 170 passed (166 + 4 new), 1 deselected.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/services/task_service.py tests/services/test_task_cancel.py
git commit -m "feat(task): cancel_task service + 4 unit tests (W2b2 M1)"
```

---

### Task 3: `POST /api/v1/tasks/{task_id}/cancel` endpoint + 3 API tests

**Files:**
- Modify: `src/dlw/api/tasks.py` (+cancel endpoint)
- Create: `tests/api/test_cancel_endpoint.py`

- [ ] **Step 1: Write the failing API tests**

Create `tests/api/test_cancel_endpoint.py`:

```python
"""Tests for POST /api/v1/tasks/{task_id}/cancel (Phase 2 W2b2 §3.2)."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from dlw.main import create_app


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


@pytest.fixture(scope="module")
def auth_headers():
    # Same bearer token as existing W1 task tests.
    import os
    token = os.environ.get("DLW_BEARER_TOKEN", "test-token")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.slow
def test_post_cancel_returns_202_and_cancelling(
    client: TestClient, auth_headers: dict[str, str],
) -> None:
    """Create a task, cancel it, expect 202 + status=cancelling."""
    create_resp = client.post(
        "/api/v1/tasks",
        json={
            "repo_id": "owner/repo",
            "revision": "b" * 40,
            "storage_id": 1,
            "path_template": "t",
            "priority": 1,
        },
        headers=auth_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    task_id = create_resp.json()["id"]

    resp = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=auth_headers)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "cancelling"


@pytest.mark.slow
def test_post_cancel_returns_404_on_missing_task(
    client: TestClient, auth_headers: dict[str, str],
) -> None:
    random_id = uuid.uuid4()
    resp = client.post(f"/api/v1/tasks/{random_id}/cancel", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.slow
def test_post_cancel_returns_409_on_terminal_task(
    client: TestClient, auth_headers: dict[str, str], db_session,
) -> None:
    """Create a task, force-mark it succeeded via DB, then cancel → 409."""
    from dlw.db.models.task import DownloadTask
    from datetime import UTC, datetime
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="succeeded",
        completed_at=datetime.now(UTC),
    )
    db_session.add(task)
    await_flush_via_sync = db_session.flush  # asyncio fixture; sync wrapper not available — use async path
    # Use the same session that the app sees:
    import asyncio
    asyncio.get_event_loop().run_until_complete(db_session.flush())
    asyncio.get_event_loop().run_until_complete(db_session.commit())

    resp = client.post(f"/api/v1/tasks/{task.id}/cancel", headers=auth_headers)
    assert resp.status_code == 409
```

Note: the test uses `TestClient` which is synchronous, but `db_session` is an async fixture. The third test mixes them awkwardly. If `tests/api/test_executors.py` has a cleaner pattern (e.g. an HTTP fixture that ALSO mutates DB via a sync helper), use that. Inspect:

```
grep -n "TestClient\|fixture" tests/api/test_executors.py | head -10
```

Adopt the same pattern. If the existing API tests use `httpx.AsyncClient` with `lifespan` mode, mirror that — generally the existing pattern is canonical.

- [ ] **Step 2: Inspect existing API test pattern + adjust the new test file**

Open `tests/api/test_executors.py` and `tests/api/test_subtasks.py`. Identify whether they use synchronous `TestClient` or async `httpx.AsyncClient(app=app)`. Whichever pattern they use, replicate it exactly in `test_cancel_endpoint.py`. Do NOT introduce a new pattern.

If the existing pattern uses async HTTP with `db_session`, the test 3 (terminal task) becomes natural — both async, single event loop. Update the test accordingly.

- [ ] **Step 3: Run — verify failure (endpoint doesn't exist yet)**

```
uv run pytest tests/api/test_cancel_endpoint.py -v
```

Expected: 404s instead of expected codes (FastAPI returns 404 for unrouted POST paths). The 3 tests fail with assertion errors on status_code.

- [ ] **Step 4: Implement the endpoint in `src/dlw/api/tasks.py`**

Read the current `tasks.py`. Add a new endpoint after the existing `POST /tasks` and `GET /tasks/{id}` definitions (keep alphabetical-by-path ordering if the file follows it; otherwise append). Imports — if not already imported, add:

```python
from dlw.services.task_service import EmptyRepo, cancel_task, create_task
```

(Replace the existing `from dlw.services.task_service import EmptyRepo, create_task` line with the above. Order alphabetical.)

Then the new endpoint:

```python
@router.post(
    "/{task_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_bearer)],
)
async def post_cancel_task(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(_session),
) -> TaskRead:
    """W2b2 §3.2: cancel a task. Idempotent for cancelling state; 409 on terminal."""
    try:
        task = await cancel_task(session, task_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    await session.commit()
    return TaskRead.model_validate(task)
```

- [ ] **Step 5: Run new tests — verify all 3 pass**

```
uv run pytest tests/api/test_cancel_endpoint.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Run full suite**

```
uv run pytest -x
```

Expected: 173 passed (170 + 3 new), 1 deselected.

- [ ] **Step 7: Commit**

```bash
git add src/dlw/api/tasks.py tests/api/test_cancel_endpoint.py
git commit -m "feat(api): POST /tasks/{id}/cancel + 3 API tests (W2b2 M1)"
```

---

### Milestone 1 verification (self)

- [ ] `alembic current` returns the new revision id.
- [ ] `cancel_task` 4 cases pass; `/cancel` endpoint 3 cases pass.
- [ ] No regressions; full suite at 173.

---

## Milestone 2 — Scheduler skip-cancelling + complete_subtask cancel-aware tail

After M2, `claim_one_subtask` refuses subtasks of cancelling/terminal parents; `complete_subtask` transitions task to `cancelled` when the last sibling becomes terminal under a `cancelling` parent.

---

### Task 4: `claim_one_subtask` parent-active EXISTS clause + 1 test

**Files:**
- Modify: `src/dlw/services/scheduler.py`
- Create: `tests/services/test_scheduler_skip_cancelling.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_scheduler_skip_cancelling.py`:

```python
"""Tests for claim_one_subtask parent-active EXISTS clause (Phase 2 W2b2 §3.3)."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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


@pytest.fixture
async def env(db_session: AsyncSession):
    db_session.add(Tenant(id=1, slug="d", display_name="D"))
    await db_session.flush()
    db_session.add(Project(id=1, tenant_id=1, name="d"))
    db_session.add(User(id=1, tenant_id=1, oidc_subject="d",
                        email="d@l", role="tenant_admin"))
    db_session.add(StorageBackend(id=1, tenant_id=1, name="d",
                                  backend_type="s3", config_encrypted=b""))
    await db_session.flush()


@pytest.mark.slow
async def test_claim_skips_subtask_under_cancelling_parent(
    db_session: AsyncSession, env,
) -> None:
    """Pending subtask whose parent is cancelling → claim returns None."""
    db_session.add(Executor(
        id="ex-1", host_id="host-1", cert_fingerprint="x",
        status="healthy", epoch=1, disk_free_gb=100, parts_dir_bytes=0,
    ))
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="cancelling",
    )
    db_session.add(task)
    await db_session.flush()
    db_session.add(FileSubTask(
        task_id=task.id, tenant_id=1, filename="m.bin",
        file_size=100, status="pending",
    ))
    await db_session.flush()

    sub, token = await claim_one_subtask(db_session, "ex-1", 1)
    assert sub is None and token is None
```

- [ ] **Step 2: Run — verify failure (W2b1 scheduler ignores parent status)**

```
uv run pytest tests/services/test_scheduler_skip_cancelling.py -v
```

Expected: 1 failure — `sub` is not None.

- [ ] **Step 3: Modify `src/dlw/services/scheduler.py`**

Read `claim_one_subtask`. The W2b1 body has:

```python
    sib = aliased(FileSubTask)
    e_other = aliased(Executor)
    same_host_holds = (
        select(sib.id)
        .join(e_other, e_other.id == sib.executor_id)
        .where(sib.task_id == FileSubTask.task_id)
        .where(sib.filename == FileSubTask.filename)
        .where(sib.status == "assigned")
        .where(e_other.host_id == e_self.host_id)
        .where(e_other.id != executor_id)
        .exists()
    )

    GiB = 1024 ** 3
    free_bytes = (e_self.disk_free_gb or 0) * GiB - (e_self.parts_dir_bytes or 0)

    stmt = (
        select(FileSubTask)
        .where(FileSubTask.status == "pending")
        .where(~same_host_holds)
        .order_by(FileSubTask.created_at)
        .limit(_K_CANDIDATES)
        .with_for_update(skip_locked=True)
    )
```

Add a new EXISTS expression between `same_host_holds` and the GiB block:

```python
    # W2b2 §3.3: skip subtasks whose parent task is cancelling/terminal.
    parent_active = (
        select(DownloadTask.id)
        .where(DownloadTask.id == FileSubTask.task_id)
        .where(DownloadTask.status.in_(("pending", "scheduling", "downloading")))
        .exists()
    )
```

Then add the clause to `stmt`:

```python
    stmt = (
        select(FileSubTask)
        .where(FileSubTask.status == "pending")
        .where(~same_host_holds)
        .where(parent_active)              # W2b2 NEW
        .order_by(FileSubTask.created_at)
        .limit(_K_CANDIDATES)
        .with_for_update(skip_locked=True)
    )
```

`DownloadTask` should already be imported at the top of `scheduler.py` (from T5 of W2a). Confirm.

- [ ] **Step 4: Run the new test — verify pass**

```
uv run pytest tests/services/test_scheduler_skip_cancelling.py -v
```

Expected: 1 passed.

- [ ] **Step 5: Run all scheduler tests for regression**

```
uv run pytest tests/services/test_scheduler.py tests/services/test_scheduler_host_affinity.py tests/services/test_scheduler_disk_preflight.py tests/services/test_scheduler_skip_cancelling.py -v
```

Expected: all pass. If a W1/W2a/W2b1 test fails because its parent task is in a state outside `(pending, scheduling, downloading)` — e.g. a test deliberately sets `task.status="succeeded"` and still expects claim to succeed — the test's intent is broken by W2b2 and needs to update its setup (set parent to `pending` or change assertion). Spot-check; the new EXISTS clause is strictly tightening.

- [ ] **Step 6: Run full suite**

```
uv run pytest -x
```

Expected: 174 passed (173 + 1 new), 1 deselected.

- [ ] **Step 7: Commit**

```bash
git add src/dlw/services/scheduler.py tests/services/test_scheduler_skip_cancelling.py
git commit -m "feat(scheduler): claim_one_subtask parent-active EXISTS clause (W2b2 M2)"
```

---

### Task 5: `complete_subtask` cancel-aware tail + 1 test

**Files:**
- Modify: `src/dlw/services/scheduler.py`
- Create: `tests/services/test_complete_subtask_cancel_aware.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_complete_subtask_cancel_aware.py`:

```python
"""Tests for complete_subtask cancel-aware tail + paused branches (Phase 2 W2b2 §3.3)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.services.scheduler import complete_subtask


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def env(db_session: AsyncSession):
    db_session.add(Tenant(id=1, slug="d", display_name="D"))
    await db_session.flush()
    db_session.add(Project(id=1, tenant_id=1, name="d"))
    db_session.add(User(id=1, tenant_id=1, oidc_subject="d",
                        email="d@l", role="tenant_admin"))
    db_session.add(StorageBackend(id=1, tenant_id=1, name="d",
                                  backend_type="s3", config_encrypted=b""))
    await db_session.flush()


@pytest.mark.slow
async def test_succeeded_under_cancelling_keeps_file_and_transitions_task(
    db_session: AsyncSession, env,
) -> None:
    """Task in cancelling + last sub completes succeeded → task transitions
    cancelled (not succeeded); sub stays succeeded (file preserved)."""
    db_session.add(Executor(
        id="ex-1", host_id="host-1", cert_fingerprint="x",
        status="healthy", epoch=1, disk_free_gb=100, parts_dir_bytes=0,
    ))
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="cancelling",
    )
    db_session.add(task)
    await db_session.flush()
    token = uuid.uuid4()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="m.bin",
        file_size=100, status="assigned",
        executor_id="ex-1", executor_epoch=1,
        assignment_token=token,
    )
    db_session.add(sub)
    await db_session.flush()

    updated_sub, updated_task = await complete_subtask(
        db_session, sub.id,
        final_status="succeeded",
        actual_sha256=None,
        bytes_downloaded=100,
        error=None,
        assignment_token=token,
        executor_epoch=1,
    )
    await db_session.flush()

    assert updated_sub.status == "succeeded"
    assert updated_task.status == "cancelled"
    assert updated_task.completed_at is not None
```

- [ ] **Step 2: Run — verify failure**

```
uv run pytest tests/services/test_complete_subtask_cancel_aware.py -v
```

Expected: 1 failure — `updated_task.status` is `"succeeded"` (W1 behavior) instead of `"cancelled"`.

- [ ] **Step 3: Modify `complete_subtask` in `src/dlw/services/scheduler.py`**

Find the W1+W2a tail (the sibling-set check just before `# W2a §3.3: route executor health update`):

```python
    statuses = {s.status for s in siblings}
    if "failed" in statuses:
        parent.status = "failed"
        parent.error_message = f"subtask {sub.filename} failed: {error}"
        parent.completed_at = datetime.now(UTC)
    elif statuses == {"succeeded"}:
        parent.status = "succeeded"
        parent.completed_at = datetime.now(UTC)
```

Replace with:

```python
    statuses = {s.status for s in siblings}
    TERMINAL = {"succeeded", "failed", "cancelled"}

    if parent.status == "cancelling" and statuses <= TERMINAL:
        # W2b2 §3.3: all siblings terminal under cancelling → transition to cancelled.
        parent.status = "cancelled"
        parent.completed_at = datetime.now(UTC)
    elif parent.status != "cancelling":
        # Normal (non-cancelling) path — unchanged from W1+W2a.
        if "failed" in statuses:
            parent.status = "failed"
            parent.error_message = f"subtask {sub.filename} failed: {error}"
            parent.completed_at = datetime.now(UTC)
        elif statuses == {"succeeded"}:
            parent.status = "succeeded"
            parent.completed_at = datetime.now(UTC)
    # else: parent is cancelling but not all siblings terminal — stay cancelling.
```

- [ ] **Step 4: Run new test — verify pass**

```
uv run pytest tests/services/test_complete_subtask_cancel_aware.py -v
```

Expected: 1 passed (the test that was added so far).

- [ ] **Step 5: Run all scheduler tests for regression**

```
uv run pytest tests/services/test_scheduler.py tests/services/test_complete_subtask_cancel_aware.py -v
```

Expected: all pass. The new tail's `elif parent.status != "cancelling":` branch preserves the W1 behavior for all non-cancelling parents.

- [ ] **Step 6: Run full suite**

```
uv run pytest -x
```

Expected: 175 passed (174 + 1 new), 1 deselected.

- [ ] **Step 7: Commit**

```bash
git add src/dlw/services/scheduler.py tests/services/test_complete_subtask_cancel_aware.py
git commit -m "feat(scheduler): complete_subtask cancel-aware sibling-terminal check (W2b2 M2)"
```

---

### Milestone 2 verification (self)

- [ ] Scheduler refuses subtasks under cancelling parents.
- [ ] complete_subtask transitions cancelling → cancelled on sibling-terminal.
- [ ] No W1/W2a/W2b1 regressions.
- [ ] Full suite at 175.

---

## Milestone 3 — paused_external state

After M3: SubTaskReport accepts `paused_external`; complete_subtask handles paused_external (and the now-symmetric paused_disk_full update) including cancel-aware force-terminate; sweep_paused_external runs alongside other sweepers; executor classifies HF 429/503.

---

### Task 6: SubTaskReport widening + complete_subtask paused_external + cancel-aware paused branches + 2 tests

**Files:**
- Modify: `src/dlw/schemas/subtask.py` (Literal widens)
- Modify: `src/dlw/services/scheduler.py` (complete_subtask paused_external branch; paused_disk_full + last_paused_at; both cancel-aware)
- Modify: `tests/services/test_complete_subtask_cancel_aware.py` (+2 cases)

- [ ] **Step 1: Append failing tests**

Open `tests/services/test_complete_subtask_cancel_aware.py` and APPEND (do not replace):

```python


@pytest.mark.slow
async def test_paused_external_short_circuits(
    db_session: AsyncSession, env,
) -> None:
    """complete_subtask(final_status='paused_external') with non-cancelling
    parent → sub becomes paused_external, last_paused_at written, no
    retry_count bump, no task status change."""
    db_session.add(Executor(
        id="ex-pe", host_id="host-pe", cert_fingerprint="x",
        status="healthy", epoch=1, disk_free_gb=100, parts_dir_bytes=0,
    ))
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="downloading",
    )
    db_session.add(task)
    await db_session.flush()
    token = uuid.uuid4()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="m.bin",
        file_size=100, status="assigned",
        executor_id="ex-pe", executor_epoch=1,
        assignment_token=token,
        retry_count=2,
    )
    db_session.add(sub)
    await db_session.flush()

    updated_sub, updated_task = await complete_subtask(
        db_session, sub.id,
        final_status="paused_external",
        actual_sha256=None,
        bytes_downloaded=0,
        error="HTTP 429",
        assignment_token=token,
        executor_epoch=1,
    )
    await db_session.flush()

    assert updated_sub.status == "paused_external"
    assert updated_sub.last_paused_at is not None
    assert updated_sub.executor_id is None
    assert updated_sub.last_error == "HTTP 429"
    assert updated_sub.retry_count == 2          # unchanged
    assert updated_task.status == "downloading"  # unchanged


@pytest.mark.slow
async def test_paused_external_under_cancelling_force_terminates_to_cancelled(
    db_session: AsyncSession, env,
) -> None:
    """paused_external arriving after /cancel → sub becomes cancelled
    (not paused_external); sibling-terminal tail transitions task to cancelled."""
    db_session.add(Executor(
        id="ex-pe2", host_id="host-pe2", cert_fingerprint="x",
        status="healthy", epoch=1, disk_free_gb=100, parts_dir_bytes=0,
    ))
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="cancelling",
    )
    db_session.add(task)
    await db_session.flush()
    token = uuid.uuid4()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="m.bin",
        file_size=100, status="assigned",
        executor_id="ex-pe2", executor_epoch=1,
        assignment_token=token,
    )
    db_session.add(sub)
    await db_session.flush()

    updated_sub, updated_task = await complete_subtask(
        db_session, sub.id,
        final_status="paused_external",
        actual_sha256=None,
        bytes_downloaded=0,
        error="HTTP 503",
        assignment_token=token,
        executor_epoch=1,
    )
    await db_session.flush()

    assert updated_sub.status == "cancelled"     # force-terminated, not paused
    assert updated_sub.last_paused_at is None    # not set on force-terminate path
    assert updated_task.status == "cancelled"
```

- [ ] **Step 2: Run — verify failures**

```
uv run pytest tests/services/test_complete_subtask_cancel_aware.py -v -k "paused_external"
```

Expected: 2 failures (complete_subtask doesn't recognize `final_status="paused_external"`).

- [ ] **Step 3: Widen `SubTaskReport.status` in `src/dlw/schemas/subtask.py`**

Read the file. Find `class SubTaskReport(BaseModel):` and its `status: Literal[...]` line. Change:

```python
class SubTaskReport(BaseModel):
    status: Literal["succeeded", "failed", "paused_disk_full"]
```

to:

```python
class SubTaskReport(BaseModel):
    status: Literal["succeeded", "failed", "paused_disk_full", "paused_external"]
```

- [ ] **Step 4: Modify `complete_subtask` in `src/dlw/services/scheduler.py`**

Locate the W2b1 paused_disk_full branch in `complete_subtask`:

```python
    # W2b1: paused_disk_full short-circuits — environmental, not a quality signal.
    if final_status == "paused_disk_full":
        sub.status = "paused_disk_full"
        sub.executor_id = None
        sub.executor_epoch = None
        sub.assignment_token = None
        sub.assigned_at = None
        sub.last_error = error
        # Don't transition parent task; don't call transition_executor.
        parent = await session.get(DownloadTask, sub.task_id)
        return sub, parent
```

Replace it with the cancel-aware version (also adds `last_paused_at`):

```python
    # W2b1+W2b2: paused_disk_full short-circuits. W2b2 adds (a) last_paused_at write
    # and (b) cancel-aware force-terminate to 'cancelled' if parent is cancelling.
    if final_status == "paused_disk_full":
        parent = await session.get(DownloadTask, sub.task_id, with_for_update=True)
        if parent is not None and parent.status == "cancelling":
            sub.status = "cancelled"
        else:
            sub.status = "paused_disk_full"
            sub.last_paused_at = datetime.now(UTC)
        sub.executor_id = None
        sub.executor_epoch = None
        sub.assignment_token = None
        sub.assigned_at = None
        sub.last_error = error
        # Continue to sibling-terminal tail check (it will transition the parent if needed).
        # Do NOT return early here — let the tail logic run so cancelled-under-cancelling
        # can transition the parent to cancelled.
```

Then ADD a new branch immediately below it (BEFORE the W4 sha256 verify gate):

```python
    # W2b2 §3.3 (b1): paused_external short-circuits — transient HF throttle.
    # Cancel-aware: force-terminate to 'cancelled' if parent is cancelling.
    if final_status == "paused_external":
        parent = await session.get(DownloadTask, sub.task_id, with_for_update=True)
        if parent is not None and parent.status == "cancelling":
            sub.status = "cancelled"
        else:
            sub.status = "paused_external"
            sub.last_paused_at = datetime.now(UTC)
        sub.executor_id = None
        sub.executor_epoch = None
        sub.assignment_token = None
        sub.assigned_at = None
        sub.last_error = error
        # Fall through to sibling-terminal tail so cancelled-under-cancelling can transition.
```

**Critical:** removed the `return sub, parent` early-return from the W2b1 paused_disk_full branch — it now falls through. This is necessary because the sibling-terminal tail must run to transition `cancelling → cancelled` when the paused-under-cancelling case force-terminated the sub.

The W1 epoch-mismatch fence at the top of the function remains unchanged.

The W2a state-machine `transition_executor` call (further down) is guarded by `if sub.executor_id is not None` — since the paused branches clear `executor_id`, the W2a block does nothing. Verify by inspection: after this M3 change, the W2a transition block should still be present and unchanged but only fires for actual succeeded/failed reports.

- [ ] **Step 5: Run the new tests — verify all 3 (succeeded under cancelling + 2 new paused) pass**

```
uv run pytest tests/services/test_complete_subtask_cancel_aware.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Run W2b1 paused_disk_full test for regression**

```
uv run pytest tests/services/test_sweep_paused_disk_full.py -v
```

Expected: 1 passed (the W2b1 sweeper test still works — it seeds the row directly and doesn't go through complete_subtask).

If any other test in `tests/services/test_scheduler.py` exercises `complete_subtask` with `final_status="paused_disk_full"` and expects an early return without parent-task transition, update the test only if the new fall-through behavior IS a genuine semantic change. Spot-check.

- [ ] **Step 7: Run full suite**

```
uv run pytest -x
```

Expected: 177 passed (175 + 2 new), 1 deselected.

- [ ] **Step 8: Commit**

```bash
git add src/dlw/schemas/subtask.py src/dlw/services/scheduler.py tests/services/test_complete_subtask_cancel_aware.py
git commit -m "feat(scheduler): paused_external + cancel-aware paused branches + last_paused_at (W2b2 M3)"
```

---

### Task 7: `sweep_paused_external` + main loop wire + 2 tests

**Files:**
- Modify: `src/dlw/services/recovery.py` (+sweep function + module constant)
- Modify: `src/dlw/main.py` (call new sweep)
- Create: `tests/services/test_sweep_paused_external.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_sweep_paused_external.py`:

```python
"""Tests for sweep_paused_external (Phase 2 W2b2 §3.4)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.services.recovery import sweep_paused_external


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def env(db_session: AsyncSession):
    db_session.add(Tenant(id=1, slug="d", display_name="D"))
    await db_session.flush()
    db_session.add(Project(id=1, tenant_id=1, name="d"))
    db_session.add(User(id=1, tenant_id=1, oidc_subject="d",
                        email="d@l", role="tenant_admin"))
    db_session.add(StorageBackend(id=1, tenant_id=1, name="d",
                                  backend_type="s3", config_encrypted=b""))
    await db_session.flush()


@pytest.mark.slow
async def test_sweep_recovers_paused_external_after_quiet_window(
    db_session: AsyncSession, env,
) -> None:
    """paused_external sub with last_paused_at=now-400s + active parent → recovered to pending."""
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="downloading",
    )
    db_session.add(task)
    await db_session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="m.bin",
        file_size=100, status="paused_external",
        last_paused_at=datetime.now(UTC) - timedelta(seconds=400),
        executor_id=None, executor_epoch=None,
    )
    db_session.add(sub)
    await db_session.flush()

    recovered = await sweep_paused_external(db_session)
    assert recovered == 1
    fresh = await db_session.get(FileSubTask, sub.id)
    assert fresh.status == "pending"


@pytest.mark.slow
async def test_sweep_skips_paused_external_under_cancelling_parent(
    db_session: AsyncSession, env,
) -> None:
    """paused_external sub whose parent is cancelling → NOT recovered."""
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="cancelling",
    )
    db_session.add(task)
    await db_session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="m.bin",
        file_size=100, status="paused_external",
        last_paused_at=datetime.now(UTC) - timedelta(seconds=400),
    )
    db_session.add(sub)
    await db_session.flush()

    recovered = await sweep_paused_external(db_session)
    assert recovered == 0
    fresh = await db_session.get(FileSubTask, sub.id)
    assert fresh.status == "paused_external"   # untouched
```

- [ ] **Step 2: Run — verify ImportError**

```
uv run pytest tests/services/test_sweep_paused_external.py -v
```

Expected: 2 collection-time errors, `ImportError: cannot import name 'sweep_paused_external' from 'dlw.services.recovery'`.

- [ ] **Step 3: Add `sweep_paused_external` to `src/dlw/services/recovery.py`**

Read the file. Confirm `import os` is at the top — if not, add it. Near the existing W2b1 module constants (or near the top), add:

```python
_PAUSED_EXTERNAL_RETRY_INTERVAL_SECONDS = int(
    os.environ.get("DLW_PAUSED_EXTERNAL_RETRY_INTERVAL_SECONDS", "300")
)
```

Append to the END of the file:

```python
async def sweep_paused_external(session: AsyncSession) -> int:
    """W2b2 §3.4: recover paused_external subtasks after a quiet period.

    Walks paused_external subtasks whose last_paused_at is older than the
    quiet interval (default 5 min) AND whose parent task is still active
    (pending/scheduling/downloading). Flips them back to 'pending' for
    re-claim. Returns count recovered. Caller commits.
    """
    quiet_threshold = datetime.now(UTC) - timedelta(
        seconds=_PAUSED_EXTERNAL_RETRY_INTERVAL_SECONDS
    )

    rows = (await session.execute(
        select(FileSubTask, DownloadTask)
        .join(DownloadTask, DownloadTask.id == FileSubTask.task_id)
        .where(FileSubTask.status == "paused_external")
        .where(FileSubTask.last_paused_at < quiet_threshold)
        .where(DownloadTask.status.in_(("pending", "scheduling", "downloading")))
        .with_for_update(skip_locked=True, of=FileSubTask)
    )).all()

    recovered = 0
    for sub, _parent in rows:
        sub.status = "pending"
        sub.executor_id = None
        sub.executor_epoch = None
        sub.assignment_token = None
        sub.assigned_at = None
        # leave last_paused_at as-is for observability
        recovered += 1
    return recovered
```

Confirm imports at top of `recovery.py`: `from datetime import UTC, datetime, timedelta` and `from sqlalchemy import select` already exist; `DownloadTask` is already imported. No new top-level imports needed.

- [ ] **Step 4: Run the new tests — verify both pass**

```
uv run pytest tests/services/test_sweep_paused_external.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Wire into `_sweep_loop_main` in `src/dlw/main.py`**

Open `src/dlw/main.py`. Find `_sweep_loop_main`. Update the import + body:

```python
async def _sweep_loop_main(factory) -> None:
    """W2a + W2b1 + W2b2: transition stale executors, recover paused_disk_full,
    recover paused_external."""
    from dlw.services.recovery import (
        sweep_executor_timeouts,
        sweep_paused_disk_full,
        sweep_paused_external,
    )

    while True:
        try:
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
            async with factory() as session:
                await sweep_executor_timeouts(session)
                await sweep_paused_disk_full(session)
                await sweep_paused_external(session)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sweep_loop iteration failed; will retry next tick")
```

- [ ] **Step 6: Run full suite**

```
uv run pytest -x
```

Expected: 179 passed (177 + 2 new), 1 deselected.

- [ ] **Step 7: Commit**

```bash
git add src/dlw/services/recovery.py src/dlw/main.py tests/services/test_sweep_paused_external.py
git commit -m "feat(recovery): sweep_paused_external + main loop wire (W2b2 M3)"
```

---

### Task 8: Runner 429/503 classification + 2 tests

**Files:**
- Modify: `src/dlw/executor/runner.py`
- Create: `tests/executor/test_runner_external_throttle.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/executor/test_runner_external_throttle.py`:

```python
"""Tests for runner classifying HF 429/503 as paused_external (Phase 2 W2b2 §3.6)."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from dlw.executor.config import ExecutorSettings
from dlw.executor.runner import ExecutorRunner


def _make_http_status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://hf.fake/repo/file")
    resp = httpx.Response(status_code=code, request=req)
    return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=resp)


def _runner_with_failing_downloader(error: Exception):
    settings = ExecutorSettings(id="ex-throttle", bearer_token="t")
    client = MagicMock()
    client.report = AsyncMock()
    stream = MagicMock()
    stream.download = AsyncMock(side_effect=error)
    chunk = MagicMock()
    chunk.download = AsyncMock(side_effect=error)
    return ExecutorRunner(
        settings=settings, client=client,
        stream_downloader=stream, chunk_downloader=chunk,
    ), client


def _build_subtask_dict(sub_id: uuid.UUID) -> dict:
    return {
        "id": str(sub_id),
        "task_id": str(uuid.uuid4()),
        "filename": "m.bin",
        "file_size": 50_000_000,    # below 100 MiB threshold → stream_downloader
        "expected_sha256": None,
    }


@pytest.mark.asyncio
async def test_runner_classifies_429_as_paused_external() -> None:
    err = _make_http_status_error(429)
    runner, client = _runner_with_failing_downloader(err)
    sub_id = uuid.uuid4()
    token = uuid.uuid4()

    await runner._execute_subtask(
        subtask=_build_subtask_dict(sub_id),
        assignment_token=token,
        repo_id="o/r",
        revision="b" * 40,
        storage_config={
            "bucket": "test", "region": "us-east-1",
            "endpoint_url": None,
            "access_key_id": "x", "secret_access_key": "y",
            "key_prefix": "",
        },
    )

    client.report.assert_awaited_once()
    kwargs = client.report.await_args.kwargs
    assert kwargs["status"] == "paused_external"
    assert kwargs["error"] == "HTTP 429"


@pytest.mark.asyncio
async def test_runner_classifies_503_as_paused_external() -> None:
    err = _make_http_status_error(503)
    runner, client = _runner_with_failing_downloader(err)
    sub_id = uuid.uuid4()
    token = uuid.uuid4()

    await runner._execute_subtask(
        subtask=_build_subtask_dict(sub_id),
        assignment_token=token,
        repo_id="o/r",
        revision="b" * 40,
        storage_config={
            "bucket": "test", "region": "us-east-1",
            "endpoint_url": None,
            "access_key_id": "x", "secret_access_key": "y",
            "key_prefix": "",
        },
    )

    client.report.assert_awaited_once()
    kwargs = client.report.await_args.kwargs
    assert kwargs["status"] == "paused_external"
    assert kwargs["error"] == "HTTP 503"
```

- [ ] **Step 2: Run — verify failure (runner doesn't classify yet)**

```
uv run pytest tests/executor/test_runner_external_throttle.py -v
```

Expected: 2 failures — either the test crashes because of how the existing `except Exception` reports, or the assertion fires because `status == "failed"` instead of `"paused_external"`.

- [ ] **Step 3: Modify `src/dlw/executor/runner.py`**

Read the file. Find `_execute_subtask`. Locate the existing exception block. The W2b1 path looks like:

```python
            try:
                result = await downloader.download(assignment=assignment)
            except DiskFullError as e:
                logger.warning("subtask %s paused_disk_full: %s", sub_id, e)
                await self._client.report(
                    subtask_id=sub_id,
                    status="paused_disk_full",
                    assignment_token=assignment_token,
                    actual_sha256=None,
                    bytes_downloaded=0,
                    error=str(e),
                )
                return
            await self._client.report(...succeeded...)
        except Exception as e:
            logger.exception("subtask %s failed", sub_id)
            ...failure report...
```

Add a new `except` between `DiskFullError` and the outer `except Exception`. Specifically, inside the same inner try (where `result = await downloader.download(...)` is):

```python
            try:
                result = await downloader.download(assignment=assignment)
            except DiskFullError as e:
                logger.warning("subtask %s paused_disk_full: %s", sub_id, e)
                await self._client.report(
                    subtask_id=sub_id,
                    status="paused_disk_full",
                    assignment_token=assignment_token,
                    actual_sha256=None,
                    bytes_downloaded=0,
                    error=str(e),
                )
                return
            except httpx.HTTPStatusError as e:                         # W2b2 NEW
                code = e.response.status_code
                if code in (429, 503):
                    logger.warning(
                        "subtask %s paused_external: HF returned %d", sub_id, code,
                    )
                    await self._client.report(
                        subtask_id=sub_id,
                        status="paused_external",
                        assignment_token=assignment_token,
                        actual_sha256=None,
                        bytes_downloaded=0,
                        error=f"HTTP {code}",
                    )
                    return
                raise   # other 4xx/5xx fall through to the generic handler
```

Confirm `import httpx as _httpx` (W3 pattern) or `import httpx` is at the top of `runner.py`. The W2b1 version of runner.py already imports httpx for the EPOCH_MISMATCH handling. If the file uses the `_httpx` alias, adjust the new branch to use `_httpx.HTTPStatusError` to match.

- [ ] **Step 4: Run the new tests — verify both pass**

```
uv run pytest tests/executor/test_runner_external_throttle.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Run all executor tests for regression**

```
uv run pytest tests/executor/ -v
```

Expected: all pass — the new branch is correctly positioned between `DiskFullError` and the generic `Exception`, so existing W4/W2b1 paths are unchanged.

- [ ] **Step 6: Run full suite**

```
uv run pytest -x
```

Expected: 181 passed (179 + 2 new), 1 deselected.

- [ ] **Step 7: Commit**

```bash
git add src/dlw/executor/runner.py tests/executor/test_runner_external_throttle.py
git commit -m "feat(executor): runner classifies HF 429/503 as paused_external (W2b2 M3)"
```

---

### Milestone 3 verification (self)

- [ ] SubTaskReport accepts paused_external.
- [ ] complete_subtask handles paused_external + paused_disk_full with cancel-aware logic.
- [ ] sweep_paused_external recovers after quiet window; skips cancelling parents.
- [ ] Main loop runs all three sweepers per tick.
- [ ] Runner classifies HF 429/503.
- [ ] Full suite at 181.

---

## Milestone 4 — Lint + OpenAPI + operator runbook + PR

After M4, lints catch the new value-domain literals, OpenAPI advertises the new endpoint and enum widenings, operator runbook covers cancel-latency expectations, PR is open with CI green.

---

### Task 9: Lint extension + OpenAPI + operator runbook

**Files:**
- Modify: `tools/lint_invariants.py`
- Modify: `api/openapi.yaml`
- Modify: `docs/operator/executor-runbook.md`

- [ ] **Step 1: Extend `tools/lint_invariants.py`**

Open `tools/lint_invariants.py`. Find the existing `VALID_SUBTASK_STATUS` set (added in W2b1). Update it:

```python
VALID_SUBTASK_STATUS = {
    "pending", "assigned", "succeeded", "failed", "cancelled",
    "paused_disk_full", "paused_external",          # W2b2 NEW
}
```

After the existing `check_subtask_status_domain` function, add a new helper modeled after it:

```python
VALID_TASK_STATUS = {
    "pending", "scheduling", "downloading",
    "succeeded", "failed", "cancelled",
    "cancelling",   # W2b2 NEW
}


def check_task_status_domain() -> list[str]:
    """W2b2 §3.9: lint literals assigned to `status` kwarg/attr in task-mutating
    service modules. Identical AST patterns to check_subtask_status_domain;
    only the value-domain set + scanned files differ."""
    errors: list[str] = []
    files = [
        ROOT / "src" / "dlw" / "api" / "tasks.py",
        ROOT / "src" / "dlw" / "services" / "task_service.py",
        ROOT / "src" / "dlw" / "services" / "scheduler.py",
    ]
    import ast as _ast
    for f in files:
        if not f.exists():
            continue
        tree = _ast.parse(f.read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.keyword) and node.arg == "status":
                if isinstance(node.value, _ast.Constant) and isinstance(node.value.value, str):
                    if node.value.value not in VALID_TASK_STATUS:
                        errors.append(
                            f"{f.relative_to(ROOT)}:{node.value.lineno}: "
                            f"invalid task status: {node.value.value!r}"
                        )
            elif (isinstance(node, _ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], _ast.Attribute)
                    and node.targets[0].attr == "status"
                    and isinstance(node.value, _ast.Constant)
                    and isinstance(node.value.value, str)):
                if node.value.value not in VALID_TASK_STATUS:
                    errors.append(
                        f"{f.relative_to(ROOT)}:{node.lineno}: "
                        f"invalid task status: {node.value.value!r}"
                    )
    return errors
```

In `main()`, find the existing `failures.extend(check_subtask_status_domain())` line. Add immediately below:

```python
    failures.extend(check_task_status_domain())
```

- [ ] **Step 2: Run lint — verify clean on production tree**

```
python tools/lint_invariants.py
```

Expected: exits 0 with the OK banner. If a violation appears, the offending literal is printed — investigate. Both `VALID_TASK_STATUS` and `VALID_SUBTASK_STATUS` should cover every literal currently in the scanned files.

Common false-positive case to watch for: `scheduler.py` writes `parent.status = "succeeded" / "failed" / "cancelled"` — all three are in `VALID_TASK_STATUS`. `scheduler.py` also writes `sub.status = "pending" / "assigned" / "succeeded" / "failed" / "cancelled" / "paused_disk_full" / "paused_external"` — all in `VALID_SUBTASK_STATUS`. No literal should fire either lint.

- [ ] **Step 3: Run lint_invariants self-tests**

```
uv run pytest tools/test_lint_invariants.py -v
```

Expected: all pass (no regression).

- [ ] **Step 4: Widen `api/openapi.yaml`**

Read the file. Make three changes:

(a) `SubTaskReport.status` enum widens. Find the `SubTaskReport:` schema. Current state (W2b1):

```yaml
        status:
          type: string
          enum: [succeeded, failed, paused_disk_full]
          description: Subtask completion status reported by executor.
```

Change to:

```yaml
        status:
          type: string
          enum: [succeeded, failed, paused_disk_full, paused_external]
          description: Subtask completion status reported by executor.
```

(b) `TaskRead.status` enum. Find the `TaskRead:` schema's `status:` property. If it's plain `type: string`, change to:

```yaml
        status:
          type: string
          enum: [pending, scheduling, downloading, succeeded, failed, cancelled, cancelling]
          description: Task lifecycle state.
```

If it already has an enum, add `cancelling` to it.

(c) Add the new operation. Find the `/tasks:` path section. Below the existing `POST` (`post_task`) operation under `/tasks`, but at the `paths:` root level (sibling of `/tasks:`), add:

```yaml
  /tasks/{task_id}/cancel:
    post:
      summary: Cancel a task (idempotent)
      tags: [tasks]
      security:
        - bearerAuth: []
      parameters:
        - {name: task_id, in: path, required: true, schema: {type: string, format: uuid}}
      responses:
        '202':
          description: Cancel accepted; task is now 'cancelling'.
          content:
            application/json:
              schema: {$ref: '#/components/schemas/TaskRead'}
        '404':
          description: Task not found
        '409':
          description: Task already in terminal state
```

Watch the indentation — match the surrounding paths' indentation exactly.

- [ ] **Step 5: Update `docs/operator/executor-runbook.md`**

Read the file (W2b1 created it). Append a new section at the end:

```markdown

## Task cancellation latency (Phase 2 W2b2+)

`POST /api/v1/tasks/{task_id}/cancel` flips the task to `cancelling`. The
scheduler stops handing out new subtasks for that task immediately.
In-flight subtasks finish naturally:

- Small files (< 100 MiB, W4 `HfS3StreamDownloader`): typically seconds.
- Large files (≥ 100 MiB, W2b1 `DirectOffsetDownloader`): can take **up to
  several minutes** depending on file size and bandwidth.

The task stays in `cancelling` until the last in-flight subtask reaches a
terminal state, then transitions to `cancelled`. Paused subtasks
(`paused_disk_full` / `paused_external`) at the moment of `/cancel` are
force-terminated synchronously inside the cancel transaction.

If a task stays in `cancelling` for unexpectedly long (e.g. > 30 minutes
on a fast network), check executor logs for stuck downloads. Operator
escalation: re-issue `/cancel` — it's idempotent and will re-force-terminate
any paused subtasks that appeared after the original cancel.

A future Phase 2 W3 release will add heartbeat-carried cancellation
signals so executors abort in-flight downloads on chunk boundaries,
reducing latency to sub-minute.
```

- [ ] **Step 6: Final local verification**

```
python tools/lint_no_direct_status_write.py
python tools/lint_invariants.py
uv run pytest -x
```

Expected: both lints clean; pytest 181 passed, 1 deselected.

Optionally, if `npx spectral` is available locally:

```
npx --yes @stoplight/spectral-cli@6 lint api/openapi.yaml --fail-severity=error
```

Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add tools/lint_invariants.py api/openapi.yaml docs/operator/executor-runbook.md
git commit -m "ci(lint): task status value-domain + OpenAPI cancel + runbook (W2b2 M4)"
```

---

### Task 10: Push branch + open PR + monitor CI (controller does this)

- [ ] **Step 1: Confirm branch state**

```bash
git status
git log main..HEAD --oneline
```

Expected: clean working tree; ~11 commits on the branch (1 spec + 9 task commits — Task 10 itself is just push/PR).

- [ ] **Step 2: Push**

```bash
git push -u origin feat/phase-2-w2b2-cancel-and-paused-external
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create \
  --title "Phase 2 Week 2b2 — task cancel + paused_external" \
  --body "$(cat <<'EOF'
## Summary

W2b2 half of `docs/v2.0/08-mvp-roadmap.md` §2.6 Day 4:

- **D4/D8 Task cancellation.** New `POST /api/v1/tasks/{task_id}/cancel` endpoint flips task to `cancelling`. Lazy propagation: scheduler refuses new claims for cancelling tasks; in-flight subtasks finish naturally (chunk-level may take minutes — documented in runbook). `complete_subtask` becomes cancel-aware — when the last sibling of a `cancelling` task terminates, the task transitions to `cancelled`. Both paused branches (`paused_disk_full` / `paused_external`) also cancel-aware: paused report arriving after `/cancel` force-terminates to `cancelled`, avoiding dead-lock.
- **D13 paused_external (per-subtask).** Executor classifies HF 429/503 as transient throttle and reports `SubTaskReport.status="paused_external"`. `complete_subtask` short-circuits like W2b1's paused_disk_full — no retry_count bump, no task transition. New `sweep_paused_external` (added to `_sweep_loop_main` alongside W2a/W2b1 sweepers) flips back to `pending` after a 5-min quiet window — provided parent is still active.
- **Schema.** One alembic: `file_subtasks.last_paused_at TIMESTAMPTZ NULL`. ORM + lint domain extensions enforce the value-domain widening (subtask += `paused_external`; task += `cancelling`).
- **CI / docs.** `tools/lint_invariants.py` gains `check_task_status_domain`. OpenAPI advertises the cancel operation + both enum widenings. Operator runbook documents cancel-latency expectations.

Spec: `docs/superpowers/specs/2026-05-14-phase-2-w2b2-cancel-and-paused-external-design.md`.
Plan: `docs/superpowers/plans/2026-05-14-phase-2-w2b2-cancel-and-paused-external.md`.

## Test plan

- [x] Backend pytest: baseline + 15 new (4 cancel_task + 3 API + 1 scheduler skip + 3 complete_subtask cancel-aware + 2 sweep paused_external + 2 runner 429/503) = 181 passed, 1 deselected. Zero regressions.
- [x] Alembic upgrade clean from W2a head (W2b1 added no migrations); downgrade clean.
- [x] `tools/lint_no_direct_status_write.py` returns 0.
- [x] `tools/lint_invariants.py` returns 0 with new task domain check.
- [x] OpenAPI `SubTaskReport.status` has 4 enum values; `TaskRead.status` has 7; new POST `/tasks/{id}/cancel` operation present; spectral clean.
- [x] Lifespan smoke: `_sweep_loop_main` calls all three sweepers per tick.

## Out of scope (deferred — see spec §1.2)

Heartbeat-carried cancel signal (Phase 2 W3 or Phase 3); `source_throttle_state` table + global state machine (Phase 3); speed_limit downlink (Phase 3); `verified` state rename (Phase 3); hard-cancel API; multi-user RBAC on /cancel.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Monitor CI**

```bash
gh pr checks $(gh pr view --json number -q .number) --watch
```

Expected: 12 checks pass. If any fail:

- **pytest** — re-run locally first. Async test patterns sometimes surface ordering issues on CI.
- **OpenAPI lint** — spectral may flag the new operation if the indentation got off. Diff against W2b1's OpenAPI changes for the pattern.
- **Invariant + cross-ref lint** — the new `check_task_status_domain` may catch a literal you missed. Fix the source.

---

### Milestone 4 verification (self)

- [ ] PR opened; CI 12/12 green.
- [ ] No diff outside the File Structure list (`gh pr diff --name-only`).
- [ ] All 15 new tests pass; no W4 / W1 / W2a / W2b1 regressions.

---

## Definition of Done

- [ ] All 9 implementation tasks committed on `feat/phase-2-w2b2-cancel-and-paused-external`.
- [ ] PR opened, CI 12/12 green.
- [ ] 15 new pytest tests pass; baseline + 15 = 181 total.
- [ ] One alembic migration applies cleanly from W2a head; reverses cleanly. `file_subtasks.last_paused_at` exists.
- [ ] `cancel_task` is idempotent on cancelling, 409 on terminal, 404 on missing.
- [ ] `POST /tasks/{id}/cancel` returns 202/404/409 correctly.
- [ ] `claim_one_subtask` refuses subtasks under cancelling/terminal parents.
- [ ] `complete_subtask` 3 new branches (paused_external short-circuit, cancel-aware paused fall-through, cancel-aware sibling-terminal tail) all working.
- [ ] `sweep_paused_external` recovers after 5-min quiet window; skips cancelling parents; wired into `_sweep_loop_main`.
- [ ] Runner classifies HF 429/503 → `paused_external`.
- [ ] OpenAPI `SubTaskReport.status` has 4 enum values; `TaskRead.status` has 7; new POST `/tasks/{id}/cancel` operation.
- [ ] `tools/lint_invariants.py`: `VALID_SUBTASK_STATUS` += `paused_external`; new `check_task_status_domain` reports 0 on production tree.
- [ ] No new runtime / dev deps; no new CI jobs.
- [ ] `docs/operator/executor-runbook.md` documents cancel-latency expectations.

---

## Plan Revisions Log

(Empty on first draft.)

| Tag | Severity | Issue | Fix applied |
|-----|----------|-------|-------------|
| _(none yet)_ | | | |

---

## References

- Spec: `docs/superpowers/specs/2026-05-14-phase-2-w2b2-cancel-and-paused-external-design.md`
- Predecessor specs:
  - W1: `docs/superpowers/specs/2026-05-11-phase-2-week-1-fence-token-recovery-design.md`
  - W2a: `docs/superpowers/specs/2026-05-13-phase-2-w2a-scheduler-state-machine-design.md`
  - W2b1: `docs/superpowers/specs/2026-05-13-phase-2-w2b1-chunk-level-downloader-design.md`
- Predecessor plans:
  - W1: `docs/superpowers/plans/2026-05-11-phase-2-week-1-fence-token-recovery.md`
  - W2a: `docs/superpowers/plans/2026-05-13-phase-2-w2a-scheduler-state-machine.md`
  - W2b1: `docs/superpowers/plans/2026-05-13-phase-2-w2b1-chunk-level-downloader.md`
- Roadmap source: `docs/v2.0/08-mvp-roadmap.md` §2.6 Phase 2 W2 Day 4
- Distributed correctness: `docs/v2.0/03-distributed-correctness.md` §7 (D8) + §8.3 (D13 per-subtask)
- Invariants: `docs/v2.0/INVARIANTS.md` §C
- W2b1 PR (merged): https://github.com/l17728/modelpull/pull/10 (squash `6037e6b`)
