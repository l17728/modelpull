"""Scheduler: atomic claim_one_subtask using FOR UPDATE SKIP LOCKED.

Pull-model: executors call /poll -> controller calls this. PostgreSQL's
SKIP LOCKED ensures two concurrent claimants never get the same row.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.task import DownloadTask, FileSubTask


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
    if "failed" in statuses:
        parent.status = "failed"
        parent.error_message = f"subtask {sub.filename} failed: {error}"
        parent.completed_at = datetime.now(UTC)
    elif statuses == {"succeeded"}:
        parent.status = "succeeded"
        parent.completed_at = datetime.now(UTC)

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
