# FU5 — True `last_referenced_at` LRU for physical keys

## Problem

Phase-4 physical reclamation (`reclaim_physical_orphans`) and FU2 quota-pressure
prioritization order orphan candidates **oldest-first by ledger `created_at`**.
A doc note (`docs/operator/storage-reclamation.md`) admits:

> `created_at` (ledger-write time) is the available proxy for the design's
> `last_referenced_at`; a true per-key last-access column is a follow-on.

FU5 delivers that column: a real `last_referenced_at` on `storage_physical_keys`,
bumped whenever the key's content is (re)referenced, and used as the LRU key for
both ordering and the grace gate.

## Why it matters (and why it's small)

`storage_objects` already carries `last_referenced_at` (bumped in `_upsert_object`
on a dedup hit). The physical-key ledger never got the parallel column. The LRU
signal only differs from `created_at` for content that is **re-referenced after
it was first written** (a later revision inherit-copying the same content, or a
re-download to the same key) and then later fully dereferenced. For such keys,
`last_referenced_at` reflects the last time the bytes were actually wanted —
which is the correct signal both for *which orphan to delete first under a cap*
and for *how long to keep an orphan past its last use*.

## §0 Design

### Column

Add to `StoragePhysicalKey` (table `storage_physical_keys`), mirroring the
existing `created_at` exactly:

```python
last_referenced_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True), server_default=func.now(), nullable=False)
```

This is an **ALTER on an existing, possibly-populated table adding a NOT NULL
column** → migration MUST add it with a matching `server_default=sa.func.now()`
(both to satisfy the NOT NULL backfill AND to avoid `compare_server_default`
autogenerate drift, since the model declares `server_default`). After the add,
the migration backfills existing rows to their *true* best-known last reference:

```python
op.add_column("storage_physical_keys", sa.Column(
    "last_referenced_at", sa.DateTime(timezone=True),
    server_default=sa.func.now(), nullable=False))
op.execute("UPDATE storage_physical_keys SET last_referenced_at = created_at")
```

(Backfilling to `created_at`, not `now()`, preserves existing prod grace timing:
a legacy key's best-known last reference is its write time. New inserts get
`now()` via the server_default.)

`downgrade()` drops the column.

No new index. The candidate set is already small (filtered by `~live` + grace)
before the sort — identical to how `created_at` ordering works today; FU5 does
not change that cardinality, so no separate index is warranted (YAGNI; the
existing `idx_phys_key_gc` on `(tenant_id, storage_id, sha256)` already serves
the per-sha `touch` UPDATE).

### Bump sites — when `last_referenced_at` is set/refreshed

A physical key's content is "referenced" in two distinct ways. Both bump it:

1. **Key (re)written** — `record_physical_key` (download + inherit-copy
   completion, the single ledger write site). Today it is
   `on_conflict_do_nothing`. Change to `on_conflict_do_update` setting
   `last_referenced_at = now()` (keep `executor_id`/`size` unchanged on
   conflict — only the timestamp is refreshed). Fresh inserts get `now()` via
   the column's server_default. This covers a re-download/re-write to the same
   `(tenant, storage, storage_key)`.

2. **Content re-referenced via dedup/inherit without a physical re-write** —
   `record_ref_only` (called from `incremental.py::diff_and_dedup` when a new
   revision inherits already-downloaded content). It bumps `storage_objects`
   refcount + inserts a `subtask_object_refs` row but never touched the physical
   ledger. FU5 adds, at the end of `record_ref_only`, a touch of **all** physical
   keys for that content sha:

   ```python
   await session.execute(
       update(StoragePhysicalKey)
       .where(StoragePhysicalKey.tenant_id == tenant_id,
              StoragePhysicalKey.storage_id == storage_id,
              StoragePhysicalKey.sha256 == sha256)
       .values(last_referenced_at=datetime.now(UTC)))
   ```

   The sha is the natural unit: orphaning is per-sha (all physical keys of a sha
   become orphans together the moment the `storage_objects` row is deleted), so
   bumping per-sha keeps every byte-copy of re-used content fresh together. If no
   physical key exists yet for the sha (pre-Phase-4 content), the UPDATE matches
   zero rows — harmless.

`record_object` (download success path) is NOT given a separate touch: it is
immediately followed in `scheduler.complete_subtask` by `record_physical_key`
(which sets the timestamp on the freshly-written key), and a download writes a
*new* sha, so no other physical key of that sha exists to touch.

### Ordering + grace switch

In `reclaim_physical_orphans`, `dispatch_local_reclaim`, and
`count_stuck_local_orphans`, replace every `created_at` used as an **ordering key
or grace cutoff** with `last_referenced_at`:

- `reclaim_physical_orphans`: WHERE `last_referenced_at < cutoff`; both ORDER BY
  branches (`order_by(prio, last_referenced_at)` and `order_by(last_referenced_at)`).
- `dispatch_local_reclaim`: WHERE `last_referenced_at < cutoff`;
  `order_by(last_referenced_at)`.
- `count_stuck_local_orphans`: WHERE `last_referenced_at < cutoff`.

**Safety direction:** `last_referenced_at >= created_at` always (a row is
referenced at or after its creation). So `last_referenced_at < cutoff` is a
**strictly stronger** condition than `created_at < cutoff` — fewer rows qualify,
deletion happens **later, never sooner**. The grace switch can only make
reclamation more conservative. This is the intended LRU-grace semantics: an
orphan is eligible `grace_seconds` after its **last reference**, not after its
first write.

`gc_orphans` (the row-level `storage_objects` GC) is untouched — it already has
its own `created_at`/`last_referenced_at` columns and is out of FU5 scope.

### What is explicitly NOT changed

- The `~live` liveness test, the `gc_delete_physical_bytes` default-off gate, the
  per-tick cap, the delete-bytes-before-row ordering, the FU4 sibling
  dispatch/confirm widening, audit actions — all unchanged.
- No openapi change, no frontend change, no executor protocol change.
- The FU2 quota-pressure `prio` case expression is unchanged; only its tiebreak
  ordering key flips from `created_at` to `last_referenced_at`.

## §1 Honest limitations

- The signal only diverges from `created_at` for content re-referenced during its
  live lifetime (multi-revision inherit / re-download). Single-use content has
  `last_referenced_at == created_at`, so ordering is identical to before. This is
  expected: the column adds resolution exactly where dedup creates it.
- Like FU2, the ordering only has an *observable* effect when the per-tick cap
  binds (backlog > `gc_max_objects_per_tick`); under the cap every eligible key is
  reclaimed each tick regardless of order.
- Still time-only for the *tracked* refcount-0 `storage_objects` eviction
  (`gc_orphans`); FU5 sharpens only the *physical-orphan* ordering/grace, matching
  FU2's documented layer boundary.

## §2 Tests (TDD)

`tests/services/test_physical_reclaim.py` (S3 path):
- `test_orders_by_last_referenced_not_created`: two orphan keys, same tenant, no
  priority. Key A: `created_at=_old(10d)`, `last_referenced_at=_old(1h)` (recently
  re-used). Key B: `created_at=_old(2d)`, `last_referenced_at=_old(9d)` (older last
  use). `max_objects_per_tick=1`. Assert **B** is reclaimed first (older last
  reference) — would FAIL under the old `created_at` ordering (which picks A).
- `test_grace_uses_last_referenced_at`: a key with `created_at=_old(30d)` (well
  past grace) but `last_referenced_at=now()` (just re-referenced). Assert it is
  NOT a candidate (grace measured from last reference). Old behavior would reclaim
  it.
- Update existing tests (`test_grace_respected`, `test_per_tick_cap`,
  `test_priority_tenant_reclaimed_first_under_cap`) to set `last_referenced_at`
  alongside `created_at` on every `StoragePhysicalKey(...)` construction (else the
  new server_default `now()` puts them inside grace and they stop being
  candidates). Priority test: keep its created/last values equal so it still
  proves priority-over-recency.

`tests/services/test_physical_keys.py` (write path):
- `test_record_physical_key_bumps_last_referenced_on_conflict`: insert a key with
  an old `last_referenced_at`, call `record_physical_key` again for the same
  `(tenant, storage, storage_key)`, assert `last_referenced_at` advanced and
  `executor_id`/`size` unchanged.
- `test_record_ref_only_touches_physical_keys_for_sha`: seed a physical key (old
  `last_referenced_at`) + its `storage_objects` row, call `record_ref_only` for
  the same sha, assert the physical key's `last_referenced_at` advanced.

`tests/services/test_local_reclaim.py` (FU3/FU4 local path): update every
`StoragePhysicalKey(...)` construction to also set `last_referenced_at=_old(...)`
(same reason — grace now reads `last_referenced_at`). Add
`test_dispatch_orders_by_last_referenced`: two local orphans, dispatch `limit=1`,
the one with the older `last_referenced_at` is dispatched first.

`tests/db/test_alembic.py`: `EXPECTED_TABLES` is unchanged (column add, not a new
table). Add an assertion in the existing column-introspection style (if present)
or a focused test that `last_referenced_at` exists on `storage_physical_keys`
after `upgrade head`.

## §3 Milestones

- **M1** — migration + model column + alembic test. Gate: `pytest tests/db -q`,
  dev-DB `:5433` upgrade clean, no autogenerate drift.
- **M2** — bump sites (`record_physical_key` upsert, `record_ref_only` touch) +
  ordering/grace switch (3 functions) + service tests. Gate: full backend `pytest
  -q`, `lint_invariants --strict`.
- **M3** — operator docs.

## §4 Migration checklist (applied)

- down_revision = `d5e6f7a8b9c0` (current single head).
- `StoragePhysicalKey` already registered in `db/models/__init__.py` +
  `alembic/env.py` (column add needs no registration change).
- ALTER existing table + NOT NULL → `server_default=sa.func.now()` in migration
  matching the model's `server_default` (no `compare_server_default` drift).
- `EXPECTED_TABLES` unchanged (no new table).
- Dev-DB `:5433` `alembic -c alembic.ini upgrade head`.
