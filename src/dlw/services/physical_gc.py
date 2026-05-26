"""v2.1 — Physical GC + LRU eviction (skeleton).

v2.0 Phase 4 ships LOGICAL GC: subtask_object_refs counts references; when
ref count drops to 0 the storage_object row is marked tombstone. But the
**physical bytes** in S3 / local backend stay until something deletes them.

This module is the v2.1 entry point that, when fully implemented, will:
  1. Periodically scan storage_objects WHERE tombstone=true AND no active refs
  2. Issue DeleteObject against the backing object store
  3. Remove the storage_objects row (cascading storage_physical_keys)
  4. Track last_accessed_at per object; when a tenant exceeds quota_storage_gb,
     evict LRU objects until under 90% of quota (active-ref-protected)
  5. Audit every deletion (action="physical_gc.evict")

**Current status: SKELETON ONLY**. The interfaces are stable; the actual
S3 DeleteObject calls + last_accessed_at column + cron loop are TODO and
gated behind DLW_PHYSICAL_GC_ENABLED. Until that lands, the API endpoints
below return 503 with `not_yet_implemented`.

See docs/v2.1-roadmap.md § 1 for the full design and verification
checklist."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class PhysicalGCNotImplemented(NotImplementedError):
    """Raised by any GC operation while the feature is still skeleton.
    Callers should catch and surface as HTTP 503 / informative log."""


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
    """Backend-agnostic DeleteObject contract. Production impls live in
    services/storage_drivers (s3.py / fs.py); tests use an in-memory stub."""

    async def delete(self, storage_id: int, key: str) -> bool:
        """Return True on successful delete (or already-gone), False on
        retryable error. Raises on un-retryable (auth, etc.)."""
        ...


async def find_tombstone_candidates(
    session: AsyncSession, *, limit: int = 100,
) -> list[EvictionCandidate]:
    """List storage_objects ready for physical deletion: tombstone=true AND
    ref_count=0 AND not currently locked by an active scheduling round.

    TODO(v2.1): real query. Currently raises so callers know not to rely
    on the result.
    """
    logger.info("find_tombstone_candidates(limit=%d): skeleton — returning []",
                limit)
    raise PhysicalGCNotImplemented(
        "find_tombstone_candidates: schema migration "
        "(last_accessed_at + tombstone-ready predicate) pending; see "
        "docs/v2.1-roadmap.md § 1")


async def find_lru_eviction_candidates(
    session: AsyncSession, *, tenant_id: int, target_free_bytes: int,
) -> list[EvictionCandidate]:
    """For a tenant that's over quota_storage_gb, list LRU objects to evict
    in oldest-first order until cumulative size >= target_free_bytes.

    Respects active-ref protection: objects referenced by a running task
    are never selected.

    TODO(v2.1): real LRU scan + active-ref join.
    """
    logger.info(
        "find_lru_eviction_candidates(tenant=%d, target=%dB): skeleton",
        tenant_id, target_free_bytes)
    raise PhysicalGCNotImplemented(
        "find_lru_eviction_candidates: last_accessed_at column not yet "
        "added; see docs/v2.1-roadmap.md § 1.2 for the migration spec")


async def evict_one(
    session: AsyncSession, candidate: EvictionCandidate, *,
    deleter: ObjectStoreDeleter,
) -> EvictionResult:
    """Delete one candidate's physical bytes from the object store + remove
    its storage_objects / storage_physical_keys rows. Atomic per object.

    TODO(v2.1): wire deleter call + DELETE row + audit row.
    """
    logger.info("evict_one(object_id=%d, reason=%s): skeleton",
                candidate.object_id, candidate.reason)
    raise PhysicalGCNotImplemented(
        f"evict_one({candidate.object_id}): wiring to ObjectStoreDeleter "
        "(s3.DeleteObject / fs.unlink) is TODO; see "
        "docs/v2.1-roadmap.md § 1.3 verification checklist")


async def gc_run_once(
    session: AsyncSession, *, tenant_id: int | None = None,
    deleter: ObjectStoreDeleter | None = None,
) -> dict:
    """One GC pass: tombstones first, then LRU pressure if over quota.

    Returns a summary dict suitable for logging + audit:
        {"tombstones_deleted": N, "lru_evicted": M,
         "bytes_freed": X, "errors": [...]}

    TODO(v2.1): orchestrate the two phases + audit per-eviction.
    """
    logger.info("gc_run_once(tenant=%s): skeleton — no-op", tenant_id)
    raise PhysicalGCNotImplemented(
        "gc_run_once: depends on find_tombstone_candidates / "
        "find_lru_eviction_candidates / evict_one — all skeleton")


# Configuration knobs that the real impl will honor. Documented here so
# the operator-facing docs can reference stable env var names even though
# the implementation isn't live yet.
_GC_INTERVAL_SECONDS_DEFAULT = 3600        # 1 hour
_LRU_TARGET_FRACTION_DEFAULT = 0.90        # evict to 90% of quota
_GC_BATCH_SIZE_DEFAULT = 100               # per pass cap
