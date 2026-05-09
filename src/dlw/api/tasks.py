"""Tasks API: POST / GET list / GET by id.

Week 2: tenant_id=1, project_id=1, owner_user_id=1 hardcoded. Multi-tenancy
scoping via JWT claims comes in Phase 3.
Week 3 UI scaffold: GET /{id} returns TaskDetail (with subtasks).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from dlw.auth.bearer import require_bearer
from dlw.db.models.task import DownloadTask
from dlw.db.session import get_engine
from dlw.schemas.task import TaskCreate, TaskDetail, TaskList, TaskRead
from dlw.services.task_service import create_task

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

_TENANT_ID = 1
_PROJECT_ID = 1
_OWNER_USER_ID = 1


async def _session():
    """Per-request session backed by Phase 1's lru_cached singleton engine.

    Do NOT call engine.dispose() here — would race with concurrent requests
    sharing the same pool (same root cause as Phase 1 P1-A health.py fix).
    Lifespan disposes the engine once at app shutdown.
    """
    engine = get_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_bearer)])
async def post_task(body: TaskCreate, session: AsyncSession = Depends(_session)) -> TaskRead:
    task = await create_task(
        session, body,
        owner_user_id=_OWNER_USER_ID, tenant_id=_TENANT_ID, project_id=_PROJECT_ID,
    )
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
