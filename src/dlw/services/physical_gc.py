"""v2.1 Sprint 3 — Physical GC + LRU eviction (real implementation).

Builds on v2.0 Phase 4 which already:
  - Tracks `last_referenced_at` on `storage_objects` and
    `storage_physical_keys` (updated on every record_object call)
  - Maintains `refcount` on `storage_objects` (decremented by
    `deref_subtask` when a subtask is deleted)
  - Has `gc_orphans()` that deletes refcount<=0 rows from the **DB**
    after a grace period

What this module adds:
  1. Actually delete the **physical bytes** in S3/fs for orphaned objects
     (gc_orphans only deletes the DB row, leaving the S3 key behind)
  2. LRU eviction: when a tenant is over `quota_storage_gb`, delete
     least-recently-used objects even before they tombstone (refcount-
     protected — never touches actively-referenced objects)
  3. Audit each eviction so quota/storage forensics can reconstruct
  4. Orchestration that combines tombstone-clear + LRU eviction in one
     deterministic pass, gated by DLW_PHYSICAL_GC_ENABLED

Caller commits."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.storage import StorageBackend
from dlw.db.models.storage_object import StorageObject, StoragePhysicalKey
from dlw.db.models.tenant import Tenant
from dlw.services.audit import write_audit

logger = logging.getLogger(__name__)


class PhysicalGCNotImplemented(NotImplementedError):
    """Legacy marker — kept so callers of the old skeleton API can be
    migrated incrementally. New code raises specific errors below."""


class PhysicalGCDisabled(RuntimeError):
    """DLW_PHYSICAL_GC_ENABLED is false; refusing to operate."""


@dataclass(frozen=True)
class EvictionCandidate:
    """One object the GC has decided to remove (tombstone or LRU pressure)."""
    object_id: int
    storage_id: int
    storage_key: str
    sha256: str
    size_bytes: int
    reason: str   # "tombstone" | "lru_pressure"
    last_accessed_at: str | None = None


@dataclass(frozen=True)
class EvictionResult:
    candidate: EvictionCandidate
    deleted: bool
    error: str | None = None


class ObjectStoreDeleter(Protocol):
    """Backend-agnostic DeleteObject contract. Production impl wraps
    `services.storage_client.delete_object_silently` per backend; tests
    use an in-memory stub that just records calls."""

    async def delete(self, storage_id: int, key: str) -> bool:
        """True on successful delete (or already-gone), False on retryable."""
        ...


# Configuration knobs read from env at module import. Tests can override
# via monkeypatch then call _refresh_config() to repick.
_GC_ENABLED_DEFAULT = "false"   # opt-in for prod safety
_GC_INTERVAL_SECONDS_DEFAULT = 3600
_LRU_TARGET_FRACTION_DEFAULT = 0.90
_GC_BATCH_SIZE_DEFAULT = 100
_TOMBSTONE_GRACE_SECONDS_DEFAULT = 600   # 10 min after refcount→0


def _is_enabled() -> bool:
    return os.environ.get("DLW_PHYSICAL_GC_ENABLED", _GC_ENABLED_DEFAULT).lower() \
        in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------

async def find_tombstone_candidates(
    session: AsyncSession, *, limit: int = _GC_BATCH_SIZE_DEFAULT,
    grace_seconds: int = _TOMBSTONE_GRACE_SECONDS_DEFAULT,
) -> list[EvictionCandidate]:
    """List storage_objects ready for physical deletion: refcount<=0 AND
    older than the grace period. Locks rows SKIP LOCKED so the GC never
    blocks the active ref/deref path."""
    cutoff = datetime.now(UTC) - timedelta(seconds=grace_seconds)
    rows = (await session.execute(
        select(StorageObject).where(
            StorageObject.refcount <= 0,
            StorageObject.created_at < cutoff,
        ).order_by(StorageObject.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )).scalars().all()
    out: list[EvictionCandidate] = []
    for r in rows:
        out.append(EvictionCandidate(
            object_id=r.id, storage_id=r.storage_id,
            storage_key=r.storage_key, sha256=r.sha256, size_bytes=r.size,
            reason="tombstone",
            last_accessed_at=r.last_referenced_at.isoformat() if r.last_referenced_at else None,
        ))
    return out


async def find_lru_eviction_candidates(
    session: AsyncSession, *, tenant_id: int, target_free_bytes: int,
    limit: int = _GC_BATCH_SIZE_DEFAULT,
) -> list[EvictionCandidate]:
    """For a tenant over quota_storage_gb, pick LRU storage_objects to
    evict in oldest-referenced-first order until cumulative size >=
    target_free_bytes.

    Active-ref protection: refcount must be <= 0 — we never evict an
    object that's still referenced by an alive subtask. (For aggressive
    eviction of refcount>0 LRU you'd need a second-chance / 2Q algorithm
    + cascade subtask cancellation, which is out of scope here.)"""
    rows = (await session.execute(
        select(StorageObject).where(
            StorageObject.tenant_id == tenant_id,
            StorageObject.refcount <= 0,
        ).order_by(StorageObject.last_referenced_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )).scalars().all()
    out: list[EvictionCandidate] = []
    freed = 0
    for r in rows:
        if freed >= target_free_bytes:
            break
        out.append(EvictionCandidate(
            object_id=r.id, storage_id=r.storage_id,
            storage_key=r.storage_key, sha256=r.sha256, size_bytes=r.size,
            reason="lru_pressure",
            last_accessed_at=r.last_referenced_at.isoformat() if r.last_referenced_at else None,
        ))
        freed += r.size
    return out


# ---------------------------------------------------------------------------
# Eviction execution
# ---------------------------------------------------------------------------

async def evict_one(
    session: AsyncSession, candidate: EvictionCandidate, *,
    deleter: ObjectStoreDeleter, actor_user_id: int | None = None,
    tenant_id: int | None = None,
) -> EvictionResult:
    """Delete one candidate's physical bytes from the object store + remove
    the storage_objects row. Best-effort: if the physical delete fails we
    LEAVE the DB row alone so the next GC pass retries (and so a partial
    delete never corrupts the dedup index)."""
    physical_ok = await deleter.delete(candidate.storage_id, candidate.storage_key)
    if not physical_ok:
        logger.warning(
            "physical_gc.evict_one: physical delete failed object=%d key=%s",
            candidate.object_id, candidate.storage_key)
        return EvictionResult(
            candidate=candidate, deleted=False,
            error="physical_delete_failed")
    # Physical bytes gone — now safe to drop the DB row + audit.
    await session.execute(
        delete(StorageObject).where(StorageObject.id == candidate.object_id))
    # Also clear matching storage_physical_keys for completeness — best
    # effort; failure here is logged but doesn't fail the whole eviction.
    try:
        await session.execute(
            delete(StoragePhysicalKey).where(
                StoragePhysicalKey.tenant_id == tenant_id if tenant_id else True,
                StoragePhysicalKey.storage_id == candidate.storage_id,
                StoragePhysicalKey.storage_key == candidate.storage_key))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "physical_gc.evict_one: phys_key cleanup failed (continuing): %s", e)
    await write_audit(
        session, action="physical_gc.evict",
        resource_type="storage_object", resource_id=str(candidate.object_id),
        outcome="success", tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        payload={"reason": candidate.reason, "storage_id": candidate.storage_id,
                  "storage_key": candidate.storage_key, "sha256": candidate.sha256,
                  "size_bytes": candidate.size_bytes,
                  "last_accessed_at": candidate.last_accessed_at})
    logger.info(
        "physical_gc.evict_one: evicted object=%d size=%d reason=%s",
        candidate.object_id, candidate.size_bytes, candidate.reason)
    return EvictionResult(candidate=candidate, deleted=True)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def gc_run_once(
    session: AsyncSession, *, deleter: ObjectStoreDeleter,
    tenant_id: int | None = None, actor_user_id: int | None = None,
    lru_target_fraction: float = _LRU_TARGET_FRACTION_DEFAULT,
    batch_size: int = _GC_BATCH_SIZE_DEFAULT,
) -> dict:
    """One GC pass:
      1. tombstones (refcount<=0 + grace) → evict
      2. (if tenant_id) check tenant.quota_storage_gb; if over,
         evict LRU until back to target_fraction × quota
      3. summary dict

    Returns {"tombstones_deleted", "lru_evicted", "bytes_freed", "errors"}."""
    if not _is_enabled():
        raise PhysicalGCDisabled(
            "DLW_PHYSICAL_GC_ENABLED is false; refusing to run")
    errors: list[str] = []
    tombstones_deleted = 0
    lru_evicted = 0
    bytes_freed = 0

    # Phase 1: tombstones (works tenant-wide if tenant_id is None)
    candidates = await find_tombstone_candidates(session, limit=batch_size)
    for c in candidates:
        r = await evict_one(
            session, c, deleter=deleter,
            actor_user_id=actor_user_id, tenant_id=tenant_id)
        if r.deleted:
            tombstones_deleted += 1
            bytes_freed += c.size_bytes
        elif r.error:
            errors.append(f"object={c.object_id}: {r.error}")

    # Phase 2: LRU eviction — only when a tenant is specified and over quota.
    if tenant_id is not None:
        tenant = await session.get(Tenant, tenant_id)
        if tenant and tenant.quota_storage_gb:
            used_bytes = (await session.scalar(
                select(func.coalesce(func.sum(StorageObject.size), 0))
                .where(StorageObject.tenant_id == tenant_id)) or 0)
            quota_bytes = tenant.quota_storage_gb * 1024 * 1024 * 1024
            target_bytes = int(quota_bytes * lru_target_fraction)
            if used_bytes > quota_bytes:
                need = used_bytes - target_bytes
                lru_candidates = await find_lru_eviction_candidates(
                    session, tenant_id=tenant_id,
                    target_free_bytes=need, limit=batch_size)
                for c in lru_candidates:
                    r = await evict_one(
                        session, c, deleter=deleter,
                        actor_user_id=actor_user_id, tenant_id=tenant_id)
                    if r.deleted:
                        lru_evicted += 1
                        bytes_freed += c.size_bytes
                    elif r.error:
                        errors.append(f"object={c.object_id}: {r.error}")

    summary = {
        "tombstones_deleted": tombstones_deleted,
        "lru_evicted": lru_evicted,
        "bytes_freed": bytes_freed,
        "errors": errors,
    }
    logger.info("physical_gc.gc_run_once: %s", summary)
    # Audit the whole run too — gives ops a single row per pass.
    await write_audit(
        session, action="physical_gc.run",
        resource_type="tenant", resource_id=str(tenant_id) if tenant_id else "all",
        outcome="success" if not errors else "partial",
        tenant_id=tenant_id, actor_user_id=actor_user_id,
        payload=summary)
    return summary


# ---------------------------------------------------------------------------
# Default deleter — wraps boto3 via storage_client
# ---------------------------------------------------------------------------

class DefaultObjectStoreDeleter:
    """Production deleter. Looks up each backend's StorageConfig and
    issues an S3 DeleteObject via storage_client.delete_object_silently.

    Caches the per-backend client to avoid the boto3 client cold-start
    on every call. Local-FS backends are handled via Path.unlink(); we
    detect that by looking at StorageBackend.backend_type."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory
        self._clients: dict[int, object] = {}

    async def delete(self, storage_id: int, key: str) -> bool:
        from dlw.services.storage_client import (
            delete_object_silently, make_s3_client, storage_config_from_backend,
        )
        async with self._session_factory() as s:
            backend = await s.get(StorageBackend, storage_id)
            if backend is None:
                logger.error(
                    "physical_gc deleter: backend %d not found", storage_id)
                return False
            if backend.backend_type == "local":
                from pathlib import Path
                try:
                    Path(key).unlink(missing_ok=True)
                    return True
                except OSError as e:
                    logger.warning(
                        "physical_gc deleter: local unlink failed key=%s: %s",
                        key, e)
                    return False
            # S3-compatible (s3, minio, gcs-with-s3-api, etc.)
            client = self._clients.get(storage_id)
            if client is None:
                cfg = storage_config_from_backend(backend)
                client = make_s3_client(cfg)
                self._clients[storage_id] = client
            cfg = storage_config_from_backend(backend)
            return await delete_object_silently(client, cfg.bucket, key)
