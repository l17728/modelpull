# Phase 2 Week 2a — Multi-Executor-Aware Scheduler + Executor State Machine Design

> **Status:** Draft (brainstormed 2026-05-13).
> **Companion plan:** `docs/superpowers/plans/2026-05-13-phase-2-w2a-scheduler-state-machine.md` (to be written by writing-plans skill after spec approval).
> **Roadmap source:** `docs/v2.0/08-mvp-roadmap.md` §2.6 — Phase 2 Week 2 (Day 1-2 + Day 3).
> **Companion split (W2b):** Day 4 (cancelling + paused_*) and Day 5 (chunk-level downloader) live in a separate spec/plan to keep this PR scoped.
> **Distributed-correctness source:** `docs/v2.0/03-distributed-correctness.md` §5 (node state machine, D3) and §4 (scheduler races) + INVARIANTS §D-10.

---

## 1. Goal & Non-Goals

### 1.1 Goal

Two orthogonal-but-co-shipped concerns that both touch `executors`:

1. **Executor state machine.** Replace the Phase-1 boolean `{healthy, unhealthy}` with the four-state finite machine specified in `03 §5.3` — `{healthy, degraded, suspect, faulty}` — eliminating the D3 "degraded↔suspect pump" by tracking `degraded_failure_streak` and `degraded_recoveries` and graduating to `faulty` once the streak hits 10. Every status change is durably recorded in a new `executor_status_history` table. All status writes are routed through a single service function `transition_executor()`; a CI lint forbids direct `ex.status =` writes elsewhere.

2. **Scheduler host-affinity reverse constraint.** Make `claim_one_subtask` aware of executor health (only `healthy` / `degraded` can be assigned new work) and add the negative side of INVARIANT D-10: a subtask cannot be assigned to an executor on a host that already has another executor holding an `assigned` sibling subtask for the same file. In W2a a file maps to exactly one subtask (`UniqueConstraint(task_id, filename)`), so the constraint is effectively a NOOP at runtime today — but the SQL is the load-bearing piece for W2b's chunk-level expansion (where one file fans out to N chunk subtasks), and W2a verifies its semantics with synthetic test data.

After W2a, `reclaim_stale_executors` (W1) is gone — replaced by `sweep_executor_timeouts` which runs the same 30 s lifespan loop but pushes every transition through the new state machine. Reclaim only fires on `→ suspect` or `→ faulty` (degraded executors keep their work; healthy and probationary stay untouched).

### 1.2 Non-goals (deferred — explicit list)

| Item | Why deferred | Where it lands |
|------|-------------|----------------|
| `cancelling` / `paused_external` / `paused_disk_full` subtask states | Different table (file_subtasks), different correctness concern (D7/D8/D13), separate test ID block (U-SM-006..009) | **W2b** (separate spec/plan) |
| `chunks_total` writes + chunk-level downloader (DirectOffsetDownloader) | Requires executor-side runner rewrite + S3 multipart resume; INVARIANT D-10 chunk-half lands here | **W2b** |
| `probationary` (canary on first /join) / `draining` (graceful shutdown) states | No Phase 2 entry-criteria use them; would add 4+ untested transition paths | **Phase 3** when multi-tenant enrollment ships |
| Priority preemption (03 §4.3) | Out of Phase 2 scope per roadmap | **v2.1** |
| Heartbeat HMAC + nonce (SEC-04) | Wire-format security, untangled from state machine | **Phase 2 W3** |
| Active/standby controller (PG advisory_lock) | Failover ≠ state machine | **Phase 2 W3** |
| P-001 heartbeat ≥ 5000 ops/s baseline | `/heartbeat` body changes again in W3 (HMAC verify), measuring twice wastes effort | **After W3** |
| `health_score` driven scheduling | W1 field exists but no policy specified; would introduce an un-validated feedback loop | **Phase 3** with capacity planner |
| WebSocket broadcast of status transitions | UI consumes status changes only at progress-poll cadence today; spec separately | **Phase 2 W3 or later** |

---

## 2. Tech Stack Additions

**None.** The full work fits inside the existing stack:

- SQLAlchemy 2.x async (existing) — new `ExecutorStatusHistory` model + one alembic revision.
- FastAPI lifespan (existing W1 pattern) — `_reclaim_loop_main` renamed to `_sweep_loop_main`.
- pytest + existing `engine` / `db_session` conftest fixtures.
- `ast` (stdlib) — for the new `tools/lint_no_direct_status_write.py` lint script (added to the existing "Invariant + cross-ref lint" CI job, no new job).

No new runtime deps. No new dev deps. No new CI jobs.

---

## 3. Components

### 3.1 New: `src/dlw/services/state_machine.py`

Single public function:

```python
async def transition_executor(
    session: AsyncSession,
    ex: Executor,
    *,
    event: Literal["heartbeat_ok", "heartbeat_timeout",
                   "task_success", "task_failure", "admin"],
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Transition | None:
    """Mutate executor counters + status per 03 §5.3.

    Returns Transition(from_status, to_status, reason, event) if status
    changed (in which case one row is appended to executor_status_history);
    None if only counters moved.

    No commit — caller commits. Caller is responsible for any side effect
    triggered by the transition (e.g. reclaim_subtasks on → suspect/faulty).
    """
```

Module-level tunables (matching 03 §5.3 defaults, env-overridable via the W1 pattern):

```python
HB_TIMEOUT_TO_SUSPECT   = 3      # consecutive_heartbeat_failures
HB_TIMEOUT_TO_FAULTY    = 6
TASK_FAIL_TO_DEGRADED   = 3      # consecutive_task_failures
DEGRADED_STREAK_FAULTY  = 10
DEGRADED_RECOVER_OK     = 5
```

`@dataclass(frozen=True) Transition` — three string fields and an `event` literal; identity equality is fine because all fields are str/Literal.

**Implementation contract:**

1. Mutate counters in-place on `ex` per the rules table (§6 below).
2. Decide `to_status` from the new counter snapshot. If `to_status == ex.status`, return `None` (counter-only, no history row).
3. Else: append one `ExecutorStatusHistory` row (FK to `ex.id`, `metadata` JSONB merged with caller's), then set `ex.status = to_status`, and return the `Transition`.
4. **Single point of `Executor.status` mutation in the whole codebase.** Enforced by §3.7 lint.

### 3.2 New: `src/dlw/db/models/executor_status_history.py`

```python
class ExecutorStatusHistory(Base):
    __tablename__ = "executor_status_history"

    id: Mapped[int]              = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    executor_id: Mapped[str]     = mapped_column(
        String(64), ForeignKey("executors.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str]     = mapped_column(String(32), nullable=False)
    to_status: Mapped[str]       = mapped_column(String(32), nullable=False)
    event: Mapped[str]           = mapped_column(String(32), nullable=False)
    reason: Mapped[str]          = mapped_column(String(256), nullable=False)
    transitioned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, nullable=False  # 'metadata' is SA-reserved name
    )
```

Indexed by `(executor_id, transitioned_at DESC)` for the future ops query "last N transitions for executor X". No back-relationship on `Executor` to keep the model file small; queries go through history table directly.

### 3.3 Modified: `src/dlw/services/scheduler.py`

**`claim_one_subtask` — augmented WHERE:**

```python
# W2a §3.3: scheduler now requires executor in {healthy, degraded}
#           AND no sibling-file subtask held by another executor on same host.
stmt = (
    select(FileSubTask)
    .where(FileSubTask.status == "pending")
    .where(_eligible_executor_subq(executor_id))            # NEW (a)
    .where(~_same_host_sibling_holding(executor_id))        # NEW (b)
    .order_by(FileSubTask.created_at)
    .limit(1)
    .with_for_update(skip_locked=True)
)
```

Two private helpers in the same module:

- `_eligible_executor_subq(executor_id)` → SA `exists()` clause asserting the calling executor exists and is in `('healthy', 'degraded')`.
- `_same_host_sibling_holding(executor_id)` → SA `exists()` clause matching INVARIANT D-10 reverse condition (§2.4 of the brainstorm summary above). Joins `executors` twice — `e_self` for `executor_id`, `e_other` for the holder of any sibling subtask with the same `(task_id, filename)`.

`reclaim_subtasks` is unchanged (still fenced by `(executor_id, executor_epoch)`).

`complete_subtask` gets one new block at the end (after the W1 fence + sha256 gates):

```python
ex = await session.get(Executor, sub.executor_id)
if ex is not None:
    await transition_executor(
        session, ex,
        event="task_success" if final_status == "succeeded" else "task_failure",
        reason=f"sub_{sub.id}",
        metadata={"subtask_id": str(sub.id), "filename": sub.filename},
    )
```

If `executor_epoch` mismatch raised earlier (W1 zombie path), this block is unreachable — correct, because a zombie completion should not move state-machine counters.

### 3.4 Modified: `src/dlw/services/recovery.py`

`reclaim_stale_executors` is **removed**. Replaced by:

```python
async def sweep_executor_timeouts(
    session: AsyncSession,
    *,
    heartbeat_threshold_seconds: int = 90,
) -> dict[str, int]:
    """Per 03 §5: scan executors that missed heartbeats; transition; reclaim
    on → suspect / → faulty entry. Returns observability counters.
    """
    threshold = datetime.now(UTC) - timedelta(seconds=heartbeat_threshold_seconds)
    candidates = (await session.execute(
        select(Executor)
        .where(Executor.status.in_(("healthy", "degraded", "suspect")))
        .where(Executor.last_heartbeat_at < threshold)
        .with_for_update(skip_locked=True)            # avoid race with /heartbeat
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

`run_recovery_routine` (startup-once routine) is unchanged — startup recovery is orthogonal to the state machine and runs before the lifespan sweeper task spawns.

### 3.5 Modified: `src/dlw/main.py` (lifespan)

Only rename + call-site swap; no structural change. `_reclaim_loop_main` → `_sweep_loop_main`. The function body keeps the same try/finally + 30 s sleep skeleton and now calls `sweep_executor_timeouts` instead of `reclaim_stale_executors`. The `_RECLAIM_INTERVAL_SECONDS` module constant is renamed to `_SWEEP_INTERVAL_SECONDS` (still 30).

### 3.6 Modified: `src/dlw/api/executors.py` (`POST /executors/{id}/heartbeat`)

Currently the endpoint does `ex.last_heartbeat_at = datetime.now(UTC)`. W2a routes through the state machine:

```python
await transition_executor(
    session, ex, event="heartbeat_ok", reason="hb_received",
)
await session.commit()
```

`transition_executor` updates `last_heartbeat_at` + zeroes `consecutive_heartbeat_failures` internally on `heartbeat_ok`. The endpoint no longer touches `ex.status` directly.

`POST /executors` (`/join`) is unchanged in W2a — new executors still come up `healthy` with `epoch += 1` (W1 behaviour). Probationary onboarding is Phase 3.

**Faulty executor heartbeat handling.** A `faulty` executor that still sends heartbeats: `transition_executor(event=heartbeat_ok)` zeroes `consecutive_heartbeat_failures` and updates `last_heartbeat_at`, but does NOT change status (the rule table in §6 only flips `suspect → degraded` on heartbeat_ok; other states are unchanged). This is intentional for W2a — admin-driven recovery (`reason='admin'`) is the only path back to healthy / degraded from faulty. A future spec (Phase 2 W3 with HMAC heartbeat or Phase 3 with admin CLI) can add a 403 gate at the endpoint level.

### 3.7 New: `tools/lint_no_direct_status_write.py`

AST-walk every file in `src/dlw/`. Fail CI if any `Assign` or `AugAssign` has an attribute target named `status` whose qualified base is `Executor` / `ex` / `executor` / `e` / `e_self` etc. (heuristic — we conservatively flag any `.status =` write inside `src/dlw/` and rely on an allowlist for the legitimate site).

```python
ALLOWED_FILES = {
    "src/dlw/services/state_machine.py",   # the only legitimate writer
}
# alembic data migrations are also legal (they read the SQL directly, not via ORM)
# — and alembic/ is outside src/dlw/, so it's already excluded.
```

Wired into the existing `Invariant + cross-ref lint` job in `.github/workflows/ci.yml`:

```yaml
- name: No direct Executor.status writes
  run: python tools/lint_no_direct_status_write.py
```

The tool exits non-zero with a clear message listing offending file:line. Self-test fixture under `tests/lint/fixtures/bad_executor_status_write.py` proves the lint catches a violation.

**Also extends the existing `tools/lint_invariants.py`** in the same job: add an assertion that the `executors.status` value domain is exactly `{healthy, degraded, suspect, faulty}` (lints `src/dlw/db/models/executor.py` + `src/dlw/services/state_machine.py` for any string literal assigned to a `status` field outside that set), and assert at least one test file name matches `*host*affinity*` (so INVARIANT D-10 has a discoverable test owner).

---

## 4. Schema Changes (single alembic migration)

`alembic/versions/xxxxxxxxxxxx_p2w2a_state_machine.py`:

```python
def upgrade() -> None:
    # 1. New table
    op.create_table(
        "executor_status_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("executor_id", sa.String(64),
                  sa.ForeignKey("executors.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("from_status", sa.String(32), nullable=False),
        sa.Column("to_status",   sa.String(32), nullable=False),
        sa.Column("event",       sa.String(32), nullable=False),
        sa.Column("reason",      sa.String(256), nullable=False),
        sa.Column("transitioned_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("metadata", postgresql.JSONB,
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.create_index(
        "ix_esh_executor_time",
        "executor_status_history",
        ["executor_id", sa.text("transitioned_at DESC")],
    )

    # 2. Data migration: legacy 'unhealthy' → 'faulty'
    op.execute("UPDATE executors SET status = 'faulty' WHERE status = 'unhealthy'")

    # 3. Synthetic history row for each migrated executor
    op.execute("""
        INSERT INTO executor_status_history
          (executor_id, from_status, to_status, event, reason, metadata)
        SELECT id, 'unhealthy', 'faulty', 'admin',
               'P2-W2a migration: legacy unhealthy → faulty', '{}'::jsonb
        FROM executors WHERE status = 'faulty'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE executors SET status = 'unhealthy'
        WHERE status IN ('faulty', 'suspect', 'degraded')
    """)
    op.drop_index("ix_esh_executor_time", table_name="executor_status_history")
    op.drop_table("executor_status_history")
```

`executors` table itself has zero column adds — W1 already shipped `consecutive_heartbeat_failures`, `consecutive_task_failures`, `degraded_failure_streak`, `health_score`. W2a just consumes them.

The `status` column stays `String(32)` (no PG enum). The value domain is enforced by:

- INVARIANT lint (`tools/lint_invariants.py`) — add a new asserted set `{healthy, degraded, suspect, faulty}` for `executors.status`.
- `transition_executor()` being the single writer.

---

## 5. Wire Format Changes

**None.** W2a does not touch any HTTP contract:

- `/executors/{id}/heartbeat` request and response shapes unchanged.
- `ExecutorRead` response schema unchanged (status field accepts new values but is already `str`).
- `/subtasks/...` paths and shapes unchanged.

The OpenAPI spec `api/openapi.yaml` only needs the documented `status` enum to grow from `{healthy, unhealthy}` to `{healthy, degraded, suspect, faulty}`. This is a backwards-compatible widening (existing clients accepting any string keep working).

---

## 6. State Machine Rules (canonical reference)

This is the single source of truth that `transition_executor()` implements. CI tests assert each rule with a dedicated case (§8).

| Event | Counter changes | Transition rule |
|-------|-----------------|-----------------|
| `heartbeat_ok` | `consecutive_heartbeat_failures := 0`; `last_heartbeat_at := now()` | `suspect → degraded` (reason `"hb_recovered"`); else status unchanged |
| `heartbeat_timeout` | `consecutive_heartbeat_failures += 1` | `healthy → suspect` when counter ≥ `HB_TIMEOUT_TO_SUSPECT` (3); `degraded → suspect` same threshold (reason `"hb_timeout_3_from_degraded"`); `suspect → faulty` when counter ≥ `HB_TIMEOUT_TO_FAULTY` (6) |
| `task_success` | `consecutive_task_failures := 0`; if `degraded`: `degraded_recoveries += 1` | `degraded → healthy` when `degraded_recoveries ≥ DEGRADED_RECOVER_OK` (5); reset `degraded_failure_streak := 0` and `degraded_recoveries := 0` on entry to healthy |
| `task_failure` | if `healthy`: `consecutive_task_failures += 1`; if `degraded`: `degraded_failure_streak += 1` | `healthy → degraded` when `consecutive_task_failures ≥ TASK_FAIL_TO_DEGRADED` (3); `degraded → faulty` when `degraded_failure_streak ≥ DEGRADED_STREAK_FAULTY` (10) |
| `admin` | None | Free-form; reason field carries the operator note |

**Invariants enforced inside `transition_executor`:**

- `suspect` and `faulty` are terminal-for-work: receiving `task_success` / `task_failure` on either logs a `WARNING` (a zombie completion or a race the W1 fence missed) but does not mutate counters or status.
- Every transition writes exactly one `executor_status_history` row, atomic with the status change (same SQLAlchemy unit-of-work, same session, same commit by caller).
- Counter resets on entry to `healthy` are mandatory (the entire D3 fix hinges on `degraded_failure_streak := 0` happening when we leave degraded).

---

## 7. Error Handling Matrix

| Situation | Behaviour |
|-----------|-----------|
| `transition_executor` called with executor row not yet `await session.get`-ed by caller | Caller responsibility; function does not re-fetch (avoids surprising row-lock acquisition). |
| `executor_status_history` insert fails (e.g. FK violation because executor was deleted concurrently) | SA propagates IntegrityError; caller's session rolls back; the outer loop (sweeper / endpoint) logs and skips. No partial transition (history row + status both rolled back in one unit). |
| `sweep_executor_timeouts` row-lock acquisition skipped (`SKIP LOCKED`) because `/heartbeat` is mid-commit | Executor missed this sweep tick; next sweep (30 s later) picks it up if still stale. Acceptable — staleness window grows by 30 s, never produces wrong status. |
| `claim_one_subtask` called by an executor in `suspect` / `faulty` | NEW subquery `_eligible_executor_subq` returns false → no row matches → `(None, None)` returned to caller (the controller endpoint), which is the existing W1 "nothing to do" response. |
| Synthetic test data violates D-10 (two subtasks same `(task_id, filename)`) | `claim_one_subtask` correctly skips the second; verified by case 9 (§8). UniqueConstraint prevents this in production. |
| Counter rolls over to a transition state and back within one event handler | Cannot happen: each event mutates counters and then evaluates a single threshold check; the rules are strictly monotone in their counter direction (failures only increase; recoveries only increase; heartbeat_ok always zeroes hb counter). |

---

## 8. Testing Strategy

### 8.1 Unit + integration (10 new cases)

| # | File | Case | Rule asserted |
|---|------|------|--------------|
| 1 | `tests/services/test_state_machine.py` | `test_healthy_to_degraded_after_3_task_failures` | `TASK_FAIL_TO_DEGRADED` |
| 2 | same | `test_degraded_to_faulty_after_streak_10` | `DEGRADED_STREAK_FAULTY` (D3 anti-pump) |
| 3 | same | `test_degraded_recovers_to_healthy_after_5_ok` | `DEGRADED_RECOVER_OK` + streak reset |
| 4 | same | `test_suspect_to_degraded_on_heartbeat_ok` | suspect recovery transitions to degraded, NOT healthy |
| 5 | same | `test_transition_writes_history_row` | each non-None transition writes exactly one history row with correct metadata |
| 6 | same | `test_no_transition_returns_none_no_history` | counter-only mutation returns `None`, no history |
| 7 | `tests/services/test_sweeper.py` | `test_sweep_transitions_stale_to_suspect_and_reclaims` | end-to-end: stale last_heartbeat_at → sweep → suspect → reclaim |
| 8 | `tests/services/test_scheduler_host_affinity.py` | `test_claim_skips_when_self_status_faulty` | scheduler refuses faulty self |
| 9 | same | `test_claim_skips_when_same_host_other_executor_holds_file` | D-10 reverse constraint. Test setup: `ALTER TABLE file_subtasks DROP CONSTRAINT file_subtasks_task_id_filename_key` inside the test transaction; insert two subtasks sharing `(task_id, filename)`; rollback restores the constraint automatically. Verifies the SQL semantics today, even though the constraint makes the hot path NOOP until W2b chunk-fanout. |
| 10 | `tests/lint/test_no_direct_status_write.py` | `test_lint_flags_direct_status_assignment` | `tools/lint_no_direct_status_write.py` correctly fails on a fixture file |

All cases use the existing `engine` / `db_session` conftest fixtures. No `freezegun` dep — case 7 manually backdates `last_heartbeat_at` via direct ORM update.

### 8.2 Test compatibility — W1 cases that must move

| W1 case | W2a action |
|---------|------------|
| `tests/services/test_recovery.py::test_reclaim_stale_executors_marks_unhealthy` | Rename function, change expected `status='suspect'`, set `consecutive_heartbeat_failures = 2` beforehand so the 3rd timeout flips status. |
| `tests/services/test_recovery.py::test_reclaim_stale_executors_reclaims_subtasks` | Update threshold setup to trigger `→ suspect` (the gating condition for reclaim). |
| `tests/api/test_executors.py` `/heartbeat` happy-path | Should still pass unchanged — `transition_executor(event=heartbeat_ok)` updates `last_heartbeat_at` and counters, observable side effects identical. |

No tests are deleted; expected delta is `+10 new, ~2 modified in place`.

### 8.3 No new CI jobs

Lint runs in existing `Invariant + cross-ref lint` step. pytest runs in existing `pytest (Phase 1 foundation)` job. Markdown/link/yaml/etc. unchanged.

### 8.4 No new dev infra

No new env vars (the five tunables in §3.1 default-to-spec values; env override is optional). No new docker-compose services. No new local commands.

---

## 9. Acceptance Criteria

- [ ] `alembic upgrade head` from W1 baseline applies cleanly; `executor_status_history` exists with the documented index; legacy `unhealthy` rows are migrated to `faulty` with a synthesised history row each.
- [ ] `alembic downgrade -1` reverses both changes (table drop + status revert).
- [ ] All 10 new pytest cases pass on local PG 18:5433 and on CI.
- [ ] The 2 modified W1 cases pass; no other Phase-1/W1 case regresses.
- [ ] `tools/lint_no_direct_status_write.py` reports 0 violations on `main` and the W2a branch.
- [ ] The lint self-test fixture (under `tests/lint/fixtures/`) provably fails the linter — verified by `tests/lint/test_no_direct_status_write.py`.
- [ ] `tools/lint_invariants.py` extended check: `executors.status` value domain `{healthy, degraded, suspect, faulty}` and `INVARIANT D-10` appears in at least one test name (`test_*host*affinity*`).
- [ ] Code review: `Executor.status` is written in exactly one source file — `src/dlw/services/state_machine.py`.
- [ ] OpenAPI spec `api/openapi.yaml`: `ExecutorRead.status` enum lists the four new values; spectral CI passes.
- [ ] No new runtime deps; no new dev deps; no new CI jobs.

---

## 10. Implementation Phasing (preview for plan)

The plan will be written by the writing-plans skill after this spec is approved. Expected milestone shape (4 milestones, 5-6 tasks):

- **M1 — Schema + state-machine core.** alembic revision + `ExecutorStatusHistory` model + `transition_executor` + 6 unit cases (state machine rules + history).
- **M2 — Sweeper + endpoint rewrite.** `sweep_executor_timeouts` + `/heartbeat` re-route + `complete_subtask` tail call + 2 cases (sweeper + integration with W1 recovery routine).
- **M3 — Scheduler reverse host-affinity.** `_eligible_executor_subq` + `_same_host_sibling_holding` + 2 cases.
- **M4 — CI lint + W1 test fix-ups + push + PR.** `tools/lint_no_direct_status_write.py` + invariants lint extension + W1 test edits + OpenAPI enum widening + PR open + monitor CI.

Branch: `feat/phase-2-w2a-scheduler-state-machine`. Branched off `main` at `a999381` (PR #7 merge).

---

## 11. References

- Roadmap: `docs/v2.0/08-mvp-roadmap.md` §2.6 Phase 2 Week 2 — Day 1-2 (scheduler) + Day 3 (state machine).
- Distributed correctness: `docs/v2.0/03-distributed-correctness.md` §5 (state machine, D3) and §4 (scheduler races).
- Architecture: `docs/v2.0/01-architecture.md` §5.3 (MultiExecutorAwareScheduler) + INVARIANT D-10.
- Invariants catalogue: `docs/v2.0/INVARIANTS.md` §D (scheduling group).
- Test plan: `docs/v2.0/07-test-plan.md` §U-SM-004..011.
- Predecessor spec: `docs/superpowers/specs/2026-05-11-phase-2-week-1-fence-token-recovery-design.md` (fence + recovery, W1).
- Predecessor plan: `docs/superpowers/plans/2026-05-11-phase-2-week-1-fence-token-recovery.md`.
- W1 PR (merged): https://github.com/l17728/modelpull/pull/7 (squash commit `a999381`).
