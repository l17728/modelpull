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


async def complete_subtask(
    session: AsyncSession,
    subtask_id: uuid.UUID,
    *,
    final_status: str,
    actual_sha256: str | None,
    bytes_downloaded: int,
    error: str | None,
    assignment_token: uuid.UUID | None = None,
) -> tuple[FileSubTask, DownloadTask]:
    """Mark subtask done, then check if parent task can transition.

    Locking: parent task fetched with FOR UPDATE so two concurrent
    completions racing on the LAST 2 subtasks cannot both observe
    'all succeeded' and both write parent.status (W2-E).

    Token verification: if assignment_token provided, must match the row's
    stored token (cheap defence; W2-F).

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

    return sub, parent
