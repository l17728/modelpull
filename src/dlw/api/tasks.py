"""Tasks API: POST / GET list / GET by id / cancel — principal-scoped (SP1)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.models.task import DownloadTask
from dlw.db.session import get_engine
from dlw.db.tenant_scope import tenant_filtered
from dlw.schemas.task import TaskCreate, TaskDetail, TaskList, TaskRead
from dlw.schemas.task_detail import (
    ParticipatingExecutors,
    SourceAllocation,
    SubtaskChunkReport,
    TaskEventsResponse,
)
from dlw.services import task_detail as _td
from dlw.services.audit import write_audit
from dlw.services.hf_metadata import (
    HfNetworkError,
    HfPrivateOrAuthRequired,
    RepoNotFound,
)
from dlw.services.quota import QuotaExceeded, check_quota_for_new_task
from dlw.services.storage_objects import deref_subtask
from dlw.services.task_service import EmptyRepo, cancel_task, create_task

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


async def _session():
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as session:
        yield session


async def _resolve_project(session: AsyncSession, principal: Principal,
                           body: TaskCreate) -> int:
    """Body project_id if the principal's tenant owns it, else the tenant's
    lowest-id project (its default)."""
    from dlw.db.models.tenant import Project
    requested = getattr(body, "project_id", None)
    if requested is not None:
        owns = await session.scalar(
            select(Project.id).where(Project.id == requested,
                                     Project.tenant_id == principal.tenant_id))
        if owns is None:
            raise HTTPException(403, detail={"code": "RBAC_DENIED"})
        return int(requested)
    pid = await session.scalar(
        select(func.min(Project.id)).where(
            Project.tenant_id == principal.tenant_id))
    if pid is None:
        raise HTTPException(409, detail="tenant has no project")
    return int(pid)


@router.post("", status_code=status.HTTP_201_CREATED)
async def post_task(
    body: TaskCreate,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "POST")),
    session: AsyncSession = Depends(_session),
) -> TaskRead:
    settings = get_settings()
    try:
        await check_quota_for_new_task(session, principal.tenant_id)
    except QuotaExceeded as e:
        # Spec §7 error-matrix + doc 04 §9.2: 429 must be audited.
        await write_audit(
            session, action="quota.exceeded", resource_type="tenant",
            resource_id=str(principal.tenant_id), outcome="denied",
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id or None,
            payload={"metric": e.metric})
        await session.commit()
        raise HTTPException(
            status_code=429,
            detail={"code": "QUOTA_EXCEEDED", "metric": e.metric}) from e
    project_id = await _resolve_project(session, principal, body)
    try:
        task = await create_task(
            session, body,
            owner_user_id=principal.user_id, tenant_id=principal.tenant_id,
            project_id=project_id,
            hf_endpoint=settings.hf_endpoint, hf_token=settings.hf_token,
        )
    except RepoNotFound as e:
        raise HTTPException(status_code=404,
                            detail=f"repo or revision not found: {e}") from e
    except HfPrivateOrAuthRequired as e:
        raise HTTPException(
            status_code=422,
            detail=f"repo is private or requires auth — public only: {e}",
        ) from e
    except HfNetworkError as e:
        raise HTTPException(status_code=503,
                            detail=f"huggingface unreachable: {e}") from e
    except EmptyRepo as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    await session.commit()
    return TaskRead.model_validate(task)


@router.get("")
async def list_tasks(
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
    session: AsyncSession = Depends(_session),
) -> TaskList:
    rows = (await session.execute(
        tenant_filtered(select(DownloadTask), DownloadTask, principal)
        .order_by(DownloadTask.created_at.desc())
    )).scalars().all()
    total = await session.scalar(
        tenant_filtered(select(func.count()).select_from(DownloadTask),
                        DownloadTask, principal))
    return TaskList(items=[TaskRead.model_validate(r) for r in rows],
                    total=int(total or 0))


@router.get("/{task_id}")
async def get_task(
    task_id: uuid.UUID,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
    session: AsyncSession = Depends(_session),
) -> TaskDetail:
    row = (await session.execute(
        tenant_filtered(
            select(DownloadTask).where(DownloadTask.id == task_id),
            DownloadTask, principal)
        .options(selectinload(DownloadTask.subtasks))
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return TaskDetail.model_validate(row)


@router.post("/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def post_cancel_task(
    task_id: uuid.UUID,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "DELETE")),
    session: AsyncSession = Depends(_session),
) -> TaskRead:
    owned = await session.scalar(
        tenant_filtered(select(DownloadTask.id)
                        .where(DownloadTask.id == task_id),
                        DownloadTask, principal))
    if owned is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        task = await cancel_task(session, task_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await session.commit()
    return TaskRead.model_validate(task)


@router.patch("/{task_id}")
async def patch_task_route(
    task_id: uuid.UUID,
    body: dict,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "POST")),
    session: AsyncSession = Depends(_session),
) -> TaskRead:
    from dlw.services.task_patch import (
        InvalidPatch, TaskNotFound, TaskPatch, TaskTerminal, patch_task,
    )
    patch = TaskPatch(
        priority=body.get("priority"),
        source_strategy=body.get("source_strategy"),
        source_blacklist=body.get("source_blacklist"))
    try:
        task = await patch_task(
            session, task_id=task_id, tenant_id=principal.tenant_id,
            patch=patch)
    except TaskNotFound as e:
        raise HTTPException(status_code=404, detail="task not found") from e
    except TaskTerminal as e:
        raise HTTPException(
            status_code=409,
            detail={"code": "TASK_TERMINAL", "status": str(e)}) from e
    except InvalidPatch as e:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_PATCH", "message": str(e)}) from e
    await session.commit()
    return TaskRead.model_validate(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT,
                response_model=None)
async def delete_task(
    task_id: uuid.UUID,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "DELETE")),
    session: AsyncSession = Depends(_session),
) -> None:
    row = (await session.execute(
        tenant_filtered(
            select(DownloadTask).where(DownloadTask.id == task_id),
            DownloadTask, principal)
        .options(selectinload(DownloadTask.subtasks))
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    if row.status not in ("succeeded", "failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail={"code": "TASK_NOT_TERMINAL", "status": row.status})
    for sub in row.subtasks:
        await deref_subtask(session, sub.id)
    await session.delete(row)          # FK cascade → subtasks → object refs
    await session.commit()


async def _task_in_tenant(
    session: AsyncSession, task_id: uuid.UUID, principal: Principal,
) -> bool:
    owned = await session.scalar(
        tenant_filtered(select(DownloadTask.id)
                        .where(DownloadTask.id == task_id),
                        DownloadTask, principal))
    return owned is not None


@router.get("/{task_id}/subtask-chunks")
async def get_subtask_chunks(
    task_id: uuid.UUID,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
    session: AsyncSession = Depends(_session),
) -> SubtaskChunkReport:
    if not await _task_in_tenant(session, task_id, principal):
        raise HTTPException(status_code=404, detail="task not found")
    return SubtaskChunkReport(
        items=await _td.chunks_for_task(session, task_id, principal.tenant_id))


@router.get("/{task_id}/source-allocation")
async def get_source_allocation(
    task_id: uuid.UUID,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
    session: AsyncSession = Depends(_session),
) -> SourceAllocation:
    if not await _task_in_tenant(session, task_id, principal):
        raise HTTPException(status_code=404, detail="task not found")
    return await _td.source_allocation_for_task(
        session, task_id, principal.tenant_id)


@router.get("/{task_id}/participating-executors")
async def get_participating_executors(
    task_id: uuid.UUID,
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
    session: AsyncSession = Depends(_session),
) -> ParticipatingExecutors:
    if not await _task_in_tenant(session, task_id, principal):
        raise HTTPException(status_code=404, detail="task not found")
    return ParticipatingExecutors(
        items=await _td.executors_for_task(
            session, task_id, principal.tenant_id))


@router.get("/{task_id}/events")
async def get_task_events(
    task_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
    session: AsyncSession = Depends(_session),
) -> TaskEventsResponse:
    if not await _task_in_tenant(session, task_id, principal):
        raise HTTPException(status_code=404, detail="task not found")
    items, next_cursor = await _td.events_for_task(
        session, task_id, principal.tenant_id, limit, cursor)
    return TaskEventsResponse(items=items, next_cursor=next_cursor)
