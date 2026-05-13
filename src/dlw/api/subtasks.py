"""Subtasks API: POST /report (executor reports outcome).

Phase 2 W1: enforces X-Executor-Epoch header fence by looking up the subtask's
executor_id and verifying that executor's current epoch matches the header.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.auth.bearer import require_bearer
from dlw.db.models.executor import Executor
from dlw.db.models.task import FileSubTask
from dlw.schemas.subtask import SubTaskReport
from dlw.services.scheduler import complete_subtask

router = APIRouter(prefix="/api/v1/subtasks", tags=["subtasks"])


@router.post("/{subtask_id}/report", dependencies=[Depends(require_bearer)])
async def post_report(
    subtask_id: uuid.UUID,
    body: SubTaskReport,
    # W6-D: use default=None so missing header raises 401 (not FastAPI auto-422)
    x_executor_epoch: int | None = Header(default=None, alias="X-Executor-Epoch"),
    session: AsyncSession = Depends(_session),
) -> dict[str, str]:
    if x_executor_epoch is None:
        raise HTTPException(
            status_code=401,
            detail="missing X-Executor-Epoch header",
        )
    # Phase 2 W1 fence: load the subtask's claimed executor and verify epoch.
    sub = await session.get(FileSubTask, subtask_id)
    if sub is None:
        raise HTTPException(status_code=404, detail=f"subtask {subtask_id} not found")
    if sub.executor_id is None:
        raise HTTPException(
            status_code=409, detail=f"subtask {subtask_id} not assigned"
        )
    ex = await session.get(Executor, sub.executor_id)
    if ex is None:
        raise HTTPException(
            status_code=404, detail=f"executor {sub.executor_id} not found"
        )
    if ex.epoch != x_executor_epoch:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "EPOCH_MISMATCH",
                "expected": ex.epoch,
                "got": x_executor_epoch,
            },
        )

    try:
        sub_done, parent = await complete_subtask(
            session, subtask_id,
            final_status=body.status,
            actual_sha256=body.actual_sha256,
            bytes_downloaded=body.bytes_downloaded,
            error=body.error,
            assignment_token=body.assignment_token,
            executor_epoch=x_executor_epoch,    # forward to fence gate
            s3_key=body.s3_key,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await session.commit()
    return {"subtask_status": sub_done.status, "task_status": parent.status}
