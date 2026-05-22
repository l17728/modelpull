# FU9 — Pre-migration Orphan Backfill (heartbeat-driven `executor_id` adoption)

## Problem

`storage_physical_keys.executor_id` was added by FU3 and set by `complete_subtask`
(FU3+). Keys written before FU3 — and any key whose executor_id was never recorded
for other reasons — carry `executor_id IS NULL`.

`count_stuck_local_orphans` (the heartbeat-time observability gauge) counts
`executor_id IS NULL` local-backend keys as "stuck." But many of those keys live
on backends that some currently-advertising executor mounts. Those executors
**can** reclaim the keys the moment GC is enabled — they are not stuck. The gauge
systematically over-reports, so operators cannot trust it.

FU9 fixes the root cause: each executor that heartbeats and advertises `base_paths`
**adopts** (sets `executor_id = self`) the NULL-executor_id local keys on the
backends it provably mounts. After adoption, only keys on backends no active
executor advertises remain NULL — those are genuinely unreachable, so the gauge
becomes accurate.

## §0 Design

### Why heartbeat-driven

`post_heartbeat` already computes `acc_ids = frozenset(await
resolve_accessible_storage_ids(session, paths))` — the storage IDs accessible to
this executor — and passes it to both `confirm_local_reclaim` and
`dispatch_local_reclaim`. FU9 adds a third consumer of `acc_ids`: an adoption step
that runs between heartbeat record and dispatch.

No new leader loop, no new scheduler, no migration, no executor protocol change.
The adoption is a plain `UPDATE storage_physical_keys SET executor_id = :eid WHERE
executor_id IS NULL AND storage_id IN :acc_ids AND tenant_id = :tid LIMIT :cap`.

### New service function

```python
async def adopt_orphan_local_keys(
    session: AsyncSession,
    executor_id: str,
    *,
    accessible_storage_ids: frozenset[int],
    limit: int,
    tenant_id: int | None = None,
) -> int:
```

- Returns the count of rows adopted (0 when nothing to do).
- Guard: if `accessible_storage_ids` is empty, return 0 immediately (no-op).
- `WHERE executor_id IS NULL AND storage_id IN accessible_storage_ids` — scoped to
  NULL-executor_id rows only; already-owned rows untouched (idempotent).
- `tenant_id` filter applied when provided (executor adopts its own tenant's keys
  only — executor.tenant_id is always available at the call site).
- `LIMIT limit` (from `gc_max_objects_per_tick` config) — caps per-heartbeat work
  so adoption never starves dispatch.
- No `FOR UPDATE SKIP LOCKED` needed — the WHERE `executor_id IS NULL` is the
  contention guard (two concurrent heartbeats targeting the same key both try the
  UPDATE; one wins and sets executor_id; the other's `WHERE executor_id IS NULL`
  predicate then excludes that row, no double-adopt).
- **Not gated on `gc_delete_physical_bytes`** — adoption is metadata repair, not
  physical deletion. The gauge should be accurate regardless of whether GC is
  enabled.

### `post_heartbeat` wiring

After `acc_ids` is computed and before `dispatch_local_reclaim`:

```python
if acc_ids:
    adopted = await adopt_orphan_local_keys(
        session, executor.id,
        accessible_storage_ids=acc_ids,
        limit=s.gc_max_objects_per_tick,
        tenant_id=executor.tenant_id,
    )
    if adopted:
        await write_audit(session, action="storage.local.adopt",
                          resource_type="storage_physical_keys",
                          resource_id=executor.id, outcome="success",
                          tenant_id=executor.tenant_id, actor_user_id=None,
                          payload={"adopted": adopted})
```

Audit fires only when rows were actually adopted (not every heartbeat) to keep the
audit log quiet.

### Effect on `count_stuck_local_orphans`

Before FU9: every pre-FU3 NULL key is counted, even if an executor mounts its
backend.
After FU9: once any advertising executor heartbeats, it adopts those keys.
`executor_id IS NULL` in the gauge then means "no executor currently advertising
this backend" — genuinely stuck.

The 1-day `cutoff` in the gauge heuristic is unchanged (pre-grace keys aren't
"stuck" yet regardless of executor_id).

## §1 Threat model / correctness

- **Not a new delete path**: adoption only updates metadata. Physical reclamation
  still flows through `dispatch_local_reclaim`/`confirm_local_reclaim` (FU3/FU4).
- **Cross-tenant safe**: `tenant_id` filter in the UPDATE ensures an executor never
  adopts keys belonging to another tenant.
- **Idempotent**: UPDATE on already-adopted rows is a no-op (WHERE `executor_id IS NULL`
  excludes them).
- **Cap safety**: `LIMIT gc_max_objects_per_tick` (default 1000) means a large
  pre-FU3 corpus is adopted gradually over successive heartbeats, not in one spike.
- **No path-traversal risk**: adoption does not read or delete files; it only writes
  executor_id into the DB row.

## §2 Tests

`tests/services/test_storage_orphan_adopt.py`:

- `test_adopt_null_key_on_accessible_storage`: seed 1 NULL-executor_id key on
  storage_id in acc_ids → `adopt_orphan_local_keys` returns 1; row now has
  executor_id set.
- `test_adopt_skips_already_owned`: seed 1 key with executor_id already set →
  returns 0; executor_id unchanged.
- `test_adopt_skips_other_tenant`: seed NULL key on same storage_id but different
  tenant_id → returns 0 when tenant_id filter applied.
- `test_adopt_empty_acc_ids`: call with `frozenset()` → returns 0 immediately,
  no DB hit (confirmed by checking no rows changed).
- `test_adopt_limit`: seed 5 NULL keys, limit=2 → returns 2; 3 remain NULL.
- `test_gauge_drops_after_adoption`: call `count_stuck_local_orphans` before →
  positive; call `adopt_orphan_local_keys` → call gauge again → lower (or 0 if
  past-grace seeded).

Integration test in `tests/api/test_executors.py` (extend existing heartbeat test):
- executor with `base_paths` and a NULL-executor_id key on matching storage →
  after heartbeat, key has executor_id set.

## §3 Files

- **Modify** `src/dlw/services/storage_objects.py`: add `adopt_orphan_local_keys`.
- **Modify** `src/dlw/api/executors.py`: call it in `post_heartbeat` (after
  `acc_ids`, before `dispatch_local_reclaim`).
- **Create** `tests/services/test_storage_orphan_adopt.py`.
- **Extend** `tests/api/test_executors.py`: heartbeat-triggers-adoption integration.

## §4 Notes

- Zero migration (rides FU3 `executor_id` column + FU4 `acc_ids` machinery).
- Zero openapi / frontend / executor-protocol change.
- CI gate: `pytest -q` + `lint_invariants --strict`.
