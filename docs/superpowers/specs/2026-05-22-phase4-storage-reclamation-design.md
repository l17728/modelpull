# Phase 4 — Physical GC / Quota-LRU Storage Reclamation (Design)

> Closes the SP3-deferred storage gaps: (a) **orphaned physical bytes** — an
> incremental `inherit` subtask server-side-copies content to a new-revision
> key that NO `storage_objects` row tracks, so it is never reclaimable; (b)
> **storage quota** — `tenants.quota_storage_gb` is exposed but never computed
> or enforced (`QuotaSnapshot.storage_gb_used` is a dead column); (c) **GC
> deletes DB rows only, never physical bytes** (`gc_orphans` has no
> `delete_object`).
> Authoritative requirements: `docs/v2.0/06-platform-and-ecosystem.md` §3.2
> (LRU/time eviction) + Invariant 14.
> Status: design self-approved per Rule #1. Branch: `feat/phase4-storage-reclamation`.

## 0. Safety posture (destructive feature)

Physical byte deletion of customer model weights is destructive and
irreversible. This design is **safe-by-default**:
- Physical deletion is gated behind a new config `gc_delete_physical_bytes`
  (default **False** — operator opts in after review).
- Physical deletion runs ONLY for **S3-type** backends (the controller can
  build an S3 client from the backend config, mirroring `recovery.py`). Local-fs
  backends are skipped (the controller has no access to an executor's disk in a
  distributed deployment) — flagged as an executor-side follow-on.
- Every physical deletion is audited (`storage.gc.physical`) with keys + bytes.
- A grace window guards every reclaim; deletion only targets bytes whose content
  sha has **no live `storage_objects` row** (fully dereferenced) — the same
  refcount invariant SP3 relies on.
- All tests use a **mocked S3 client** — no live object-store calls in CI.

## 1. Scope

**Part A — Storage usage accounting + quota enforcement (pure DB, safe, default-on):**
1. `aggregate_snapshots` (`services/quota.py`) computes
   `QuotaSnapshot.storage_gb_used = ceil(SUM(storage_objects.size for tenant) / 1GiB)`
   — making the dead column live.
2. `check_quota_for_new_task` enforces `quota_storage_gb`: when
   `storage_gb_used >= quota_storage_gb`, raise `QuotaExceeded("storage")`.
3. The existing `/quota/current` API already surfaces `storage_gb_used` /
   `storage_gb_quota` (reads the now-live column) — no API change.

**Part B — Physical-key ledger + S3 physical reclamation (the core gap):**
4. New table `storage_physical_keys (id, tenant_id, storage_id, sha256,
   storage_key, size, created_at)`, UNIQUE `(tenant_id, storage_id, storage_key)`.
   This is a durable record of **every physical object written**, decoupled from
   the dedup `storage_objects` row (which only tracks the first writer's key).
5. Record a physical key on every successful subtask completion (download AND
   inherit) in `complete_subtask`, from `sub.s3_key` + `sub.actual_sha256` +
   size. This captures the new-revision inherit keys that `storage_objects`
   misses, closing gap (a) controller-side (no executor change — the controller
   already knows each subtask's written key).
6. New `reclaim_physical_orphans(session, *, grace_seconds, delete_enabled,
   make_client)` in `services/storage_objects.py`: select `storage_physical_keys`
   rows whose `(tenant_id, storage_id, sha256)` has **no** surviving
   `storage_objects` row (fully dereferenced — the dedup row was already
   DB-GC'd by `gc_orphans`) AND `created_at < cutoff`. For each, if
   `delete_enabled` and the backend is S3: delete the physical bytes
   (best-effort per key, audited), then delete the `storage_physical_keys` row.
   If not enabled: count only (dry-run telemetry), delete nothing.
7. A new leader-gated loop `_physical_gc_loop` (registered in `_on_active`
   alongside `_gc_loop`) drives it on `gc_interval_seconds`. Builds S3 clients
   from decrypted `StorageBackend.config_encrypted` (the `recovery.py`
   `_make_s3_client` + JSON-decode pattern, extracted to a shared helper).

**Part C — Time + quota-LRU eviction policy (§3.2):**
8. Config `gc_archive_after_days` (default 90). The DB-row GC (`gc_orphans`)
   already deletes refcount<=0 rows past `gc_grace_seconds`; Part C is the
   *physical* policy: `reclaim_physical_orphans`'s grace is
   `max(gc_grace_seconds, archive_after_days)` for the time-trigger.
9. Quota-trigger LRU: when a tenant's `storage_gb_used >= 90% * quota_storage_gb`,
   prioritize reclaiming that tenant's dereferenced physical keys ordered by the
   object's `last_referenced_at` (oldest first). (Refcount>0 objects are never
   touched — LRU only reorders *already-dereferenced* candidates; it does not
   evict live data. True "evict live LRU data to free quota" is NOT in scope —
   that would delete referenced content, which violates the refcount invariant;
   §3.2's "LRU evict refcount=0 objects" is what we implement.)

**Out of scope (named, deferred):**
- **Local-fs physical deletion** — needs an executor-side reclaim RPC (controller
  has no remote disk access). Flagged; S3 covers the dev (minio) + typical prod.
- **Evicting refcount>0 (live) data** to free quota — would delete referenced
  content; §3.2's policy is explicitly refcount=0 only. Over-quota with all data
  live is surfaced (enforcement blocks new tasks) but not force-evicted.
- **Cold-storage tiering** (§3.2 "move to cold storage") — delete-or-keep only.
- Backfilling `storage_physical_keys` for objects written before this migration
  (pre-existing inherit orphans stay until their sha is re-written) — a one-off
  reconcile script is a follow-on.

## 2. Data model

`storage_physical_keys`:
```python
class StoragePhysicalKey(Base):
    __tablename__ = "storage_physical_keys"
    __table_args__ = (UniqueConstraint("tenant_id", "storage_id", "storage_key"),)
    id: Mapped[int]            # BigInteger PK autoincrement
    tenant_id: Mapped[int]     # FK tenants.id
    storage_id: Mapped[int]    # bigint (no FK, matches storage_objects convention)
    sha256: Mapped[str]        # String(64)
    storage_key: Mapped[str]   # String(1024) — the ACTUAL physical key written
    size: Mapped[int]          # BigInteger
    created_at: Mapped[datetime]  # server_default now()
```
Migration: create the table + index `idx_phys_key_gc` on `(tenant_id, storage_id, sha256)` (for the "any live storage_objects row?" join) + register model in `db/models/__init__.py` + `alembic/env.py` + `EXPECTED_TABLES`. down_revision = current head `b3c4d5e6f7a8`. (Index on `created_at` for the grace filter folded into the gc index.)

## 3. Recording physical keys

In `services/storage_objects.py`, add `record_physical_key(session, *,
tenant_id, storage_id, sha256, storage_key, size)` →
`pg_insert(...).on_conflict_do_nothing(index_elements=[tenant,storage,key])`
(idempotent — re-completing the same key is a no-op).

Call it from `scheduler.py::complete_subtask` on the success path, right where
`record_object` is called (the `if final_status == "succeeded" and sub.s3_key
and sub.actual_sha256:` block) — for BOTH normal and inherit successes (record
the key unconditionally; `record_object` itself stays as-is for refcount). For
inherit subtasks, `sub.s3_key` is the new-revision destination key (the executor
reports the key it wrote/copied to). **Implementer must verify** `sub.s3_key`
holds the inherit dst key on success (read the executor inherit-completion
report path); if it doesn't, that's a BLOCKER to surface (the orphan-close
depends on it).

## 4. Storage accounting (Part A)

`services/quota.py`:
```python
async def aggregate_snapshots(session):  # extend existing
    ...  # existing bytes_month + concurrent recompute
    # NEW: per-tenant storage usage from the dedup ledger (counts each unique
    # content once — matches invariant 14 dedup semantics).
    rows = await session.execute(
        select(StorageObject.tenant_id,
               func.coalesce(func.sum(StorageObject.size), 0))
        .group_by(StorageObject.tenant_id))
    # write storage_gb_used = ceil(bytes / 1024**3) into each QuotaSnapshot
```
`check_quota_for_new_task`: after the existing bytes/concurrent checks, add:
```python
    if snap.storage_gb_used >= tenant.quota_storage_gb:
        raise QuotaExceeded("storage")
```
(Uses the snapshot's `storage_gb_used`, refreshed by the quota loop. Conservative
— a tenant at/over storage quota can't create new tasks until reclamation or a
quota bump frees space.)

## 5. Physical reclamation (Part B/C)

`services/storage_objects.py`:
```python
async def reclaim_physical_orphans(
    session, *, grace_seconds, delete_enabled, make_client, audit,
) -> dict:
    """Find physical keys whose content sha has no surviving storage_objects
    row (fully dereferenced) and are past grace. For S3 backends, delete bytes
    when delete_enabled. Returns {"candidates": n, "deleted": m, "bytes": b}."""
    cutoff = now - grace_seconds
    # candidates: phys rows with NO matching live storage_objects (tenant,storage,sha)
    cand = select(StoragePhysicalKey).where(
        StoragePhysicalKey.created_at < cutoff,
        ~exists().where(
            StorageObject.tenant_id == StoragePhysicalKey.tenant_id,
            StorageObject.storage_id == StoragePhysicalKey.storage_id,
            StorageObject.sha256 == StoragePhysicalKey.sha256))
    # order by created_at; (quota-LRU ordering applied by caller if a tenant
    # is >=90% — pass an optional priority_tenant_id)
    ...
    for row in rows:
        if delete_enabled and backend_is_s3(row.storage_id):
            client = make_client(row.storage_id)   # cached per storage_id
            ok = delete_object_silently(client, bucket, row.storage_key)
            if ok: await audit("storage.gc.physical", ...); session.delete(row); deleted+=1
        # not enabled OR not s3 → leave the row (dry-run / follow-on)
```
The loop builds clients lazily per `storage_id` from `StorageBackend`
(decrypt-config helper extracted from `recovery.py::_load_storage_config` +
`_make_s3_client`; reuse `_delete_object_silently`). `backend_is_s3` checks
`StorageBackend.backend_type == "s3"`.

**Ordering / crash-safety:** delete physical bytes FIRST, then the DB row, so a
crash between leaves a re-discoverable `storage_physical_keys` row (idempotent
retry next tick — re-deleting an absent S3 key is a no-op). Never delete the DB
row before the bytes.

`config.py` additions:
```python
gc_delete_physical_bytes: bool = Field(default=False)   # operator opt-in
gc_archive_after_days: int = Field(default=90, ge=0)
```

`main.py` `_physical_gc_loop` (mirror `_gc_loop`): every `gc_interval_seconds`,
call `reclaim_physical_orphans` with `grace_seconds = max(gc_grace_seconds,
archive_after_days*86400)`, `delete_enabled = gc_delete_physical_bytes`, a
`make_client` closure, and the audit writer. Registered in `_on_active`,
cancelled in `_on_step_down` (same boilerplate as `_gc_loop`).

## 6. Tests

- **`tests/services/test_storage_quota.py`**: `aggregate_snapshots` writes
  `storage_gb_used` from summed object sizes; `check_quota_for_new_task` raises
  `QuotaExceeded("storage")` at/over quota, passes under; per-tenant isolation.
- **`tests/services/test_physical_keys.py`**: `record_physical_key` idempotent;
  inherit + download both record their actual key; two revisions of the same sha
  record two distinct physical keys (the orphan case) under one dedup row.
- **`tests/services/test_physical_reclaim.py`**: seed phys keys, some with a live
  `storage_objects` row (NOT reclaimed) and some fully dereferenced (reclaimed);
  with a **mock S3 client** (records `delete_object` calls), assert: delete_enabled
  + s3 → bytes deleted + rows gone + audit; delete_disabled → candidates counted,
  nothing deleted; non-s3 backend → skipped; grace respected; crash-order (delete
  bytes before row) verified by the mock call sequence.
- **`tests/test_phase4_lifespan.py`** (or extend `test_sp3_lifespan.py`): the
  `_physical_gc_loop` is registered on active. Mark `slow`; don't run the full
  leader loop — assert the task holder/registration like the SP3 GC test.
- EXPECTED_TABLES += `storage_physical_keys`.

## 7. Milestones

- **M1 — Part A (accounting+enforcement)**: migration (table + the index) +
  `StoragePhysicalKey` model + `storage_gb_used` compute + storage quota check +
  tests + full backend gate + dev-DB upgrade + drift gate. (Part A is
  independently shippable + safe.)
- **M2 — Part B (ledger + reclamation)**: `record_physical_key` + wire into
  `complete_subtask` + `reclaim_physical_orphans` + decrypt/client helper +
  `_physical_gc_loop` + config + tests (mock S3) + full backend gate.
- **M3 — docs**: operator section in `docs/operator/` (storage reclamation: the
  quota enforcement, the physical-key ledger, the `gc_delete_physical_bytes`
  opt-in, S3-only + local-fs follow-on, the safety ordering). Backend gate.

## 8. Risks & Contingencies

- **Destructive**: mitigated by default-off, S3-only, refcount-derived candidates,
  grace, audit, mock-only tests, delete-bytes-before-row ordering. See §0.
- **`sub.s3_key` for inherit**: the orphan-close depends on it being the dst key
  — implementer verifies; BLOCKER if not (fallback: record from the executor
  report or `compose_key` at completion).
- **Migration alters nothing existing** (new table only) → Python defaults, no
  server_default drift (per the SP4a checklist; this is NOT the alter-existing
  case). down_revision `b3c4d5e6f7a8`; register in 2 places; EXPECTED_TABLES;
  dev-DB upgrade; autogenerate drift gate must be clean.
- **No openapi change, no literal null examples.** CI doesn't gate ruff — real
  gate is pytest + `lint_invariants`; `ruff --select I001 --fix` new files only.
- **boto3 client construction in tests**: never real — `make_client` is injected
  so tests pass a mock; the loop's real `make_client` is exercised only via the
  reused recovery.py helpers (already covered by recovery tests).

## 9. Self-Review

- **Gap (a) orphaned inherit bytes** → physical-key ledger records every written
  key incl. inherit dst → reclaimable. ✓
- **Gap (b) storage quota** → `storage_gb_used` computed + enforced. ✓
- **Gap (c) physical deletion** → `reclaim_physical_orphans` deletes S3 bytes
  (gated). ✓
- **Invariant 14** (dedup) preserved: accounting counts each sha once via
  `storage_objects`; reclamation only touches fully-dereferenced shas. ✓
- **Refcount safety**: nothing with a live `storage_objects` row is ever deleted.
- **Placeholder scan**: the local-fs deferral + the dry-run mode are deliberate,
  documented, not TODOs.
- **Consistency**: reuses `recovery.py` S3 helpers, the leader-loop boilerplate,
  the `gc_orphans` SKIP-LOCKED/grace idiom, the migration checklist.
