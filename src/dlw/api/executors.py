"""Executors API: join / heartbeat / poll."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session  # reuse session dep
from dlw.auth.bearer import require_bearer
from dlw.schemas.executor import (
    AssignmentResponse,
    ExecutorHeartbeat,
    ExecutorJoin,
    ExecutorRead,
)
from dlw.schemas.subtask import SubTaskRead
from dlw.services.executor_service import join_executor, record_heartbeat
from dlw.services.scheduler import claim_one_subtask

router = APIRouter(prefix="/api/v1/executors", tags=["executors"])


@router.post("/join", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_bearer)])
async def post_join(
    body: ExecutorJoin, session: AsyncSession = Depends(_session)
) -> ExecutorRead:
    ex = await join_executor(session, body)
    await session.commit()
    return ExecutorRead.model_validate(ex)


@router.post("/{executor_id}/heartbeat", dependencies=[Depends(require_bearer)])
async def post_heartbeat(
    executor_id: str,
    body: ExecutorHeartbeat,
    session: AsyncSession = Depends(_session),
) -> ExecutorRead:
    try:
        ex = await record_heartbeat(session, executor_id, body)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    await session.commit()
    return ExecutorRead.model_validate(ex)


@router.post("/{executor_id}/poll", dependencies=[Depends(require_bearer)])
async def post_poll(
    executor_id: str, session: AsyncSession = Depends(_session)
) -> AssignmentResponse:
    sub, token = await claim_one_subtask(session, executor_id)
    if sub is None:
        return AssignmentResponse(assigned=False)
    sub_read = SubTaskRead.model_validate(sub)
    await session.commit()
    return AssignmentResponse(assigned=True, subtask=sub_read, assignment_token=token)
