# Phase 2 Week 2b2 — Task Cancel + paused_external Design

> **Status:** Draft (brainstormed 2026-05-14).
> **Companion plan:** `docs/superpowers/plans/2026-05-14-phase-2-w2b2-cancel-and-paused-external.md` (to be written by writing-plans skill after spec approval).
> **Roadmap source:** `docs/v2.0/08-mvp-roadmap.md` §2.6 — Phase 2 Week 2 Day 4. Closes the half of D4/D8 not covered by W2b1 (which addressed Day 5 chunk-level + D7 paused_disk_full) and the individual-subtask half of D13 (paused_external state; global throttle state machine deferred).
> **Distributed-correctness source:** `docs/v2.0/03-distributed-correctness.md` §7 (cancelling task state, D8) + §8 (paused_external, D13; only §8.3 individual-subtask state is in scope here).

---

## 1. Goal & Non-Goals

### 1.1 Goal

Two subtask-lifecycle concerns shipped together:

1. **D4/D8 task cancellation.** A new `POST /api/v1/tasks/{task_id}/cancel` endpoint flips a task to `cancelling`. The scheduler stops handing out new subtasks for that task. In-flight subtasks finish naturally — lazy propagation — small files in seconds, chunk-level (W2b1) in minutes; the latency is documented. `complete_subtask` becomes cancel-aware: a `succeeded` report under `cancelling` preserves the W4-uploaded S3 object (the subtask stays `succeeded`); a `failed` report records the failure. When the last sibling of a `cancelling` task reaches a terminal state, the task transitions `cancelling → cancelled` rather than `succeeded`/`failed`. Any `paused_external` / `paused_disk_full` subtasks already present at `/cancel` time are force-terminated to `cancelled` inside the same transaction — this avoids a dead-lock where the sweepers would refuse to recover paused subs under a cancelling parent and the parent would therefore never transition.

2. **D13 paused_external (individual subtask).** Executors classify `httpx.HTTPStatusError(429)` or `503` from HF as transient external throttle and report `SubTaskReport.status="paused_external"` (mirroring W2b1's `paused_disk_full`). `complete_subtask` treats it like `paused_disk_full`: clear executor binding, write the new `last_paused_at` timestamp, no retry_count bump, no task-status change, no `transition_executor` call. A new `sweep_paused_external` (added to W2a's `_sweep_loop_main` alongside `sweep_executor_timeouts` and W2b1's `sweep_paused_disk_full`) flips `paused_external` subtasks back to `pending` once `last_paused_at` is older than 5 minutes — provided the parent task is still active.

After W2b2, users can cancel in-flight tasks; HF 429/503 errors stop counting as permanent failures; both new flows are purely passive — no new background tasks, no heartbeat protocol changes, no executor-side state machine.

### 1.2 Non-goals (deferred — explicit list)

| Item | Why deferred | Where it lands |
|---|---|---|
| Heartbeat-carried cancellation signal / mid-flight `asyncio.CancelledError` propagation | Requires executor + downloader rewrites; not needed for correctness | **Phase 2 W3** or **Phase 3** |
| `source_throttle_state` table + global state machine (`normal/throttled/circuit_open`) | Self-contained subsystem; W2b2 handles only the per-subtask state | **Phase 3** |
| 5-min rolling 429/5xx aggregation across executors | Requires the global state machine to act on | **Phase 3** |
| `policies_to_apply.global_speed_limit_bytes_per_sec` heartbeat-response downlink | Coordinated rate limiting | **Phase 3** |
| Executor-side bandwidth throttle enforcement | Requires the downlink | **Phase 3** |
| Per-source state (Phase 3 has multi-source: ModelScope etc.) | Phase 3 multi-source scope | **Phase 3** |
| `verified` subtask state as distinct from `succeeded` (spec 03 §7.2) | `succeeded` already preserves the file via W4 S3 upload; the rename is cosmetic for W2b2 | **Phase 3** (along with verification routine refresh) |
| Force-cancel / hard-cancel API (drops in-flight without waiting) | YAGNI; lazy cancel is sufficient with documented latency | — |
| WebSocket broadcast of cancel signal to UI clients | UI poll cadence is sufficient | **Phase 3** |
| S3 object cleanup on `cancelled` parent | Operational ergonomics; the W4-uploaded objects stay in S3 by design (user can restart the task and reuse) | **Phase 3** ops |
| Multi-user RBAC on `/cancel` | Phase 3 multi-tenant | **Phase 3** |

---

## 2. Tech Stack Additions

**None.** Existing stack covers everything:

- FastAPI router endpoint with `Depends(require_bearer)` (W1 pattern).
- SQLAlchemy 2.x async + `select(...).join(...).exists()` for the parent-active EXISTS clause.
- One alembic migration adding a single nullable column.
- `httpx.HTTPStatusError` classification (the exception type is already imported in `runner.py`).
- `pytest` + existing `engine` / `db_session` / `env` fixtures.

No new runtime deps. No new dev deps. No new CI jobs.

---

## 3. Components

### 3.1 New: `cancel_task` in `src/dlw/services/task_service.py`

The existing `task_service.py` ships `create_task` (W1). Append:

```python
async def cancel_task(session: AsyncSession, task_id: uuid.UUID) -> DownloadTask:
    """W2b2 §A1: idempotently flip task to 'cancelling'.

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

    from sqlalchemy import update
    await session.execute(
        update(FileSubTask)
        .where(FileSubTask.task_id == task_id)
        .where(FileSubTask.status.in_(("paused_disk_full", "paused_external")))
        .values(status="cancelled")
    )
    return task
```

No `transition_executor` call (cancellation is environmental, not a quality signal). The function does not commit — caller commits.

### 3.2 New: `POST /api/v1/tasks/{task_id}/cancel` in `src/dlw/api/tasks.py`

Add alongside the W1 `POST /tasks` (create) and `GET /tasks/{id}` endpoints:

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
    from dlw.services.task_service import cancel_task

    try:
        task = await cancel_task(session, task_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    await session.commit()
    return TaskRead.model_validate(task)
```

`TaskRead` (W1) is `model_validate`-able from `DownloadTask`. No schema changes needed.

### 3.3 Modified: `src/dlw/services/scheduler.py`

Two changes.

**(a) `claim_one_subtask` — add parent-active EXISTS clause.** Current W2b1 body filters `status="pending"`, applies host-affinity NOT EXISTS, then iterates K candidates with disk pre-flight. Add one more clause to the SQL:

```python
parent_active = (
    select(DownloadTask.id)
    .where(DownloadTask.id == FileSubTask.task_id)
    .where(DownloadTask.status.in_(("pending", "scheduling", "downloading")))
    .exists()
)

stmt = (
    select(FileSubTask)
    .where(FileSubTask.status == "pending")
    .where(~same_host_holds)            # W2a
    .where(parent_active)                # W2b2 NEW
    .order_by(FileSubTask.created_at)
    .limit(_K_CANDIDATES)
    .with_for_update(skip_locked=True)
)
```

EXISTS avoids the row-multiplication of an inner JOIN. `(pending, scheduling, downloading)` is the W2b2 "active task" set; tasks in `cancelling`/`cancelled`/`failed`/`succeeded` produce no candidates.

**(b) `complete_subtask` — three additive changes.**

(b1) New `paused_external` short-circuit branch — wedged next to W2b1's `paused_disk_full` branch (just after the W1 fence check). **Cancel-aware:** if the parent is already `cancelling` at the moment of the paused report, force-terminate the sub to `cancelled` instead — this avoids the dead-lock where `sweep_paused_external` skips cancelling parents and the sub never reaches terminal:

```python
if final_status == "paused_external":
    parent = await session.get(DownloadTask, sub.task_id, with_for_update=True)
    if parent is not None and parent.status == "cancelling":
        # Cancel-aware: a paused report arriving after /cancel becomes terminal.
        sub.status = "cancelled"
    else:
        sub.status = "paused_external"
        sub.last_paused_at = datetime.now(UTC)
    sub.executor_id = None
    sub.executor_epoch = None
    sub.assignment_token = None
    sub.assigned_at = None
    sub.last_error = error
    return sub, parent
```

Note the `with_for_update=True` — the cancel-aware check races with `cancel_task` (which also locks the parent FOR UPDATE). The second-to-lock observes the first-to-lock's outcome.

(b2) Extend the W2b1 `paused_disk_full` branch with the **same** cancel-aware structure plus a `last_paused_at` write — for symmetry:

```python
if final_status == "paused_disk_full":
    parent = await session.get(DownloadTask, sub.task_id, with_for_update=True)
    if parent is not None and parent.status == "cancelling":
        sub.status = "cancelled"
    else:
        sub.status = "paused_disk_full"
        sub.last_paused_at = datetime.now(UTC)   # NEW W2b2 field
    sub.executor_id = None
    sub.executor_epoch = None
    sub.assignment_token = None
    sub.assigned_at = None
    sub.last_error = error
    return sub, parent
```

Before W2b2 the W2b1 paused_disk_full branch loaded parent without lock and did not check status. After W2b2 it does both — consistent with the new paused_external branch.

(b3) Cancel-aware sibling-terminal check at the tail. Replace W1+W2a's:

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

with:

```python
statuses = {s.status for s in siblings}
TERMINAL = {"succeeded", "failed", "cancelled"}

if parent.status == "cancelling" and statuses <= TERMINAL:
    parent.status = "cancelled"
    parent.completed_at = datetime.now(UTC)
elif parent.status != "cancelling":
    if "failed" in statuses:
        parent.status = "failed"
        parent.error_message = f"subtask {sub.filename} failed: {error}"
        parent.completed_at = datetime.now(UTC)
    elif statuses == {"succeeded"}:
        parent.status = "succeeded"
        parent.completed_at = datetime.now(UTC)
# else: parent is cancelling but not all siblings terminal — stay cancelling.
```

The W1 epoch-mismatch fence at the top of the function is unchanged.

### 3.4 New: `sweep_paused_external` in `src/dlw/services/recovery.py`

Append to the file (next to W1's `run_recovery_routine` / W2a's `sweep_executor_timeouts` / W2b1's `sweep_paused_disk_full`):

```python
_PAUSED_EXTERNAL_RETRY_INTERVAL_SECONDS = int(
    os.environ.get("DLW_PAUSED_EXTERNAL_RETRY_INTERVAL_SECONDS", "300")
)


async def sweep_paused_external(session: AsyncSession) -> int:
    """W2b2 §C1: recover paused_external subtasks after a quiet period.

    Walks paused_external subtasks whose last_paused_at is older than the
    quiet interval (default 5 min) AND whose parent task is still active
    (pending/scheduling/downloading). Flips them back to 'pending' for
    re-claim. Returns count recovered.

    Thundering-herd risk if HF is hard-down: all paused subs flip at once
    and re-fail. Phase 3's source_throttle_state machine prevents — global
    circuit_open suppresses retries until cool-down.
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
        # last_paused_at left as-is for observability
        recovered += 1
    return recovered
```

`os` is already imported by W2a's scheduler module-level constants pattern. If not in `recovery.py`, add `import os` at the top.

### 3.5 Modified: `src/dlw/main.py`

Extend `_sweep_loop_main` to call all three sweepers per tick:

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

### 3.6 Modified: `src/dlw/executor/runner.py`

Insert a new `except` branch in `_execute_subtask`, between W2b1's `except DiskFullError` and the generic `except Exception`:

```python
except DiskFullError as e:                   # W2b1
    ...
    return
except httpx.HTTPStatusError as e:           # W2b2 NEW
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
    raise   # other HTTP errors fall through to the generic handler
except Exception as e:                       # existing
    ...
```

`httpx` is already imported at the top of `runner.py`.

**Downloader-level retry interaction.** `HfS3StreamDownloader` and `DirectOffsetDownloader` both wrap downloads in `@_TRANSIENT_RETRY` (W4 / W2b1) which retries on `500 <= status < 600`. By the time a 503 escapes into `_execute_subtask`, the 3-attempt exponential backoff has been exhausted — so `paused_external` triggers only on persistent throttling. 429 does NOT match the predicate (`500 <= status < 600`), so it bubbles up immediately — controller-side 5-min sweep handles the retry instead of executor-side hammering. Both behaviors are correct for the W2b2 design.

### 3.7 Modified: `src/dlw/schemas/subtask.py`

Widen the Literal again:

```python
class SubTaskReport(BaseModel):
    status: Literal[
        "succeeded", "failed", "paused_disk_full", "paused_external",
    ]
    ...
```

### 3.8 Modified: `src/dlw/db/models/task.py`

Single attribute add on `FileSubTask`:

```python
last_paused_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
)
```

### 3.9 Modified: `tools/lint_invariants.py`

Two changes:

**(a)** `VALID_SUBTASK_STATUS` (W2b1) grows by one value:

```python
VALID_SUBTASK_STATUS = {
    "pending", "assigned", "succeeded", "failed", "cancelled",
    "paused_disk_full", "paused_external",          # W2b2 NEW
}
```

**(b)** New helper `check_task_status_domain` (mirror of subtask domain helper). Scans `api/tasks.py` + `services/task_service.py` + `services/scheduler.py` for any literal assigned to `status` outside the task value-domain set:

```python
VALID_TASK_STATUS = {
    "pending", "scheduling", "downloading",
    "succeeded", "failed", "cancelled",
    "cancelling",   # W2b2 NEW
}


def check_task_status_domain() -> list[str]:
    """Mirror of check_subtask_status_domain for DownloadTask.status writes."""
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

Wired into `main()` via `failures.extend(check_task_status_domain())` next to the W2b1 subtask helper.

**Cross-talk caveat.** The new task-status helper scans `services/scheduler.py`. That file has both `Executor.status` writes (no — those are forbidden by W2a's `lint_no_direct_status_write`) and `FileSubTask.status` writes (W2b1 lint covers it) and `DownloadTask.status` writes (`parent.status = "failed"` etc. — those W2b2 must accept). All three set names: `parent.status = "failed"` is a task-status write; the literal `"failed"` is in both `VALID_TASK_STATUS` AND `VALID_SUBTASK_STATUS`, so neither lint fires. The W2b2 introduces `"cancelling"` and `"cancelled"` literals in `scheduler.py` — both must be in `VALID_TASK_STATUS`. They are.

A subtle case: `sub.status = "cancelled"` (in `cancel_task` via `update().values(status="cancelled")`) is a subtask-status write — `"cancelled"` is in `VALID_SUBTASK_STATUS`, so the subtask lint passes; the task lint scans the same file and finds `"cancelled"` which is also in `VALID_TASK_STATUS`, so the task lint also passes. No false positive.

---

## 4. Schema Changes

**One alembic migration** (`<rev>_p2w2b2_cancel_and_paused_external.py`). Filename's revision id is auto-generated by alembic.

```python
def upgrade() -> None:
    op.add_column(
        "file_subtasks",
        sa.Column("last_paused_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("file_subtasks", "last_paused_at")
```

`down_revision` = `5cfd4bb519f6` (W2a state machine — W2b1 made no migrations, so the alembic head remained at W2a's revision).

Value-domain widenings (enforced by `tools/lint_invariants.py`, no DDL):

- `file_subtasks.status` += `paused_external`
- `download_tasks.status` += `cancelling`

---

## 5. Wire Format Changes

### 5.1 New endpoint `POST /api/v1/tasks/{task_id}/cancel`

| Aspect | Value |
|---|---|
| Request | empty body, bearer auth |
| Response 202 | `TaskRead` (status now `cancelling`, `cancelled_at` set) |
| Response 404 | task not found |
| Response 409 | task in terminal state (`succeeded`/`failed`/`cancelled`) — idempotent cancel-of-already-cancelling returns 202 |
| Response 401 | missing/invalid bearer |

### 5.2 `SubTaskReport.status` enum widens

OpenAPI: `[succeeded, failed, paused_disk_full]` → `[succeeded, failed, paused_disk_full, paused_external]`. Backwards-compatible widening.

### 5.3 `TaskRead.status` enum widens

`[pending, scheduling, downloading, succeeded, failed, cancelled]` → adds `cancelling`. Phase 1 frontend renders raw status strings; no breakage.

---

## 6. Error Handling Matrix

| Situation | Behaviour |
|---|---|
| `cancel_task` on already-cancelled task | 409 from API; `ValueError` from service |
| `cancel_task` on already-cancelling task | 202 (idempotent), task body returned with current state |
| `complete_subtask` reports `succeeded` while task is `cancelling` | sub stays `succeeded` (file preserved); if it was the last sibling, task transitions `cancelling → cancelled` |
| `complete_subtask` reports `failed` while task is `cancelling` | sub stays `failed`; sibling-terminal check transitions task to `cancelled` if applicable |
| `complete_subtask` reports `paused_external` (or `paused_disk_full`) while task is `cancelling` | The W2b2 cancel-aware paused branch (§3.3 b1/b2) locks parent FOR UPDATE; observes `parent.status == "cancelling"`; force-terminates the sub to `cancelled`. Sibling-terminal tail check then transitions the task to `cancelled`. No dead-lock. |
| Sweep finds a `paused_external` sub with `last_paused_at` NULL | Cannot happen post-W2b2 migration on freshly written rows (every paused write sets the field). Old W2b1 paused_disk_full rows pre-existing the migration have `last_paused_at = NULL` — they are NOT affected because `sweep_paused_external` filters on `status="paused_external"` (a new value, no legacy rows have it). W2b1's `sweep_paused_disk_full` does NOT consult `last_paused_at` (it filters on disk capacity), so legacy paused_disk_full rows still recover normally. |
| Runner sees 4xx (404/401/403) | Not 429 → falls through to generic `except Exception` → reports `failed`. Repo-not-found and auth errors are NOT transient. |
| Runner sees 500/502/504 | Tenacity retries 3 times; if persistent, escapes to generic `except Exception` → reports `failed`. Genuine HF outage rather than throttle. |
| Runner sees S3 `ClientError(503)` (storage side, not HF) | NOT `httpx.HTTPStatusError` (boto3 raises `botocore.exceptions.ClientError`). Falls through to generic handler → reports `failed`. Tested by ensuring the new `except httpx.HTTPStatusError` is positioned correctly in the except chain. |

---

## 7. Testing Strategy

### 7.1 Unit + integration (~15 new cases)

| # | File | Case | What it asserts |
|---|---|---|---|
| 1 | `tests/services/test_task_cancel.py` | `test_cancel_task_flips_status_and_cancelled_at` | Pending task → `cancel_task` → status=`cancelling`, cancelled_at set |
| 2 | same | `test_cancel_task_force_terminates_paused_subtasks` | seed paused_disk_full + paused_external subs → cancel → both become cancelled |
| 3 | same | `test_cancel_task_idempotent_when_already_cancelling` | cancel → cancel again → no error, no double-write |
| 4 | same | `test_cancel_task_raises_on_terminal_state` | task in `(succeeded/failed/cancelled)` → `ValueError` |
| 5 | `tests/api/test_cancel_endpoint.py` | `test_post_cancel_returns_202_and_cancelling` | bearer auth, valid task → 202 + body.status == `cancelling` |
| 6 | same | `test_post_cancel_returns_409_on_terminal_task` | task is `succeeded` → 409 |
| 7 | same | `test_post_cancel_returns_404_on_missing_task` | random uuid → 404 |
| 8 | `tests/services/test_scheduler_skip_cancelling.py` | `test_claim_skips_subtask_under_cancelling_parent` | pending subtask whose parent is `cancelling` → `claim_one_subtask` returns None |
| 9 | `tests/services/test_complete_subtask_cancel_aware.py` | `test_succeeded_under_cancelling_keeps_file_and_transitions_task` | task cancelling + last in-flight sub completes `succeeded` → parent flips `cancelled`; sub stays `succeeded` |
| 10 | same | `test_paused_external_short_circuits` | `complete_subtask(final_status="paused_external")` with non-cancelling parent → sub status set, last_paused_at written, no retry_count bump |
| 10b | same | `test_paused_external_under_cancelling_force_terminates_to_cancelled` | `complete_subtask(final_status="paused_external")` with parent in `cancelling` → sub becomes `cancelled` (not `paused_external`); sibling-terminal tail then transitions task to `cancelled` |
| 11 | `tests/services/test_sweep_paused_external.py` | `test_sweep_recovers_paused_external_after_quiet_window` | seed paused_external with `last_paused_at = now-400s` → sweep → pending |
| 12 | same | `test_sweep_skips_paused_external_under_cancelling_parent` | parent is `cancelling` → sweep does NOT recover (keeps it out of the queue) |
| 13 | `tests/executor/test_runner_external_throttle.py` | `test_runner_classifies_429_as_paused_external` | Mock downloader raises `HTTPStatusError(429)` → runner reports `paused_external` |
| 14 | same | `test_runner_classifies_503_as_paused_external` | same, 503 |

### 7.2 Existing tests compatibility

- `tests/services/test_scheduler*.py` — fixtures seed parent tasks as `status="pending"`. Parent-active EXISTS clause matches `pending`. No edits.
- `tests/services/test_complete_subtask*.py` / `test_scheduler.py` complete_subtask tests — exercise `final_status` in `("succeeded", "failed")` with NON-cancelling parent. The new cancel-aware branch is gated on `parent.status == "cancelling"`, so the W1/W2a path is unchanged. No edits expected; spot-check during implementation.
- `tests/services/test_sweep_paused_disk_full.py` (W2b1) — W2b2 adds `last_paused_at = now()` to the paused_disk_full branch. Test seeds the sub directly (with default `last_paused_at = NULL`) so the W2b1 test continues to pass.

### 7.3 Test infrastructure

- **No new test dependencies.** All cases use existing `engine` / `db_session` fixtures.
- **Fake HTTP throttle for tests 13+14**: construct `httpx.HTTPStatusError` directly:
  ```python
  resp = httpx.Response(status_code=429, request=httpx.Request("GET", "http://hf.fake"))
  err = httpx.HTTPStatusError("429 Too Many Requests", request=resp.request, response=resp)
  ```
  Inject via `monkeypatch.setattr` on a mock downloader's `download` to raise this exception.
- **Time travel for `last_paused_at`**: Test 11 sets `last_paused_at = datetime.now(UTC) - timedelta(seconds=400)` directly on the seeded row; default threshold is 300s, so the sub is eligible for recovery.

### 7.4 No new CI jobs

- Lint extensions live in the existing `invariant_lint` job (same as W2a + W2b1).
- Pytest runs in the existing `pytest (Phase 1 foundation)` job.
- OpenAPI lint (spectral) auto-runs on the openapi.yaml change.

---

## 8. Acceptance Criteria

- [ ] 1 alembic migration applies cleanly from W2a head (`5cfd4bb519f6`); reverses cleanly. `file_subtasks.last_paused_at` exists.
- [ ] `cancel_task` + 4 unit tests pass.
- [ ] `POST /api/v1/tasks/{task_id}/cancel` + 3 API tests pass (202/404/409).
- [ ] `claim_one_subtask` parent-active clause + 1 test pass; W1/W2a/W2b1 scheduler tests unchanged.
- [ ] `complete_subtask` 3 new branches (paused_external short-circuit, paused_disk_full + last_paused_at, cancel-aware tail) + 2 tests pass; W4 sha256 + W2a state-machine + W2b1 paused_disk_full tests unchanged.
- [ ] `sweep_paused_external` + 2 tests pass; main loop runs all three sweepers per tick.
- [ ] Runner classifies 429/503 → `paused_external`; 2 tests pass.
- [ ] OpenAPI: `SubTaskReport.status` has 4 enum values; `TaskRead.status` has 7 enum values; new `POST /tasks/{id}/cancel` operation; spectral CI passes.
- [ ] `tools/lint_invariants.py`: `VALID_SUBTASK_STATUS` += `paused_external`; new `check_task_status_domain` reports 0 on production tree.
- [ ] No new runtime / dev deps; no new CI jobs.
- [ ] `docs/operator/executor-runbook.md` adds a section on cancel-latency expectation (chunk-level downloads can take up to N minutes to finalize cancellation).

---

## 9. Implementation Phasing (preview for plan)

The plan will be written by the writing-plans skill after this spec is approved. Expected milestone shape (4 milestones, ~8 tasks):

- **M1 — Schema + cancel_task service + API endpoint.** alembic migration + FileSubTask attr + `cancel_task` + endpoint + 4 + 3 tests (cancel service + API).
- **M2 — Scheduler skip-cancelling + complete_subtask cancel-aware tail.** parent-active EXISTS clause + sibling-terminal-under-cancelling branch + 1 + 1 test.
- **M3 — paused_external state.** SubTaskReport widening + `complete_subtask` paused_external branch + `last_paused_at` writes on both paused branches + `sweep_paused_external` + main loop wire + executor 429/503 classification + 2 + 2 + 2 tests.
- **M4 — Lint + OpenAPI + operator runbook + push + PR.** `VALID_SUBTASK_STATUS` extension + new `check_task_status_domain` helper + OpenAPI 3 widenings + cancel-latency operator note + PR open + CI monitor.

Branch: `feat/phase-2-w2b2-cancel-and-paused-external`. Branched off `main` at `6037e6b` (PR #10 merge).

---

## 10. References

- Spec source: brainstormed 2026-05-14 (this document).
- Roadmap: `docs/v2.0/08-mvp-roadmap.md` §2.6 Phase 2 W2 Day 4.
- Distributed correctness: `docs/v2.0/03-distributed-correctness.md` §7 (cancelling, D8) + §8.3 (paused_external per-subtask state; the full §8 global throttle is deferred).
- Invariants: `docs/v2.0/INVARIANTS.md` §C (consistency / fence token).
- Predecessor specs:
  - `docs/superpowers/specs/2026-05-11-phase-2-week-1-fence-token-recovery-design.md` (W1)
  - `docs/superpowers/specs/2026-05-13-phase-2-w2a-scheduler-state-machine-design.md` (W2a)
  - `docs/superpowers/specs/2026-05-13-phase-2-w2b1-chunk-level-downloader-design.md` (W2b1)
- W2b1 PR (merged): https://github.com/l17728/modelpull/pull/10 (squash `6037e6b`).
