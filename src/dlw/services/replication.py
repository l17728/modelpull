"""v2.1 Sprint 4 — Replication job service.

Creates + lists + cancels replication jobs. Sprint 4 deliberately does
NOT include the worker that copies bytes (that's Sprint 5). All jobs
created here stay in `pending` until the worker (services/replication_worker.py
in Sprint 5) picks them up.

Constraints:
- Tenant isolation: callers can only see + cancel their own jobs.
- Same-object idempotency: a partial unique index on
  (source_object_id, target_storage_id) WHERE status IN ('pending','running')
  prevents queuing a duplicate live job. Re-queueing after a completed
  job IS allowed (e.g. to re-replicate after eviction).
- Same-storage check: source object and target storage cannot be the
  same backend (that would be a no-op + would hit the unique).

Caller commits."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.replication import ReplicationJob
from dlw.db.models.storage import StorageBackend
from dlw.db.models.storage_object import StorageObject
from dlw.services.audit import write_audit

logger = logging.getLogger(__name__)

_LIVE_STATUSES = ("pending", "running")
_TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "skipped_existing")


class ObjectNotFound(LookupError):
    """Source storage_object missing or in another tenant."""


class TargetNotFound(LookupError):
    """target_storage_id doesn't exist or not visible to this tenant."""


class InvalidTarget(ValueError):
    """source object's storage_id == target_storage_id (no-op)."""


class DuplicateJob(ValueError):
    """An active (pending/running) job for the same (object, target) exists."""


class JobNotFound(LookupError):
    """No job with that id (or wrong tenant)."""


class NotCancellable(ValueError):
    """Job is already in a terminal status."""


@dataclass(frozen=True)
class CreateJobRequest:
    source_object_id: int
    target_storage_id: int


async def create_replication_job(
    session: AsyncSession, *, tenant_id: int, actor_user_id: int,
    req: CreateJobRequest,
) -> ReplicationJob:
    """Create a new replication job in `pending` state. Validates that the
    source object belongs to this tenant and target storage is reachable."""
    source = await session.get(StorageObject, req.source_object_id)
    if source is None or source.tenant_id != tenant_id:
        logger.warning(
            "create_replication_job: source object %d not in tenant %d",
            req.source_object_id, tenant_id)
        raise ObjectNotFound(str(req.source_object_id))

    # Target backend must exist and be visible to this tenant (own or global).
    target = await session.get(StorageBackend, req.target_storage_id)
    if target is None or (target.tenant_id is not None
                           and target.tenant_id != tenant_id):
        logger.warning(
            "create_replication_job: target storage %d not visible to tenant %d",
            req.target_storage_id, tenant_id)
        raise TargetNotFound(str(req.target_storage_id))

    if source.storage_id == req.target_storage_id:
        raise InvalidTarget(
            "source and target storage_id are the same — no-op")

    job = ReplicationJob(
        tenant_id=tenant_id,
        source_object_id=req.source_object_id,
        target_storage_id=req.target_storage_id,
        status="pending",
    )
    session.add(job)
    try:
        await session.flush()
    except IntegrityError as e:
        # Partial unique caught a live duplicate — surface as DuplicateJob
        # so the caller can return 409 cleanly.
        raise DuplicateJob(
            f"active job for object {req.source_object_id} → storage "
            f"{req.target_storage_id} already exists") from e
    logger.info(
        "create_replication_job: id=%d tenant=%d actor=%d "
        "object=%d → storage=%d",
        job.id, tenant_id, actor_user_id,
        req.source_object_id, req.target_storage_id)
    await write_audit(
        session, action="replication.job.create",
        resource_type="replication_job", resource_id=str(job.id),
        outcome="success", tenant_id=tenant_id, actor_user_id=actor_user_id,
        payload={"source_object_id": req.source_object_id,
                  "target_storage_id": req.target_storage_id})
    return job


async def list_replication_jobs(
    session: AsyncSession, *, tenant_id: int,
    status: str | None = None, limit: int = 50,
) -> list[ReplicationJob]:
    """List replication jobs for one tenant (newest first)."""
    stmt = select(ReplicationJob).where(ReplicationJob.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(ReplicationJob.status == status)
    stmt = stmt.order_by(ReplicationJob.id.desc()).limit(max(1, min(int(limit), 200)))
    return list((await session.execute(stmt)).scalars().all())


async def get_replication_job(
    session: AsyncSession, *, tenant_id: int, job_id: int,
) -> ReplicationJob:
    """Get one job by id, scoped to tenant."""
    job = await session.get(ReplicationJob, job_id)
    if job is None or job.tenant_id != tenant_id:
        raise JobNotFound(str(job_id))
    return job


async def cancel_replication_job(
    session: AsyncSession, *, tenant_id: int, actor_user_id: int,
    job_id: int,
) -> ReplicationJob:
    """Mark a pending/running job as cancelled. Terminal jobs return
    NotCancellable (mapped to HTTP 409 by the REST endpoint)."""
    job = await get_replication_job(session, tenant_id=tenant_id, job_id=job_id)
    if job.status in _TERMINAL_STATUSES:
        raise NotCancellable(job.status)
    job.status = "cancelled"
    from datetime import UTC, datetime
    job.completed_at = datetime.now(UTC)
    logger.info(
        "cancel_replication_job: id=%d tenant=%d actor=%d (was %s)",
        job_id, tenant_id, actor_user_id, job.status)
    await write_audit(
        session, action="replication.job.cancel",
        resource_type="replication_job", resource_id=str(job_id),
        outcome="success", tenant_id=tenant_id, actor_user_id=actor_user_id,
        payload={})
    return job
