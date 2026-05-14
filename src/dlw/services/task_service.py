"""Task service: creation + HF-driven sub-task generation.

Phase 1 W4: enumerate real files via huggingface_hub. Public repos default;
private repos require DLW_HF_TOKEN env on the controller.

Caller is responsible for transaction boundary (commit/rollback).
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.schemas.task import TaskCreate
from dlw.services.hf_metadata import list_repo_tree


class EmptyRepo(Exception):
    """HF returned zero downloadable files (after metadata filter)."""


async def create_task(
    session: AsyncSession,
    body: TaskCreate,
    *,
    owner_user_id: int,
    tenant_id: int,
    project_id: int,
    hf_endpoint: str,
    hf_token: str | None,
) -> DownloadTask:
    """Persist a download task plus one FileSubTask per HF repo file.

    Raises:
      RepoNotFound | HfPrivateOrAuthRequired | HfNetworkError — from list_repo_tree
      EmptyRepo — repo has no downloadable files at this revision
    """
    files = await list_repo_tree(
        body.repo_id, body.revision,
        hf_endpoint=hf_endpoint, hf_token=hf_token,
    )
    if not files:
        raise EmptyRepo(f"{body.repo_id}@{body.revision} has no files")

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
    await session.flush()

    for f in files:
        session.add(FileSubTask(
            task_id=task.id,
            tenant_id=tenant_id,
            filename=f.path,
            file_size=f.size,
            expected_sha256=f.sha256,
            status="pending",
        ))
    await session.flush()
    return task


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
