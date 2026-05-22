# FU2 — Phase 4 Quota-Trigger LRU Ordering (Design)

> Second queued follow-on. Wires the Phase-4 named-deferred quota-trigger LRU
> (doc 06 §3.2: "tenant 存储用量 ≥ 90% 配额，按 last_referenced_at LRU 淘汰
> refcount=0 的对象"): when a tenant is at/over a storage-pressure threshold,
> the physical-reclamation GC reclaims THAT tenant's dereferenced physical keys
> FIRST (before other tenants'), so under the per-tick cap a space-pressured
> tenant's orphaned bytes are freed preferentially.
> Status: self-approved per Rule #1. Branch: `feat/fu2-quota-trigger-lru`.

## 0. What this is (and isn't)

Phase 4's reclamation already reclaims EVERY fully-dereferenced physical key past
grace, oldest-first, capped at `gc_max_objects_per_tick` (default 1000). The
ordering only matters when that cap binds (a backlog > the cap). FU2 makes the
ordering **quota-aware**: keys belonging to tenants at/over the storage-pressure
threshold sort before everyone else's (then oldest-first within each group). It
does NOT change WHICH keys are eligible (still refcount=0 / no surviving
`storage_objects` row, past grace), only the priority when capped. §3.2's
"refcount=0 only" semantics are preserved — no live data is ever evicted.

**Honest framing (pre-review — do not overstate this):**

- **Layer mismatch vs §3.2's literal target.** §3.2 says "按 last_referenced_at
  LRU 淘汰 **refcount=0 的对象**" — those are `storage_objects` rows that are
  refcount=0 but **still tracked** (still counted in `storage_gb_used`, within
  their GC grace). That deletion is `gc_orphans()`, which is **time-only**
  (`refcount<=0 AND created_at<cutoff`), has **no quota awareness and no cap**,
  and FU2 **does NOT touch it**. FU2 instead reorders
  `reclaim_physical_orphans`, whose candidates are keys whose `storage_objects`
  row is **already gone** (fully orphaned). So FU2 is **NOT** "wiring §3.2's
  quota-trigger" in the literal sense — it **partially approximates** it by
  applying the quota-pressure heuristic to the *physical-orphan reclaim* layer
  (real per-tenant DISK relief), while the tracked-refcount=0 eviction that §3.2
  names stays time-only. Making `gc_orphans` quota-aware is a separate, larger
  change, explicitly NOT in FU2.
- **Doesn't reduce `storage_gb_used`.** A reclaimable key's `storage_objects`
  row is already GC'd, so freeing it lowers physical disk/object-store bytes but
  NOT the tracked quota number. The pressure signal is a *priority heuristic*
  ("this tenant is space-pressured → free its orphaned bytes first").
- **Integer-GiB-floor coarseness.** `storage_gb_used` is floor(bytes/GiB) and
  `quota_storage_gb` is integer GiB (Phase 4 Part A), so `used >= 0.9*quota`
  is coarse: a 1-GiB-quota tenant never trips until used hits exactly 1 GiB
  (=100%); the trigger is effectively inert below ~10-GiB quotas. Documented.
- **Narrow effect window.** The ordering only changes behavior when the per-tick
  cap (default 1000) binds — i.e. a reclaim backlog > 1000 orphan keys AND a
  pressured tenant has keys in it AND the snapshot is fresh. In normal operation
  it is near-inert; the operator doc must not oversell it.

## 1. Scope

**In scope (additive; no migration, no new dep, no API/frontend change):**

1. **`pressured_tenant_ids(session, *, threshold)` → `frozenset[int]`**
   (`services/storage_objects.py`): the tenants where `quota_storage_gb > 0 AND
   storage_gb_used >= threshold * quota_storage_gb`, by joining `QuotaSnapshot`
   (the `storage_gb_used` made live in Phase 4 Part A) + `Tenant`
   (`quota_storage_gb`). Uses the maintained snapshot (refreshed each minute by
   the quota loop) — stale-tolerant for a prioritization heuristic.
2. **`reclaim_physical_orphans` priority ordering**: add a
   `priority_tenant_ids: frozenset[int] = frozenset()` param; the candidate
   `order_by` becomes `case((tenant_id.in_(priority), 0), else_=1), created_at`
   so priority-tenant keys come first, then oldest-first. Everything else
   (eligibility, cap, S3-only, delete-bytes-before-row, audit) is unchanged.
3. **`_physical_gc_loop` wiring** (`main.py`): compute
   `priority = await pressured_tenant_ids(session, threshold=_gs().gc_quota_pressure_threshold)`
   and pass it to `reclaim_physical_orphans`.
4. **Config** `gc_quota_pressure_threshold: float = 0.9` (clamped (0, 1]).

**Out of scope (named):**
- True `last_referenced_at` LRU within a tenant — `storage_physical_keys` has
  `created_at` not `last_referenced_at` (the dedup row that held
  `last_referenced_at` is already GC'd by the time a key is reclaimable). FU2
  orders by `created_at` (oldest-first), which is the available proxy; doc 06
  §3.2's `last_referenced_at` is approximated. Documented; a true per-key
  last-access column is a larger follow-on.
- Evicting refcount>0 (live) content to free quota — §3.2 is refcount=0 only;
  unchanged.
- Live (not snapshot) recomputation of per-tenant pressure each tick — uses the
  minute-maintained `QuotaSnapshot`; acceptable for a heuristic.

## 2. Implementation

`services/storage_objects.py`:
```python
from sqlalchemy import case   # add to imports

async def pressured_tenant_ids(
    session: AsyncSession, *, threshold: float = 0.9,
) -> frozenset[int]:
    """Tenants at/over `threshold` of their storage quota (live snapshot).
    Empty if none / no quota set."""
    from dlw.db.models.tenant import Tenant
    from dlw.db.models.usage import QuotaSnapshot
    rows = (await session.execute(
        select(QuotaSnapshot.tenant_id)
        .join(Tenant, Tenant.id == QuotaSnapshot.tenant_id)
        .where(Tenant.quota_storage_gb > 0,
               QuotaSnapshot.storage_gb_used
               >= threshold * Tenant.quota_storage_gb))).scalars().all()
    return frozenset(int(t) for t in rows)
```
`reclaim_physical_orphans` — add the param + reorder:
```python
async def reclaim_physical_orphans(
    session, *, grace_seconds, delete_enabled, make_client, audit,
    max_objects_per_tick=1000, priority_tenant_ids=frozenset(),
) -> dict:
    ...
    priority = case(
        (StoragePhysicalKey.tenant_id.in_(priority_tenant_ids), 0), else_=1
    ) if priority_tenant_ids else None
    stmt = (select(StoragePhysicalKey)
            .where(StoragePhysicalKey.created_at < cutoff, ~live))
    stmt = (stmt.order_by(priority, StoragePhysicalKey.created_at)
            if priority is not None
            else stmt.order_by(StoragePhysicalKey.created_at))
    rows = (await session.execute(
        stmt.limit(max(1, max_objects_per_tick))
        .with_for_update(skip_locked=True))).scalars().all()
    ...                                    # unchanged delete loop
```
(`case`/`in_([])` edge: an empty `priority_tenant_ids` skips the case entirely —
`in_(empty)` in SQLAlchemy emits a constant-false that's valid but wasteful, so
guard with the `if priority_tenant_ids` to keep the existing single-order path
byte-identical when there's no pressure.)

`main.py` `_physical_gc_loop` — after loading backends, before reclaim:
```python
    from dlw.services.storage_objects import pressured_tenant_ids
    priority = await pressured_tenant_ids(
        session, threshold=_gs().gc_quota_pressure_threshold)
    res = await reclaim_physical_orphans(
        session, ..., priority_tenant_ids=priority)
```

`config.py`: `gc_quota_pressure_threshold: float = Field(default=0.9, gt=0.0, le=1.0)`.

## 3. Tests

- **`tests/services/test_quota_pressure.py`**: seed tenants + `QuotaSnapshot`
  rows — tenant at 95% (`quota_storage_gb=10`, `storage_gb_used=10`) → in the
  set; at 50% → not; `quota_storage_gb=0` → not (unlimited). `threshold=0.9`.
- **`tests/services/test_physical_reclaim.py`** (extend): with
  `max_objects_per_tick=1` and TWO old dereferenced keys — one owned by a
  priority tenant, one not — assert the PRIORITY tenant's key is the one
  reclaimed (the cap binds, priority wins). Plus: `priority_tenant_ids=frozenset()`
  → behaves exactly as before (oldest-first; existing tests stay green).
- **`tests/test_phase4_lifespan.py`** (extend if it asserts loop wiring): the
  loop still builds (the new config field + `pressured_tenant_ids` call import
  cleanly).

## 4. Milestones

- **M1**: `pressured_tenant_ids` + `reclaim_physical_orphans` priority param +
  config + tests + backend gate.
- **M2**: `_physical_gc_loop` wiring + lifespan smoke + docs note in
  `docs/operator/storage-reclamation.md` (quota-trigger now prioritizes pressured
  tenants under the cap; `created_at` proxy for LRU; threshold config) + backend gate.

(Two milestones; small. Could be one PR.)

## 5. Risks & Contingencies

- **No behavior change when no tenant is pressured** — `priority_tenant_ids`
  empty → the order_by is byte-identical to today (guarded). Pure addition.
- **Snapshot staleness** — `pressured_tenant_ids` reads the minute-maintained
  `QuotaSnapshot`; a tenant that just crossed 90% may not be prioritized until
  the next quota tick. Acceptable for a priority heuristic (the cap rarely binds;
  even unprioritized keys are reclaimed, just later).
- **`created_at` vs `last_referenced_at`** — documented approximation; a true
  per-key last-access column is deferred.
- **`in_(frozenset)` / `case` SQL** — guarded so empty priority keeps the
  single-order query; verified `case((col.in_(ids), 0), else_=1)` compiles on PG.
- **Still safe-by-default destructive** — FU2 only reorders candidates; the
  Phase-4 rails (default-off `gc_delete_physical_bytes`, S3-only, grace,
  delete-bytes-before-row, audit, refcount=0-only) are untouched.
- CI doesn't gate ruff — real gate pytest + `lint_invariants`.

## 6. Self-Review

- **Partially approximates §3.2** (NOT "wires" — see §0): pressured tenants'
  *orphaned physical keys* reclaimed first under the cap (disk relief); the
  tracked-refcount=0 eviction §3.2 names stays time-only in `gc_orphans`.
  refcount=0-only preserved; `created_at` proxies LRU.
- **Pure addition**: empty-pressure path byte-identical to today. ✓
- **Honest**: the layer-mismatch, the "doesn't reduce storage_gb_used", the
  integer-GiB-floor coarseness, the LRU `created_at` proxy, and the narrow
  cap-binding effect window are ALL documented in §0 + the operator doc, not
  hidden. ✓
- **Consistency**: reuses `QuotaSnapshot.storage_gb_used` (Phase 4 Part A), the
  reclaim candidate query, the loop wiring.
