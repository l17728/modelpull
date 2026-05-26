"""v2.1 Sprint 4 — Cross-region replication REST endpoints.

Tenant-scoped CRUD over the replication_jobs table. Sprint 4 ships
create / list / get / cancel — the worker that actually moves bytes
lands in Sprint 5."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.principal import Principal, require_principal
from dlw.db.session import get_engine
from dlw.services.replication import (
    CreateJobRequest,
    DuplicateJob,
    InvalidTarget,
    JobNotFound,
    NotCancellable,
    ObjectNotFound,
    TargetNotFound,
    cancel_replication_job,
    create_replication_job,
    get_replication_job,
    list_replication_jobs,
)

router = APIRouter(prefix="/api/v1/replication", tags=["replication"])


async def _session() -> AsyncSession:
    f = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with f() as s:
        yield s


class ReplicationJobOut(BaseModel):
    id: int
    tenant_id: int
    source_object_id: int
    target_storage_id: int
    status: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    bytes_transferred: int
    retry_count: int
    error_message: str | None


class CreateBody(BaseModel):
    source_object_id: int
    target_storage_id: int


def _to_out(job) -> ReplicationJobOut:
    return ReplicationJobOut.model_validate(job, from_attributes=True)


@router.post("", status_code=201)
async def post_replication_job(
    body: CreateBody,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(_session),
) -> ReplicationJobOut:
    try:
        job = await create_replication_job(
            session, tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            req=CreateJobRequest(
                source_object_id=body.source_object_id,
                target_storage_id=body.target_storage_id))
    except ObjectNotFound as e:
        raise HTTPException(404, detail="source_object not found") from e
    except TargetNotFound as e:
        raise HTTPException(404, detail="target_storage not found") from e
    except InvalidTarget as e:
        raise HTTPException(422, detail={"code": "INVALID_TARGET",
                                          "message": str(e)}) from e
    except DuplicateJob as e:
        raise HTTPException(409, detail={"code": "DUPLICATE_JOB",
                                          "message": str(e)}) from e
    await session.commit()
    return _to_out(job)


@router.get("")
async def get_replication_jobs(
    status: str | None = None,
    limit: int = 50,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(_session),
) -> dict:
    jobs = await list_replication_jobs(
        session, tenant_id=principal.tenant_id, status=status, limit=limit)
    return {"items": [_to_out(j).model_dump(mode="json") for j in jobs]}


@router.get("/{job_id}")
async def get_one_replication_job(
    job_id: int,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(_session),
) -> ReplicationJobOut:
    try:
        job = await get_replication_job(
            session, tenant_id=principal.tenant_id, job_id=job_id)
    except JobNotFound as e:
        raise HTTPException(404, detail="job not found") from e
    return _to_out(job)


@router.post("/{job_id}/cancel")
async def post_cancel_replication_job(
    job_id: int,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(_session),
) -> ReplicationJobOut:
    try:
        job = await cancel_replication_job(
            session, tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id, job_id=job_id)
    except JobNotFound as e:
        raise HTTPException(404, detail="job not found") from e
    except NotCancellable as e:
        raise HTTPException(409, detail={"code": "NOT_CANCELLABLE",
                                          "status": str(e)}) from e
    await session.commit()
    return _to_out(job)
