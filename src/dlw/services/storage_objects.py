"""Global-dedup storage_objects service (Phase 3 SP3; doc 06 §3.1).
Caller commits (service-layer convention)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.storage_object import StorageObject, SubtaskObjectRef


async def _ref_exists(session: AsyncSession, subtask_id: uuid.UUID) -> bool:
    return (await session.scalar(
        select(SubtaskObjectRef.object_id)
        .where(SubtaskObjectRef.subtask_id == subtask_id).limit(1))
    ) is not None


async def _upsert_object(
    session: AsyncSession, *, tenant_id: int, storage_id: int,
    storage_key: str, sha256: str, size: int, bump: bool,
) -> int:
    stmt = pg_insert(StorageObject).values(
        tenant_id=tenant_id, storage_id=storage_id, storage_key=storage_key,
        sha256=sha256, size=size, refcount=1)
    if bump:
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "storage_id", "sha256"],
            set_={"refcount": StorageObject.refcount + 1,
                  "last_referenced_at": datetime.now(UTC)})
    else:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["tenant_id", "storage_id", "sha256"])
    await session.execute(stmt)
    return await session.scalar(
        select(StorageObject.id).where(
            StorageObject.tenant_id == tenant_id,
            StorageObject.storage_id == storage_id,
            StorageObject.sha256 == sha256))


async def record_ref_only(
    session: AsyncSession, *, tenant_id: int, storage_id: int,
    storage_key: str, sha256: str, size: int, subtask_id: uuid.UUID,
) -> int:
    oid = await _upsert_object(
        session, tenant_id=tenant_id, storage_id=storage_id,
        storage_key=storage_key, sha256=sha256, size=size, bump=True)
    await session.execute(pg_insert(SubtaskObjectRef).values(
        subtask_id=subtask_id, object_id=oid).on_conflict_do_nothing())
    return oid


async def record_object(
    session: AsyncSession, *, tenant_id: int, storage_id: int,
    storage_key: str, sha256: str, size: int, subtask_id: uuid.UUID,
) -> None:
    """complete_subtask success path. Idempotent for inherit subtasks: if a
    ref already exists for this subtask (added by record_ref_only in
    diff_and_dedup), do nothing — never double-count."""
    if await _ref_exists(session, subtask_id):
        return
    oid = await _upsert_object(
        session, tenant_id=tenant_id, storage_id=storage_id,
        storage_key=storage_key, sha256=sha256, size=size, bump=True)
    await session.execute(pg_insert(SubtaskObjectRef).values(
        subtask_id=subtask_id, object_id=oid).on_conflict_do_nothing())


async def deref_subtask(
    session: AsyncSession, subtask_id: uuid.UUID
) -> None:
    oids = (await session.execute(
        select(SubtaskObjectRef.object_id)
        .where(SubtaskObjectRef.subtask_id == subtask_id))).scalars().all()
    for oid in oids:
        await session.execute(
            update(StorageObject).where(StorageObject.id == oid)
            .values(refcount=StorageObject.refcount - 1))
    await session.execute(
        delete(SubtaskObjectRef)
        .where(SubtaskObjectRef.subtask_id == subtask_id))


async def gc_orphans(
    session: AsyncSession, *, grace_seconds: int
) -> int:
    """Delete storage_objects rows with refcount<=0 older than the grace.
    SKIP LOCKED so it never blocks ref/deref. Returns count. Caller commits."""
    from datetime import timedelta
    cutoff = datetime.now(UTC) - timedelta(seconds=grace_seconds)
    ids = (await session.execute(
        select(StorageObject.id)
        .where(StorageObject.refcount <= 0,
                StorageObject.created_at < cutoff)
        .with_for_update(skip_locked=True))).scalars().all()
    if not ids:
        return 0
    await session.execute(
        delete(StorageObject).where(StorageObject.id.in_(ids)))
    return len(ids)
