"""Global-dedup storage_objects service (Phase 3 SP3; doc 06 §3.1).
Caller commits (service-layer convention)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import case, delete, exists, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.storage_object import StorageObject, StoragePhysicalKey, SubtaskObjectRef


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


async def record_physical_key(
    session: AsyncSession, *, tenant_id: int, storage_id: int,
    sha256: str, storage_key: str, size: int, executor_id: str | None = None,
) -> None:
    """Phase 4: durable ledger of a physical object key (download + inherit).
    Idempotent on (tenant, storage, key). Caller commits."""
    await session.execute(pg_insert(StoragePhysicalKey).values(
        tenant_id=tenant_id, storage_id=storage_id, sha256=sha256,
        storage_key=storage_key, size=size,
        executor_id=executor_id).on_conflict_do_nothing(
            index_elements=["tenant_id", "storage_id", "storage_key"]))


async def pressured_tenant_ids(
    session: AsyncSession, *, threshold: float = 0.9,
) -> frozenset[int]:
    """Tenants at/over `threshold` of their storage quota (from the maintained
    QuotaSnapshot). Empty if none / no quota set."""
    from dlw.db.models.tenant import Tenant
    from dlw.db.models.usage import QuotaSnapshot
    rows = (await session.execute(
        select(QuotaSnapshot.tenant_id)
        .join(Tenant, Tenant.id == QuotaSnapshot.tenant_id)
        .where(Tenant.quota_storage_gb > 0,
               QuotaSnapshot.storage_gb_used
               >= threshold * Tenant.quota_storage_gb))).scalars().all()
    return frozenset(int(t) for t in rows)


async def reclaim_physical_orphans(
    session: AsyncSession, *, grace_seconds: int, delete_enabled: bool,
    make_client, audit, max_objects_per_tick: int = 1000,
    priority_tenant_ids: frozenset[int] = frozenset(),
) -> dict:
    """Reclaim physical keys whose content sha has NO surviving storage_objects
    row (fully dereferenced) and are past grace. S3 backends only; bytes deleted
    only when delete_enabled. Deletes bytes BEFORE the ledger row (crash-safe).
    Caps candidates at max_objects_per_tick (oldest first). Caller commits.
    make_client(storage_id) -> (client, bucket, backend_type)."""
    from datetime import timedelta

    from dlw.services.storage_client import delete_object_silently
    cutoff = datetime.now(UTC) - timedelta(seconds=grace_seconds)
    live = exists().where(
        StorageObject.tenant_id == StoragePhysicalKey.tenant_id,
        StorageObject.storage_id == StoragePhysicalKey.storage_id,
        StorageObject.sha256 == StoragePhysicalKey.sha256)
    if priority_tenant_ids:
        prio = case(
            (StoragePhysicalKey.tenant_id.in_(priority_tenant_ids), 0), else_=1)
        stmt = (select(StoragePhysicalKey)
                .where(StoragePhysicalKey.created_at < cutoff, ~live)
                .order_by(prio, StoragePhysicalKey.created_at))
    else:
        stmt = (select(StoragePhysicalKey)
                .where(StoragePhysicalKey.created_at < cutoff, ~live)
                .order_by(StoragePhysicalKey.created_at))
    rows = (await session.execute(
        stmt.limit(max(1, max_objects_per_tick))
        .with_for_update(skip_locked=True))).scalars().all()
    deleted = 0
    clients: dict[int, tuple] = {}
    for row in rows:
        if not delete_enabled:
            continue
        if row.storage_id not in clients:
            clients[row.storage_id] = make_client(row.storage_id)
        client, bucket, backend_type = clients[row.storage_id]
        if backend_type != "s3" or client is None:
            continue
        ok = await delete_object_silently(client, bucket, row.storage_key)
        if not ok:
            continue
        audit(action="storage.gc.physical", id=row.id, tenant_id=row.tenant_id,
              storage_key=row.storage_key, size=row.size)
        await session.delete(row)
        deleted += 1
    return {"candidates": len(rows), "deleted": deleted}


async def confirm_local_reclaim(
    session: AsyncSession, executor_id: str, key_ids: list[int], *, audit,
) -> int:
    rows = (await session.execute(
        select(StoragePhysicalKey).where(
            StoragePhysicalKey.id.in_(key_ids),
            StoragePhysicalKey.executor_id == executor_id))).scalars().all()
    for r in rows:
        await audit(action="storage.gc.physical.local", id=r.id,
                    tenant_id=r.tenant_id, storage_key=r.storage_key, size=r.size)
        await session.delete(r)
    return len(rows)


async def dispatch_local_reclaim(
    session: AsyncSession, executor_id: str, *, grace_seconds: int, limit: int,
) -> list:
    """Local-fs orphan keys written by this executor, past grace. Returns
    list[ReclaimItem] — base_path resolved from the backend config (a backend
    whose config lacks base_path is SKIPPED, not dispatched — local backends
    MUST carry base_path in config_encrypted)."""
    from datetime import timedelta

    from dlw.db.models.storage import StorageBackend
    from dlw.schemas.executor import ReclaimItem
    from dlw.services.storage_client import storage_config_from_backend

    cutoff = datetime.now(UTC) - timedelta(seconds=grace_seconds)
    live = exists().where(
        StorageObject.tenant_id == StoragePhysicalKey.tenant_id,
        StorageObject.storage_id == StoragePhysicalKey.storage_id,
        StorageObject.sha256 == StoragePhysicalKey.sha256)
    rows = (await session.execute(
        select(StoragePhysicalKey)
        .join(StorageBackend, StorageBackend.id == StoragePhysicalKey.storage_id)
        .where(StoragePhysicalKey.executor_id == executor_id,
               StoragePhysicalKey.created_at < cutoff, ~live,
               StorageBackend.backend_type == "local")
        .order_by(StoragePhysicalKey.created_at)
        .limit(max(1, limit))
        .with_for_update(skip_locked=True, of=StoragePhysicalKey))).scalars().all()
    out: list[ReclaimItem] = []
    cache: dict[int, str | None] = {}
    for r in rows:
        if r.storage_id not in cache:
            b = await session.get(StorageBackend, r.storage_id)
            cache[r.storage_id] = (storage_config_from_backend(b).base_path
                                   if b else None)
        base = cache[r.storage_id]
        if base:
            out.append(ReclaimItem(id=r.id, base_path=base,
                                   storage_key=r.storage_key))
    return out


async def count_stuck_local_orphans(session: AsyncSession) -> int:
    """Count local-fs physical keys that are past-grace and ~live but have no
    dispatchable executor (executor_id IS NULL). Cheap single-query gauge for
    observability; called by the heartbeat handler when gc_delete_physical_bytes
    is enabled. Caller does NOT commit."""
    from datetime import timedelta

    from sqlalchemy import func as _func

    from dlw.db.models.storage import StorageBackend
    cutoff = datetime.now(UTC) - timedelta(seconds=86400)  # 1-day heuristic
    live = exists().where(
        StorageObject.tenant_id == StoragePhysicalKey.tenant_id,
        StorageObject.storage_id == StoragePhysicalKey.storage_id,
        StorageObject.sha256 == StoragePhysicalKey.sha256)
    result = await session.scalar(
        select(_func.count(StoragePhysicalKey.id))
        .join(StorageBackend, StorageBackend.id == StoragePhysicalKey.storage_id)
        .where(
            StoragePhysicalKey.executor_id.is_(None),
            StoragePhysicalKey.created_at < cutoff,
            ~live,
            StorageBackend.backend_type == "local",
        ))
    return int(result or 0)


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
