"""Subtasks API: POST /report (executor reports outcome)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.auth.bearer import require_bearer
from dlw.schemas.subtask import SubTaskReport
from dlw.services.scheduler import complete_subtask

router = APIRouter(prefix="/api/v1/subtasks", tags=["subtasks"])


@router.post("/{subtask_id}/report", dependencies=[Depends(require_bearer)])
async def post_report(
    subtask_id: uuid.UUID,
    body: SubTaskReport,
    session: AsyncSession = Depends(_session),
) -> dict[str, str]:
    try:
        sub, parent = await complete_subtask(
            session, subtask_id,
            final_status=body.status,
            actual_sha256=body.actual_sha256,
            bytes_downloaded=body.bytes_downloaded,
            error=body.error,
            assignment_token=body.assignment_token,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await session.commit()
    return {"subtask_status": sub.status, "task_status": parent.status}
