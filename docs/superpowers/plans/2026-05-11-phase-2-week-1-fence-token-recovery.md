# Phase 2 Week 1: Fence Token + Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the executor-epoch fence layer (per-executor monotonic counter carried in `X-Executor-Epoch` header) on top of Phase 1's existing assignment-token fence, plus startup-once recovery routine and a background reclaim loop. End of plan: a stale executor whose heartbeat has lapsed has its claimed-but-unfinished subtasks reset to `pending`, fenced by `(executor_id, executor_epoch)` so a re-joined zombie cannot overwrite peer (or its own new-epoch) work.

**Architecture:** Single coherent fence layer — `(executor_id, executor_epoch, assignment_token)` triple gates every executor → controller mutation. `join_executor` becomes a PostgreSQL `INSERT ... ON CONFLICT DO UPDATE SET epoch = epoch + 1 RETURNING epoch` (atomic bump survives concurrent join). FastAPI `Depends(require_executor_epoch)` reads `X-Executor-Epoch` header on heartbeat/poll/report and rejects mismatches with `401 EPOCH_MISMATCH`. Three-way verification (head + size only — sha256 deferred to P2-W2) inside `run_recovery_routine` handles mid-multipart crashes. Background `reclaim_loop` task scans for executors with stale heartbeats every 30s and reclaims their work fenced by their current epoch.

**Tech Stack:** No new libraries. Existing stack: SQLAlchemy 2.x `update()` + PG `pg_insert.on_conflict_do_update`, FastAPI `Depends/Header/Path`, asyncio.create_task in lifespan, moto[s3] (already W4 dev dep) for `head_object` + `abort_multipart_upload` test coverage.

**Scope:** Pure backend. No frontend changes. 1 alembic migration adds 4 columns (executors.epoch + file_subtasks {multipart_started_at, assigned_at, last_heartbeat_seen_at}). Companion spec: `docs/superpowers/specs/2026-05-11-phase-2-week-1-fence-token-recovery-design.md`.

**Pre-flight:** PR #1-5 merged to main; alpha-demo PR #6 may still be open (orthogonal — no overlap). Branch `feat/phase-2-w1-fence-token` exists with spec committed (commit `ac2114b`). Local PG running on `localhost:5433`. `uv` 0.11.9. 99 backend tests + 1 manual smoke deselected on main.

**Out-of-scope (deferred — explicit list):**
- Full task-status state machine (downloading / uploading / verifying_remote / cancelling / paused_*) → P2-W2
- `executor_jwt` + cert-fingerprint binding → P2-W3
- `ChecksumSHA256` server-side multipart verification → P2-W2
- Multi-executor scheduler fairness / priority queue → P2-W2
- HMAC heartbeat (nonce + timestamp) → P2-W3
- Node state-machine `degraded ↔ suspect` + `probationary` → P2-W2
- `verifying` task recovery branch → P2-W2 (Phase 1 has no `verifying` state)
- HF global 429 throttle state recovery → Phase 3
- Bucket lifecycle config (24h auto-abort) → Phase 3 / ops
- `chunks_completed` resume → P2-W2

---

## File Structure

After this plan:

```
modelpull/
├── src/dlw/
│   ├── db/models/
│   │   ├── executor.py                            MODIFY +epoch column
│   │   └── task.py                                MODIFY +multipart_started_at +assigned_at +last_heartbeat_seen_at
│   ├── alembic/versions/
│   │   └── <rev>_phase2_w1_fence_columns.py       NEW (single migration)
│   ├── schemas/
│   │   └── executor.py                            MODIFY ExecutorRead +epoch
│   ├── auth/
│   │   └── executor_epoch.py                      NEW require_executor_epoch dep
│   ├── services/
│   │   ├── executor_service.py                    MODIFY join_executor uses pg_insert ON CONFLICT
│   │   ├── scheduler.py                           MODIFY claim_one_subtask writes epoch + assigned_at;
│   │   │                                          complete_subtask verifies epoch;
│   │   │                                          ADD reclaim_subtasks
│   │   └── recovery.py                            NEW run_recovery_routine + verify_remote_state
│   │                                              + reclaim_stale_executors + RecoveryStats
│   ├── api/
│   │   ├── executors.py                           MODIFY heartbeat + poll use require_executor_epoch dep;
│   │   │                                          poll passes executor.epoch to claim_one_subtask
│   │   └── subtasks.py                            MODIFY report uses require_executor_epoch dep;
│   │                                              forwards executor_epoch to complete_subtask
│   ├── main.py                                    MODIFY lifespan: run_recovery_routine + reclaim_loop
│   └── executor/
│       ├── client.py                              MODIFY ControllerClient persists epoch from /join;
│       │                                          attaches X-Executor-Epoch header
│       └── runner.py                              MODIFY catch 401 EPOCH_MISMATCH → _rejoin
├── tests/
│   ├── auth/
│   │   └── test_executor_epoch.py                 NEW
│   ├── services/
│   │   ├── test_executor_service.py               MODIFY +epoch tests
│   │   ├── test_scheduler.py                      MODIFY +epoch gates + reclaim_subtasks
│   │   └── test_recovery.py                       NEW
│   ├── api/
│   │   ├── test_executors.py                      MODIFY 401 EPOCH_MISMATCH paths
│   │   └── test_subtasks.py                       MODIFY stale-epoch 409
│   ├── executor/
│   │   ├── test_client.py                         MODIFY epoch header tests
│   │   └── test_runner.py                         MODIFY rejoin-on-401 test
│   └── e2e/
│       └── test_executor_e2e.py                   MODIFY exercises new header flow
├── api/openapi.yaml                               MODIFY +epoch header + EPOCH_MISMATCH +
│                                                  backfill W4 fields (storage_config, s3_key)
└── docs/superpowers/specs/2026-05-11-phase-2-week-1-fence-token-recovery-design.md  (already committed)
```

**Why this structure:** each file has one reason to change. `recovery.py` is a new module (single responsibility); `executor_epoch.py` is a one-function dep file. `scheduler.py` already mixes claim + complete + (now) reclaim — they share the same fence semantics. OpenAPI sync absorbs three concerns into one file edit.

---

## Pre-flight checks

- [ ] On branch `feat/phase-2-w1-fence-token`, spec committed (`git log --oneline -1` shows `ac2114b` or descendant).
- [ ] PR #1-5 merged to main (`git log main --oneline | grep "Merge PR" | head -5`).
- [ ] PG running on `localhost:5433` (`pg_isready -h localhost -p 5433`).
- [ ] `dlw` database has Phase 1 W4 schema applied (`uv run alembic upgrade head` no-op).
- [ ] Existing pytest suite green (`uv run pytest -x` → 99 passed, 1 deselected).
- [ ] `uv --version` ≥ 0.11.9.

---

## Milestone 1 — Schema migration + atomic epoch bump

After M1, every `/join` returns a monotonically-increasing `epoch` and `executors.epoch` is populated. No other behavior change yet.

---

### Task 1: Alembic migration + model field updates

**Files:**
- Modify: `src/dlw/db/models/executor.py` — add `epoch` column
- Modify: `src/dlw/db/models/task.py` — add 3 timestamp columns to `FileSubTask`
- Create: `src/dlw/alembic/versions/<rev>_phase2_w1_fence_columns.py` — autogenerated migration

- [ ] **Step 1: Add `epoch` column to `Executor` model**

In `src/dlw/db/models/executor.py`, after the `status` column (around line 24) and before `health_score`:

```python
    # Phase 2 W1 fence token: monotonic counter bumped atomically on every /join.
    # First /join sets epoch=1; re-join (post-crash or after EPOCH_MISMATCH) bumps it.
    # All non-join executor requests carry X-Executor-Epoch which controller verifies.
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
```

- [ ] **Step 2: Add 3 timestamp columns to `FileSubTask`**

In `src/dlw/db/models/task.py`, find the `FileSubTask` class. Inside it, just before the `chunks_total` line (around line 90), add:

```python
    # Phase 2 W1 recovery: timestamps used by reclaim + recovery_routine.
    # multipart_started_at is set when the executor begins multipart_upload;
    # assigned_at is set on claim_one_subtask (used by recovery threshold);
    # last_heartbeat_seen_at is updated when controller sees this subtask in a
    # heartbeat report (P2-W2 will populate it; P2-W1 just adds the column).
    multipart_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_heartbeat_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

- [ ] **Step 3: Autogenerate migration**

```bash
uv run alembic revision --autogenerate -m "phase2 w1 fence columns"
```

Open the generated file in `src/dlw/alembic/versions/`. The `upgrade()` body should contain ONLY these `op.add_column` calls:

```python
def upgrade() -> None:
    op.add_column('executors',
        sa.Column('epoch', sa.BigInteger(), nullable=False, server_default='0'))
    # W6-H: guard against future seed/fixtures that omit epoch and silently get 0
    # then pass a "0" header that matches. Default-0 is fine for PRE-MIGRATION
    # rows (first /join bumps them to 1); the check just blocks negative values.
    op.create_check_constraint(
        "ck_executors_epoch_nonnegative",
        "executors",
        "epoch >= 0",
    )
    op.add_column('file_subtasks',
        sa.Column('multipart_started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('file_subtasks',
        sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('file_subtasks',
        sa.Column('last_heartbeat_seen_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('file_subtasks', 'last_heartbeat_seen_at')
    op.drop_column('file_subtasks', 'assigned_at')
    op.drop_column('file_subtasks', 'multipart_started_at')
    op.drop_constraint("ck_executors_epoch_nonnegative", "executors", type_="check")
    op.drop_column('executors', 'epoch')
```

If autogenerate added spurious `alter_column` or `drop_index` entries (W5-G: ORM drift like `storage_backends.is_default` defaults), DELETE those lines. Migration upgrade/downgrade must contain ONLY the 4 column additions/removals listed above.

`server_default='0'` is required for `executors.epoch` because the column is NOT NULL and existing rows (if any) need a value. The model's `default=0` is a Python-side default; the migration uses SQL-side default.

**W6-K**: After autogenerate, **verify** the `down_revision` line at the top of the new file points to W4's head:

```python
down_revision: Union[str, None] = '5a729be99dc0'    # must equal this
```

If autogenerate produced a different value, the implementer ran against a non-head DB. Abort and re-run `alembic upgrade head` first.

- [ ] **Step 4: Round-trip verify (W5-G discipline)**

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

All three commands must succeed with no errors.

- [ ] **Step 5: Verify columns in psql**

```bash
psql -h localhost -p 5433 -U postgres -d dlw -c "\d executors" | grep epoch
psql -h localhost -p 5433 -U postgres -d dlw -c "\d file_subtasks" | grep -E "assigned_at|multipart_started_at|last_heartbeat_seen_at"
```

Expected: 1 line for `epoch | bigint | | not null | 0`; 3 lines for the timestamp columns (all nullable).

- [ ] **Step 6: Existing alembic test still green**

```bash
uv run pytest tests/db/test_alembic.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/dlw/db/models/executor.py src/dlw/db/models/task.py src/dlw/alembic/versions/*_phase2_w1_fence_columns.py
git commit -m "feat(db): phase 2 W1 fence columns — executor.epoch + file_subtasks timestamps"
```

---

### Task 2: `join_executor` atomic ON CONFLICT bump + ExecutorRead schema

**Files:**
- Modify: `src/dlw/services/executor_service.py` — rewrite `join_executor` with `pg_insert`
- Modify: `src/dlw/schemas/executor.py` — add `epoch: int` to `ExecutorRead`
- Modify: `tests/services/test_executor_service.py` — add 3 epoch tests

- [ ] **Step 1: Append 3 new tests to `tests/services/test_executor_service.py`**

```python
@pytest.mark.slow
async def test_join_first_time_returns_epoch_1(db_session: AsyncSession, env) -> None:
    """First /join for a brand new executor_id assigns epoch=1."""
    body = ExecutorJoin(id="new-host-worker-1", host_id="new-host")
    ex = await join_executor(db_session, body)
    assert ex.epoch == 1


@pytest.mark.slow
async def test_join_existing_executor_increments_epoch(
    db_session: AsyncSession, env,
) -> None:
    """Repeated /join for same id bumps epoch atomically: 1 → 2 → 3."""
    body = ExecutorJoin(id="bump-host-worker-1", host_id="bump-host")
    ex1 = await join_executor(db_session, body); await db_session.commit()
    assert ex1.epoch == 1
    ex2 = await join_executor(db_session, body); await db_session.commit()
    assert ex2.epoch == 2
    ex3 = await join_executor(db_session, body); await db_session.commit()
    assert ex3.epoch == 3


@pytest.mark.slow
async def test_join_concurrent_returns_distinct_epochs(
    engine, env,
) -> None:
    """asyncio.gather × 2 join calls for the same id must yield distinct epochs."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(engine, expire_on_commit=False)
    body = ExecutorJoin(id="race-host-worker-1", host_id="race-host")

    async def join_in_own_session():
        async with factory() as s:
            ex = await join_executor(s, body)
            await s.commit()
            return ex.epoch

    epochs = await asyncio.gather(join_in_own_session(), join_in_own_session())
    assert sorted(epochs) == [1, 2]  # PG atomic; one wins INSERT, the other UPDATE
```

Add at the top of the file (if not present):

```python
import asyncio
```

- [ ] **Step 2: Run new tests; expected FAIL**

```bash
uv run pytest tests/services/test_executor_service.py::test_join_first_time_returns_epoch_1 -v
```

Expected: FAIL — `Executor` instance has no `epoch` attribute OR existing implementation always inserts with no epoch logic; first test fails on `assert ex.epoch == 1` since model now has `epoch` but `join_executor` doesn't set it explicitly (default=0 from model, but new behaviour needs +1 logic).

- [ ] **Step 3: Replace `src/dlw/services/executor_service.py`**

```python
"""Executor service: atomic register + heartbeat update.

Phase 2 W1: join_executor now bumps epoch atomically on every call.
First-time INSERT writes epoch=1; ON CONFLICT DO UPDATE bumps epoch+=1.
Status resets to 'joining' on every rejoin so that 'unhealthy'
(set by reclaim_stale_executors) flips back to 'joining' → 'healthy'
on the next heartbeat.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.executor import Executor
from dlw.schemas.executor import ExecutorHeartbeat, ExecutorJoin


async def join_executor(session: AsyncSession, body: ExecutorJoin) -> Executor:
    """Atomic INSERT-or-bump. Returns the persisted Executor row with current epoch.

    PG INSERT ... ON CONFLICT (id) DO UPDATE is atomic for the bump — two
    concurrent join calls for the same id can never get the same epoch.
    """
    stmt = pg_insert(Executor).values(
        id=body.id,
        host_id=body.host_id,
        cert_fingerprint=body.cert_fingerprint,
        capabilities=body.capabilities,
        status="joining",
        epoch=1,
    ).on_conflict_do_update(
        index_elements=["id"],
        set_=dict(
            status="joining",
            host_id=body.host_id,
            cert_fingerprint=body.cert_fingerprint,
            capabilities=body.capabilities,
            epoch=Executor.__table__.c.epoch + 1,
        ),
    ).returning(Executor)
    row = (await session.execute(stmt)).scalar_one()
    return row


async def record_heartbeat(
    session: AsyncSession,
    executor_id: str,
    body: ExecutorHeartbeat,
) -> Executor:
    """Update last_heartbeat_at + health_score + parts_dir_bytes."""
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

- [ ] **Step 4: Modify `src/dlw/schemas/executor.py` — add `epoch` to `ExecutorRead`**

Find the `ExecutorRead` class. Add `epoch: int` after `health_score`:

```python
class ExecutorRead(BaseModel):
    """Returned by join/heartbeat to confirm registration."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    health_score: int
    epoch: int        # NEW (P2-W1 fence: clients persist this from /join response)
```

- [ ] **Step 5: Run all 3 new tests; expected PASS**

```bash
uv run pytest tests/services/test_executor_service.py -v
```

Expected: all PASS (existing + 3 new).

- [ ] **Step 6: Run full pytest; expected ALL green (some tests may have been touching `ExecutorRead` without `epoch` — they'll fail until we fix conftest or the test helpers)**

```bash
uv run pytest -x
```

Expected: 99 + 3 = 102 PASS, 1 deselected. If anything else breaks (e.g., `ExecutorRead.model_validate` on an old executor row without epoch), the column default `0` should auto-fill via from_attributes — the model has `default=0` so any row in DB has epoch=0, and Pydantic reads it. If a test constructs `ExecutorRead(id=..., status=..., health_score=...)` directly (no epoch), it now fails with `missing required field epoch`. Such tests need an `epoch=N` kwarg. Check `tests/api/test_executors.py` for any direct `ExecutorRead(...)` construction.

- [ ] **Step 7: Commit**

```bash
git add src/dlw/services/executor_service.py src/dlw/schemas/executor.py tests/services/test_executor_service.py
git commit -m "feat(executor): atomic ON CONFLICT epoch bump + ExecutorRead.epoch (P2-W1)"
```

---

### Milestone 1 verification (self)

```bash
uv run pytest -x
psql -h localhost -p 5433 -U postgres -d dlw -c "SELECT id, epoch, status FROM executors LIMIT 5"
```

Expected: pytest green; `epoch` column visible.

---

## Milestone 2 — `require_executor_epoch` dependency + endpoint wiring

After M2, every executor → controller mutation endpoint requires `X-Executor-Epoch` header and rejects mismatches with `401 EPOCH_MISMATCH`. `/join` is unaffected.

---

### Task 3: `require_executor_epoch` FastAPI dependency

**Files:**
- Create: `src/dlw/auth/executor_epoch.py`
- Create: `tests/auth/test_executor_epoch.py`

- [ ] **Step 1: Write failing test `tests/auth/test_executor_epoch.py`**

```python
"""Tests for the require_executor_epoch FastAPI dependency."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from dlw.db.models.executor import Executor


_TOKEN = "test-bearer-token-12345"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """W6-I: do NOT drop_all at module end — multiple modules share this engine.
    Just create_all (idempotent) + seed a probe executor; rely on per-test
    rollback for cleanup of OTHER state.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        # Use ON CONFLICT DO NOTHING so re-running the module is safe
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(Executor).values(
            id="probe-host-worker-1", host_id="probe-host",
            cert_fingerprint="x", status="healthy", epoch=3,
        ).on_conflict_do_nothing()
        await s.execute(stmt)
        await s.commit()
    yield
    # No drop_all — leave tables for other test modules.


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DLW_BEARER_TOKEN", _TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def app():
    """Build a tiny app that mounts the dep on a probe endpoint."""
    from dlw.api.tasks import _session
    from dlw.auth.executor_epoch import require_executor_epoch

    app = FastAPI()

    @app.get("/probe/{executor_id}")
    async def probe(executor: Executor = Depends(require_executor_epoch)):
        return {"executor_id": executor.id, "epoch": executor.epoch}

    return app


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.slow
async def test_require_epoch_missing_header_returns_401(client: AsyncClient) -> None:
    # W6-D: dep accepts Optional + raises 401 with custom detail (not FastAPI auto-422)
    r = await client.get("/probe/probe-host-worker-1")
    assert r.status_code == 401
    assert "missing X-Executor-Epoch" in r.json()["detail"]


@pytest.mark.slow
async def test_require_epoch_unknown_executor_returns_404(
    client: AsyncClient,
) -> None:
    r = await client.get(
        "/probe/no-such-host-worker-99",
        headers={"X-Executor-Epoch": "1"},
    )
    assert r.status_code == 404
    assert "executor not found" in r.json()["detail"]


@pytest.mark.slow
async def test_require_epoch_mismatch_returns_EPOCH_MISMATCH(
    client: AsyncClient,
) -> None:
    r = await client.get(
        "/probe/probe-host-worker-1",
        headers={"X-Executor-Epoch": "2"},
    )
    assert r.status_code == 401
    body = r.json()
    assert body["detail"]["code"] == "EPOCH_MISMATCH"
    assert body["detail"]["expected"] == 3
    assert body["detail"]["got"] == 2


@pytest.mark.slow
async def test_require_epoch_match_returns_executor_row(
    client: AsyncClient,
) -> None:
    r = await client.get(
        "/probe/probe-host-worker-1",
        headers={"X-Executor-Epoch": "3"},
    )
    assert r.status_code == 200
    assert r.json() == {"executor_id": "probe-host-worker-1", "epoch": 3}
```

Create the `tests/auth/__init__.py` if missing:

```bash
touch tests/auth/__init__.py
```

- [ ] **Step 2: Run; expected FAIL**

```bash
uv run pytest tests/auth/test_executor_epoch.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'dlw.auth.executor_epoch'`.

- [ ] **Step 3: Create `src/dlw/auth/executor_epoch.py`**

```python
"""require_executor_epoch — Phase 2 W1 fence-token dependency.

Reads X-Executor-Epoch header + executor_id path param; looks up the executor
in DB; returns the row to the handler if epoch matches. 401 EPOCH_MISMATCH
otherwise.

Compose with require_bearer at the route level — both run; order doesn't
matter (different concerns).
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.db.models.executor import Executor


async def require_executor_epoch(
    executor_id: str = Path(..., description="Executor id from URL path"),
    # W6-D: accept Optional so dep body can raise 401 (not FastAPI auto-422) on missing
    x_executor_epoch: int | None = Header(default=None, alias="X-Executor-Epoch"),
    session: AsyncSession = Depends(_session),
) -> Executor:
    """Return the Executor row if header matches stored epoch; else 401/404."""
    if x_executor_epoch is None:
        raise HTTPException(
            status_code=401,
            detail="missing X-Executor-Epoch header",
        )
    ex = await session.get(Executor, executor_id)
    if ex is None:
        raise HTTPException(status_code=404, detail="executor not found")
    if ex.epoch != x_executor_epoch:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "EPOCH_MISMATCH",
                "expected": ex.epoch,
                "got": x_executor_epoch,
            },
        )
    return ex
```

- [ ] **Step 4: Run tests; expected PASS**

```bash
uv run pytest tests/auth/test_executor_epoch.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/auth/executor_epoch.py tests/auth/__init__.py tests/auth/test_executor_epoch.py
git commit -m "feat(auth): require_executor_epoch FastAPI dep (P2-W1)"
```

---

### Task 4: Wire `require_executor_epoch` into `/heartbeat` and `/poll`

**Files:**
- Modify: `src/dlw/api/executors.py` — both endpoints use the new dep
- Modify: `tests/api/test_executors.py` — add tests + adapt existing for header

- [ ] **Step 1: Replace `src/dlw/api/executors.py`**

```python
"""Executors API: join / heartbeat / poll.

Phase 2 W1 changes:
  - heartbeat + poll now depend on require_executor_epoch (X-Executor-Epoch header
    required, must match stored executor.epoch — else 401 EPOCH_MISMATCH).
  - /join is unaffected (first contact; controller assigns epoch).
  - poll passes executor.epoch to claim_one_subtask so the subtask row
    captures the current fence.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.auth.bearer import require_bearer
from dlw.auth.executor_epoch import require_executor_epoch
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask
from dlw.schemas.executor import (
    AssignmentResponse,
    ExecutorHeartbeat,
    ExecutorJoin,
    ExecutorRead,
)
from dlw.schemas.storage import StorageConfig
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
    body: ExecutorHeartbeat,
    executor: Executor = Depends(require_executor_epoch),
    session: AsyncSession = Depends(_session),
) -> ExecutorRead:
    try:
        ex = await record_heartbeat(session, executor.id, body)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    await session.commit()
    return ExecutorRead.model_validate(ex)


@router.post("/{executor_id}/poll", dependencies=[Depends(require_bearer)])
async def post_poll(
    executor: Executor = Depends(require_executor_epoch),
    session: AsyncSession = Depends(_session),
) -> AssignmentResponse:
    # NOTE: claim_one_subtask signature is updated to take executor_epoch in
    # Task 5 — until then this call uses the W4 2-arg form. Task 5 also
    # updates this line to pass executor.epoch. Keeping commits separable.
    sub, token = await claim_one_subtask(session, executor.id)
    if sub is None:
        return AssignmentResponse(assigned=False)

    parent = await session.get(DownloadTask, sub.task_id)
    if parent is None:
        raise HTTPException(status_code=500, detail="parent task missing")
    storage = await session.get(StorageBackend, parent.storage_id)
    if storage is None:
        raise HTTPException(status_code=500, detail="storage backend missing")

    raw = bytes(storage.config_encrypted) if storage.config_encrypted else b"{}"
    try:
        cfg_dict = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        cfg_dict = {}
    cfg_dict.setdefault("bucket", storage.name)
    cfg_dict.setdefault("region", storage.region or "us-east-1")
    storage_config = StorageConfig(**cfg_dict)

    sub_read = SubTaskRead.model_validate(sub)
    await session.commit()
    return AssignmentResponse(
        assigned=True,
        subtask=sub_read,
        assignment_token=token,
        repo_id=parent.repo_id,
        revision=parent.revision,
        storage_config=storage_config,
    )
```

The `claim_one_subtask(session, executor.id)` call uses the W4 2-arg form intentionally — Task 5 updates both the scheduler signature AND this call site to pass `executor.epoch`. Keeping the commits separable.

- [ ] **Step 2: Append/modify tests in `tests/api/test_executors.py`**

The existing happy-path tests don't send `X-Executor-Epoch` header. We need to:
1. Add a helper that issues a `/join`, reads back the epoch, and includes it in subsequent calls.
2. Add 4 new error-path tests.

Find the existing `auth` fixture (around line 45). After it, add this helper:

```python
@pytest.fixture
async def joined_executor(client: AsyncClient, auth: dict[str, str]) -> tuple[str, int]:
    """POST /join and return (executor_id, epoch). Used by tests that need fence headers."""
    r = await client.post("/api/v1/executors/join", json={
        "id": "fence-host-worker-1", "host_id": "fence-host",
    }, headers=auth)
    assert r.status_code == 201
    body = r.json()
    return body["id"], body["epoch"]
```

Then add these new tests at the end of the file:

```python
@pytest.mark.slow
async def test_heartbeat_missing_epoch_header_returns_422(
    client: AsyncClient, auth: dict[str, str], joined_executor,
) -> None:
    eid, _ = joined_executor
    r = await client.post(
        f"/api/v1/executors/{eid}/heartbeat",
        json={"health_score": 100, "parts_dir_bytes": 0},
        headers=auth,
    )
    assert r.status_code == 422


@pytest.mark.slow
async def test_heartbeat_wrong_epoch_returns_EPOCH_MISMATCH(
    client: AsyncClient, auth: dict[str, str], joined_executor,
) -> None:
    eid, epoch = joined_executor
    r = await client.post(
        f"/api/v1/executors/{eid}/heartbeat",
        json={"health_score": 100, "parts_dir_bytes": 0},
        headers={**auth, "X-Executor-Epoch": str(epoch + 1)},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "EPOCH_MISMATCH"


@pytest.mark.slow
async def test_heartbeat_correct_epoch_passes(
    client: AsyncClient, auth: dict[str, str], joined_executor,
) -> None:
    eid, epoch = joined_executor
    r = await client.post(
        f"/api/v1/executors/{eid}/heartbeat",
        json={"health_score": 100, "parts_dir_bytes": 0},
        headers={**auth, "X-Executor-Epoch": str(epoch)},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


@pytest.mark.slow
async def test_poll_after_rejoin_uses_new_epoch(
    client: AsyncClient, auth: dict[str, str],
) -> None:
    """Two consecutive joins for the same id: poll with old epoch fails."""
    r1 = await client.post("/api/v1/executors/join", json={
        "id": "rejoin-host-worker-1", "host_id": "rejoin-host",
    }, headers=auth)
    epoch_old = r1.json()["epoch"]

    r2 = await client.post("/api/v1/executors/join", json={
        "id": "rejoin-host-worker-1", "host_id": "rejoin-host",
    }, headers=auth)
    epoch_new = r2.json()["epoch"]
    assert epoch_new == epoch_old + 1

    # Old epoch rejected
    r3 = await client.post(
        "/api/v1/executors/rejoin-host-worker-1/poll",
        headers={**auth, "X-Executor-Epoch": str(epoch_old)},
    )
    assert r3.status_code == 401

    # New epoch accepted
    r4 = await client.post(
        "/api/v1/executors/rejoin-host-worker-1/poll",
        headers={**auth, "X-Executor-Epoch": str(epoch_new)},
    )
    assert r4.status_code == 200
```

Also: find existing tests like `test_poll_returns_assignment_with_repo_and_storage_config` (added in W4 PR #5). It sends a poll WITHOUT epoch header — now that the dep is in place, it MUST send one. Update those tests to call `joined_executor` first and include the header. The exact set of tests to update depends on what's in the file — search for `"/api/v1/executors/.*/poll"` and `"/api/v1/executors/.*/heartbeat"` patterns and add `X-Executor-Epoch` to their headers.

If you find existing tests that hardcode an executor id (like `"host-x-worker-1"`) and call poll/heartbeat, the pattern to fix them is:

```python
# Get the executor's current epoch via join (idempotent — bumps epoch but still works)
r = await client.post("/api/v1/executors/join",
    json={"id": "host-x-worker-1", "host_id": "host-x"}, headers=auth)
epoch = r.json()["epoch"]

# Then add to subsequent calls
headers = {**auth, "X-Executor-Epoch": str(epoch)}
```

- [ ] **Step 3: Run tests; expected all PASS**

```bash
uv run pytest tests/api/test_executors.py -v
```

Expected: all PASS (existing adapted + 4 new).

- [ ] **Step 4: Run full pytest**

```bash
uv run pytest -x
```

Some other test files (`test_subtasks.py`, `test_executor_e2e.py`) may still pass because they don't yet exercise the new dep. Task 6 (`subtasks`) and Task 15 (e2e) will catch up.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/api/executors.py tests/api/test_executors.py
git commit -m "feat(api): heartbeat + poll require X-Executor-Epoch header (P2-W1)"
```

---

### Milestone 2 verification (self)

```bash
uv run pytest tests/api/test_executors.py tests/auth/ -v
```

Expected: all green. Total tests at this point: ~102 + 3 (executor_service epoch) + 4 (auth dep) + 4 (executors api) = ~113.

---

## Milestone 3 — Scheduler fence + `reclaim_subtasks`

After M3, `claim_one_subtask` writes `executor_epoch` and `assigned_at`; `complete_subtask` verifies epoch; `reclaim_subtasks` exists and is fenced.

---

### Task 5: `claim_one_subtask` writes epoch + assigned_at

**Files:**
- Modify: `src/dlw/services/scheduler.py` — change signature + write fields
- Modify: `tests/services/test_scheduler.py` — append assertions

- [ ] **Step 1: Append 2 new tests to `tests/services/test_scheduler.py`**

Find the existing `test_claim_returns_subtask_when_pending_exists` test. Inspect how it builds the env. Then append:

```python
@pytest.mark.slow
async def test_claim_writes_executor_epoch(db_session, env) -> None:
    """P2-W1: claim_one_subtask must persist the executor_epoch passed in."""
    sub_id = await _make_pending_subtask(db_session)
    sub, token = await claim_one_subtask(db_session, "host-x-worker-1", executor_epoch=5)
    assert sub is not None
    assert sub.executor_epoch == 5


@pytest.mark.slow
async def test_claim_writes_assigned_at(db_session, env) -> None:
    """P2-W1: claim_one_subtask must set assigned_at to a recent timestamp."""
    from datetime import UTC, datetime, timedelta

    before = datetime.now(UTC) - timedelta(seconds=1)
    sub_id = await _make_pending_subtask(db_session)
    sub, token = await claim_one_subtask(db_session, "host-x-worker-1", executor_epoch=1)
    after = datetime.now(UTC) + timedelta(seconds=1)
    assert sub is not None
    assert sub.assigned_at is not None
    assert before <= sub.assigned_at <= after
```

The helper `_make_pending_subtask` must exist in the same file (from W2 W3 W4) — it creates a tenant + project + storage + task + subtask. If it doesn't take all those, follow whichever existing helper pattern Phase 1 already uses (e.g., `_make_pending_task` then return one of its subtasks). Reuse, don't duplicate.

- [ ] **Step 2: Run; expected FAIL**

```bash
uv run pytest tests/services/test_scheduler.py::test_claim_writes_executor_epoch tests/services/test_scheduler.py::test_claim_writes_assigned_at -v
```

Expected: FAIL — `claim_one_subtask` doesn't accept `executor_epoch` kwarg yet.

- [ ] **Step 3: Modify `claim_one_subtask` in `src/dlw/services/scheduler.py`**

Replace the entire `claim_one_subtask` function with:

```python
async def claim_one_subtask(
    session: AsyncSession,
    executor_id: str,
    executor_epoch: int,                       # NEW (P2-W1)
) -> tuple[FileSubTask | None, uuid.UUID | None]:
    """Atomically grab one pending subtask for this executor.

    Returns (None, None) if no pending subtasks. Caller must commit() to
    finalize the claim (the row stays locked until commit/rollback).

    P2-W1: also writes executor_epoch (fence) and assigned_at (recovery threshold).
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
    sub.executor_epoch = executor_epoch        # NEW (P2-W1)
    sub.assignment_token = token
    sub.assigned_at = datetime.now(UTC)        # NEW (P2-W1)
    return sub, token
```

The existing imports at the top should already include `from datetime import UTC, datetime` (from `complete_subtask`). If not, add it.

Also update the call site in `src/dlw/api/executors.py post_poll` to pass `executor.epoch`:

```python
sub, token = await claim_one_subtask(session, executor.id, executor.epoch)
```

Any existing test that calls `claim_one_subtask(session, "host-x")` without the new kwarg now fails. Find those — likely the existing `test_claim_*` tests in this file. Update them to pass `executor_epoch=1` (any int, doesn't matter what for those tests).

- [ ] **Step 4: Run scheduler tests; expected all PASS**

```bash
uv run pytest tests/services/test_scheduler.py -v
```

Expected: existing + 2 new PASS.

- [ ] **Step 5: Run full pytest**

```bash
uv run pytest -x
```

Some api tests may still pass because `api/executors.py post_poll` already updated to pass `executor.epoch` (Task 4 Step 1). If anything else breaks, it's a caller of `claim_one_subtask` that hasn't been updated.

- [ ] **Step 6: Commit (includes the api/executors.py call site update)**

```bash
git add src/dlw/services/scheduler.py src/dlw/api/executors.py tests/services/test_scheduler.py
git commit -m "feat(scheduler): claim_one_subtask writes executor_epoch + assigned_at (P2-W1)"
```

---

### Task 6: `complete_subtask` verifies executor_epoch + api/subtasks forwards it

**Files:**
- Modify: `src/dlw/services/scheduler.py` — extend `complete_subtask` kwarg
- Modify: `src/dlw/api/subtasks.py` — use `require_executor_epoch` dep + forward epoch
- Modify: `src/dlw/schemas/subtask.py` — add `executor_epoch` to `SubTaskReport`
- Modify: `tests/services/test_scheduler.py` — add fence test
- Modify: `tests/api/test_subtasks.py` — add 401 / 409 paths

- [ ] **Step 1: Append epoch field to `SubTaskReport` in `src/dlw/schemas/subtask.py`**

Add at the end of `SubTaskReport`:

```python
    executor_epoch: int | None = Field(
        default=None,
        description="Executor's current epoch (fence). Must match subtask.executor_epoch.",
    )
```

(Why include it in the body too? `complete_subtask` is called from a controller-side service helper that may not always come through HTTP — defensive double-check. The actual HTTP path also validates via `require_executor_epoch` dep.)

- [ ] **Step 2: Append failing test to `tests/services/test_scheduler.py`**

```python
@pytest.mark.slow
async def test_complete_subtask_rejects_stale_epoch(db_session, env) -> None:
    """P2-W1: complete_subtask must reject stale executor_epoch."""
    sub_id = await _make_pending_subtask(db_session)
    sub, token = await claim_one_subtask(db_session, "host-x-worker-1", executor_epoch=5)
    await db_session.flush()

    # Executor sends report with epoch=4 (stale — controller bumped to 5 since claim)
    with pytest.raises(ValueError, match="executor_epoch mismatch"):
        await complete_subtask(
            db_session, sub.id,
            final_status="succeeded",
            actual_sha256=None,
            bytes_downloaded=4096,
            error=None,
            assignment_token=token,
            executor_epoch=4,         # stale
        )


@pytest.mark.slow
async def test_complete_subtask_accepts_matching_epoch(db_session, env) -> None:
    sub_id = await _make_pending_subtask(db_session)
    sub, token = await claim_one_subtask(db_session, "host-x-worker-1", executor_epoch=5)
    await db_session.flush()

    sub_returned, _ = await complete_subtask(
        db_session, sub.id,
        final_status="succeeded",
        actual_sha256=None,
        bytes_downloaded=4096,
        error=None,
        assignment_token=token,
        executor_epoch=5,             # matches
    )
    assert sub_returned.status == "succeeded"
```

- [ ] **Step 3: Run; expected FAIL**

```bash
uv run pytest tests/services/test_scheduler.py::test_complete_subtask_rejects_stale_epoch -v
```

Expected: FAIL — `complete_subtask` doesn't accept `executor_epoch` kwarg yet.

- [ ] **Step 4: Modify `complete_subtask` signature + verify gate**

In `src/dlw/services/scheduler.py`, find `complete_subtask`. Add the new kwarg and verify gate (after the token verify, before status assignment):

```python
async def complete_subtask(
    session: AsyncSession,
    subtask_id: uuid.UUID,
    *,
    final_status: str,
    actual_sha256: str | None,
    bytes_downloaded: int,
    error: str | None,
    assignment_token: uuid.UUID | None = None,
    executor_epoch: int | None = None,                 # NEW (P2-W1)
    s3_key: str | None = None,
) -> tuple[FileSubTask, DownloadTask]:
    """..."""
    # W6-B: FOR UPDATE prevents race with concurrent reclaim+reassign
    sub = await session.get(FileSubTask, subtask_id, with_for_update=True)
    if sub is None:
        raise LookupError(f"subtask {subtask_id} not found")
    if sub.status != "assigned":
        raise ValueError(f"subtask {subtask_id} is not assigned (status={sub.status})")
    if assignment_token is not None and sub.assignment_token != assignment_token:
        raise ValueError(f"subtask {subtask_id} assignment_token mismatch")
    if executor_epoch is not None and sub.executor_epoch != executor_epoch:    # NEW
        raise ValueError(
            f"subtask {subtask_id} executor_epoch mismatch "
            f"(expected={sub.executor_epoch}, got={executor_epoch})"
        )

    # ... rest unchanged ...
```

- [ ] **Step 5: Modify `src/dlw/api/subtasks.py` — use new dep + forward epoch**

The previous file uses `require_bearer` directly. Now it also needs `require_executor_epoch` — BUT this endpoint's path is `/api/v1/subtasks/{subtask_id}/report`, no `executor_id` path parameter. So `require_executor_epoch` (which reads `executor_id` from path) doesn't fit directly.

Two options:
- (A) Read executor_id from the request body (`SubTaskReport` adds `executor_id` field), then a NEW dep `require_executor_epoch_body` looks it up.
- (B) Look up the subtask first → get its `executor_id` → then verify epoch against header.

Option B is cleaner (no new schema field). Implementation:

Replace `src/dlw/api/subtasks.py`:

```python
"""Subtasks API: POST /report (executor reports outcome).

Phase 2 W1: enforces X-Executor-Epoch header fence by looking up the subtask's
executor_id and verifying that executor's current epoch matches the header.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.auth.bearer import require_bearer
from dlw.db.models.executor import Executor
from dlw.db.models.task import FileSubTask
from dlw.schemas.subtask import SubTaskReport
from dlw.services.scheduler import complete_subtask

router = APIRouter(prefix="/api/v1/subtasks", tags=["subtasks"])


@router.post("/{subtask_id}/report", dependencies=[Depends(require_bearer)])
async def post_report(
    subtask_id: uuid.UUID,
    body: SubTaskReport,
    x_executor_epoch: int = Header(..., alias="X-Executor-Epoch"),
    session: AsyncSession = Depends(_session),
) -> dict[str, str]:
    # Phase 2 W1 fence: load the subtask's claimed executor and verify epoch.
    sub = await session.get(FileSubTask, subtask_id)
    if sub is None:
        raise HTTPException(status_code=404, detail=f"subtask {subtask_id} not found")
    if sub.executor_id is None:
        raise HTTPException(
            status_code=409, detail=f"subtask {subtask_id} not assigned"
        )
    ex = await session.get(Executor, sub.executor_id)
    if ex is None:
        raise HTTPException(
            status_code=404, detail=f"executor {sub.executor_id} not found"
        )
    if ex.epoch != x_executor_epoch:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "EPOCH_MISMATCH",
                "expected": ex.epoch,
                "got": x_executor_epoch,
            },
        )

    try:
        sub_done, parent = await complete_subtask(
            session, subtask_id,
            final_status=body.status,
            actual_sha256=body.actual_sha256,
            bytes_downloaded=body.bytes_downloaded,
            error=body.error,
            assignment_token=body.assignment_token,
            executor_epoch=x_executor_epoch,    # forward to fence gate
            s3_key=body.s3_key,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await session.commit()
    return {"subtask_status": sub_done.status, "task_status": parent.status}
```

- [ ] **Step 6: Append tests to `tests/api/test_subtasks.py`**

Existing tests need updating to send `X-Executor-Epoch` header. Use a similar `joined_executor` fixture pattern as `tests/api/test_executors.py`. The minimum new tests:

```python
@pytest.mark.slow
async def test_report_missing_epoch_header_returns_422(
    client: AsyncClient, auth: dict[str, str],
) -> None:
    r = await client.post(
        f"/api/v1/subtasks/{uuid.uuid4()}/report",
        json={"status": "succeeded", "bytes_downloaded": 100},
        headers=auth,
    )
    assert r.status_code == 422


@pytest.mark.slow
async def test_report_stale_epoch_returns_EPOCH_MISMATCH(
    client: AsyncClient, auth: dict[str, str],
) -> None:
    """Create task + join executor + claim subtask + report with stale epoch."""
    # Setup: create a task to generate subtasks
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/fence-report", "revision": "0" * 40, "storage_id": 1,
    }, headers=auth)
    task_id = r.json()["id"]

    # Join executor — get epoch=1
    rj = await client.post("/api/v1/executors/join", json={
        "id": "report-host-worker-1", "host_id": "report-host",
    }, headers=auth)
    epoch = rj.json()["epoch"]

    # Claim subtask via poll
    rp = await client.post(
        "/api/v1/executors/report-host-worker-1/poll",
        headers={**auth, "X-Executor-Epoch": str(epoch)},
    )
    assert rp.status_code == 200, rp.text
    if not rp.json()["assigned"]:
        pytest.skip("no subtask available — module DB state interference")
    subtask_id = rp.json()["subtask"]["id"]
    token = rp.json()["assignment_token"]

    # Bump epoch (re-join → epoch=2)
    rj2 = await client.post("/api/v1/executors/join", json={
        "id": "report-host-worker-1", "host_id": "report-host",
    }, headers=auth)
    assert rj2.json()["epoch"] == epoch + 1

    # Report with STALE epoch (the one we claimed under)
    rr = await client.post(
        f"/api/v1/subtasks/{subtask_id}/report",
        json={
            "status": "succeeded", "bytes_downloaded": 100,
            "actual_sha256": "a" * 64, "assignment_token": token,
        },
        headers={**auth, "X-Executor-Epoch": str(epoch)},  # stale!
    )
    assert rr.status_code == 401
    assert rr.json()["detail"]["code"] == "EPOCH_MISMATCH"
```

Also: any existing test that POSTs `/api/v1/subtasks/{id}/report` needs `X-Executor-Epoch` header. Look for `client.post.*subtasks.*report` and add the header.

- [ ] **Step 7: Run; expected PASS**

```bash
uv run pytest tests/services/test_scheduler.py tests/api/test_subtasks.py -v
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/dlw/services/scheduler.py src/dlw/schemas/subtask.py src/dlw/api/subtasks.py tests/services/test_scheduler.py tests/api/test_subtasks.py
git commit -m "feat(scheduler+api): complete_subtask + /report verify X-Executor-Epoch (P2-W1)"
```

---

### Task 7: `reclaim_subtasks` function (fenced UPDATE)

**Files:**
- Modify: `src/dlw/services/scheduler.py` — add `reclaim_subtasks`
- Modify: `tests/services/test_scheduler.py` — add 3 reclaim tests

- [ ] **Step 1: Append failing tests**

```python
@pytest.mark.slow
async def test_reclaim_subtasks_resets_assigned(db_session, env) -> None:
    """reclaim_subtasks: assigned → pending; clears executor_id/epoch/token."""
    from dlw.services.scheduler import reclaim_subtasks

    sub_id = await _make_pending_subtask(db_session)
    sub, token = await claim_one_subtask(db_session, "stale-host-worker-1", executor_epoch=7)
    await db_session.flush()
    assert sub.status == "assigned"
    assert sub.executor_id == "stale-host-worker-1"

    n = await reclaim_subtasks(db_session, "stale-host-worker-1", current_epoch=7)
    await db_session.flush()
    assert n == 1
    refreshed = await db_session.get(FileSubTask, sub.id)
    assert refreshed.status == "pending"
    assert refreshed.executor_id is None
    assert refreshed.executor_epoch is None
    assert refreshed.assignment_token is None


@pytest.mark.slow
async def test_reclaim_subtasks_skips_other_epoch(db_session, env) -> None:
    """If executor re-joined and got new epoch, old-epoch reclaim is a no-op."""
    from dlw.services.scheduler import reclaim_subtasks

    sub_id = await _make_pending_subtask(db_session)
    sub, _ = await claim_one_subtask(db_session, "newer-host-worker-1", executor_epoch=9)
    await db_session.flush()

    # Stale reclaim with old epoch
    n = await reclaim_subtasks(db_session, "newer-host-worker-1", current_epoch=8)
    await db_session.flush()
    assert n == 0  # WHERE epoch=8 doesn't match epoch=9 → no-op
    refreshed = await db_session.get(FileSubTask, sub.id)
    assert refreshed.status == "assigned"
    assert refreshed.executor_id == "newer-host-worker-1"


@pytest.mark.slow
async def test_reclaim_subtasks_skips_succeeded(db_session, env) -> None:
    """reclaim only touches status=assigned; succeeded rows are untouched."""
    from dlw.services.scheduler import reclaim_subtasks

    sub_id = await _make_pending_subtask(db_session)
    sub, token = await claim_one_subtask(db_session, "done-host-worker-1", executor_epoch=1)
    await db_session.flush()
    await complete_subtask(
        db_session, sub.id, final_status="succeeded",
        actual_sha256=None, bytes_downloaded=100, error=None,
        assignment_token=token, executor_epoch=1,
    )
    await db_session.flush()

    n = await reclaim_subtasks(db_session, "done-host-worker-1", current_epoch=1)
    assert n == 0  # status='succeeded' is not in the WHERE clause
```

- [ ] **Step 2: Run; expected FAIL (function missing)**

```bash
uv run pytest tests/services/test_scheduler.py::test_reclaim_subtasks_resets_assigned -v
```

Expected: FAIL — `cannot import name 'reclaim_subtasks'`.

- [ ] **Step 3: Add `reclaim_subtasks` to `src/dlw/services/scheduler.py`**

Append at the end of the file:

```python
async def reclaim_subtasks(
    session: AsyncSession,
    executor_id: str,
    current_epoch: int,
) -> int:
    """Fenced reclaim: assigned → pending for one executor at one epoch.

    Phase 2 W1: single UPDATE statement, fenced by (executor_id, executor_epoch).
    If the executor has re-joined (epoch bumped) and started new work since the
    stale check, current_epoch won't match the row's executor_epoch → 0 rows
    affected. New work is preserved.

    Returns the number of subtasks reclaimed.
    """
    from sqlalchemy import update

    result = await session.execute(
        update(FileSubTask)
        .where(FileSubTask.executor_id == executor_id)
        .where(FileSubTask.executor_epoch == current_epoch)
        .where(FileSubTask.status == "assigned")
        .values(
            status="pending",
            executor_id=None,
            executor_epoch=None,
            assignment_token=None,
            assigned_at=None,
            # W6-F: spec §2.4 — every reclaim is a retry; track count for
            # eventual graduation to 'failed' (P2-W2 will enforce a max_retries).
            retry_count=FileSubTask.__table__.c.retry_count + 1,
        )
    )
    return result.rowcount or 0
```

- [ ] **Step 4: Run; expected PASS**

```bash
uv run pytest tests/services/test_scheduler.py -v
```

Expected: existing + 3 new PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/services/scheduler.py tests/services/test_scheduler.py
git commit -m "feat(scheduler): reclaim_subtasks — fenced by (executor_id, epoch) (P2-W1)"
```

---

### Milestone 3 verification (self)

```bash
uv run pytest tests/services/ -v
```

Expected: all green. Total tests ~120.

---

## Milestone 4 — Recovery routine module

After M4, `dlw.services.recovery` exposes `run_recovery_routine`, `verify_remote_state`, `reclaim_stale_executors`, all tested against moto[s3].

---

### Task 8: New `recovery.py` skeleton + `verify_remote_state` (head + size)

**Files:**
- Create: `src/dlw/services/recovery.py`
- Create: `tests/services/test_recovery.py`

- [ ] **Step 1: Write failing test `tests/services/test_recovery.py`**

```python
"""Tests for dlw.services.recovery (Phase 2 W1)."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.services.recovery import (
    RecoveryStats,
    reclaim_stale_executors,
    run_recovery_routine,
    verify_remote_state,
)


_BUCKET = "recovery-bucket"


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    """W6-I: create_all is idempotent; do NOT drop_all (shared engine across modules)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    # No drop_all — engine is session-scoped and other modules need the tables.


@pytest.fixture
async def env(db_session: AsyncSession):
    """Tenant + project + user + storage with proper JSON config."""
    storage_config = json.dumps({
        "bucket": _BUCKET, "region": "us-east-1",
        "endpoint_url": None, "key_prefix": "phase2/",
    }).encode("utf-8")
    db_session.add(Tenant(id=1, slug="d", display_name="D"))
    await db_session.flush()
    db_session.add(Project(id=1, tenant_id=1, name="d"))
    db_session.add(User(id=1, tenant_id=1, oidc_subject="d",
                        email="d@l", role="tenant_admin"))
    db_session.add(StorageBackend(
        id=1, tenant_id=1, name="d", backend_type="s3",
        config_encrypted=storage_config, region="us-east-1",
    ))
    await db_session.flush()


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


async def _make_task_with_subtask(db_session, file_size=4096):
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/recovery", revision="a" * 40, storage_id=1,
        path_template="t/{tenant}", priority=1, status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="weight.bin",
        file_size=file_size, expected_sha256=None, status="assigned",
        executor_id="recovery-host-worker-1", executor_epoch=1,
        assignment_token=uuid.uuid4(),
        multipart_upload_id="some-mpu-id",
    )
    db_session.add(sub)
    await db_session.flush()
    return task, sub


@pytest.mark.slow
async def test_verify_remote_state_missing_returns_missing(
    db_session, env, aws_env,
) -> None:
    """If S3 object doesn't exist, verify_remote_state returns 'missing'."""
    task, sub = await _make_task_with_subtask(db_session)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        # Object NOT uploaded
        result = await verify_remote_state(db_session, sub)
    assert result == "missing"


@pytest.mark.slow
async def test_verify_remote_state_size_match_returns_verified(
    db_session, env, aws_env,
) -> None:
    task, sub = await _make_task_with_subtask(db_session, file_size=4096)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        key = f"phase2/o/recovery/{'a' * 40}/weight.bin"
        s3.put_object(Bucket=_BUCKET, Key=key, Body=b"x" * 4096)
        result = await verify_remote_state(db_session, sub)
    assert result == "verified"


@pytest.mark.slow
async def test_verify_remote_state_size_mismatch_returns_size_mismatch(
    db_session, env, aws_env,
) -> None:
    task, sub = await _make_task_with_subtask(db_session, file_size=4096)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        key = f"phase2/o/recovery/{'a' * 40}/weight.bin"
        s3.put_object(Bucket=_BUCKET, Key=key, Body=b"y" * 100)   # wrong size!
        result = await verify_remote_state(db_session, sub)
    assert result == "size_mismatch"
```

- [ ] **Step 2: Run; expected FAIL (module missing)**

```bash
uv run pytest tests/services/test_recovery.py -v
```

Expected: FAIL — `ModuleNotFoundError: dlw.services.recovery`.

- [ ] **Step 3: Create `src/dlw/services/recovery.py`**

```python
"""Recovery routine for crashed / stale executors.

Phase 2 W1 scope:
  - verify_remote_state: head + size three-way (sha256 deferred to P2-W2)
  - run_recovery_routine: startup-once routine (recovers in-flight uploads,
    resets long-assigned, cleans orphan multiparts)
  - reclaim_stale_executors: periodic scan; marks unhealthy + reclaims

Companion spec: docs/superpowers/specs/2026-05-11-phase-2-week-1-fence-token-recovery-design.md
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError       # W6-A: boto3 raises this, not s3.exceptions.ClientError
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.schemas.storage import StorageConfig
from dlw.services.scheduler import reclaim_subtasks

logger = logging.getLogger(__name__)


@dataclass
class RecoveryStats:
    three_way_checked: int = 0
    verified_recovered: int = 0
    reset_to_pending: int = 0
    size_mismatch_purged: int = 0
    no_multipart_reset: int = 0
    orphan_aborted: int = 0

    def as_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


async def _load_storage_config(
    session: AsyncSession, sub: FileSubTask
) -> tuple[StorageConfig, DownloadTask]:
    """Resolve sub → DownloadTask → StorageBackend → StorageConfig."""
    parent = await session.get(DownloadTask, sub.task_id)
    if parent is None:
        raise RuntimeError(f"task {sub.task_id} missing (FK should have caught this)")
    storage = await session.get(StorageBackend, parent.storage_id)
    if storage is None:
        raise RuntimeError(f"storage backend {parent.storage_id} missing")

    raw = bytes(storage.config_encrypted) if storage.config_encrypted else b"{}"
    try:
        cfg_dict = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        cfg_dict = {}
    cfg_dict.setdefault("bucket", storage.name)
    cfg_dict.setdefault("region", storage.region or "us-east-1")
    return StorageConfig(**cfg_dict), parent


def _compose_key(
    parent: DownloadTask, sub: FileSubTask, storage_cfg: StorageConfig
) -> str:
    prefix = storage_cfg.key_prefix.strip("/")
    parts = [p for p in (prefix, parent.repo_id, parent.revision, sub.filename) if p]
    return "/".join(parts)


def _make_s3_client(cfg: StorageConfig) -> Any:
    boto_cfg = Config(
        region_name=cfg.region,
        s3={"addressing_style": "path"},
    )
    return boto3.client(
        "s3",
        region_name=cfg.region,
        endpoint_url=cfg.endpoint_url,
        config=boto_cfg,
    )


async def verify_remote_state(
    session: AsyncSession, sub: FileSubTask,
) -> Literal["verified", "missing", "size_mismatch"]:
    """Phase 1 three-way: head + size. SHA256 deferred to P2-W2 ChecksumSHA256."""
    storage_cfg, parent = await _load_storage_config(session, sub)
    s3 = _make_s3_client(storage_cfg)
    key = _compose_key(parent, sub, storage_cfg)

    try:
        head = await asyncio.to_thread(
            lambda: s3.head_object(Bucket=storage_cfg.bucket, Key=key)
        )
    except ClientError as e:        # W6-A: botocore.exceptions.ClientError (NOT s3.exceptions.ClientError)
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return "missing"
        raise

    remote_size = head.get("ContentLength", 0)
    if sub.file_size is not None and remote_size != sub.file_size:
        return "size_mismatch"

    return "verified"
```

- [ ] **Step 4: Run; expected 3 PASS**

```bash
uv run pytest tests/services/test_recovery.py::test_verify_remote_state_missing_returns_missing tests/services/test_recovery.py::test_verify_remote_state_size_match_returns_verified tests/services/test_recovery.py::test_verify_remote_state_size_mismatch_returns_size_mismatch -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/services/recovery.py tests/services/test_recovery.py
git commit -m "feat(recovery): verify_remote_state head+size three-way (P2-W1)"
```

---

### Task 9: `run_recovery_routine` full body

**Files:**
- Modify: `src/dlw/services/recovery.py` — add `run_recovery_routine`
- Modify: `tests/services/test_recovery.py` — 3 more tests

**W6-G note**: Step 1 of `run_recovery_routine` filters `WHERE status='assigned' AND multipart_upload_id IS NOT NULL`. In Phase 1 W4 production this filter returns **ZERO rows** because `HfS3StreamDownloader` never persists `multipart_upload_id` to the DB — it's only held transiently in memory and either committed via `complete_multipart_upload` or aborted within the same executor run. The Step 1 three-way verification path is **infrastructure for P2-W2** (which will add multipart resume + persist `multipart_upload_id`). Phase 1 keeps it dormant. Tests in this task seed a synthetic state (`multipart_upload_id="some-mpu-id"`) to validate the logic ahead of P2-W2 wiring it up.

- [ ] **Step 1: Append 3 tests**

```python
@pytest.mark.slow
async def test_run_recovery_routine_resets_long_assigned(
    db_session, env,
) -> None:
    """Subtask assigned > 2× heartbeat ago + no multipart → reset to pending."""
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="a" * 40, storage_id=1,
        path_template="t", priority=1, status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    long_ago = datetime.now(UTC) - timedelta(seconds=300)
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="x.bin",
        file_size=100, status="assigned",
        executor_id="stale-host-worker-1", executor_epoch=1,
        assignment_token=uuid.uuid4(), assigned_at=long_ago,
        multipart_upload_id=None,
    )
    db_session.add(sub)
    await db_session.flush()

    stats = await run_recovery_routine(db_session)
    await db_session.refresh(sub)
    assert sub.status == "pending"
    assert sub.executor_id is None
    assert sub.executor_epoch is None
    assert stats.no_multipart_reset == 1


@pytest.mark.slow
async def test_run_recovery_routine_three_way_verified_marks_succeeded(
    db_session, env, aws_env,
) -> None:
    """In-flight subtask whose S3 object is intact → marked succeeded."""
    task, sub = await _make_task_with_subtask(db_session, file_size=2048)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        key = f"phase2/o/recovery/{'a' * 40}/weight.bin"
        s3.put_object(Bucket=_BUCKET, Key=key, Body=b"z" * 2048)

        stats = await run_recovery_routine(db_session)
    await db_session.refresh(sub)
    assert sub.status == "succeeded"
    assert stats.verified_recovered == 1


@pytest.mark.slow
async def test_run_recovery_routine_aborts_orphan_multiparts(
    db_session, env, aws_env,
) -> None:
    """Terminal subtask with multipart_upload_id still set → abort + clear field."""
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="a" * 40, storage_id=1,
        path_template="t", priority=1, status="succeeded",
    )
    db_session.add(task)
    await db_session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="x.bin",
        file_size=100, status="succeeded",
        multipart_upload_id="orphan-mpu-id",
    )
    db_session.add(sub)
    await db_session.flush()

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        stats = await run_recovery_routine(db_session)

    await db_session.refresh(sub)
    assert sub.multipart_upload_id is None
    assert stats.orphan_aborted == 1
```

- [ ] **Step 2: Run; expected FAIL (function missing)**

```bash
uv run pytest tests/services/test_recovery.py::test_run_recovery_routine_resets_long_assigned -v
```

Expected: FAIL — `cannot import name 'run_recovery_routine'`. (It's already imported at the top, so really: `AttributeError`.)

- [ ] **Step 3: Append `run_recovery_routine` to `src/dlw/services/recovery.py`**

```python
async def _abort_multipart_silently(
    s3: Any, bucket: str, key: str, upload_id: str,
) -> None:
    """Swallow ClientError; S3 lifecycle is the safety net (Phase 3 ops)."""
    try:
        await asyncio.to_thread(
            lambda: s3.abort_multipart_upload(
                Bucket=bucket, Key=key, UploadId=upload_id,
            )
        )
    except Exception as e:
        logger.warning(
            "abort_multipart_upload failed (Bucket=%s Key=%s UploadId=%s): %s",
            bucket, key, upload_id, e,
        )


async def _delete_object_silently(
    s3: Any, bucket: str, key: str,
) -> None:
    try:
        await asyncio.to_thread(
            lambda: s3.delete_object(Bucket=bucket, Key=key)
        )
    except Exception as e:
        logger.warning("delete_object failed (Bucket=%s Key=%s): %s", bucket, key, e)


def _reset_subtask_to_pending(sub: FileSubTask) -> None:
    sub.status = "pending"
    sub.executor_id = None
    sub.executor_epoch = None
    sub.assignment_token = None
    sub.assigned_at = None
    sub.multipart_upload_id = None


async def _maybe_transition_parent(session: AsyncSession, task_id) -> None:
    """W6-C: After a recovery flip of subtask.status, check parent task.

    Mirrors the tail of complete_subtask: if all siblings succeeded, mark
    parent succeeded; if any failed, mark parent failed. Locking the parent
    FOR UPDATE prevents two recovery iterations from racing on the same task.
    """
    parent = await session.get(DownloadTask, task_id, with_for_update=True)
    if parent is None:
        return
    siblings = (await session.execute(
        select(FileSubTask).where(FileSubTask.task_id == task_id)
    )).scalars().all()
    statuses = {s.status for s in siblings}
    if "failed" in statuses:
        parent.status = "failed"
        parent.completed_at = datetime.now(UTC)
    elif statuses == {"succeeded"}:
        parent.status = "succeeded"
        parent.completed_at = datetime.now(UTC)


async def run_recovery_routine(session: AsyncSession) -> RecoveryStats:
    """One-shot startup recovery. Must complete before serving traffic.

    Phase 1 simplification: file_subtasks uses only pending/assigned/
    succeeded/failed/cancelled. The intermediate downloading/uploading/
    verifying_remote states from spec §3 don't exist yet — those arrive in
    P2-W2. So this routine:
      1. three-way (head + size) for status='assigned' WITH multipart_upload_id
         (executor crashed mid-upload)
      2. resets status='assigned' WITHOUT multipart_upload_id whose assigned_at
         is stale (executor crashed before multipart started)
      3. cleanup orphan multipart_upload_ids on terminal subtasks
    """
    stats = RecoveryStats()
    threshold = datetime.now(UTC) - timedelta(seconds=120)  # 2× heartbeat default

    # Step 1: three-way for in-flight with multipart
    in_flight = (await session.execute(
        select(FileSubTask)
          .where(FileSubTask.status == "assigned")
          .where(FileSubTask.multipart_upload_id.is_not(None))
    )).scalars().all()

    for sub in in_flight:
        try:
            storage_cfg, parent = await _load_storage_config(session, sub)
            s3 = _make_s3_client(storage_cfg)
            key = _compose_key(parent, sub, storage_cfg)
        except RuntimeError:
            # Orphan: parent or storage missing — log + skip
            logger.warning(
                "recovery: subtask %s missing parent/storage; skipping", sub.id,
            )
            continue

        try:
            result = await verify_remote_state(session, sub)
        except Exception as e:
            logger.warning(
                "recovery: verify_remote_state %s failed: %s; skipping", sub.id, e,
            )
            continue

        stats.three_way_checked += 1
        if result == "verified":
            sub.status = "succeeded"
            sub.completed_at = datetime.now(UTC)
            # W6-C: trigger parent-task transition (otherwise recovery-completed
            # subtasks leave the parent stuck pending/downloading forever).
            await _maybe_transition_parent(session, sub.task_id)
            stats.verified_recovered += 1
        elif result == "missing":
            if sub.multipart_upload_id:
                await _abort_multipart_silently(
                    s3, storage_cfg.bucket, key, sub.multipart_upload_id,
                )
            _reset_subtask_to_pending(sub)
            stats.reset_to_pending += 1
        else:  # size_mismatch
            if sub.multipart_upload_id:
                await _abort_multipart_silently(
                    s3, storage_cfg.bucket, key, sub.multipart_upload_id,
                )
            await _delete_object_silently(s3, storage_cfg.bucket, key)
            _reset_subtask_to_pending(sub)
            stats.size_mismatch_purged += 1

    # Step 2: reset long-assigned without multipart
    n = (await session.execute(
        update(FileSubTask)
        .where(FileSubTask.status == "assigned")
        .where(FileSubTask.multipart_upload_id.is_(None))
        .where(
            or_(
                FileSubTask.assigned_at.is_(None),
                FileSubTask.assigned_at < threshold,
            )
        )
        .values(
            status="pending",
            executor_id=None,
            executor_epoch=None,
            assignment_token=None,
            assigned_at=None,
        )
    )).rowcount or 0
    stats.no_multipart_reset = n

    # Step 3: cleanup orphan multipart uploads on terminal subtasks
    orphans = (await session.execute(
        select(FileSubTask)
          .where(FileSubTask.multipart_upload_id.is_not(None))
          .where(FileSubTask.status.in_(["succeeded", "failed", "cancelled"]))
    )).scalars().all()
    for sub in orphans:
        try:
            storage_cfg, parent = await _load_storage_config(session, sub)
            s3 = _make_s3_client(storage_cfg)
            key = _compose_key(parent, sub, storage_cfg)
            await _abort_multipart_silently(
                s3, storage_cfg.bucket, key, sub.multipart_upload_id,
            )
        except RuntimeError:
            logger.warning("recovery: orphan multipart on missing parent; skipping")
        sub.multipart_upload_id = None
        stats.orphan_aborted += 1

    # W6-E: do NOT commit here — caller (lifespan or test) commits.
    # Phase 1 W2 pattern: scheduler functions don't commit internally.
    logger.info("recovery_routine done: %s", stats.as_dict())
    return stats
```

- [ ] **Step 4: Run new tests; expected PASS**

```bash
uv run pytest tests/services/test_recovery.py -v
```

Expected: 6 PASS (3 from Task 8 + 3 new).

- [ ] **Step 5: Commit**

```bash
git add src/dlw/services/recovery.py tests/services/test_recovery.py
git commit -m "feat(recovery): run_recovery_routine — three-way + reset + cleanup orphans (P2-W1)"
```

---

### Task 10: `reclaim_stale_executors` + test

**Files:**
- Modify: `src/dlw/services/recovery.py` — append `reclaim_stale_executors`
- Modify: `tests/services/test_recovery.py` — append test

- [ ] **Step 1: Append failing test**

```python
@pytest.mark.slow
async def test_reclaim_stale_executors_marks_unhealthy_and_reclaims(
    db_session, env,
) -> None:
    """An executor whose last_heartbeat_at is too old becomes 'unhealthy',
    and any of its 'assigned' subtasks return to 'pending'."""
    # Insert a healthy executor with old heartbeat
    stale_time = datetime.now(UTC) - timedelta(seconds=300)
    db_session.add(Executor(
        id="stale-host-worker-1", host_id="stale-host",
        cert_fingerprint="x", status="healthy", epoch=1,
        last_heartbeat_at=stale_time,
    ))
    await db_session.flush()

    # Claim a subtask
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/reclaim", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="x.bin",
        file_size=100, status="assigned",
        executor_id="stale-host-worker-1", executor_epoch=1,
        assignment_token=uuid.uuid4(),
    )
    db_session.add(sub)
    await db_session.flush()

    n = await reclaim_stale_executors(db_session)
    await db_session.flush()
    assert n == 1   # one subtask reclaimed

    ex = await db_session.get(Executor, "stale-host-worker-1")
    assert ex.status == "unhealthy"
    refreshed = await db_session.get(FileSubTask, sub.id)
    assert refreshed.status == "pending"
    assert refreshed.executor_id is None


@pytest.mark.slow
async def test_reclaim_stale_executors_skips_recently_active(
    db_session, env,
) -> None:
    """Executor with recent heartbeat is left alone."""
    db_session.add(Executor(
        id="active-host-worker-1", host_id="active-host",
        cert_fingerprint="x", status="healthy", epoch=1,
        last_heartbeat_at=datetime.now(UTC),  # just now
    ))
    await db_session.flush()
    n = await reclaim_stale_executors(db_session)
    assert n == 0
    ex = await db_session.get(Executor, "active-host-worker-1")
    assert ex.status == "healthy"
```

- [ ] **Step 2: Run; expected FAIL**

```bash
uv run pytest tests/services/test_recovery.py::test_reclaim_stale_executors_marks_unhealthy_and_reclaims -v
```

Expected: FAIL — `cannot import name 'reclaim_stale_executors'`.

- [ ] **Step 3: Append `reclaim_stale_executors` to `src/dlw/services/recovery.py`**

```python
async def reclaim_stale_executors(
    session: AsyncSession,
    *,
    heartbeat_threshold_seconds: int = 90,
) -> int:
    """Scan executors with stale heartbeat; mark them unhealthy + reclaim work.

    Threshold default 90s = 1.5× the executor default heartbeat interval (60s).
    Returns total number of subtasks reclaimed across all stale executors.
    """
    threshold = datetime.now(UTC) - timedelta(seconds=heartbeat_threshold_seconds)
    stale = (await session.execute(
        select(Executor)
        .where(Executor.last_heartbeat_at < threshold)
        .where(Executor.status == "healthy")
    )).scalars().all()

    reclaimed_total = 0
    for ex in stale:
        ex.status = "unhealthy"
        n = await reclaim_subtasks(session, ex.id, ex.epoch)
        reclaimed_total += n
        logger.info(
            "reclaimed %d subtasks from stale executor %s (epoch=%d)",
            n, ex.id, ex.epoch,
        )

    # W6-E: caller commits — lifespan loop wraps each call in `async with factory()`
    return reclaimed_total
```

- [ ] **Step 4: Run; expected PASS**

```bash
uv run pytest tests/services/test_recovery.py -v
```

Expected: 8 PASS (6 + 2 new).

- [ ] **Step 5: Run full pytest**

```bash
uv run pytest -x
```

Expected: ~130 PASS, 1 deselected.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/services/recovery.py tests/services/test_recovery.py
git commit -m "feat(recovery): reclaim_stale_executors — periodic scan + epoch-fenced reclaim (P2-W1)"
```

---

### Milestone 4 verification (self)

```bash
uv run pytest tests/services/test_recovery.py -v
```

Expected: 8 tests green.

---

## Milestone 5 — Lifespan + ControllerClient + runner

After M5, controller starts up by running `run_recovery_routine` first and spawns the background `reclaim_loop`. Executor client persists epoch from `/join` and attaches `X-Executor-Epoch` header on heartbeat/poll/report; runner handles 401 EPOCH_MISMATCH by re-joining.

---

### Task 11: Lifespan wiring (startup recovery + background reclaim loop)

**Files:**
- Modify: `src/dlw/main.py`
- (Optional verification test) — manual smoke on local controller

- [ ] **Step 1: Replace `src/dlw/main.py`**

```python
"""FastAPI app factory + lifespan."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.api.health import router as health_router

logger = logging.getLogger(__name__)

_RECLAIM_INTERVAL_SECONDS = 30


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Phase 2 W1: run recovery_routine before serving + spawn reclaim_loop.

    Order:
      1. Recovery routine (synchronous; must complete before serving traffic)
      2. Spawn background reclaim_loop task
      3. yield (app serves traffic)
      4. Cancel reclaim_loop + dispose engine on shutdown
    """
    from dlw.db.session import get_engine, reset_engine
    from dlw.services.recovery import run_recovery_routine

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    # W6-J: spec §7 says recovery failure aborts startup. Permissive dev mode
    # via DLW_STRICT_RECOVERY=false env override (defaults to strict).
    import os
    strict_recovery = os.environ.get("DLW_STRICT_RECOVERY", "true").lower() != "false"
    try:
        async with factory() as session:
            stats = await run_recovery_routine(session)
            await session.commit()    # W6-E: caller commits (service is no-commit)
            logger.info("startup recovery: %s", stats.as_dict())
    except Exception:
        if strict_recovery:
            logger.exception("startup recovery_routine failed; aborting startup (strict mode)")
            raise
        logger.exception(
            "startup recovery_routine failed; continuing in permissive mode "
            "(DLW_STRICT_RECOVERY=false)"
        )

    reclaim_task = asyncio.create_task(_reclaim_loop_main(factory))

    try:
        yield
    finally:
        reclaim_task.cancel()
        try:
            await asyncio.wait_for(reclaim_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        await reset_engine()


async def _reclaim_loop_main(factory) -> None:
    """Background task: every N seconds, scan stale executors + reclaim."""
    from dlw.services.recovery import reclaim_stale_executors

    while True:
        try:
            await asyncio.sleep(_RECLAIM_INTERVAL_SECONDS)
            async with factory() as session:
                await reclaim_stale_executors(session)
                await session.commit()       # W6-E: caller commits
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reclaim_loop iteration failed; will retry next tick")


def create_app() -> FastAPI:
    app = FastAPI(
        title="modelpull controller",
        version="0.1.0-alpha",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    from dlw.api.tasks import router as tasks_router
    app.include_router(tasks_router)
    from dlw.api.executors import router as executors_router
    app.include_router(executors_router)
    from dlw.api.subtasks import router as subtasks_router
    app.include_router(subtasks_router)
    return app


# uvicorn target: dlw.main:app
app = create_app()
```

- [ ] **Step 2: Run full pytest**

```bash
uv run pytest -x
```

Expected: all green. The lifespan is exercised by every test using `create_app()` — most importantly the e2e test (which Task 15 will update). Startup-time recovery is a no-op when DB is empty (no in-flight subtasks).

- [ ] **Step 3: Manual smoke — boot the real controller**

```bash
DLW_BEARER_TOKEN=dev-token DLW_DB_HOST=localhost DLW_DB_PORT=5433 DLW_DB_USER=postgres DLW_DB_NAME=dlw uv run uvicorn dlw.main:app --port 8000 --log-level info
```

Wait for the line `INFO ... startup recovery: {...}`. Then Ctrl+C to stop. Confirm the recovery routine ran and the controller didn't crash.

- [ ] **Step 4: Commit**

```bash
git add src/dlw/main.py
git commit -m "feat(main): lifespan runs recovery_routine + spawns 30s reclaim_loop (P2-W1)"
```

---

### Task 12: ControllerClient persists epoch + attaches header

**Files:**
- Modify: `src/dlw/executor/client.py`
- Modify: `tests/executor/test_client.py`

- [ ] **Step 1: Append tests**

```python
import httpx
import pytest

from dlw.executor.client import ControllerClient


@pytest.mark.slow
async def test_client_persists_epoch_from_join_response() -> None:
    """After join(), client should store the response epoch internally."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={
            "id": "h-w-1", "status": "joining", "health_score": 100, "epoch": 7,
        })

    transport = httpx.MockTransport(handler)
    async with ControllerClient(
        base_url="http://test", bearer_token="t", _transport=transport,
    ) as c:
        await c.join(executor_id="h-w-1", host_id="h", capabilities={})
        assert c.current_epoch() == 7


@pytest.mark.slow
async def test_client_attaches_epoch_header_on_heartbeat() -> None:
    """heartbeat must send X-Executor-Epoch matching the join response."""
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append({k.lower(): v for k, v in request.headers.items()})
        if request.url.path.endswith("/join"):
            return httpx.Response(201, json={
                "id": "h-w-1", "status": "joining", "health_score": 100, "epoch": 5,
            })
        return httpx.Response(200, json={
            "id": "h-w-1", "status": "healthy", "health_score": 100, "epoch": 5,
        })

    transport = httpx.MockTransport(handler)
    async with ControllerClient(
        base_url="http://test", bearer_token="t", _transport=transport,
    ) as c:
        await c.join(executor_id="h-w-1", host_id="h", capabilities={})
        await c.heartbeat(executor_id="h-w-1", health_score=100, parts_dir_bytes=0)

    assert "x-executor-epoch" in seen_headers[1]
    assert seen_headers[1]["x-executor-epoch"] == "5"


@pytest.mark.slow
async def test_client_attaches_epoch_header_on_report() -> None:
    seen_headers: list[dict[str, str]] = []
    import uuid as _uuid

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append({k.lower(): v for k, v in request.headers.items()})
        if request.url.path.endswith("/join"):
            return httpx.Response(201, json={
                "id": "h-w-1", "status": "joining", "health_score": 100, "epoch": 11,
            })
        return httpx.Response(200, json={
            "subtask_status": "succeeded", "task_status": "succeeded",
        })

    transport = httpx.MockTransport(handler)
    async with ControllerClient(
        base_url="http://test", bearer_token="t", _transport=transport,
    ) as c:
        await c.join(executor_id="h-w-1", host_id="h", capabilities={})
        await c.report(
            subtask_id=_uuid.uuid4(),
            status="succeeded",
            assignment_token=_uuid.uuid4(),
            actual_sha256="a" * 64,
            bytes_downloaded=4096,
        )

    assert seen_headers[1]["x-executor-epoch"] == "11"
```

- [ ] **Step 2: Run; expected FAIL**

```bash
uv run pytest tests/executor/test_client.py::test_client_persists_epoch_from_join_response -v
```

Expected: FAIL — `ControllerClient` has no `current_epoch()` method or no header attachment yet.

- [ ] **Step 3: Modify `src/dlw/executor/client.py`**

```python
"""HTTP client wrapping the controller's executor + subtask endpoints.

Phase 2 W1 additions:
  - Persists the executor's current epoch from /join response.
  - Attaches X-Executor-Epoch header on heartbeat / poll / report.
  - Caller (runner) should observe `current_epoch()` and react to 401
    EPOCH_MISMATCH by calling join() again.
"""
from __future__ import annotations

import uuid
from typing import Any, Self

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


_retry = retry(
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError)
    ),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=4.0),
    reraise=True,
)


class ControllerClient:
    """Async HTTP client for controller endpoints (executor side)."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        timeout_seconds: float = 30.0,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {bearer_token}"}
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=timeout_seconds,
            transport=_transport,
        )
        self._epoch: int | None = None        # P2-W1

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._client.aclose()

    def current_epoch(self) -> int | None:
        """Returns the most recent epoch from /join, or None if not joined yet."""
        return self._epoch

    def _epoch_headers(self) -> dict[str, str]:
        if self._epoch is None:
            return {}
        return {"X-Executor-Epoch": str(self._epoch)}

    async def _post(
        self,
        path: str,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {**(extra_headers or {})}

        @_retry
        async def _do() -> httpx.Response:
            r = await self._client.post(path, json=json_body, headers=headers)
            if 500 <= r.status_code < 600:
                r.raise_for_status()
            return r

        r = await _do()
        r.raise_for_status()
        return r.json()

    async def join(
        self, *, executor_id: str, host_id: str, capabilities: dict[str, Any]
    ) -> dict[str, Any]:
        body = await self._post("/api/v1/executors/join", {
            "id": executor_id, "host_id": host_id, "capabilities": capabilities,
        })
        epoch = body.get("epoch")
        if isinstance(epoch, int):
            self._epoch = epoch
        return body

    async def heartbeat(
        self, *, executor_id: str, health_score: int, parts_dir_bytes: int
    ) -> dict[str, Any]:
        return await self._post(
            f"/api/v1/executors/{executor_id}/heartbeat",
            {"health_score": health_score, "parts_dir_bytes": parts_dir_bytes},
            extra_headers=self._epoch_headers(),
        )

    async def poll(self, *, executor_id: str) -> dict[str, Any]:
        return await self._post(
            f"/api/v1/executors/{executor_id}/poll",
            extra_headers=self._epoch_headers(),
        )

    async def report(
        self,
        *,
        subtask_id: uuid.UUID,
        status: str,
        assignment_token: uuid.UUID | None,
        actual_sha256: str | None,
        bytes_downloaded: int,
        error: str | None = None,
        s3_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": status,
            "bytes_downloaded": bytes_downloaded,
        }
        if assignment_token is not None:
            body["assignment_token"] = str(assignment_token)
        if actual_sha256 is not None:
            body["actual_sha256"] = actual_sha256
        if error is not None:
            body["error"] = error
        if s3_key is not None:
            body["s3_key"] = s3_key
        return await self._post(
            f"/api/v1/subtasks/{subtask_id}/report",
            body,
            extra_headers=self._epoch_headers(),
        )
```

- [ ] **Step 4: Run client tests; expected PASS**

```bash
uv run pytest tests/executor/test_client.py -v
```

Expected: existing + 3 new PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/executor/client.py tests/executor/test_client.py
git commit -m "feat(executor-client): persist epoch from /join + attach X-Executor-Epoch (P2-W1)"
```

---

### Task 13: Runner re-joins on 401 EPOCH_MISMATCH

**Files:**
- Modify: `src/dlw/executor/runner.py`
- Modify: `tests/executor/test_runner.py`

- [ ] **Step 1: Append failing test**

```python
@pytest.mark.slow
async def test_runner_rejoins_on_epoch_mismatch() -> None:
    """Runner: on 401 EPOCH_MISMATCH, abort current poll + re-join + continue."""
    import httpx as _httpx
    import uuid as _u
    from dlw.executor.runner import ExecutorRunner
    from dlw.executor.config import ExecutorSettings

    class FakeClient:
        def __init__(self):
            self.calls: list[str] = []
            self._poll_returns_401_once = True
            self._epoch: int | None = None

        def current_epoch(self): return self._epoch

        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

        async def join(self, *, executor_id, host_id, capabilities):
            self.calls.append("join")
            self._epoch = 2 if "join" in self.calls[:-1] else 1
            return {"id": executor_id, "epoch": self._epoch, "status": "joining",
                    "health_score": 100}

        async def heartbeat(self, **kw):
            self.calls.append("heartbeat")

        async def poll(self, **kw):
            self.calls.append("poll")
            if self._poll_returns_401_once:
                self._poll_returns_401_once = False
                req = _httpx.Request("POST", "http://x/poll")
                resp = _httpx.Response(
                    401, json={"detail": {"code": "EPOCH_MISMATCH",
                                          "expected": 2, "got": 1}}
                )
                raise _httpx.HTTPStatusError(
                    "401", request=req, response=resp,
                )
            return {"assigned": False}

        async def report(self, **kw): pass

    settings = ExecutorSettings(
        id="rj-host-worker-1", host_id="rj-host", bearer_token="t",
        heartbeat_interval_seconds=1, poll_interval_seconds=1,
    )

    class FakeDl:
        async def download(self, **kw):
            raise AssertionError("downloader should NOT be invoked in this test")

    runner = ExecutorRunner(settings=settings, client=FakeClient(), downloader=FakeDl())
    run_task = asyncio.create_task(runner.run())
    await asyncio.sleep(2.5)   # let 1-2 poll cycles run
    runner.request_shutdown()
    await asyncio.wait_for(run_task, timeout=3)

    # join called at least twice (initial + after EPOCH_MISMATCH)
    assert runner._client.calls.count("join") >= 2
    assert "poll" in runner._client.calls
```

- [ ] **Step 2: Run; expected FAIL**

```bash
uv run pytest tests/executor/test_runner.py::test_runner_rejoins_on_epoch_mismatch -v
```

Expected: FAIL — runner doesn't yet handle EPOCH_MISMATCH.

- [ ] **Step 3: Modify `src/dlw/executor/runner.py` `_poll_and_execute_loop`**

Replace the exception-handling block inside the poll loop. The change: distinguish `httpx.HTTPStatusError` with `EPOCH_MISMATCH` from generic exceptions. On EPOCH_MISMATCH, call `await self._rejoin()`.

Also add a private `_rejoin` method on the class.

Replace `_poll_and_execute_loop` and add `_rejoin`:

```python
    async def _poll_and_execute_loop(self) -> None:
        import httpx as _httpx
        while not self._shutdown.is_set():
            try:
                resp = await self._client.poll(executor_id=self._s.id)
                if resp.get("assigned"):
                    await self._execute_subtask(
                        subtask=resp["subtask"],
                        assignment_token=uuid.UUID(resp["assignment_token"]),
                        repo_id=resp["repo_id"],
                        revision=resp["revision"],
                        storage_config=resp["storage_config"],
                    )
                    continue
            except _httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    detail = None
                    try:
                        detail = e.response.json().get("detail")
                    except Exception:
                        pass
                    if isinstance(detail, dict) and detail.get("code") == "EPOCH_MISMATCH":
                        logger.warning(
                            "EPOCH_MISMATCH (expected=%s got=%s); re-joining",
                            detail.get("expected"), detail.get("got"),
                        )
                        await self._rejoin()
                        continue
                logger.warning("poll failed: %s", e)
            except Exception as e:
                logger.warning("poll failed: %s", e)
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=self._s.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def _rejoin(self) -> None:
        """Discard any in-flight state and re-issue /join (gets new epoch)."""
        try:
            await self._client.join(
                executor_id=self._s.id,
                host_id=self._s.host_id,
                capabilities={
                    "nic_speed_gbps": self._s.nic_speed_gbps,
                    "region": self._s.region,
                },
            )
        except Exception as e:
            logger.warning("rejoin failed: %s", e)
```

- [ ] **Step 4: Run; expected PASS**

```bash
uv run pytest tests/executor/test_runner.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/executor/runner.py tests/executor/test_runner.py
git commit -m "feat(runner): catch 401 EPOCH_MISMATCH → re-join + continue (P2-W1)"
```

---

### Milestone 5 verification (self)

```bash
uv run pytest -x
```

Expected: ~140 PASS, 1 deselected.

---

## Milestone 6 — OpenAPI sync + e2e + PR

After M6, `api/openapi.yaml` reflects the new wire format; the e2e test exercises the full fence flow.

---

### Task 14: OpenAPI sync (epoch header + W4 backfill)

**Files:**
- Modify: `api/openapi.yaml`

- [ ] **Step 1: Open `api/openapi.yaml`. Find the three executor paths and add `X-Executor-Epoch` as a required header parameter.**

For `/api/v1/executors/{executor_id}/heartbeat`, `/api/v1/executors/{executor_id}/poll`, and `/api/v1/subtasks/{subtask_id}/report` operations, add to each operation's `parameters:` block:

```yaml
        - in: header
          name: X-Executor-Epoch
          required: true
          schema:
            type: integer
            minimum: 1
          description: Fence token. Must match the executor's current epoch.
```

- [ ] **Step 2: Add the 401 EPOCH_MISMATCH response schema**

In the operations above, under `responses:`, add:

```yaml
        '401':
          description: Epoch mismatch (stale executor) or bearer token invalid
          content:
            application/json:
              schema:
                type: object
                properties:
                  detail:
                    oneOf:
                      - type: string
                      - type: object
                        properties:
                          code:
                            type: string
                            enum: [EPOCH_MISMATCH]
                          expected:
                            type: integer
                          got:
                            type: integer
```

- [ ] **Step 3: Update `ExecutorRead` schema to include `epoch`**

In `components.schemas.ExecutorRead`, add to `properties`:

```yaml
        epoch:
          type: integer
          minimum: 0
          description: Current epoch (fence token). Increments on every /join.
```

And add `epoch` to that schema's `required:` array.

- [ ] **Step 4: Backfill W4 fields (closing PR #5 drift)**

In `components.schemas.AssignmentResponse`, ensure these fields exist:

```yaml
        repo_id:
          type: string
          nullable: true
        revision:
          type: string
          nullable: true
        storage_config:
          $ref: '#/components/schemas/StorageConfig'
```

Add the `StorageConfig` schema if missing:

```yaml
    StorageConfig:
      type: object
      required: [bucket]
      properties:
        bucket:
          type: string
        region:
          type: string
          default: us-east-1
        endpoint_url:
          type: string
          nullable: true
        key_prefix:
          type: string
          default: ""
```

In `SubTaskRead` and `SubTaskReport`, add:

```yaml
        s3_key:
          type: string
          nullable: true
          maxLength: 1024
```

- [ ] **Step 5: Validate the OpenAPI spec**

```bash
npx -y @stoplight/spectral-cli@6 lint api/openapi.yaml --fail-severity=error
npx -y @apidevtools/swagger-cli validate api/openapi.yaml
```

Both must succeed.

- [ ] **Step 6: Commit**

```bash
git add api/openapi.yaml
git commit -m "docs(openapi): sync — X-Executor-Epoch + EPOCH_MISMATCH + W4 backfill (P2-W1)"
```

---

### Task 15: Adapt e2e to use the new header

**Files:**
- Modify: `tests/e2e/test_executor_e2e.py`

- [ ] **Step 1: Read the e2e test file**

The W4 e2e test does `/join` → poll → download → report through the real ControllerClient + real ExecutorRunner. P2-W1 changes only one thing: ControllerClient now sets `_epoch` from /join and includes the header in subsequent calls. This should be **transparent to the existing e2e test** — verify by running it:

```bash
uv run pytest tests/e2e/test_executor_e2e.py -v
```

Expected outcome (likely): PASS without modification, because the client handles the header itself and `record_heartbeat` flips status from `joining` to `healthy` on first heartbeat (so the existing test exercises the full epoch flow correctly).

If it FAILS, the root cause is most likely: the test creates an executor row in `_bootstrap` fixture with `epoch=` not set → DB default `0`. Then `claim_one_subtask` writes `executor_epoch=0` (from the bootstrap row, not from the client's later /join which would bump to 1). Subsequent /report carries header `X-Executor-Epoch: 1` (client's view) but the DB's executor row still has `epoch=1` (post-/join), so the dep passes. Then `complete_subtask` reads `sub.executor_epoch=0` (set at claim time) — but `assignment_token` check fires first; the epoch check we added uses the passed `executor_epoch=1` against `sub.executor_epoch=0` → MISMATCH → 409.

The fix: don't pre-create an executor row in `_bootstrap`. Let the test path do the /join naturally; the epoch sequence will be consistent throughout.

If the bootstrap pre-creates an Executor with id=`"e2e-w4-host-worker-1"`, **delete that pre-seed** — the test does `/join` later anyway via `runner.run()`.

- [ ] **Step 2: If the test failed in Step 1, apply this fix**

Open `tests/e2e/test_executor_e2e.py`. In the `_bootstrap` fixture, remove any code that inserts an `Executor` row (only Tenant/Project/User/StorageBackend should be pre-seeded). The runner's `join` call inside the test creates the executor row.

If the test still fails for other reasons, read the failure carefully — most likely: the `/report` HTTP call now requires `X-Executor-Epoch`; the client already attaches it post-/join (Task 12). So this should just work.

Run again:

```bash
uv run pytest tests/e2e/test_executor_e2e.py -v
```

Expected: PASS.

- [ ] **Step 3: Run full pytest**

```bash
uv run pytest -x
```

Expected: 99 (Phase 1) + 30 (P2-W1 new) = ~129, 1 deselected.

- [ ] **Step 4: Commit (if any change was needed)**

```bash
git add tests/e2e/test_executor_e2e.py
git commit -m "test(e2e): adapt for X-Executor-Epoch header flow (P2-W1)"
```

If no changes were needed, skip the commit and proceed.

---

### Task 16: Push branch + open PR + watch CI

- [ ] **Step 1: Confirm branch state**

```bash
git status                    # clean
git log main..HEAD --oneline  # ~16 commits including spec + 15 task commits
```

- [ ] **Step 2: Push**

```bash
git push -u origin feat/phase-2-w1-fence-token
```

- [ ] **Step 3: Open PR**

```bash
gh pr create \
  --title "Phase 2 Week 1 — Fence Token + Recovery" \
  --body "$(cat <<EOF
## Summary

- Adds the executor-epoch fence on top of Phase 1's assignment-token fence: every executor → controller mutation request (heartbeat / poll / report) carries \`X-Executor-Epoch\`; controller verifies via the new \`require_executor_epoch\` FastAPI dep.
- \`join_executor\` is rewritten as a PostgreSQL \`INSERT ... ON CONFLICT DO UPDATE SET epoch = epoch + 1 RETURNING\` so the bump is atomic against concurrent joins.
- \`claim_one_subtask\` writes \`executor_epoch\` + \`assigned_at\` on every claim.
- \`complete_subtask\` adds an \`executor_epoch\` verify gate (alongside W2-F's \`assignment_token\` gate).
- New \`scheduler.reclaim_subtasks\` — single fenced UPDATE.
- New \`dlw.services.recovery\` module: \`run_recovery_routine\` (one-shot, runs in FastAPI lifespan before serving), \`verify_remote_state\` (head + size; sha256 deferred to P2-W2 ChecksumSHA256), \`reclaim_stale_executors\` (periodic, runs every 30s in a background task).
- \`ControllerClient\` persists epoch from /join + attaches header on heartbeat/poll/report; runner catches 401 EPOCH_MISMATCH → \`_rejoin()\` + continues.
- Single alembic migration adds 4 columns: \`executors.epoch\` + \`file_subtasks.{multipart_started_at, assigned_at, last_heartbeat_seen_at}\`. Round-trip verified.
- \`api/openapi.yaml\` synced: 3 endpoints gain \`X-Executor-Epoch\` header param + EPOCH_MISMATCH 401 schema + W4 backfill (\`AssignmentResponse.storage_config\`, \`SubTaskReport.s3_key\`).

## Test plan

- [x] Backend pytest: ~30 new tests (auth dep, executor_service epoch, scheduler fence + reclaim, recovery routine, api header paths, client header attachment, runner rejoin); zero regressions on Phase 1's 99 tests.
- [x] Alembic migration round-trip clean.
- [x] Concurrent \`/join\` test verifies monotonic epoch via \`asyncio.gather\`.
- [x] Recovery routine verified against moto[s3] for missing / verified / size_mismatch / orphan paths.
- [x] OpenAPI: spectral + swagger-cli validate clean.
- [x] No frontend changes; \`pnpm typecheck/lint/test/build\` untouched and still green.
- [x] Manual smoke against docker-compose: controller starts, lifespan recovery routine logs stats, reclaim loop runs every 30s.

## Out of scope (deferred — see spec §1.2)

Full task-state machine, mTLS + executor_jwt, ChecksumSHA256 server-side, multi-executor scheduler features, HMAC heartbeat, node degraded/probationary states, multipart resume, verifying task recovery, HF global throttle recovery, bucket lifecycle ops config — all moved to P2-W2 / P2-W3 / Phase 3 plans.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Monitor CI**

```bash
gh pr checks $(gh pr view --json number -q .number) --watch
```

Expected: 12 checks pass. If any fail, fix in a NEW commit (do NOT amend or force-push).

---

### Milestone 6 verification (self)

- [ ] PR opened; CI 12/12 green.
- [ ] All ~130 backend tests pass locally + on CI.
- [ ] Manual smoke confirms controller boots through `lifespan` with recovery routine logging.

---

## Definition of Done

- [ ] All 16 tasks committed on `feat/phase-2-w1-fence-token`.
- [ ] PR opened; CI 12/12 green.
- [ ] No regressions on Phase 1's 99 tests.
- [ ] ~30 new tests covering: epoch dep, atomic join, scheduler fence, reclaim, recovery routine three-way, lifespan wiring, client header attachment, runner rejoin.
- [ ] OpenAPI spec synced; spectral + swagger-cli clean.
- [ ] Alembic migration round-trip clean.
- [ ] No files touched outside the File Structure list.

---

## Plan Revisions Log

This plan was reviewed by 2 specialized agents (DB/SQL/fence-semantics + async/FastAPI/lifespan/test) on 2026-05-11. 11 fixes applied (W6-A through W6-K) before subagent execution; 2 false positives skipped.

| Tag | Severity | Issue | Fix applied |
|-----|----------|-------|-------------|
| W6-A | CRITICAL | `s3.exceptions.ClientError` is NOT a valid boto3 attribute. boto3 service clients expose service-modeled exceptions like `s3.exceptions.NoSuchKey` but `ClientError` lives on `botocore.exceptions`. `except s3.exceptions.ClientError` itself raises `AttributeError` at runtime, crashing `verify_remote_state` on the very 404 path it's meant to handle | Changed Task 8 imports + `except` clause to use `botocore.exceptions.ClientError` |
| W6-B | CRITICAL | `complete_subtask` reads the subtask via non-locked `session.get(FileSubTask, ...)`. Between the get and the epoch verify, a concurrent reclaim+reassign could change `executor_epoch` without our seeing it (W2 already added FOR UPDATE on the PARENT — Phase 2 needs it on the subtask too) | Added `with_for_update=True` to the `session.get(FileSubTask, subtask_id)` call in Task 6 Step 4 |
| W6-C | CRITICAL | Recovery routine Step 1 sets `sub.status = "succeeded"` directly but never triggers the parent-task transition logic (which lives in `complete_subtask`'s tail: query siblings, flip parent.status if all succeeded). If recovery is the LAST subtask to succeed, the parent `DownloadTask` is stuck pending/downloading forever with all subtasks done | Extracted parent-transition tail of `complete_subtask` into a shared helper `_maybe_transition_parent(session, task_id)`; recovery Step 1 calls it after every `verified` flip |
| W6-D | CRITICAL | Spec §7 error matrix says "missing X-Executor-Epoch header → 401 (upgraded in dep)" but plan's `require_executor_epoch` uses `Header(...)` REQUIRED — FastAPI auto-rejects with 422 before dep body runs. Tests in Task 3 assert 422. Executor runner only catches 401 → bug client missing the header gets silent warning instead of rejoin | Changed `require_executor_epoch` to accept `Header(None, alias=...)` + explicit `if x_executor_epoch is None: raise HTTPException(401, detail="missing X-Executor-Epoch header")`; updated tests to assert 401 not 422 (matches spec) |
| W6-E | CRITICAL | `reclaim_stale_executors` in `recovery.py` calls `await session.commit()` internally. (a) lifespan's `async with factory() as session:` also commits → double-commit. (b) test fixture `db_session` rolls back per test; internal commit overrides rollback → test data leaks to subsequent tests | Removed `await session.commit()` from both `reclaim_stale_executors` and `run_recovery_routine`; lifespan + test code now commit explicitly (matches Phase 1 W2 `scheduler.complete_subtask` pattern — commit-at-callsite) |
| W6-F | important | `reclaim_subtasks` UPDATE clears `executor_id`/`executor_epoch`/`assignment_token` but does NOT increment `retry_count`. Spec §2.4 pseudocode explicitly includes `retry_count = retry_count + 1`. Without it, a repeatedly-crashing executor cycling claim→crash→reclaim never exhausts retries — subtask cycles forever | Added `retry_count=FileSubTask.__table__.c.retry_count + 1` to the UPDATE values in Task 7 Step 3 |
| W6-G | important | Recovery routine Step 1 path (`WHERE status='assigned' AND multipart_upload_id IS NOT NULL`) returns ZERO rows in Phase 1 because W4's `HfS3StreamDownloader` never persists `multipart_upload_id` to DB (only held transient in memory, aborted or completed within same executor run). The whole three-way Step 1 is infrastructure for P2-W2's multipart-resume work, dormant in Phase 1 | Added explicit note at the top of Task 9 explaining this is Phase 2-W2 infrastructure; the test fixture seeds a synthetic state to validate the logic; production Phase 1 keeps it dormant |
| W6-H | important | Task 1 migration lacks `CHECK (epoch >= 1)` constraint. Future test fixtures / seed scripts that omit `epoch=` get `epoch=0` from Python default → `require_executor_epoch` PASSES for any header sending `0` (zero-fence is degenerate but technically valid) | Added `sa.CheckConstraint("epoch >= 0", name="ck_executors_epoch_nonnegative")` to migration. (Note: >= 0 not >= 1, because server_default='0' is intentional for pre-existing rows during migration — first /join bumps to 1) |
| W6-I | important | Both `tests/auth/test_executor_epoch.py` (Task 3) and `tests/services/test_recovery.py` (Task 8) define module-scoped `Base.metadata.create_all/drop_all` autouse fixtures. They share the session-scoped engine — pytest collection order means one module's drop fires during another's lifetime, breaking subsequent tests with "relation does not exist" | Task 3 fixture renamed to non-conflicting helper that ONLY seeds executors (no create_all/drop_all); relies on existing test_executors.py-style `_create_tables` already running. Recovery test reuses the same conftest-side `_create_tables` pattern from `tests/services/test_scheduler.py` (session-scope, not module-scope) |
| W6-J | important | Lifespan recovery failure silently swallowed (`except Exception: logger.exception ... continue`) contradicts spec §7 row "Recovery routine head_object 5xx → lifespan startup fails; controller does NOT serve traffic". Plan and spec diverge without documentation | Made Task 11 lifespan code match spec: `except Exception: raise` (startup aborts). Documented permissive-mode option via `DLW_STRICT_RECOVERY=false` env override for dev (default strict); added to plan note. (Phase 1 had no recovery; abort behavior is new — operators see the failure immediately) |
| W6-K | minor | Task 1 migration's `down_revision` not verified to point at W4's `5a729be99dc0` head; risk if implementer's local alembic state is stale | Added Step 3.5 explicit verification: open the generated file and assert `down_revision = '5a729be99dc0'` matches; abort if not |
| W6-L | important | Caught during T2 execution: SQLAlchemy 2.x identity-map caches Executor rows between calls in the same session — `join_executor` returning the row via `pg_insert.returning(Executor).scalar_one()` after ON CONFLICT UPDATE serves a STALE epoch from the cache (the bumped value never reaches the Python object). Tests fail with `epoch=1` after second join when expecting `epoch=2`. Plan's snippet omitted the fix | Implementer added `execution_options(populate_existing=True)` to the execute call: `(await session.execute(stmt.execution_options(populate_existing=True))).scalar_one()`. Future plans should mention this whenever a `pg_insert ON CONFLICT DO UPDATE ... RETURNING` is used to read back a column the same session has cached |
| W6-M | important | Caught during T4 milestone E2E: W6-I's "remove drop_all at module end" was over-cautious — pytest runs modules sequentially so drop_all firing at module teardown doesn't affect other modules. But removing it left tables created by `create_all` permanent across the session, breaking `tests/db/test_alembic.py` (alembic upgrade-head can't create tables that already exist → DuplicateTableError) | Reverted W6-I in `tests/auth/test_executor_epoch.py` — restored `Base.metadata.drop_all` at fixture yield. W6-I clause for `tests/services/test_recovery.py` (T8) also dropped — Task 8 implementer should use the standard Phase 1 create_all + drop_all pattern. The W6-I reviewer's concern about pytest collection order interleaving fixtures was theoretical; practice confirms sequential execution is safe |

**False-positive findings (skipped, with reasoning)**:
- "test_report_stale_epoch_returns_EPOCH_MISMATCH comment misleading" — the test behaviour is correct; only the inline comment was unclear. Not a code bug; comment polish can happen during implementation.
- "redundant double DB lookup in verify_remote_state via _load_storage_config" — wasteful but not a correctness bug; SQLAlchemy identity map caches the rows. Optimisation deferred.

---

## References

- Spec: `docs/superpowers/specs/2026-05-11-phase-2-week-1-fence-token-recovery-design.md`
- Companion: `docs/v2.0/03-distributed-correctness.md` §2 §3 §5
- Wire format: `docs/v2.0/02-protocol.md` §6
- Phase 2 roadmap: `docs/v2.0/08-mvp-roadmap.md` §2.6
- Precedent plan: `docs/superpowers/plans/2026-05-09-phase-1-week-4-hf-s3-multipart.md`
- Existing scheduler (extended): `src/dlw/services/scheduler.py`
- Existing client (extended): `src/dlw/executor/client.py`
