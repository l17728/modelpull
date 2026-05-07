"""Scheduler: atomic claim_one_subtask using FOR UPDATE SKIP LOCKED.

Pull-model: executors call /poll -> controller calls this. PostgreSQL's
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
