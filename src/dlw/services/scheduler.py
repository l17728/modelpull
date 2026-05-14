"""Scheduler: atomic claim_one_subtask using FOR UPDATE SKIP LOCKED.

Pull-model: executors call /poll -> controller calls this. PostgreSQL's
SKIP LOCKED ensures two concurrent claimants never get the same row.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

_K_CANDIDATES = int(os.environ.get("DLW_SCHEDULER_CANDIDATES", "16"))
_DISK_SAFETY_MARGIN_BYTES = int(
    os.environ.get("DLW_DISK_SAFETY_MARGIN_BYTES", str(200 * 1024 * 1024))
)
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.executor import Executor
from dlw.db.models.task import DownloadTask, FileSubTask


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

    # W2b2 §3.3: skip subtasks whose parent task is cancelling/terminal.
    parent_active = (
        select(DownloadTask.id)
        .where(DownloadTask.id == FileSubTask.task_id)
        .where(DownloadTask.status.in_(("pending", "scheduling", "downloading")))
        .exists()
    )

    # W2b1: candidate scan with disk pre-flight.
    GiB = 1024 ** 3
    free_bytes = (e_self.disk_free_gb or 0) * GiB - (e_self.parts_dir_bytes or 0)

    stmt = (
        select(FileSubTask)
        .where(FileSubTask.status == "pending")
        .where(~same_host_holds)
        .where(parent_active)           # W2b2 NEW
        .order_by(FileSubTask.created_at)
        .limit(_K_CANDIDATES)
        .with_for_update(skip_locked=True)
    )
    candidates = (await session.execute(stmt)).scalars().all()
    for sub in candidates:
        size = sub.file_size or 0
        if size + _DISK_SAFETY_MARGIN_BYTES <= free_bytes:
            token = uuid.uuid4()
            sub.status = "assigned"
            sub.executor_id = executor_id
            sub.executor_epoch = executor_epoch
            sub.assignment_token = token
            sub.assigned_at = datetime.now(UTC)
            return sub, token
    # No candidate fit; other locked rows release on session commit.
    return None, None


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
    """Mark subtask done, then check if parent task can transition.

    Phase 1 W4 additions:
      - sha256 verification: when final_status=='succeeded' and the row has
        expected_sha256 set, mismatch flips final_status to 'failed' with a
        descriptive error. Single source of truth for the verify gate.
      - s3_key: optional kwarg; persisted to the row. Phase 1 uses it for
        debugging; Phase 2 uses it for multipart resume keying.
    """
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

    # W2b1+W2b2: paused_disk_full / paused_external short-circuit.
    # Cancel-aware: if parent is cancelling, force-terminate sub to 'cancelled'
    # and fall through to the sibling-terminal tail (so cancelling → cancelled
    # can fire). Otherwise apply paused state and return early — paused subs
    # are NOT terminal, so no completed_at, no task transition, no executor
    # state-machine call.
    if final_status in ("paused_disk_full", "paused_external"):
        parent_locked = await session.get(
            DownloadTask, sub.task_id, with_for_update=True
        )
        if parent_locked is not None and parent_locked.status == "cancelling":
            # Cancel-aware: force-terminate to cancelled (terminal). Fall through.
            sub.status = "cancelled"
            sub.executor_id = None
            sub.executor_epoch = None
            sub.assignment_token = None
            sub.assigned_at = None
            sub.last_error = error
            sub.completed_at = datetime.now(UTC)
            # Re-fetch siblings + run the cancel-aware tail manually here.
            parent = parent_locked
            siblings = (await session.execute(
                select(FileSubTask).where(FileSubTask.task_id == sub.task_id)
            )).scalars().all()
            statuses = {s.status for s in siblings}
            TERMINAL = {"succeeded", "failed", "cancelled"}
            if statuses <= TERMINAL:
                parent.status = "cancelled"
                parent.completed_at = datetime.now(UTC)
            return sub, parent
        else:
            sub.status = final_status
            sub.last_paused_at = datetime.now(UTC)
            sub.executor_id = None
            sub.executor_epoch = None
            sub.assignment_token = None
            sub.assigned_at = None
            sub.last_error = error
            parent = await session.get(DownloadTask, sub.task_id)
            return sub, parent

    # W4: sha256 verification gate
    if (
        final_status == "succeeded"
        and sub.expected_sha256 is not None
        and actual_sha256 != sub.expected_sha256
    ):
        final_status = "failed"
        expected_short = sub.expected_sha256[:12]
        actual_short = (actual_sha256 or "")[:12]
        error = (f"sha256 mismatch: expected={expected_short}… "
                 f"actual={actual_short}…")

    sub.status = final_status
    sub.actual_sha256 = actual_sha256
    sub.bytes_downloaded = bytes_downloaded
    sub.last_error = error
    sub.completed_at = datetime.now(UTC)
    if s3_key is not None:
        sub.s3_key = s3_key

    parent = await session.get(
        DownloadTask, sub.task_id, with_for_update=True
    )
    siblings = (await session.execute(
        select(FileSubTask).where(FileSubTask.task_id == sub.task_id)
    )).scalars().all()

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
