# Phase 2 Week 2a — Multi-Executor-Aware Scheduler + Executor State Machine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the W2a half of `docs/v2.0/08-mvp-roadmap.md` §2.6 — D1-2 (multi-executor-aware scheduler with reverse host-affinity) + D3 (4+1 state machine with `executor_status_history` audit). W2b (cancelling + paused_* + chunk-level downloader) is a separate spec/plan.

**Architecture:** Introduce a single state-machine service module (`src/dlw/services/state_machine.py`) that owns every write to `Executor.status` and synchronously appends an `executor_status_history` row on each transition. A CI lint forbids direct `ex.status = ...` writes elsewhere. The W1 lifespan loop is rewired from `reclaim_stale_executors` to `sweep_executor_timeouts`, which transitions stale executors through the state machine and reclaims subtasks only on entry to `suspect` / `faulty`. The scheduler's `claim_one_subtask` gains executor-status eligibility and the reverse side of INVARIANT D-10 (no two executors on the same host hold sibling subtasks of the same file).

**Tech Stack:** SQLAlchemy 2.x async + alembic + FastAPI lifespan + pytest + Python stdlib `ast` for the lint. No new runtime or dev deps; no new CI jobs (the new lint is added as a step inside the existing `invariant_lint` workflow job).

**Scope:** 1 schema change (one new table; no column adds), 10 implementation tasks across 4 milestones. Branch `feat/phase-2-w2a-scheduler-state-machine` exists with the spec committed (commits `45d0684` + `0472aef`). Companion spec: `docs/superpowers/specs/2026-05-13-phase-2-w2a-scheduler-state-machine-design.md`.

**Pre-flight:** Phase 2 W1 merged into `main` at `a999381`. Local PG 18 running on `localhost:5433`. `uv` 0.11.9. PostgreSQL client tools in PATH. Spec approved by the user 2026-05-13. Existing Phase-1+W1 pytest suite green.

**Out-of-scope (deferred — see spec §1.2):** cancelling / paused_external / paused_disk_full subtask states (W2b); chunk-level downloader (W2b); probationary / draining executor states (Phase 3); priority preemption (v2.1); heartbeat HMAC (Phase 2 W3); active/standby (Phase 2 W3); P-001 baseline (after W3).

---

## File Structure

After this plan:

```
modelpull/
├── src/dlw/
│   ├── db/models/
│   │   ├── __init__.py                          MODIFY (+ExecutorStatusHistory import)
│   │   └── executor_status_history.py           NEW
│   ├── services/
│   │   ├── state_machine.py                     NEW (transition_executor + Transition + tunables)
│   │   ├── executor_service.py                  MODIFY (record_heartbeat re-routes status writes)
│   │   ├── scheduler.py                         MODIFY (claim_one_subtask host-affinity, complete_subtask tail)
│   │   └── recovery.py                          MODIFY (sweep_executor_timeouts replaces reclaim_stale_executors)
│   ├── api/
│   │   └── executors.py                         (unchanged — record_heartbeat already wraps state mutation)
│   ├── alembic/versions/
│   │   └── <rev>_p2w2a_state_machine.py         NEW
│   └── main.py                                  MODIFY (rename loop/constant; call sweep_executor_timeouts)
├── tests/
│   ├── services/
│   │   ├── test_state_machine.py                NEW (6 cases)
│   │   ├── test_sweeper.py                      NEW (1 case)
│   │   ├── test_scheduler_host_affinity.py      NEW (2 cases)
│   │   └── test_recovery.py                     MODIFY (rename two W1 cases; update expectations)
│   ├── api/test_executors.py                    (unchanged — record_heartbeat behavior equivalent)
│   ├── lint/
│   │   ├── __init__.py                          NEW (empty)
│   │   ├── fixtures/
│   │   │   ├── __init__.py                      NEW (empty)
│   │   │   └── bad_executor_status_write.py     NEW (fixture for self-test)
│   │   └── test_no_direct_status_write.py       NEW (1 case)
│   └── conftest.py                              (unchanged)
├── tools/
│   ├── lint_no_direct_status_write.py           NEW
│   └── lint_invariants.py                       MODIFY (+executor_status_value_domain check)
├── .github/workflows/ci.yml                     MODIFY (+lint step inside invariant_lint job)
└── api/openapi.yaml                             MODIFY (ExecutorRead.status enum)
```

**Why this structure:** state-machine logic is isolated in one module owned by one function; storage of the audit trail is a single narrow table; the scheduler gains host-affinity without growing past ~200 lines; the lint sits next to the existing `lint_invariants.py`. No file balloons.

---

## Pre-flight checks

- [ ] On branch `feat/phase-2-w2a-scheduler-state-machine`, spec committed (`git log --oneline -2` shows `0472aef` + `45d0684`).
- [ ] `main` at `a999381` (PR #7 merge): `git log main --oneline -1`.
- [ ] PG running on `localhost:5433` (`pg_isready -h localhost -p 5433`).
- [ ] `dlw` database exists with current head migration (`uv run alembic current` shows `8040f6a49655`).
- [ ] Existing pytest suite green (`uv run pytest -x` → all pass, 1 deselected).
- [ ] `uv --version` ≥ 0.11.9.

---

## Milestone 1 — Schema + state-machine core

After M1, the migration applies cleanly, the `ExecutorStatusHistory` model is registered with `Base.metadata`, and `transition_executor()` implements all six rules from spec §6 with 6 passing unit tests.

---

### Task 1: `ExecutorStatusHistory` ORM model + registration

**Files:**
- Create: `src/dlw/db/models/executor_status_history.py`
- Modify: `src/dlw/db/models/__init__.py`

- [ ] **Step 1: Create the model file**

```python
"""ExecutorStatusHistory — durable audit of every Executor.status transition (W2a §3.2)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class ExecutorStatusHistory(Base):
    __tablename__ = "executor_status_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    executor_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("executors.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(256), nullable=False)
    transitioned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 'metadata' is reserved on the SQLAlchemy Base; use a trailing-underscore Python
    # attr and an explicit column-name override to keep the DB column named cleanly.
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, nullable=False,
    )
```

- [ ] **Step 2: Register the model in `src/dlw/db/models/__init__.py`**

Open the file. It currently looks like:

```python
"""ORM models. Importing this module also registers them with Base.metadata."""

from dlw.db.models.audit import AuditLog
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User

__all__ = [
    "AuditLog", "DownloadTask", "Executor", "FileSubTask",
    "Project", "StorageBackend", "Tenant", "User",
]
```

Insert the new import after `Executor` and update `__all__`:

```python
"""ORM models. Importing this module also registers them with Base.metadata."""

from dlw.db.models.audit import AuditLog
from dlw.db.models.executor import Executor
from dlw.db.models.executor_status_history import ExecutorStatusHistory
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User

__all__ = [
    "AuditLog", "DownloadTask", "Executor", "ExecutorStatusHistory",
    "FileSubTask", "Project", "StorageBackend", "Tenant", "User",
]
```

- [ ] **Step 3: Smoke-check via Python REPL**

Run: `uv run python -c "from dlw.db.models import ExecutorStatusHistory; print(ExecutorStatusHistory.__tablename__)"`
Expected: `executor_status_history`

- [ ] **Step 4: Commit**

```bash
git add src/dlw/db/models/executor_status_history.py src/dlw/db/models/__init__.py
git commit -m "feat(db): ExecutorStatusHistory model + register (W2a M1)"
```

---

### Task 2: Alembic migration

**Files:**
- Create: `src/dlw/alembic/versions/<rev>_p2w2a_state_machine.py` (filename auto-generated by alembic)

- [ ] **Step 1: Generate the empty revision**

```bash
uv run alembic revision -m "p2w2a state machine"
```

Alembic prints the new revision file path. Note the 12-char hex revision ID it generated.

- [ ] **Step 2: Verify `down_revision`**

Open the newly created file. Confirm:

```python
revision: str = '<new id>'
down_revision: Union[str, None] = '8040f6a49655'   # W1 fence columns
```

If the auto-generated `down_revision` is anything other than `'8040f6a49655'`, fix it before continuing.

- [ ] **Step 3: Implement `upgrade()` and `downgrade()`**

Replace the empty body with:

```python
"""p2w2a state machine

Revision ID: <new id>
Revises: 8040f6a49655
Create Date: <auto>
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '<new id>'
down_revision: Union[str, None] = '8040f6a49655'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. New column on executors: degraded_recoveries (counter used by state machine).
    op.add_column(
        "executors",
        sa.Column("degraded_recoveries", sa.Integer(), nullable=False, server_default="0"),
    )

    # 2. New table.
    op.create_table(
        "executor_status_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "executor_id", sa.String(64),
            sa.ForeignKey("executors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(32), nullable=False),
        sa.Column("to_status",   sa.String(32), nullable=False),
        sa.Column("event",       sa.String(32), nullable=False),
        sa.Column("reason",      sa.String(256), nullable=False),
        sa.Column(
            "transitioned_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "metadata", postgresql.JSONB,
            server_default=sa.text("'{}'::jsonb"), nullable=False,
        ),
    )
    op.create_index(
        "ix_esh_executor_time",
        "executor_status_history",
        ["executor_id", sa.text("transitioned_at DESC")],
    )

    # 3. Data migration: legacy 'unhealthy' → 'faulty'. 'joining' rows stay
    #    valid (joining is in the W2a value domain). 'healthy' rows untouched.
    op.execute("UPDATE executors SET status = 'faulty' WHERE status = 'unhealthy'")

    # 4. Synthetic history row for each migrated row so the audit trail is complete.
    op.execute("""
        INSERT INTO executor_status_history
          (executor_id, from_status, to_status, event, reason, metadata)
        SELECT id, 'unhealthy', 'faulty', 'admin',
               'P2-W2a migration: legacy unhealthy -> faulty',
               '{}'::jsonb
        FROM executors WHERE status = 'faulty'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE executors SET status = 'unhealthy'
        WHERE status IN ('faulty', 'suspect', 'degraded')
    """)
    op.drop_index("ix_esh_executor_time", table_name="executor_status_history")
    op.drop_table("executor_status_history")
    op.drop_column("executors", "degraded_recoveries")
```

Also add the matching ORM attribute to `src/dlw/db/models/executor.py`. Find the cluster of counters near `degraded_failure_streak`:

```python
    degraded_failure_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
```

Insert below:

```python
    degraded_recoveries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
```

Replace `<new id>` with the actual revision id Alembic generated in Step 1.

- [ ] **Step 4: Apply the migration to local dev DB**

```bash
uv run alembic upgrade head
```

Expected output ends with `Running upgrade 8040f6a49655 -> <new id>, p2w2a state machine`.

- [ ] **Step 5: Verify the new table exists**

```bash
psql -h localhost -p 5433 -U postgres -d dlw -c "\d executor_status_history"
```

Expected: lists the 8 columns (`id`, `executor_id`, `from_status`, `to_status`, `event`, `reason`, `transitioned_at`, `metadata`) and the index `ix_esh_executor_time`.

- [ ] **Step 6: Verify downgrade reverses cleanly**

```bash
uv run alembic downgrade -1
psql -h localhost -p 5433 -U postgres -d dlw -c "\d executor_status_history"
```

Expected: the second command prints `Did not find any relation named "executor_status_history".`

Re-apply:

```bash
uv run alembic upgrade head
```

- [ ] **Step 7: Commit**

```bash
git add src/dlw/alembic/versions/<rev>_p2w2a_state_machine.py
git commit -m "feat(db): p2w2a alembic — executor_status_history + unhealthy→faulty (W2a M1)"
```

---

### Task 3: `transition_executor()` core + 6 unit tests

**Files:**
- Create: `src/dlw/services/state_machine.py`
- Create: `tests/services/test_state_machine.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_state_machine.py`:

```python
"""Tests for executor state machine (Phase 2 W2a §6 rules)."""
from __future__ import annotations

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.executor_status_history import ExecutorStatusHistory
from dlw.services.state_machine import (
    DEGRADED_RECOVER_OK,
    DEGRADED_STREAK_FAULTY,
    HB_TIMEOUT_TO_FAULTY,
    HB_TIMEOUT_TO_SUSPECT,
    TASK_FAIL_TO_DEGRADED,
    Transition,
    transition_executor,
)


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _make_executor(
    session: AsyncSession, *, ex_id: str = "ex-1", host: str = "host-A",
    status: str = "healthy", epoch: int = 1,
) -> Executor:
    ex = Executor(
        id=ex_id, host_id=host, cert_fingerprint="x",
        status=status, epoch=epoch,
    )
    session.add(ex)
    await session.flush()
    return ex


@pytest.mark.slow
async def test_healthy_to_degraded_after_3_task_failures(db_session: AsyncSession) -> None:
    ex = await _make_executor(db_session, ex_id="ex-h2d")
    # First two failures: counter rises, no transition.
    for _ in range(TASK_FAIL_TO_DEGRADED - 1):
        t = await transition_executor(db_session, ex, event="task_failure", reason="t")
        assert t is None
    # Third failure: → degraded.
    t = await transition_executor(db_session, ex, event="task_failure", reason="t")
    assert t == Transition(
        from_status="healthy", to_status="degraded",
        reason="3_task_failures", event="task_failure",
    )
    assert ex.status == "degraded"
    assert ex.degraded_failure_streak == 0    # Reset on entry to degraded.


@pytest.mark.slow
async def test_degraded_to_faulty_after_streak_10(db_session: AsyncSession) -> None:
    ex = await _make_executor(db_session, ex_id="ex-d2f", status="degraded")
    for _ in range(DEGRADED_STREAK_FAULTY - 1):
        t = await transition_executor(db_session, ex, event="task_failure", reason="t")
        assert t is None
    t = await transition_executor(db_session, ex, event="task_failure", reason="t")
    assert t.from_status == "degraded" and t.to_status == "faulty"
    assert ex.status == "faulty"


@pytest.mark.slow
async def test_degraded_recovers_to_healthy_after_5_ok(db_session: AsyncSession) -> None:
    ex = await _make_executor(
        db_session, ex_id="ex-d2h", status="degraded",
    )
    ex.degraded_failure_streak = 4   # Should be reset on entry to healthy.
    for _ in range(DEGRADED_RECOVER_OK - 1):
        t = await transition_executor(db_session, ex, event="task_success", reason="t")
        assert t is None
    t = await transition_executor(db_session, ex, event="task_success", reason="t")
    assert t.to_status == "healthy"
    assert ex.status == "healthy"
    assert ex.degraded_failure_streak == 0


@pytest.mark.slow
async def test_suspect_to_degraded_on_heartbeat_ok(db_session: AsyncSession) -> None:
    ex = await _make_executor(db_session, ex_id="ex-s2d", status="suspect")
    t = await transition_executor(db_session, ex, event="heartbeat_ok", reason="hb")
    assert t.from_status == "suspect" and t.to_status == "degraded"
    assert ex.consecutive_heartbeat_failures == 0


@pytest.mark.slow
async def test_transition_writes_history_row(db_session: AsyncSession) -> None:
    ex = await _make_executor(db_session, ex_id="ex-hist")
    await transition_executor(db_session, ex, event="task_failure", reason="t")
    await transition_executor(db_session, ex, event="task_failure", reason="t")
    await transition_executor(db_session, ex, event="task_failure", reason="t")
    await db_session.flush()
    n = await db_session.scalar(
        select(func.count()).select_from(ExecutorStatusHistory)
        .where(ExecutorStatusHistory.executor_id == "ex-hist")
    )
    assert n == 1   # Only the threshold-crossing event writes a row.
    row = (await db_session.execute(
        select(ExecutorStatusHistory)
        .where(ExecutorStatusHistory.executor_id == "ex-hist")
    )).scalar_one()
    assert row.from_status == "healthy"
    assert row.to_status == "degraded"
    assert row.event == "task_failure"


@pytest.mark.slow
async def test_no_transition_returns_none_no_history(db_session: AsyncSession) -> None:
    ex = await _make_executor(db_session, ex_id="ex-noop")
    t = await transition_executor(db_session, ex, event="task_failure", reason="t")
    assert t is None
    await db_session.flush()
    n = await db_session.scalar(
        select(func.count()).select_from(ExecutorStatusHistory)
        .where(ExecutorStatusHistory.executor_id == "ex-noop")
    )
    assert n == 0
```

- [ ] **Step 2: Run tests — verify they fail with import error**

```bash
uv run pytest tests/services/test_state_machine.py -v
```

Expected: 6 errors (collection-time), `ModuleNotFoundError: dlw.services.state_machine`.

- [ ] **Step 3: Implement `state_machine.py`**

Create `src/dlw/services/state_machine.py`:

```python
"""Executor status state machine (Phase 2 W2a, spec §6).

ALL Python attribute writes to Executor.status MUST go through
transition_executor(). CI lint (tools/lint_no_direct_status_write.py)
enforces this. The atomic INSERT-or-bump in services.executor_service
.join_executor uses pg_insert.values(status=...) — a SQL-builder kwarg,
not a Python attribute assignment — and is correctly outside the lint's
pattern.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.executor import Executor
from dlw.db.models.executor_status_history import ExecutorStatusHistory

logger = logging.getLogger(__name__)

Event = Literal[
    "heartbeat_ok", "heartbeat_timeout",
    "task_success", "task_failure", "admin",
]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


HB_TIMEOUT_TO_SUSPECT  = _env_int("DLW_HB_TIMEOUT_TO_SUSPECT", 3)
HB_TIMEOUT_TO_FAULTY   = _env_int("DLW_HB_TIMEOUT_TO_FAULTY", 6)
TASK_FAIL_TO_DEGRADED  = _env_int("DLW_TASK_FAIL_TO_DEGRADED", 3)
DEGRADED_STREAK_FAULTY = _env_int("DLW_DEGRADED_STREAK_FAULTY", 10)
DEGRADED_RECOVER_OK    = _env_int("DLW_DEGRADED_RECOVER_OK", 5)

VALID_STATUS = ("joining", "healthy", "degraded", "suspect", "faulty")


@dataclass(frozen=True)
class Transition:
    from_status: str
    to_status: str
    reason: str
    event: Event


async def transition_executor(
    session: AsyncSession,
    ex: Executor,
    *,
    event: Event,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Transition | None:
    """Mutate executor counters + status per spec §6.

    Returns Transition if status changed (and one history row is appended),
    None if only counters moved. Caller commits. Caller is responsible for
    side effects of a transition (e.g. reclaim_subtasks on → suspect/faulty).
    """
    from_status = ex.status
    to_status, transition_reason = from_status, reason

    if event == "heartbeat_ok":
        ex.consecutive_heartbeat_failures = 0
        ex.last_heartbeat_at = datetime.now(UTC)
        if from_status == "joining":
            to_status, transition_reason = "healthy", "first_hb"
        elif from_status == "suspect":
            to_status, transition_reason = "degraded", "hb_recovered"

    elif event == "heartbeat_timeout":
        if from_status == "joining":
            logger.warning(
                "transition_executor: heartbeat_timeout on joining executor %s; no-op",
                ex.id,
            )
            return None
        ex.consecutive_heartbeat_failures += 1
        n = ex.consecutive_heartbeat_failures
        if from_status == "healthy" and n >= HB_TIMEOUT_TO_SUSPECT:
            to_status, transition_reason = "suspect", f"hb_timeout_{HB_TIMEOUT_TO_SUSPECT}"
        elif from_status == "degraded" and n >= HB_TIMEOUT_TO_SUSPECT:
            to_status = "suspect"
            transition_reason = f"hb_timeout_{HB_TIMEOUT_TO_SUSPECT}_from_degraded"
        elif from_status == "suspect" and n >= HB_TIMEOUT_TO_FAULTY:
            to_status, transition_reason = "faulty", f"hb_timeout_{HB_TIMEOUT_TO_FAULTY}"

    elif event == "task_success":
        if from_status in ("suspect", "faulty", "joining"):
            logger.warning(
                "transition_executor: task_success on %s executor %s; no-op",
                from_status, ex.id,
            )
            return None
        ex.consecutive_task_failures = 0
        if from_status == "degraded":
            ex.degraded_recoveries += 1
            if ex.degraded_recoveries >= DEGRADED_RECOVER_OK:
                to_status, transition_reason = "healthy", f"recovered_{DEGRADED_RECOVER_OK}_ok"

    elif event == "task_failure":
        if from_status in ("suspect", "faulty", "joining"):
            logger.warning(
                "transition_executor: task_failure on %s executor %s; no-op",
                from_status, ex.id,
            )
            return None
        if from_status == "healthy":
            ex.consecutive_task_failures += 1
            if ex.consecutive_task_failures >= TASK_FAIL_TO_DEGRADED:
                to_status = "degraded"
                transition_reason = f"{TASK_FAIL_TO_DEGRADED}_task_failures"
        elif from_status == "degraded":
            ex.degraded_failure_streak += 1
            if ex.degraded_failure_streak >= DEGRADED_STREAK_FAULTY:
                to_status = "faulty"
                transition_reason = f"degraded_streak_{DEGRADED_STREAK_FAULTY}"

    elif event == "admin":
        # Caller provides target via metadata["to_status"]; reason is the operator note.
        if metadata and "to_status" in metadata:
            to_status = metadata["to_status"]
            transition_reason = reason or "admin"

    if to_status == from_status:
        return None

    # Reset counters when entering healthy (degraded → healthy or joining → healthy).
    if to_status == "healthy":
        ex.degraded_failure_streak = 0
        ex.degraded_recoveries = 0
        ex.consecutive_task_failures = 0

    # Reset counter when leaving degraded for faulty (anti-pump: any future
    # task_failure on faulty is a no-op, but if admin resets to degraded later
    # the streak should start fresh).
    if from_status == "degraded" and to_status == "faulty":
        ex.degraded_failure_streak = 0
        ex.degraded_recoveries = 0

    transition = Transition(
        from_status=from_status,
        to_status=to_status,
        reason=transition_reason or "",
        event=event,
    )
    session.add(ExecutorStatusHistory(
        executor_id=ex.id,
        from_status=from_status,
        to_status=to_status,
        event=event,
        reason=transition.reason,
        metadata_=dict(metadata or {}),
    ))
    ex.status = to_status
    logger.info(
        "executor_transition: id=%s %s->%s event=%s reason=%s",
        ex.id, from_status, to_status, event, transition.reason,
    )
    return transition
```

Note: `Executor.degraded_recoveries` was added to both the alembic upgrade and the ORM in Task 2 — if you skipped that ORM edit, do it now (see Task 2 Step 3, the "ORM attribute" section).

- [ ] **Step 4: Run tests — verify all 6 pass**

```bash
uv run pytest tests/services/test_state_machine.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Run the full suite to confirm no regressions**

```bash
uv run pytest -x
```

Expected: baseline + 6 passed, 1 deselected.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/services/state_machine.py tests/services/test_state_machine.py
git commit -m "feat(state): transition_executor + 6 unit tests (W2a M1)"
```

---

### Milestone 1 verification (self)

- [ ] `alembic current` returns the new revision id.
- [ ] Six new `test_state_machine.py` cases pass.
- [ ] Existing pytest suite is unchanged in count and result.

---

## Milestone 2 — Endpoint rewiring + sweeper

After M2, `/heartbeat` routes status writes through `transition_executor`; `complete_subtask` calls `transition_executor` after sha256 verify; `sweep_executor_timeouts` replaces W1's `reclaim_stale_executors` and is wired into the lifespan loop.

---

### Task 4: Refactor `record_heartbeat` to route through state machine

**Files:**
- Modify: `src/dlw/services/executor_service.py`

- [ ] **Step 1: Edit `record_heartbeat`**

Current W1 body (lines ~50-66):

```python
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

Replace with:

```python
async def record_heartbeat(
    session: AsyncSession,
    executor_id: str,
    body: ExecutorHeartbeat,
) -> Executor:
    """Update non-status fields + route status mutation through state machine.

    W2a §3.6: state transitions (joining → healthy, suspect → degraded) and
    counter resets are handled inside transition_executor. This function
    retains responsibility for the non-status fields posted in the heartbeat
    body (health_score, parts_dir_bytes).
    """
    from dlw.services.state_machine import transition_executor   # local import: avoids cycle

    ex = await session.get(Executor, executor_id)
    if ex is None:
        raise LookupError(f"executor {executor_id} not found (must POST /join first)")
    ex.health_score = body.health_score
    ex.parts_dir_bytes = body.parts_dir_bytes
    await transition_executor(
        session, ex,
        event="heartbeat_ok",
        reason="hb_received",
        metadata={"health_score": body.health_score},
    )
    return ex
```

Also remove the now-unused `from datetime import UTC, datetime` import line if pyflakes flags it (run pyflakes mentally: `datetime.now(UTC)` is no longer referenced here, but the module may still need `datetime` elsewhere; leave it if `from datetime import` is used by anything else).

- [ ] **Step 2: Verify W1 heartbeat tests still pass**

```bash
uv run pytest tests/api/test_executors.py -v -k heartbeat
```

Expected: all W1 `/heartbeat` cases pass (observable behaviour is unchanged — `last_heartbeat_at` is now set inside `transition_executor`, counter still resets, joining → healthy still fires).

If any case fails because of a status assertion (e.g. an assertion that the status is `"healthy"` after a rejoin-heartbeat sequence), update the test only if W2a's behaviour genuinely changed; do not loosen assertions to hide a regression.

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest -x
```

Expected: full baseline + 6 (from M1) passes.

- [ ] **Step 4: Commit**

```bash
git add src/dlw/services/executor_service.py
git commit -m "refactor(executor): record_heartbeat routes status mutation via state machine (W2a M2)"
```

---

### Task 5: `complete_subtask` tail call to state machine

**Files:**
- Modify: `src/dlw/services/scheduler.py`

- [ ] **Step 1: Add the transition call**

Open `src/dlw/services/scheduler.py`. The W1 `complete_subtask` ends with:

```python
    statuses = {s.status for s in siblings}
    if "failed" in statuses:
        parent.status = "failed"
        parent.error_message = f"subtask {sub.filename} failed: {error}"
        parent.completed_at = datetime.now(UTC)
    elif statuses == {"succeeded"}:
        parent.status = "succeeded"
        parent.completed_at = datetime.now(UTC)

    return sub, parent
```

Insert a state-machine call before `return sub, parent`:

```python
    statuses = {s.status for s in siblings}
    if "failed" in statuses:
        parent.status = "failed"
        parent.error_message = f"subtask {sub.filename} failed: {error}"
        parent.completed_at = datetime.now(UTC)
    elif statuses == {"succeeded"}:
        parent.status = "succeeded"
        parent.completed_at = datetime.now(UTC)

    # W2a §3.3: route executor health update through the state machine.
    # Unreachable if the W1 epoch-mismatch raised earlier (zombie completion).
    if sub.executor_id is not None:
        from dlw.services.state_machine import transition_executor   # local: avoids cycle
        ex = await session.get(Executor, sub.executor_id)
        if ex is not None:
            await transition_executor(
                session, ex,
                event="task_success" if final_status == "succeeded" else "task_failure",
                reason=f"sub_{sub.id}",
                metadata={"subtask_id": str(sub.id), "filename": sub.filename},
            )

    return sub, parent
```

Add the import near the top with the other model imports:

```python
from dlw.db.models.executor import Executor
```

- [ ] **Step 2: Run scheduler tests**

```bash
uv run pytest tests/services/test_scheduler.py -v
```

Expected: all W1 `test_scheduler.py` cases pass. If a case asserts that `executor.status` is unchanged after `complete_subtask`, it might still pass — `task_success` on a `healthy` executor doesn't transition unless degraded; verify the test's setup. Adjust only if a genuine regression.

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest -x
```

Expected: baseline + 6 still pass.

- [ ] **Step 4: Commit**

```bash
git add src/dlw/services/scheduler.py
git commit -m "feat(scheduler): complete_subtask routes executor health via state machine (W2a M2)"
```

---

### Task 6: `sweep_executor_timeouts` replaces `reclaim_stale_executors`

**Files:**
- Modify: `src/dlw/services/recovery.py`
- Modify: `src/dlw/main.py`
- Modify: `tests/services/test_recovery.py`
- Create: `tests/services/test_sweeper.py`

- [ ] **Step 1: Write the failing sweeper test**

Create `tests/services/test_sweeper.py`:

```python
"""Tests for sweep_executor_timeouts (Phase 2 W2a §3.4)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.recovery import sweep_executor_timeouts


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
async def test_sweep_transitions_stale_to_suspect_and_reclaims(
    db_session: AsyncSession, env,
) -> None:
    """A healthy executor with stale heartbeat + counter == 2 → 3rd timeout
    advances it to suspect and reclaims its 'assigned' subtask."""
    stale_time = datetime.now(UTC) - timedelta(seconds=300)
    ex = Executor(
        id="ex-stale-1", host_id="host-S", cert_fingerprint="x",
        status="healthy", epoch=1, last_heartbeat_at=stale_time,
        consecutive_heartbeat_failures=2,
    )
    db_session.add(ex)
    await db_session.flush()

    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="x.bin",
        file_size=100, status="assigned",
        executor_id="ex-stale-1", executor_epoch=1,
        assignment_token=uuid.uuid4(),
    )
    db_session.add(sub)
    await db_session.flush()

    counters = await sweep_executor_timeouts(db_session)
    await db_session.flush()

    assert counters == {"transitioned": 1, "reclaimed": 1}
    refreshed_ex = await db_session.get(Executor, "ex-stale-1")
    assert refreshed_ex.status == "suspect"
    refreshed_sub = await db_session.get(FileSubTask, sub.id)
    assert refreshed_sub.status == "pending"
    assert refreshed_sub.executor_id is None
```

The `env` fixture mentioned here is the per-test pytest helper used in W1 tests for clean rows; if `tests/services/test_recovery.py` defines it inside that file, copy it into `test_sweeper.py` too, or extract to a shared conftest. (Read `tests/services/test_recovery.py` lines 1-80 to confirm — if `env` is a module-level fixture there, define a minimal local equivalent in `test_sweeper.py` mirroring it.)

- [ ] **Step 2: Replace `reclaim_stale_executors` with `sweep_executor_timeouts`**

Open `src/dlw/services/recovery.py`. Delete the W1 `reclaim_stale_executors` function (lines ~295-323 — the one at the very bottom). Replace with:

```python
async def sweep_executor_timeouts(
    session: AsyncSession,
    *,
    heartbeat_threshold_seconds: int = 90,
) -> dict[str, int]:
    """Per 03 §5: scan executors that missed heartbeats; transition through
    the state machine; reclaim subtasks only on entry to suspect / faulty.

    Returns observability counters: {"transitioned": N, "reclaimed": M}.
    Caller commits.
    """
    from dlw.services.state_machine import transition_executor   # local: avoids cycle

    threshold = datetime.now(UTC) - timedelta(seconds=heartbeat_threshold_seconds)
    candidates = (await session.execute(
        select(Executor)
        .where(Executor.status.in_(("healthy", "degraded", "suspect")))
        .where(Executor.last_heartbeat_at < threshold)
        .with_for_update(skip_locked=True)
    )).scalars().all()

    counters = {"transitioned": 0, "reclaimed": 0}
    for ex in candidates:
        t = await transition_executor(
            session, ex,
            event="heartbeat_timeout",
            reason="sweep_timeout",
            metadata={"threshold_s": heartbeat_threshold_seconds},
        )
        if t is None:
            continue
        counters["transitioned"] += 1
        if t.to_status in ("suspect", "faulty"):
            counters["reclaimed"] += await reclaim_subtasks(session, ex.id, ex.epoch)
    return counters
```

Remove the now-unused `reclaim_subtasks` import path if it changed (the function is still imported from `scheduler`, leave that).

- [ ] **Step 3: Rewire `main.py` lifespan**

Open `src/dlw/main.py`. Three textual changes:

(a) `_RECLAIM_INTERVAL_SECONDS = 30` → `_SWEEP_INTERVAL_SECONDS = 30`

(b) Inside `lifespan`:

```python
    reclaim_task = asyncio.create_task(_reclaim_loop_main(factory))
```

→

```python
    sweep_task = asyncio.create_task(_sweep_loop_main(factory))
```

And the matching cancellation block:

```python
    finally:
        reclaim_task.cancel()
        try:
            await asyncio.wait_for(reclaim_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
```

→

```python
    finally:
        sweep_task.cancel()
        try:
            await asyncio.wait_for(sweep_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
```

(c) The function:

```python
async def _reclaim_loop_main(factory) -> None:
    """Background task: every N seconds, scan stale executors + reclaim."""
    from dlw.services.recovery import reclaim_stale_executors

    while True:
        try:
            await asyncio.sleep(_RECLAIM_INTERVAL_SECONDS)
            async with factory() as session:
                await reclaim_stale_executors(session)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reclaim_loop iteration failed; will retry next tick")
```

→

```python
async def _sweep_loop_main(factory) -> None:
    """Background task: every N seconds, transition stale executors + reclaim."""
    from dlw.services.recovery import sweep_executor_timeouts

    while True:
        try:
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
            async with factory() as session:
                await sweep_executor_timeouts(session)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sweep_loop iteration failed; will retry next tick")
```

- [ ] **Step 4: Update W1 test cases**

Open `tests/services/test_recovery.py`. Find the two cases that test the W1 sweeper:

```
test_reclaim_stale_executors_marks_unhealthy_and_reclaims
test_reclaim_stale_executors_skips_recently_active
```

Update the import:

```python
from dlw.services.recovery import (
    ...
    reclaim_stale_executors,   # REMOVE this line
)
```

→ remove `reclaim_stale_executors` and add `sweep_executor_timeouts`:

```python
from dlw.services.recovery import (
    ...
    sweep_executor_timeouts,
)
```

Rewrite the first case:

```python
@pytest.mark.slow
async def test_sweep_transitions_stale_to_suspect_and_reclaims_w1(
    db_session, env,
) -> None:
    """W2a successor of the W1 test: stale executor with counter==2 advances
    to suspect on the 3rd timeout, and its 'assigned' subtask returns to 'pending'."""
    stale_time = datetime.now(UTC) - timedelta(seconds=300)
    db_session.add(Executor(
        id="stale-host-worker-1", host_id="stale-host",
        cert_fingerprint="x", status="healthy", epoch=1,
        last_heartbeat_at=stale_time,
        consecutive_heartbeat_failures=2,   # W2a: 3rd timeout flips status.
    ))
    await db_session.flush()

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

    counters = await sweep_executor_timeouts(db_session)
    await db_session.flush()
    assert counters["reclaimed"] == 1

    ex = await db_session.get(Executor, "stale-host-worker-1")
    assert ex.status == "suspect"
    refreshed = await db_session.get(FileSubTask, sub.id)
    assert refreshed.status == "pending"
    assert refreshed.executor_id is None
```

Rewrite the second case:

```python
@pytest.mark.slow
async def test_sweep_skips_recently_active(db_session, env) -> None:
    """Executor with recent heartbeat is left alone, counter unchanged."""
    db_session.add(Executor(
        id="active-host-worker-1", host_id="active-host",
        cert_fingerprint="x", status="healthy", epoch=1,
        last_heartbeat_at=datetime.now(UTC),
    ))
    await db_session.flush()
    counters = await sweep_executor_timeouts(db_session)
    assert counters == {"transitioned": 0, "reclaimed": 0}
    ex = await db_session.get(Executor, "active-host-worker-1")
    assert ex.status == "healthy"
```

- [ ] **Step 5: Run the new sweeper test**

```bash
uv run pytest tests/services/test_sweeper.py -v
```

Expected: 1 passed.

- [ ] **Step 6: Run the rewritten recovery tests**

```bash
uv run pytest tests/services/test_recovery.py -v
```

Expected: all pass — including the two renamed cases plus the unchanged W1 cases (`test_verify_remote_state_*`, `test_run_recovery_routine_*`).

- [ ] **Step 7: Run the full suite**

```bash
uv run pytest -x
```

Expected: baseline + 6 (M1) + 1 (sweeper) passes; the two W1 recovery cases changed in place (same count, same result).

- [ ] **Step 8: Commit**

```bash
git add src/dlw/services/recovery.py src/dlw/main.py \
        tests/services/test_recovery.py tests/services/test_sweeper.py
git commit -m "feat(recovery): sweep_executor_timeouts via state machine + rewire lifespan (W2a M2)"
```

---

### Milestone 2 verification (self)

- [ ] `record_heartbeat` no longer writes `ex.status` directly; W1 `/heartbeat` tests pass.
- [ ] `complete_subtask` calls `transition_executor`; W1 scheduler tests pass.
- [ ] `sweep_executor_timeouts` replaces `reclaim_stale_executors` end-to-end (production + test code).
- [ ] `tests/services/test_sweeper.py` passes.
- [ ] `tests/services/test_recovery.py` passes with the two rewritten cases.

---

## Milestone 3 — Scheduler reverse host-affinity

After M3, `claim_one_subtask` skips ineligible executors and refuses to hand a subtask to an executor whose host already has another executor holding a sibling-file subtask.

---

### Task 7: `claim_one_subtask` augmented WHERE + 2 tests

**Files:**
- Modify: `src/dlw/services/scheduler.py`
- Create: `tests/services/test_scheduler_host_affinity.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/test_scheduler_host_affinity.py`:

```python
"""Tests for claim_one_subtask host-affinity + status eligibility (W2a §3.3)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.scheduler import claim_one_subtask


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed_pending_subtask(session: AsyncSession) -> tuple[DownloadTask, FileSubTask]:
    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id="o/r", revision="b" * 40, storage_id=1,
        path_template="t", priority=1, status="pending",
    )
    session.add(task)
    await session.flush()
    sub = FileSubTask(
        task_id=task.id, tenant_id=1, filename="model.bin",
        file_size=100, status="pending",
    )
    session.add(sub)
    await session.flush()
    return task, sub


@pytest.mark.slow
async def test_claim_skips_when_self_status_faulty(db_session: AsyncSession) -> None:
    db_session.add(Executor(
        id="ex-faulty", host_id="host-1", cert_fingerprint="x",
        status="faulty", epoch=1,
    ))
    await _seed_pending_subtask(db_session)

    sub, token = await claim_one_subtask(db_session, "ex-faulty", 1)
    assert sub is None and token is None


@pytest.mark.slow
async def test_claim_skips_when_same_host_other_executor_holds_file(
    db_session: AsyncSession,
) -> None:
    """D-10 reverse: ex-A2 on host-1 cannot claim sibling subtask while ex-A1
    on host-1 already holds an 'assigned' subtask of the same (task_id, filename).
    Bypass UniqueConstraint(task_id, filename) inside this test transaction by
    dropping the constraint; rollback restores it."""
    await db_session.execute(text(
        "ALTER TABLE file_subtasks DROP CONSTRAINT file_subtasks_task_id_filename_key"
    ))

    db_session.add(Executor(
        id="ex-A1", host_id="host-1", cert_fingerprint="x",
        status="healthy", epoch=1,
    ))
    db_session.add(Executor(
        id="ex-A2", host_id="host-1", cert_fingerprint="x",
        status="healthy", epoch=1,
    ))
    task, _existing = await _seed_pending_subtask(db_session)
    # Mark the existing subtask as assigned to ex-A1.
    _existing.status = "assigned"
    _existing.executor_id = "ex-A1"
    _existing.executor_epoch = 1
    _existing.assignment_token = uuid.uuid4()
    # Insert a sibling pending subtask with the same task_id + filename (constraint dropped).
    sibling = FileSubTask(
        task_id=task.id, tenant_id=1, filename="model.bin",
        file_size=100, status="pending",
    )
    db_session.add(sibling)
    await db_session.flush()

    # ex-A2 on host-1 must not claim it.
    sub, token = await claim_one_subtask(db_session, "ex-A2", 1)
    assert sub is None and token is None

    # An executor on a different host can claim it.
    db_session.add(Executor(
        id="ex-B1", host_id="host-2", cert_fingerprint="x",
        status="healthy", epoch=1,
    ))
    await db_session.flush()
    sub, token = await claim_one_subtask(db_session, "ex-B1", 1)
    assert sub is not None
    assert sub.executor_id == "ex-B1"
```

- [ ] **Step 2: Run the tests — verify the first fails on faulty path**

```bash
uv run pytest tests/services/test_scheduler_host_affinity.py -v
```

Expected: at least the `faulty` test fails (today `claim_one_subtask` ignores executor status). The host-affinity test should also fail.

- [ ] **Step 3: Implement the augmented WHERE**

Open `src/dlw/services/scheduler.py`. Replace the body of `claim_one_subtask`:

```python
async def claim_one_subtask(
    session: AsyncSession,
    executor_id: str,
    executor_epoch: int,
) -> tuple[FileSubTask | None, uuid.UUID | None]:
    """Atomically grab one pending subtask for this executor.

    W2a §3.3: enforces two constraints in addition to W1's status='pending'.
      (a) calling executor must exist and be in ('healthy', 'degraded'); else
          returns (None, None) without locking any row.
      (b) reverse host-affinity per INVARIANT D-10: no row whose (task_id,
          filename) is held by another executor on the same host_id.

    Caller must commit() to finalize the claim.
    """
    from sqlalchemy.orm import aliased

    # (a) Self-eligibility — read first, return early if ineligible.
    e_self = await session.get(Executor, executor_id)
    if e_self is None or e_self.status not in ("healthy", "degraded"):
        return None, None

    # (b) Reverse host-affinity NOT EXISTS clause.
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

    stmt = (
        select(FileSubTask)
        .where(FileSubTask.status == "pending")
        .where(~same_host_holds)
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
    sub.executor_epoch = executor_epoch
    sub.assignment_token = token
    sub.assigned_at = datetime.now(UTC)
    return sub, token
```

Add the `aliased` import at module top (if not already present): `from sqlalchemy.orm import aliased`. (It is fine to keep the local import inside the function — your call.)

- [ ] **Step 4: Run tests — verify both pass**

```bash
uv run pytest tests/services/test_scheduler_host_affinity.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest -x
```

Expected: baseline + 9 new (6 M1 + 1 sweeper + 2 host-affinity) passes.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/services/scheduler.py tests/services/test_scheduler_host_affinity.py
git commit -m "feat(scheduler): host-affinity reverse constraint + status eligibility (W2a M3)"
```

---

### Milestone 3 verification (self)

- [ ] `claim_one_subtask` refuses faulty / suspect callers.
- [ ] `claim_one_subtask` refuses to assign a sibling subtask to an executor on a host that already has another executor holding the file.
- [ ] Both new cases pass; no regression elsewhere.

---

## Milestone 4 — CI lint + PR

After M4, the CI gate forbids any new `Executor.status = ...` Python attribute writes outside `state_machine.py`, the invariant lint covers the new value domain and a host-affinity test owner, the OpenAPI spec advertises the new status enum, and the PR is opened with CI green.

---

### Task 8: `tools/lint_no_direct_status_write.py` + self-test

**Files:**
- Create: `tools/lint_no_direct_status_write.py`
- Create: `tests/lint/__init__.py` (empty)
- Create: `tests/lint/fixtures/__init__.py` (empty)
- Create: `tests/lint/fixtures/bad_executor_status_write.py`
- Create: `tests/lint/test_no_direct_status_write.py`

- [ ] **Step 1: Write the lint tool**

Create `tools/lint_no_direct_status_write.py`:

```python
#!/usr/bin/env python3
"""
lint_no_direct_status_write.py — forbid Python attribute writes to
Executor.status outside the state machine module (W2a §3.7).

Heuristic: AST-walks every .py file under `src/dlw/` and flags any
`Assign` or `AugAssign` whose target is an attribute named `status`
on a value resembling an Executor row (named `ex`, `executor`,
`e_self`, `e_other`, etc.).

ALLOWED_FILES below are exempt — currently only state_machine.py.
SQL-builder kwargs (`pg_insert.values(status=...)`, `.update().values(
status=...)`) are NOT attribute assignments and are correctly outside
the pattern.

Exit codes:
  0 — clean
  1 — at least one violation
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOT = ROOT / "src" / "dlw"
ALLOWED_FILES = {
    SCAN_ROOT / "services" / "state_machine.py",
}
EXECUTOR_NAME_HINTS = {"ex", "executor", "e_self", "e_other", "e"}


def _is_status_attribute_target(target: ast.expr) -> bool:
    if not isinstance(target, ast.Attribute):
        return False
    if target.attr != "status":
        return False
    if isinstance(target.value, ast.Name) and target.value.id in EXECUTOR_NAME_HINTS:
        return True
    # ex.row.status or similar nested attribute chain: be permissive — flag any
    # `.status` write whose root name hint matches; ignore other nesting.
    cur = target.value
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    if isinstance(cur, ast.Name) and cur.id in EXECUTOR_NAME_HINTS:
        return True
    return False


def scan_file(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if _is_status_attribute_target(tgt):
                    hits.append((node.lineno, ast.unparse(tgt)))
        elif isinstance(node, ast.AugAssign):
            if _is_status_attribute_target(node.target):
                hits.append((node.lineno, ast.unparse(node.target)))
    return hits


def main() -> int:
    violations: list[tuple[Path, int, str]] = []
    for py in SCAN_ROOT.rglob("*.py"):
        if py in ALLOWED_FILES:
            continue
        for line, text in scan_file(py):
            violations.append((py.relative_to(ROOT), line, text))

    if violations:
        print("Direct Executor.status writes found (route through state_machine.transition_executor):")
        for path, line, text in violations:
            print(f"  {path}:{line}  {text} = ...")
        return 1
    print("No direct Executor.status writes in src/dlw/. OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it against the current tree**

```bash
python tools/lint_no_direct_status_write.py
```

Expected: `No direct Executor.status writes in src/dlw/. OK.`

If a violation appears, it means M2's refactor missed a site — fix the source rather than allow-listing it. (Recovery's `_reset_subtask_to_pending` writes `sub.status = "pending"`, but `sub` is a `FileSubTask`, not an `Executor` — the heuristic only fires on `ex` / `executor` / `e_self` / `e_other`, so this is correctly out of scope.)

- [ ] **Step 3: Add the self-test fixture**

Create `tests/lint/__init__.py` (empty file) and `tests/lint/fixtures/__init__.py` (empty file).

Create `tests/lint/fixtures/bad_executor_status_write.py`:

```python
"""Self-test fixture: this file deliberately contains a violation.
The lint scans src/dlw/ only — this file lives under tests/ and is
NOT scanned in CI. The unit test points the lint at this file directly."""
from __future__ import annotations


def naughty(ex) -> None:
    ex.status = "faulty"     # noqa: violation we want the lint to catch
```

- [ ] **Step 4: Write the self-test**

Create `tests/lint/test_no_direct_status_write.py`:

```python
"""Self-test: the lint correctly flags a known-bad fixture."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
import lint_no_direct_status_write as linter   # noqa: E402


def test_lint_flags_direct_status_assignment() -> None:
    fixture = (
        Path(__file__).resolve().parent
        / "fixtures" / "bad_executor_status_write.py"
    )
    hits = linter.scan_file(fixture)
    assert any("status" in text for _, text in hits), \
        f"expected lint to flag fixture, got {hits=}"
```

- [ ] **Step 5: Run the self-test**

```bash
uv run pytest tests/lint/test_no_direct_status_write.py -v
```

Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
git add tools/lint_no_direct_status_write.py tests/lint/
git commit -m "feat(ci): lint_no_direct_status_write + self-test fixture (W2a M4)"
```

---

### Task 9: Extend `tools/lint_invariants.py` + CI wiring + OpenAPI enum

**Files:**
- Modify: `tools/lint_invariants.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `api/openapi.yaml`

- [ ] **Step 1: Extend `tools/lint_invariants.py`**

Add two new checks. Open `tools/lint_invariants.py` and add at the end of the existing check sequence (before the final `if errors: sys.exit(1)`):

```python
# W2a §3.7: executors.status value domain.
VALID_EXECUTOR_STATUS = {"joining", "healthy", "degraded", "suspect", "faulty"}

def check_executor_status_domain() -> list[str]:
    """Lint string literals assigned to a `status` kwarg/attr in two source files."""
    errors: list[str] = []
    files = [
        ROOT / "src" / "dlw" / "services" / "state_machine.py",
        ROOT / "src" / "dlw" / "services" / "executor_service.py",
    ]
    import ast as _ast
    for f in files:
        if not f.exists():
            continue
        tree = _ast.parse(f.read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            # Pattern 1: `status="<literal>"` keyword argument (pg_insert.values, dict, etc.).
            if isinstance(node, _ast.keyword) and node.arg == "status":
                if isinstance(node.value, _ast.Constant) and isinstance(node.value.value, str):
                    if node.value.value not in VALID_EXECUTOR_STATUS:
                        errors.append(
                            f"{f.relative_to(ROOT)}:{node.value.lineno}: "
                            f"invalid status value: {node.value.value!r}"
                        )
            # Pattern 2: `ex.status = "<literal>"` attribute assignment.
            elif isinstance(node, _ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], _ast.Attribute) \
                    and node.targets[0].attr == "status" \
                    and isinstance(node.value, _ast.Constant) \
                    and isinstance(node.value.value, str):
                if node.value.value not in VALID_EXECUTOR_STATUS:
                    errors.append(
                        f"{f.relative_to(ROOT)}:{node.lineno}: "
                        f"invalid status value: {node.value.value!r}"
                    )
    return errors


def check_d10_host_affinity_test_owner() -> list[str]:
    """INVARIANT D-10 must have at least one discoverable test owner."""
    import glob
    matches = glob.glob(str(ROOT / "tests" / "**" / "test_*host*affinity*.py"), recursive=True)
    if not matches:
        return ["INVARIANT D-10 (host-affinity) has no test file matching `test_*host*affinity*.py`"]
    return []
```

Wire them into the existing `main()` (the accumulator there is named `failures: list[str]`, not `errors`). Search for the comment `# --- Report ---` near the end of `main()`; insert the two calls immediately above it:

```python
    failures.extend(check_executor_status_domain())
    failures.extend(check_d10_host_affinity_test_owner())

    # --- Report ---
    if failures:
        ...
```

Re-run the lint tool's existing unit tests to make sure nothing broke:

```bash
uv run pytest tools/test_lint_invariants.py -v
```

Expected: same count as before plus any new cases you add (none required).

- [ ] **Step 2: Add a step to the CI `invariant_lint` job**

Edit `.github/workflows/ci.yml`. Inside the `invariant_lint` job, after the existing "Run invariant lint" step, add:

```yaml
      - name: No direct Executor.status writes (W2a)
        run: python tools/lint_no_direct_status_write.py
```

- [ ] **Step 3: Widen the OpenAPI enum**

Edit `api/openapi.yaml`. Find `ExecutorRead.status` (around line 2050) and change:

```yaml
        status:
          type: string
```

to:

```yaml
        status:
          type: string
          enum: [joining, healthy, degraded, suspect, faulty]
          description: Executor lifecycle state (W2a §6).
```

- [ ] **Step 4: Run the local equivalent of CI**

```bash
python tools/lint_no_direct_status_write.py
python tools/lint_invariants.py
uv run pytest tools/test_lint_invariants.py -v
```

Expected: all clean.

For OpenAPI: if you have `spectral` locally, `spectral lint api/openapi.yaml --fail-severity=error` should be clean. Otherwise CI will catch it.

- [ ] **Step 5: Run the full pytest suite one more time**

```bash
uv run pytest -x
```

Expected: baseline + 10 new passes.

- [ ] **Step 6: Commit**

```bash
git add tools/lint_invariants.py .github/workflows/ci.yml api/openapi.yaml
git commit -m "ci(lint): invariant value-domain + D-10 owner + OpenAPI enum (W2a M4)"
```

---

### Task 10: Push branch + open PR + monitor CI

- [ ] **Step 1: Confirm branch state**

```bash
git status
git log main..HEAD --oneline
```

Expected: clean working tree; ~8 commits on the branch (2 spec commits + 6 task commits roughly).

- [ ] **Step 2: Push**

```bash
git push -u origin feat/phase-2-w2a-scheduler-state-machine
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create \
  --title "Phase 2 Week 2a — scheduler host-affinity + executor state machine" \
  --body "$(cat <<'EOF'
## Summary

W2a half of `docs/v2.0/08-mvp-roadmap.md` §2.6:

- **Executor state machine.** New `src/dlw/services/state_machine.py` with `transition_executor()` — single Python writer to `Executor.status`. Value domain `{joining, healthy, degraded, suspect, faulty}`. Fixes the D3 "degraded↔suspect pump" by tracking `degraded_failure_streak` (→ faulty at 10) and `degraded_recoveries` (→ healthy at 5). Every transition durably appends a row to the new `executor_status_history` table.
- **Sweeper rewrite.** `sweep_executor_timeouts` replaces W1's `reclaim_stale_executors`; the lifespan loop is rewired. Subtasks are reclaimed only on entry to `suspect` / `faulty`.
- **Scheduler.** `claim_one_subtask` now requires the caller to be in `{healthy, degraded}` and refuses INVARIANT D-10 conflicts (no two executors on the same host hold sibling subtasks of the same file). The clause is functionally a NOOP today because of `UniqueConstraint(task_id, filename)`, but it is the load-bearing piece for W2b's chunk-level fanout.
- **CI lint.** `tools/lint_no_direct_status_write.py` — AST scan forbids `ex.status = ...` Python attribute writes outside `state_machine.py`. Plus a value-domain check and a D-10 test-owner check in `tools/lint_invariants.py`.

Spec: \`docs/superpowers/specs/2026-05-13-phase-2-w2a-scheduler-state-machine-design.md\`.
Plan: \`docs/superpowers/plans/2026-05-13-phase-2-w2a-scheduler-state-machine.md\`.

W2b (cancelling / paused_* / chunk-level downloader) is a follow-up spec.

## Test plan

- [x] Backend pytest: baseline + 10 new (6 state machine + 1 sweeper + 2 host-affinity + 1 lint self-test). Zero regressions.
- [x] \`alembic upgrade head\` applies cleanly from W1 baseline; \`alembic downgrade -1\` reverses cleanly.
- [x] \`tools/lint_no_direct_status_write.py\` returns 0.
- [x] \`tools/lint_invariants.py\` (existing + new checks) returns 0.
- [x] OpenAPI \`spectral\` clean.
- [x] No new runtime / dev deps; no new CI jobs.

## Out of scope (deferred — see spec §1.2)

W2b subtask states (cancelling / paused_external / paused_disk_full); chunk-level downloader; probationary / draining; preemption; heartbeat HMAC; active/standby.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Monitor CI**

```bash
gh pr checks $(gh pr view --json number -q .number) --watch
```

Expected: 12 checks pass. If any fail:

- **`Invariant + cross-ref lint`** — the new domain/owner checks may surface a missed value in a less-common location. Read the failure, fix the source rather than weakening the lint, commit, push (do NOT amend / force-push).
- **`pytest`** — re-run locally first. If only the lint self-test fails on CI, double-check that the fixture file has the violation (does not get reformatted/sanitised by any pre-commit hook).
- **`OpenAPI lint`** — spectral may reject `enum:` on a property typed `string` without `additionalProperties: false`. Recheck the exact diff against `api/openapi.yaml` patterns elsewhere in the file.

---

### Milestone 4 verification (self)

- [ ] PR opened; CI 12/12 green.
- [ ] No diff outside the File Structure list (`gh pr diff --name-only`).
- [ ] All 10 new tests pass; no W1 regressions.
- [ ] `Executor.status` is written via Python attribute in exactly one file (`state_machine.py`).

---

## Definition of Done

- [ ] All 10 tasks committed on `feat/phase-2-w2a-scheduler-state-machine`.
- [ ] PR opened, CI 12/12 green.
- [ ] 10 new pytest tests pass; baseline + 10 total.
- [ ] `alembic upgrade head` clean on a fresh DB AND on a W1-baseline DB.
- [ ] `alembic downgrade -1` reverses cleanly.
- [ ] `tools/lint_no_direct_status_write.py` reports 0 violations.
- [ ] `tools/lint_invariants.py` includes the value-domain check and the D-10 test-owner check.
- [ ] `api/openapi.yaml` `ExecutorRead.status` enum lists the 5 values.
- [ ] No new runtime deps in `pyproject.toml`; no new dev deps; no new CI jobs.

---

## Plan Revisions Log

(Empty on first draft. Populated by any pre-execution multi-agent reviewer pass.)

| Tag | Severity | Issue | Fix applied |
|-----|----------|-------|-------------|
| _(none yet)_ | | | |

---

## References

- Spec: `docs/superpowers/specs/2026-05-13-phase-2-w2a-scheduler-state-machine-design.md`
- Spec patch: commit `0472aef` (W1 reality fix — `joining` in domain)
- Predecessor spec: `docs/superpowers/specs/2026-05-11-phase-2-week-1-fence-token-recovery-design.md`
- Predecessor plan: `docs/superpowers/plans/2026-05-11-phase-2-week-1-fence-token-recovery.md`
- Roadmap source: `docs/v2.0/08-mvp-roadmap.md` §2.6 Phase 2 W2 — D1-2 + D3
- Distributed correctness: `docs/v2.0/03-distributed-correctness.md` §5 (state machine, D3) + §4 (scheduler races)
- Invariant catalogue: `docs/v2.0/INVARIANTS.md` §D-10
- Test plan IDs: `docs/v2.0/07-test-plan.md` §U-SM-004..011
- W1 PR (merged): https://github.com/l17728/modelpull/pull/7 (squash `a999381`)
