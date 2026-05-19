"""Scheduling-phase incremental diff + global dedup (Phase 3 SP3; doc §2).
Runs before SP2 plan_task_sources. Caller commits."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.storage_object import StorageObject
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.storage_objects import record_ref_only


async def diff_and_dedup(session: AsyncSession, task: DownloadTask) -> None:
    """For each still-pending subtask whose HF expected_sha256 already has a
    storage_objects row for (tenant, storage, sha): flip it to `inherit`
    (ref+refcount now, no download). Unifies upgrade_from_revision diff with
    cross-task dedup — completed subtasks of any prior task/revision already
    produced storage_objects, so one lookup covers both."""
    subs = (await session.execute(
        select(FileSubTask).where(
            FileSubTask.task_id == task.id,
            FileSubTask.status == "pending"))).scalars().all()
    for sub in subs:
        if sub.expected_sha256 is None:
            continue
        obj = (await session.execute(
            select(StorageObject).where(
                StorageObject.tenant_id == task.tenant_id,
                StorageObject.storage_id == task.storage_id,
                StorageObject.sha256 == sub.expected_sha256))
        ).scalar_one_or_none()
        if obj is None:
            continue
        sub.status = "inherit"
        sub.inherit_from_key = obj.storage_key
        await record_ref_only(
            session, tenant_id=task.tenant_id, storage_id=task.storage_id,
            storage_key=obj.storage_key, sha256=sub.expected_sha256,
            size=sub.file_size or obj.size, subtask_id=sub.id)
