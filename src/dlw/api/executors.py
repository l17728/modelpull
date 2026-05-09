"""Executors API: join / heartbeat / poll.

Phase 1 W4: /poll response now includes repo_id + revision + storage_config
so the executor can construct HF URL + boto3 client without a second roundtrip.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.auth.bearer import require_bearer
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

    # Load parent task + storage backend (single round-trip via direct gets)
    parent = await session.get(DownloadTask, sub.task_id)
    if parent is None:
        # Should never happen given FK; defensive
        raise HTTPException(status_code=500, detail="parent task missing")
    storage = await session.get(StorageBackend, parent.storage_id)
    if storage is None:
        raise HTTPException(status_code=500, detail="storage backend missing")

    # Phase 1: config_encrypted is plain JSON bytes (Phase 3 plan adds envelope)
    raw = bytes(storage.config_encrypted) if storage.config_encrypted else b"{}"
    try:
        cfg_dict = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        cfg_dict = {}
    # Tolerate empty config (test fixtures use b""): default bucket = name
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
