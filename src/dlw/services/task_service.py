"""Task service: creation + sub-task generation.

In Week 2 we mock sub-task generation as 2 placeholder files. Real HuggingFace
Hub resolution comes in Week 4 plan.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.schemas.task import TaskCreate

# Week 2 mock: every task gets exactly these 2 subtasks
_MOCK_FILES: list[tuple[str, int | None, str | None]] = [
    ("config.json", 4096, None),
    ("model.safetensors", 1_073_741_824, None),
]


async def create_task(
    session: AsyncSession,
    body: TaskCreate,
    *,
    owner_user_id: int,
    tenant_id: int,
    project_id: int,
) -> DownloadTask:
    """Persist a download task plus its mock subtasks atomically.

    Caller is responsible for transaction boundary (commit/rollback).
    """
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

    for filename, size, sha in _MOCK_FILES:
        session.add(FileSubTask(
            task_id=task.id,
            tenant_id=tenant_id,
            filename=filename,
            file_size=size,
            expected_sha256=sha,
            status="pending",
        ))
    await session.flush()
    return task
