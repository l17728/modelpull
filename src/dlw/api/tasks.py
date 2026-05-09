"""Tasks API: POST / GET list / GET by id.

Phase 1 W4: POST /tasks now calls HF Hub via task_service to enumerate the
repo's files at the given revision. Errors translated to user-visible HTTP
status codes:
  - HF 404 (repo or revision missing)        -> 404
  - HF 401/403 (private or auth required)    -> 422 (Phase 1 only supports public)
  - HF 5xx / network                          -> 503
  - Empty repo                                -> 422
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from dlw.auth.bearer import require_bearer
from dlw.config import get_settings
from dlw.db.models.task import DownloadTask
from dlw.db.session import get_engine
from dlw.schemas.task import TaskCreate, TaskDetail, TaskList, TaskRead
from dlw.services.hf_metadata import (
    HfNetworkError,
    HfPrivateOrAuthRequired,
    RepoNotFound,
)
from dlw.services.task_service import EmptyRepo, create_task

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

_TENANT_ID = 1
_PROJECT_ID = 1
_OWNER_USER_ID = 1


async def _session():
    engine = get_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_bearer)])
async def post_task(body: TaskCreate, session: AsyncSession = Depends(_session)) -> TaskRead:
    settings = get_settings()
    try:
        task = await create_task(
            session, body,
            owner_user_id=_OWNER_USER_ID, tenant_id=_TENANT_ID, project_id=_PROJECT_ID,
            hf_endpoint=settings.hf_endpoint, hf_token=settings.hf_token,
        )
    except RepoNotFound as e:
        raise HTTPException(status_code=404, detail=f"repo or revision not found: {e}") from e
    except HfPrivateOrAuthRequired as e:
        raise HTTPException(
            status_code=422,
            detail=f"repo is private or requires auth — Phase 1 supports public repos only: {e}",
        ) from e
    except HfNetworkError as e:
        raise HTTPException(status_code=503, detail=f"huggingface unreachable: {e}") from e
    except EmptyRepo as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    await session.commit()
    return TaskRead.model_validate(task)


@router.get("", dependencies=[Depends(require_bearer)])
async def list_tasks(session: AsyncSession = Depends(_session)) -> TaskList:
    rows = (await session.execute(
        select(DownloadTask).where(DownloadTask.tenant_id == _TENANT_ID)
        .order_by(DownloadTask.created_at.desc())
    )).scalars().all()
    total = await session.scalar(
        select(func.count()).select_from(DownloadTask)
        .where(DownloadTask.tenant_id == _TENANT_ID)
    )
    return TaskList(items=[TaskRead.model_validate(r) for r in rows], total=int(total or 0))


@router.get("/{task_id}", dependencies=[Depends(require_bearer)])
async def get_task(task_id: uuid.UUID, session: AsyncSession = Depends(_session)) -> TaskDetail:
    row = (await session.execute(
        select(DownloadTask)
          .where(DownloadTask.id == task_id, DownloadTask.tenant_id == _TENANT_ID)
          .options(selectinload(DownloadTask.subtasks))
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return TaskDetail.model_validate(row)
