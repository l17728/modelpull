"""v2.1 Sprint 4 — Replication job service tests."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
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


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.storage_object import StorageObject
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=600, slug="repA", display_name="RepA"))
        s.add(Tenant(id=601, slug="repB", display_name="RepB"))
        await s.flush()
        s.add(StorageBackend(id=600, tenant_id=600, name="src",
                              backend_type="s3", config_encrypted=b"",
                              region="us-east-1"))
        s.add(StorageBackend(id=601, tenant_id=600, name="dst",
                              backend_type="s3", config_encrypted=b"",
                              region="ap-east-1"))
        s.add(StorageBackend(id=602, tenant_id=601, name="other-tenant",
                              backend_type="s3", config_encrypted=b""))
        await s.flush()
        s.add(StorageObject(
            id=6000, tenant_id=600, storage_id=600, storage_key="k600",
            sha256="6" * 64, size=1024, refcount=1))
        s.add(StorageObject(
            id=6001, tenant_id=601, storage_id=602, storage_key="k601",
            sha256="7" * 64, size=2048, refcount=1))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


async def test_create_happy_path(session):
    job = await create_replication_job(
        session, tenant_id=600, actor_user_id=1,
        req=CreateJobRequest(source_object_id=6000, target_storage_id=601))
    assert job.status == "pending"
    assert job.tenant_id == 600
    assert job.source_object_id == 6000
    assert job.target_storage_id == 601
    assert job.bytes_transferred == 0
    await session.rollback()


async def test_create_unknown_source_object(session):
    with pytest.raises(ObjectNotFound):
        await create_replication_job(
            session, tenant_id=600, actor_user_id=1,
            req=CreateJobRequest(source_object_id=99999, target_storage_id=601))


async def test_create_cross_tenant_source_rejected(session):
    """A tenant cannot replicate another tenant's object."""
    with pytest.raises(ObjectNotFound):
        await create_replication_job(
            session, tenant_id=600, actor_user_id=1,
            req=CreateJobRequest(source_object_id=6001, target_storage_id=601))


async def test_create_unknown_target(session):
    with pytest.raises(TargetNotFound):
        await create_replication_job(
            session, tenant_id=600, actor_user_id=1,
            req=CreateJobRequest(source_object_id=6000, target_storage_id=99999))


async def test_create_target_in_another_tenant_rejected(session):
    """Target backend belonging to a different tenant is invisible."""
    with pytest.raises(TargetNotFound):
        await create_replication_job(
            session, tenant_id=600, actor_user_id=1,
            req=CreateJobRequest(source_object_id=6000, target_storage_id=602))


async def test_create_same_storage_rejected(session):
    """Cannot replicate to the same backend the object is already on."""
    with pytest.raises(InvalidTarget):
        await create_replication_job(
            session, tenant_id=600, actor_user_id=1,
            req=CreateJobRequest(source_object_id=6000, target_storage_id=600))


async def test_create_duplicate_active_job_rejected(session):
    await create_replication_job(
        session, tenant_id=600, actor_user_id=1,
        req=CreateJobRequest(source_object_id=6000, target_storage_id=601))
    with pytest.raises(DuplicateJob):
        await create_replication_job(
            session, tenant_id=600, actor_user_id=1,
            req=CreateJobRequest(source_object_id=6000, target_storage_id=601))
    await session.rollback()


async def test_list_filters_by_tenant(session):
    """Tenant 601 should never see tenant 600's jobs."""
    await create_replication_job(
        session, tenant_id=600, actor_user_id=1,
        req=CreateJobRequest(source_object_id=6000, target_storage_id=601))
    await session.commit()
    jobs_other = await list_replication_jobs(session, tenant_id=601)
    assert all(j.tenant_id == 601 for j in jobs_other)
    # Tenant 600 sees its own
    jobs_own = await list_replication_jobs(session, tenant_id=600)
    assert any(j.tenant_id == 600 for j in jobs_own)
    # Cleanup — set to terminal so subsequent tests don't trip the partial unique
    from dlw.db.models.replication import ReplicationJob
    from sqlalchemy import select, update
    await session.execute(
        update(ReplicationJob).where(ReplicationJob.tenant_id == 600)
        .values(status="succeeded"))
    await session.commit()


async def test_list_filters_by_status(session):
    job = await create_replication_job(
        session, tenant_id=600, actor_user_id=1,
        req=CreateJobRequest(source_object_id=6000, target_storage_id=601))
    await session.commit()
    pendings = await list_replication_jobs(
        session, tenant_id=600, status="pending")
    assert any(j.id == job.id for j in pendings)
    succeeded = await list_replication_jobs(
        session, tenant_id=600, status="succeeded")
    assert all(j.status == "succeeded" for j in succeeded)
    # Cleanup
    from sqlalchemy import update
    from dlw.db.models.replication import ReplicationJob
    await session.execute(
        update(ReplicationJob).where(ReplicationJob.id == job.id)
        .values(status="succeeded"))
    await session.commit()


async def test_get_cross_tenant_returns_not_found(session):
    job = await create_replication_job(
        session, tenant_id=600, actor_user_id=1,
        req=CreateJobRequest(source_object_id=6000, target_storage_id=601))
    await session.commit()
    with pytest.raises(JobNotFound):
        await get_replication_job(session, tenant_id=601, job_id=job.id)
    # Cleanup
    from sqlalchemy import update
    from dlw.db.models.replication import ReplicationJob
    await session.execute(
        update(ReplicationJob).where(ReplicationJob.id == job.id)
        .values(status="succeeded"))
    await session.commit()


async def test_cancel_pending_job(session):
    job = await create_replication_job(
        session, tenant_id=600, actor_user_id=1,
        req=CreateJobRequest(source_object_id=6000, target_storage_id=601))
    await session.commit()
    cancelled = await cancel_replication_job(
        session, tenant_id=600, actor_user_id=1, job_id=job.id)
    assert cancelled.status == "cancelled"
    assert cancelled.completed_at is not None
    await session.commit()


async def test_cancel_terminal_returns_not_cancellable(session):
    job = await create_replication_job(
        session, tenant_id=600, actor_user_id=1,
        req=CreateJobRequest(source_object_id=6000, target_storage_id=601))
    # Manually mark succeeded
    from sqlalchemy import update
    from dlw.db.models.replication import ReplicationJob
    await session.execute(
        update(ReplicationJob).where(ReplicationJob.id == job.id)
        .values(status="succeeded"))
    await session.flush()
    with pytest.raises(NotCancellable):
        await cancel_replication_job(
            session, tenant_id=600, actor_user_id=1, job_id=job.id)
    await session.rollback()


async def test_audit_row_on_create(session):
    from sqlalchemy import select
    from dlw.db.models.audit import AuditLog
    job = await create_replication_job(
        session, tenant_id=600, actor_user_id=42,
        req=CreateJobRequest(source_object_id=6000, target_storage_id=601))
    audit = (await session.execute(
        select(AuditLog).where(
            AuditLog.action == "replication.job.create",
            AuditLog.resource_id == str(job.id)))).scalar_one()
    assert audit.actor_user_id == 42
    assert audit.payload["source_object_id"] == 6000
    assert audit.payload["target_storage_id"] == 601
    await session.rollback()
