"""Executors API: join / heartbeat / poll.

Phase 2 W1 changes:
  - heartbeat + poll now depend on require_executor_epoch (X-Executor-Epoch header
    required, must match stored executor.epoch — else 401 EPOCH_MISMATCH).
  - /join is unaffected (first contact; controller assigns epoch).
  - poll passes executor.epoch to claim_one_subtask so the subtask row
    captures the current fence.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.auth.bearer import require_bearer
from dlw.auth.executor_epoch import require_executor_epoch
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask
from dlw.schemas.executor import (
    AssignmentResponse,
    ExecutorHeartbeat,
    ExecutorJoin,
    ExecutorRead,
)
from dlw.schemas.storage import StorageConfig
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
    body: ExecutorHeartbeat,
    executor: Executor = Depends(require_executor_epoch),
    session: AsyncSession = Depends(_session),
) -> ExecutorRead:
    try:
        ex = await record_heartbeat(session, executor.id, body)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    await session.commit()
    return ExecutorRead.model_validate(ex)


@router.post("/{executor_id}/poll", dependencies=[Depends(require_bearer)])
async def post_poll(
    executor: Executor = Depends(require_executor_epoch),
    session: AsyncSession = Depends(_session),
) -> AssignmentResponse:
    # NOTE: claim_one_subtask signature is updated to take executor_epoch in
    # Task 5 — until then this call uses the W4 2-arg form. Task 5 also
    # updates this line to pass executor.epoch. Keeping commits separable.
    sub, token = await claim_one_subtask(session, executor.id)
    if sub is None:
        return AssignmentResponse(assigned=False)

    parent = await session.get(DownloadTask, sub.task_id)
    if parent is None:
        raise HTTPException(status_code=500, detail="parent task missing")
    storage = await session.get(StorageBackend, parent.storage_id)
    if storage is None:
        raise HTTPException(status_code=500, detail="storage backend missing")

    raw = bytes(storage.config_encrypted) if storage.config_encrypted else b"{}"
    try:
        cfg_dict = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        cfg_dict = {}
    cfg_dict.setdefault("bucket", storage.name)
    cfg_dict.setdefault("region", storage.region or "us-east-1")
    storage_config = StorageConfig(**cfg_dict)

    sub_read = SubTaskRead.model_validate(sub)
    await session.commit()
    return AssignmentResponse(
        assigned=True,
        subtask=sub_read,
        assignment_token=token,
        repo_id=parent.repo_id,
        revision=parent.revision,
        storage_config=storage_config,
    )
